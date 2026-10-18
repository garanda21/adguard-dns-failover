"""Entry point: scheduler loop and manual commands."""

from __future__ import annotations

import argparse
import os
import re
import logging
import signal
import sys
import time
from datetime import datetime

from croniter import croniter

from app import detector
from app.adguard import AdGuardClient, AdGuardError, extract_backup
from app.config import Config, ConfigError, Instance, load_config
from app.notifier import TelegramNotifier, build_message
from app.state import BackupStore

log = logging.getLogger("adguard-dns-failover")


def block_payload(cfg: Config) -> dict:
    payload = {"upstream_dns": cfg.block_upstreams, "bootstrap_dns": cfg.block_bootstrap}
    if cfg.block_fallback:
        payload["fallback_dns"] = cfg.block_fallback
    if cfg.block_upstream_mode:
        payload["upstream_mode"] = cfg.block_upstream_mode
    return payload


def _upstream_host(upstream: str) -> str:
    """Extract the host from an upstream like 'https://h/p', 'tls://h', '[/d/]h:53' or a plain IP."""
    value = re.sub(r"^\[/[^\]]*/\]", "", upstream.strip())
    value = re.sub(r"^[a-z0-9+]+://", "", value, flags=re.I)
    value = value.split("/", 1)[0]
    if value.startswith("["):
        return value[1:].split("]", 1)[0].lower()
    return (value.rsplit(":", 1)[0] if value.count(":") == 1 else value).lower()


def is_block_dns_active(cfg: Config, dns_info: dict) -> bool:
    """True when every current upstream points to a block DNS host (also catches manual setups)."""
    block_hosts = {_upstream_host(u) for u in cfg.block_upstreams + cfg.block_bootstrap}
    current = [u for u in dns_info.get("upstream_dns") or [] if u.strip() and not u.strip().startswith("#")]
    return bool(current) and all(_upstream_host(u) in block_hosts for u in current)


def apply_block_dns(cfg: Config, inst: Instance, store: BackupStore) -> bool:
    """Switch to the block DNS. Returns True if the config was changed."""
    client = AdGuardClient(inst, cfg.http_timeout)
    info = client.get_dns_info()
    backup = store.get(inst.id)
    if backup is None:
        if is_block_dns_active(cfg, info):
            log.warning("%s already uses block upstreams and no backup exists; skipping backup", inst.name)
        else:
            dns = extract_backup(info)
            if cfg.dry_run:
                log.info("[dry-run] %s would back up %s", inst.name, dns)
            else:
                store.save(inst.id, inst.url, dns)
                log.info("%s backup saved: upstreams=%s bootstrap=%s",
                         inst.name, dns.get("upstream_dns"), dns.get("bootstrap_dns"))
    if is_block_dns_active(cfg, info):
        log.info("%s already on block DNS", inst.name)
        return False
    if cfg.dry_run:
        log.info("[dry-run] %s would switch to block DNS: %s", inst.name, block_payload(cfg))
        return True
    client.set_dns_config(block_payload(cfg))
    client.clear_cache()
    log.info("%s switched to block DNS and cache cleared", inst.name)
    return True


def restore(cfg: Config, inst: Instance, store: BackupStore) -> bool:
    """Restore the backed up DNS. Returns True if the config was changed."""
    backup = store.get(inst.id)
    if backup is None:
        log.debug("%s no backup, nothing to restore", inst.name)
        return False
    client = AdGuardClient(inst, cfg.http_timeout)
    dns = backup["dns"]
    if cfg.dry_run:
        log.info("[dry-run] %s would restore %s", inst.name, dns)
        return True
    client.set_dns_config(dns)
    client.clear_cache()
    store.delete(inst.id)
    log.info("%s original DNS restored (upstreams=%s) and cache cleared",
             inst.name, dns.get("upstream_dns"))
    return True


