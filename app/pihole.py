"""Minimal Pi-hole v6 API client (FTL /api)."""

from __future__ import annotations

import requests

from app.config import Instance
from app.errors import DnsServerError

APP_SUDO_HINT = (
    "app passwords cannot change settings: enable webserver.api.app_sudo in Pi-hole "
    "(Settings > All settings > Webserver and API) or use the main password"
)
READ_ONLY_HINT = (
    "dns.upstreams looks read-only: remove FTLCONF_dns_upstreams from the Pi-hole "
    "environment and set the upstreams from the web UI instead"
)


class PiHoleError(DnsServerError):
    pass


class PiHoleClient:
    """Use as a context manager so the API session is always released (seats are limited)."""

    def __init__(self, instance: Instance, timeout: float = 15.0):
        self.instance = instance
        self.timeout = timeout
        self.http = requests.Session()
        self.http.verify = instance.verify_ssl
        self.sid: str | None = None
        # True when logged in with an app password (needs app_sudo to change settings).
        self.app_session = False

    def __enter__(self) -> "PiHoleClient":
        self.login()
        return self

    def __exit__(self, *_exc) -> None:
        self.logout()

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.instance.url}/api/{path}"
        headers = {"X-FTL-SID": self.sid} if self.sid else {}
        try:
            resp = self.http.request(method, url, timeout=self.timeout, headers=headers, **kwargs)
        except requests.exceptions.SSLError as exc:
            raise PiHoleError(
                f"{method} {url} failed: TLS certificate not trusted. If Pi-hole uses its default "
                f"self-signed certificate, set PIHOLE_{self.instance.id}_VERIFY_SSL=false or use http://"
            ) from exc
        except requests.RequestException as exc:
            raise PiHoleError(f"{method} {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            detail = resp.text.strip()[:300]
            if "app_sudo" in detail:
                detail = APP_SUDO_HINT
            raise PiHoleError(f"{method} {url} -> HTTP {resp.status_code}: {detail}")
        return resp

    def login(self) -> None:
        # Without a password Pi-hole accepts unauthenticated requests.
        if not self.instance.password:
            return
        data = self._request("POST", "auth", json={"password": self.instance.password}).json()
        session = data.get("session") or {}
        if not session.get("valid"):
            raise PiHoleError(f"Pi-hole login failed: {session.get('message') or 'invalid session'}")
        self.sid = session.get("sid")
        self.app_session = "app-password" in (session.get("message") or "")

    def logout(self) -> None:
        if not self.sid:
            return
        try:
            self._request("DELETE", "auth")
        except PiHoleError:
            pass  # Session will expire on its own.
        finally:
            self.sid = None

    def get_upstreams(self) -> list[str]:
        data = self._request("GET", "config/dns/upstreams").json()
        return list(data["config"]["dns"]["upstreams"])

    def upstreams_read_only(self) -> bool:
        """True when dns.upstreams is set through an FTLCONF_ env var (API cannot change it)."""
        try:
            data = self._request("GET", "config/dns/upstreams", params={"detailed": "true"}).json()
            flags = data["config"]["dns"]["upstreams"].get("flags") or {}
        except (PiHoleError, KeyError, AttributeError, ValueError):
            return False
        return bool(flags.get("env_var"))

    def app_sudo_enabled(self) -> bool:
        data = self._request("GET", "config/webserver/api/app_sudo").json()
        return bool(data["config"]["webserver"]["api"]["app_sudo"])

    def set_upstreams(self, upstreams: list[str]) -> None:
        if self.upstreams_read_only():
            raise PiHoleError(READ_ONLY_HINT)
        payload = {"config": {"dns": {"upstreams": upstreams}}}
        # Apply without the implicit restart; restart_dns() below restarts once and empties the cache.
        self._request("PATCH", "config", params={"restart": "false"}, json=payload)

    def restart_dns(self) -> None:
        self._request("POST", "action/restartdns")
