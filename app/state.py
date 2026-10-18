"""Persistent backups of the original DNS settings per instance."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class BackupStore:
    def __init__(self, data_dir: Path):
        self.dir = data_dir / "backups"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, instance_id: str) -> Path:
        return self.dir / f"instance_{instance_id}.json"

    def get(self, instance_id: str) -> dict | None:
        path = self._path(instance_id)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def save(self, instance_id: str, url: str, dns: dict) -> None:
        data = {
            "url": url,
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "dns": dns,
        }
        # Atomic write so a crash never leaves a half-written backup.
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self._path(instance_id))

    def delete(self, instance_id: str) -> None:
        path = self._path(instance_id)
        if path.exists():
            # Keep the last restored backup around for reference.
            os.replace(path, path.with_suffix(".restored.json"))
