"""Telegram notifications on block state changes."""

from __future__ import annotations

import html
import logging
from datetime import datetime

import requests

from app.detector import BlockStatus

log = logging.getLogger(__name__)


def _isp_lines(status: BlockStatus) -> str:
    return "\n".join(
        f"{'🔴' if s.blocked else '🟢'} <b>{html.escape(s.isp)}</b>: {s.blocked}/{s.total} IPs ({s.pct:.0f}%)"
        for s in status.isps
    )


def _instance_lines(names: list[str], icon: str) -> str:
    return "\n".join(f"{icon} {html.escape(n)}" for n in names)


def build_message(blocked: bool, status: BlockStatus, ok: list[str], failed: list[str],
                  upstreams: list[str]) -> str:
    now = datetime.now().astimezone().strftime("%d/%m/%Y %H:%M")
    if blocked:
        parts = ["🚨🛡️ <b>¡Bloqueo de IPs detectado!</b> 🛡️🚨", ""]
        if ok:
            parts.append("🛡️ Tus servidores DNS han cambiado a los DNS alternativos para esquivar el bloqueo.")
        else:
            parts.append("⚠️ <b>No se ha podido cambiar ningún servidor DNS.</b> Revisa los logs.")
        parts += ["", "📡 <b>Estado por operador:</b>", _isp_lines(status)]
        if ok:
            parts += ["", "✅ <b>Instancias actualizadas:</b>", _instance_lines(ok, "🔀")]
            if upstreams:
                parts += ["", "🌐 <b>Upstreams:</b>", "\n".join(f"<code>{html.escape(u)}</code>" for u in upstreams)]
            parts += ["", "🧹 Caché DNS limpiada. ¡Navegación restablecida! 🚀"]
    else:
        parts = ["🎉🟢 <b>Fin del bloqueo de IPs</b> 🟢🎉", ""]
        if ok:
            parts += ["🔙 Se han restaurado los DNS originales.",
                      "", "✅ <b>Instancias restauradas:</b>", _instance_lines(ok, "♻️"),
                      "", "🧹 Caché DNS limpiada. Todo vuelve a la normalidad 😎"]
        else:
            parts.append("⚠️ <b>No se han podido restaurar los DNS originales.</b> Revisa los logs.")
    if failed:
        parts += ["", "⚠️ <b>Errores en:</b>", _instance_lines(failed, "❌")]
    parts += ["", f"🕒 {now}"]
    return "\n".join(parts)


class TelegramNotifier:
    def __init__(self, token: str | None, chat_ids: list[str], timeout: float = 15.0):
        self.token = token
        self.chat_ids = chat_ids
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_ids)

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        for chat_id in self.chat_ids:
            try:
                resp = requests.post(url, timeout=self.timeout, json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                })
                if resp.status_code >= 400:
                    log.error("Telegram send to %s failed: HTTP %s %s", chat_id, resp.status_code, resp.text[:200])
            except requests.RequestException as exc:
                # Never leak the token: only log the exception type.
                log.error("Telegram send to %s failed: %s", chat_id, type(exc).__name__)
