"""Minimal AdGuard Home API client."""

from __future__ import annotations

import requests

from app.config import Instance

# DNS fields saved in backups and restored afterwards.
BACKUP_FIELDS = ("upstream_dns", "bootstrap_dns", "fallback_dns", "upstream_mode")


class AdGuardError(Exception):
    pass


class AdGuardClient:
    def __init__(self, instance: Instance, timeout: float = 15.0):
        self.instance = instance
        self.timeout = timeout
        self.session = requests.Session()
        if instance.user is not None:
            self.session.auth = (instance.user, instance.password or "")

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.instance.url}/control/{path}"
        try:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise AdGuardError(f"{method} {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise AdGuardError(f"{method} {url} -> HTTP {resp.status_code}: {resp.text.strip()[:200]}")
        return resp

    def get_dns_info(self) -> dict:
        return self._request("GET", "dns_info").json()

    def set_dns_config(self, config: dict) -> None:
        self._request("POST", "dns_config", json=config)

    def clear_cache(self) -> None:
        self._request("POST", "cache_clear")


def extract_backup(dns_info: dict) -> dict:
    return {k: dns_info.get(k) for k in BACKUP_FIELDS if dns_info.get(k) is not None}
