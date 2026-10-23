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
from app.adguard import AdGuardClient, extract_backup
from app.config import Config, ConfigError, Instance, load_config
from app.errors import DnsServerError
from app.notifier import TelegramNotifier, build_check_message, build_message
from app.pihole import APP_SUDO_HINT, READ_ONLY_HINT, PiHoleClient, PiHoleError
from app.state import BackupStore

log = logging.getLogger("dns-blockswitch")


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


def _hosts(upstreams: list[str]) -> set[str]:
    return {_upstream_host(u.split("#", 1)[0]) for u in upstreams}


class AdGuardBackend:
    """Backs up and switches upstream/bootstrap/fallback DNS of an AdGuard Home instance."""

    def __init__(self, cfg: Config, inst: Instance):
        self.cfg = cfg
        self.client = AdGuardClient(inst, cfg.http_timeout)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def current(self) -> dict:
        return extract_backup(self.client.get_dns_info())

    def is_block_active(self, dns: dict) -> bool:
        return is_block_dns_active(self.cfg, dns)

    def ensure_writable(self) -> None:
        return None

    def block_config(self) -> dict:
        return block_payload(self.cfg)

    def apply(self, dns: dict) -> None:
        self.client.set_dns_config(dns)
        self.client.clear_cache()


class PiHoleBackend:
    """Backs up and switches dns.upstreams of a Pi-hole v6 instance."""

    def __init__(self, cfg: Config, inst: Instance):
        self.cfg = cfg
        self.client = PiHoleClient(inst, cfg.http_timeout)

    def __enter__(self):
        self.client.login()
        return self

    def __exit__(self, *_exc):
        self.client.logout()

    def current(self) -> dict:
        return {"upstreams": self.client.get_upstreams()}

    def is_block_active(self, dns: dict) -> bool:
        current = [u for u in dns.get("upstreams") or [] if u.strip()]
        return bool(current) and _hosts(current) <= _hosts(self.cfg.block_pihole_upstreams)

    def ensure_writable(self) -> None:
        if self.client.upstreams_read_only():
            raise PiHoleError(READ_ONLY_HINT)
        if self.client.app_session and not self.client.app_sudo_enabled():
            raise PiHoleError(APP_SUDO_HINT)

    def block_config(self) -> dict:
        return {"upstreams": self.cfg.block_pihole_upstreams}

    def apply(self, dns: dict) -> None:
        self.client.set_upstreams(dns["upstreams"])
        self.client.restart_dns()  # Restarting FTL also empties its DNS cache.


def make_backend(cfg: Config, inst: Instance):
    return PiHoleBackend(cfg, inst) if inst.kind == "pihole" else AdGuardBackend(cfg, inst)


def _describe(dns: dict) -> str:
    if "upstreams" in dns:
        return f"upstreams={dns['upstreams']}"
    return f"upstreams={dns.get('upstream_dns')} bootstrap={dns.get('bootstrap_dns')}"


def apply_block_dns(cfg: Config, inst: Instance, store: BackupStore) -> bool:
    """Switch to the block DNS. Returns True if the config was changed."""
    with make_backend(cfg, inst) as backend:
        dns = backend.current()
        active = backend.is_block_active(dns)
        if not active:
            # Fail before taking a backup that could never be restored.
            backend.ensure_writable()
        saved_now = False
        if store.get(inst.key) is None:
            if active:
                log.warning("%s already uses block upstreams and no backup exists; skipping backup", inst.name)
            elif cfg.dry_run:
                log.info("[dry-run] %s would back up %s", inst.name, dns)
            else:
                store.save(inst.key, inst.url, dns)
                saved_now = True
                log.info("%s backup saved: %s", inst.name, _describe(dns))
        if active:
            log.info("%s already on block DNS", inst.name)
            return False
        if cfg.dry_run:
            log.info("[dry-run] %s would switch to block DNS: %s", inst.name, backend.block_config())
            return True
        try:
            backend.apply(backend.block_config())
        except DnsServerError:
            if saved_now:
                # Config was not changed, so the backup must not trigger a later restore.
                store.discard(inst.key)
            raise
    log.info("%s switched to block DNS and cache cleared", inst.name)
    return True


def restore(cfg: Config, inst: Instance, store: BackupStore) -> bool:
    """Restore the backed up DNS. Returns True if the config was changed."""
    backup = store.get(inst.key)
    if backup is None:
        log.debug("%s no backup, nothing to restore", inst.name)
        return False
    dns = backup["dns"]
    if cfg.dry_run:
        log.info("[dry-run] %s would restore %s", inst.name, dns)
        return True
    with make_backend(cfg, inst) as backend:
        backend.apply(dns)
    store.delete(inst.key)
    log.info("%s original DNS restored (%s) and cache cleared", inst.name, _describe(dns))
    return True