def run_once(cfg: Config, store: BackupStore, notifier: TelegramNotifier | None = None) -> int:
    try:
        status = detector.check(cfg.api_url, cfg.block_threshold_pct, cfg.block_min_ips, cfg.http_timeout)
    except Exception as exc:  # noqa: BLE001
        log.error("Block check failed, leaving DNS untouched: %s", exc)
        return 1

    summary = ", ".join(f"{s.isp} {s.blocked}/{s.total} ({s.pct:.0f}%)" for s in status.isps)
    log.info("Block active: %s | threshold %d IPs or %.0f%% | %s | data %s",
             status.active, cfg.block_min_ips, cfg.block_threshold_pct, summary, status.last_update)

    changed: list[str] = []
    failed: list[str] = []
    for inst in cfg.instances:
        try:
            if apply_block_dns(cfg, inst, store) if status.active else restore(cfg, inst, store):
                changed.append(inst.label)
        except AdGuardError as exc:
            failed.append(inst.label)
            log.error("%s: %s", inst.name, exc)

    # Notify only on transitions (some instance changed) or on errors while switching.
    if notifier and (changed or failed) and not cfg.dry_run:
        notifier.send(build_message(status.active, status, changed, failed, cfg.block_upstreams))
    return 1 if failed else 0


def next_run(crons: list[str], now: datetime) -> datetime:
    return min(croniter(expr, now).get_next(datetime) for expr in crons)


def loop(cfg: Config, store: BackupStore) -> None:
    stop = False

    def _handle(signum, _frame):
        nonlocal stop
        log.info("Signal %s received, exiting", signum)
        stop = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    log.info("adguard-dns-failover %s", os.environ.get("APP_VERSION", "dev"))
    log.info("Instances: %s", ", ".join(i.name for i in cfg.instances))
    log.info("Schedules: %s", " | ".join(cfg.cron_schedules))
    notifier = make_notifier(cfg)
    log.info("Telegram notifications: %s", "enabled" if notifier.enabled else "disabled")
    if cfg.run_on_start:
        run_once(cfg, store, notifier)
    while not stop:
        target = next_run(cfg.cron_schedules, datetime.now().astimezone())
        log.info("Next check at %s", target.isoformat(timespec="minutes"))
        while not stop and datetime.now().astimezone() < target:
            time.sleep(min(30, max(0.5, (target - datetime.now().astimezone()).total_seconds())))
        if not stop:
            run_once(cfg, store, notifier)


def make_notifier(cfg: Config) -> TelegramNotifier:
    return TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_ids, cfg.http_timeout)


def status_cmd(cfg: Config, store: BackupStore) -> int:
    try:
        st = detector.check(cfg.api_url, cfg.block_threshold_pct, cfg.block_min_ips, cfg.http_timeout)
        print(f"Block active: {st.active} (data {st.last_update})")
        for s in st.isps:
            print(f"  {s.isp:<12} {s.blocked}/{s.total} ({s.pct:.0f}%)")
    except Exception as exc:  # noqa: BLE001
        print(f"Block check failed: {exc}")
    for inst in cfg.instances:
        backup = store.get(inst.id)
        try:
            ups = AdGuardClient(inst, cfg.http_timeout).get_dns_info().get("upstream_dns")
        except AdGuardError as exc:
            ups = f"error: {exc}"
        print(f"{inst.name}: upstreams={ups} backup={'yes (' + backup['saved_at'] + ')' if backup else 'no'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adguard-dns-failover")
    parser.add_argument("command", nargs="?", default="daemon",
                        choices=["daemon", "run-once", "status", "restore", "test-telegram"])
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    try:
        cfg = load_config()
    except (ConfigError, OSError, ValueError) as exc:
        log.error("Configuration error: %s", exc)
        return 2
    store = BackupStore(cfg.data_dir)

    if args.command == "run-once":
        return run_once(cfg, store, make_notifier(cfg))
    if args.command == "test-telegram":
        notifier = make_notifier(cfg)
        if not notifier.enabled:
            log.error("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first")
            return 2
        notifier.send("🤖 <b>adguard-dns-failover</b> conectado. Te avisaré cuando haya bloqueos de IPs 🚨")
        return 0
    if args.command == "status":
        return status_cmd(cfg, store)
    if args.command == "restore":
        errors = 0
        for inst in cfg.instances:
            try:
                restore(cfg, inst, store)
            except AdGuardError as exc:
                errors += 1
                log.error("%s: %s", inst.name, exc)
        return 1 if errors else 0
    loop(cfg, store)
    return 0
