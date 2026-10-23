"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_API_URL = "https://hayahora.futbol/estado/data.json"
DEFAULT_BLOCK_UPSTREAMS = "https://dns.xdp.es/dns-query;tls://dns.xdp.es"
DEFAULT_BLOCK_BOOTSTRAP = "85.208.114.51;51.170.52.10"
# Pi-hole only supports plain DNS upstreams (IP or IP#port), no DoH/DoT.
DEFAULT_BLOCK_PIHOLE_UPSTREAMS = "85.208.114.51;51.170.52.10"

KIND_LABELS = {"adguard": "AdGuard", "pihole": "Pi-hole"}


class ConfigError(Exception):
    pass


@dataclass
class Instance:
    id: str
    url: str
    user: str | None
    password: str | None
    display_name: str | None = None
    kind: str = "adguard"
    verify_ssl: bool = True

    @property
    def key(self) -> str:
        """Backup key; AdGuard keeps the bare id for backward compatibility."""
        return self.id if self.kind == "adguard" else f"{self.kind}_{self.id}"

    @property
    def label(self) -> str:
        """Human friendly name for notifications."""
        return self.display_name or re.sub(r"^https?://", "", self.url)

    @property
    def name(self) -> str:
        return f"{KIND_LABELS[self.kind]} #{self.id} ({self.url})"


@dataclass
class Config:
    instances: list[Instance]
    cron_schedules: list[str]
    run_on_start: bool = True
    api_url: str = DEFAULT_API_URL
    block_threshold_pct: float = 50.0
    block_min_ips: int = 20
    isps: list[str] = field(default_factory=list)
    block_upstreams: list[str] = field(default_factory=list)
    block_bootstrap: list[str] = field(default_factory=list)
    block_fallback: list[str] = field(default_factory=list)
    block_upstream_mode: str = ""
    block_pihole_upstreams: list[str] = field(default_factory=list)
    data_dir: Path = Path("/data")
    http_timeout: float = 15.0
    dry_run: bool = False
    telegram_bot_token: str | None = None
    telegram_chat_ids: list[str] = field(default_factory=list)
    telegram_startup_message: bool = True


def split_list(value: str | None) -> list[str]:
    """Split a ';' or newline separated list, dropping empty items."""
    if not value:
        return []
    return [item.strip() for item in re.split(r"[;\n]", value) if item.strip()]


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "si", "sí")


def _secret(env: dict[str, str], key: str) -> str | None:
    """Read KEY or KEY_FILE (Docker secrets)."""
    if env.get(key):
        return env[key]
    path = env.get(f"{key}_FILE")
    if path:
        return Path(path).read_text().strip()
    return None


def load_instances(env: dict[str, str]) -> list[Instance]:
    return _load_kind(env, "ADGUARD", "adguard") + _load_kind(env, "PIHOLE", "pihole")


def _load_kind(env: dict[str, str], prefix: str, kind: str) -> list[Instance]:
    ids = sorted(
        {m.group(1) for k in env if (m := re.fullmatch(rf"{prefix}_(\w+?)_URL", k))},
        key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else 0, x),
    )
    instances = []
    for i in ids:
        url = env[f"{prefix}_{i}_URL"].strip().rstrip("/")
        if not url:
            continue
        if not re.match(r"^https?://", url):
            raise ConfigError(f"{prefix}_{i}_URL must start with http:// or https://")
        instances.append(
            Instance(
                id=i,
                url=url,
                user=_secret(env, f"{prefix}_{i}_USER"),
                password=_secret(env, f"{prefix}_{i}_PASSWORD"),
                display_name=(env.get(f"{prefix}_{i}_NAME") or "").strip() or None,
                kind=kind,
                # Set to false for self-signed certificates (e.g. Pi-hole's default HTTPS).
                verify_ssl=_bool(env.get(f"{prefix}_{i}_VERIFY_SSL"), True),
            )
        )
    return instances


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    instances = load_instances(env)
    if not instances:
        raise ConfigError("No instances configured (set ADGUARD_1_URL and/or PIHOLE_1_URL, ...)")

    crons = split_list(env.get("CRON_SCHEDULES", "*/30 * * * *"))
    from croniter import croniter

    for expr in crons:
        if not croniter.is_valid(expr):
            raise ConfigError(f"Invalid cron expression: {expr!r}")

    upstreams = split_list(env.get("BLOCK_UPSTREAMS", DEFAULT_BLOCK_UPSTREAMS))
    if not upstreams:
        raise ConfigError("BLOCK_UPSTREAMS cannot be empty")

    return Config(
        instances=instances,
        cron_schedules=crons,
        run_on_start=_bool(env.get("RUN_ON_START"), True),
        api_url=env.get("API_URL") or DEFAULT_API_URL,
        block_threshold_pct=float(env.get("BLOCK_THRESHOLD_PCT") or 50),
        block_min_ips=int(env.get("BLOCK_MIN_IPS") or 20),
        isps=[i.strip() for i in re.split(r"[,;\n]", env.get("ISPS") or "") if i.strip()],
        block_upstreams=upstreams,
        block_bootstrap=split_list(env.get("BLOCK_BOOTSTRAP", DEFAULT_BLOCK_BOOTSTRAP)),
        # Defaults to the block upstreams; set BLOCK_FALLBACK=none to leave fallback untouched.
        block_fallback=(
            [] if (env.get("BLOCK_FALLBACK") or "").strip().lower() == "none"
            else split_list(env.get("BLOCK_FALLBACK")) or upstreams
        ),
        block_upstream_mode=env.get("BLOCK_UPSTREAM_MODE", "").strip(),
        block_pihole_upstreams=split_list(env.get("BLOCK_PIHOLE_UPSTREAMS", DEFAULT_BLOCK_PIHOLE_UPSTREAMS)),
        data_dir=Path(env.get("DATA_DIR") or "/data"),
        http_timeout=float(env.get("HTTP_TIMEOUT") or 15),
        dry_run=_bool(env.get("DRY_RUN"), False),
        telegram_bot_token=_secret(env, "TELEGRAM_BOT_TOKEN"),
        telegram_chat_ids=split_list(env.get("TELEGRAM_CHAT_ID")),
        telegram_startup_message=_bool(env.get("TELEGRAM_STARTUP_MESSAGE"), True),
    )