def check_instances(cfg: Config, notifier: TelegramNotifier | None = None) -> list[str]:
    """Connect to every instance without changing anything; return labels that failed."""
    ok: list[str] = []
    failed: list[tuple[str, str]] = []
    for inst in cfg.instances:
        try:
            with make_backend(cfg, inst) as backend:
                dns = backend.current()
                backend.ensure_writable()
            ok.append(inst.label)
            log.info("%s: OK, writable (%s)", inst.name, _describe(dns))
        except DnsServerError as exc:
            failed.append((inst.label, str(exc)))
            log.error("%s: connection check failed: %s", inst.name, exc)
    # Errors are always reported; the "all good" summary can be turned off.
    if notifier and (failed or cfg.telegram_startup_message):
        notifier.send(build_check_message(ok, failed, cfg.dry_run))
    return [label for label, _ in failed]


def run_once(cfg: Config, store: BackupStore, notifier: TelegramNotifier | None = None) -> int:
    try:
        status = detector.check(cfg.api_url, cfg.block_threshold_pct, cfg.block_min_ips, cfg.http_timeout, cfg.isps)
    except Exception as exc:  # noqa: BLE001
        log.error("Block check failed, leaving DNS untouched: %s", exc)
        return 1

    if status.unknown_isps:
        log.warning("ISPS not found in data (ignored): %s", ", ".join(status.unknown_isps))
    summary = ", ".join(f"{s.isp} {s.blocked}/{s.total} ({s.pct:.0f}%)" for s in status.isps)
    log.info("Block active: %s | threshold %d IPs or %.0f%% | %s | data %s",
             status.active, cfg.block_min_ips, cfg.block_threshold_pct, summary, status.last_update)

    changed: list[str] = []
    failed: list[str] = []
    for inst in cfg.instances:
        try:
            if apply_block_dns(cfg, inst, store) if status.active else restore(cfg, inst, store):
                changed.append(inst.label)
        except DnsServerError as exc:
            failed.append(inst.label)
            log.error("%s: %s", inst.name, exc)

    # Notify only on transitions (some instance changed) or on errors while switching.
    if notifier and (changed or failed) and not cfg.dry_run:
        kinds = {i.kind for i in cfg.instances}
        shown = cfg.block_upstreams if "adguard" in kinds else cfg.block_pihole_upstreams
        notifier.send(build_message(status.active, status, changed, failed, shown))
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

    log.info("dns-blockswitch %s", os.environ.get("APP_VERSION", "dev"))
    log.info("Mode: %s", "DRY-RUN (no changes will be made)" if cfg.dry_run else "live")
    log.info("Instances: %s", ", ".join(i.name for i in cfg.instances))
    log.info("Schedules: %s", " | ".join(cfg.cron_schedules))
    log.info("ISPs checked: %s", ", ".join(cfg.isps) if cfg.isps else "all")
    notifier = make_notifier(cfg)
    log.info("Telegram notifications: %s", "enabled" if notifier.enabled else "disabled")
    check_instances(cfg, notifier)
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
        st = detector.check(cfg.api_url, cfg.block_threshold_pct, cfg.block_min_ips, cfg.http_timeout, cfg.isps)
        print(f"Block active: {st.active} (data {st.last_update})")
        for s in st.isps:
            print(f"  {s.isp:<12} {s.blocked}/{s.total} ({s.pct:.0f}%)")
    except Exception as exc:  # noqa: BLE001
        print(f"Block check failed: {exc}")
    for inst in cfg.instances:
        backup = store.get(inst.key)
        try:
            with make_backend(cfg, inst) as backend:
                dns = backend.current()
            ups = dns.get("upstreams", dns.get("upstream_dns"))
        except DnsServerError as exc:
            ups = f"error: {exc}"
        print(f"{inst.name}: upstreams={ups} backup={'yes (' + backup['saved_at'] + ')' if backup else 'no'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Self-signed certificates are opted into explicitly with *_VERIFY_SSL=false.
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    parser = argparse.ArgumentParser(prog="dns-blockswitch")
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
        notifier.send("🤖 <b>dns-blockswitch</b> conectado. Te avisaré cuando haya bloqueos de IPs 🚨")
        return 0
    if args.command == "status":
        return status_cmd(cfg, store)
    if args.command == "restore":
        errors = 0
        for inst in cfg.instances:
            try:
                restore(cfg, inst, store)
            except DnsServerError as exc:
                errors += 1
                log.error("%s: %s", inst.name, exc)
        return 1 if errors else 0
    loop(cfg, store)
    return 0
