"""Backup creation/restore and storage release. (part of MeetingService)."""
from __future__ import annotations

import json
import shutil
import urllib.request
from datetime import datetime, timezone
from sqlalchemy import select
from core.backup import create_backup, list_backups, restore_backup
from core.security.secrets import is_loopback_endpoint
from core.store.db import session_scope
from core.store.models import BackupRecord
from core.logging_setup import get_logger
from core.services._common import _NO_REDIRECT_OPENER, ActiveMeetingError

log = get_logger("ma.service")


class BackupsMixin:

    # --- Phase 7c: backup / restore ---
    def _backup_due(self) -> bool:
        """Auto-backup is due if none exists yet or the newest is stale."""
        from datetime import timezone
        with session_scope() as s:
            newest = s.scalars(select(BackupRecord).order_by(
                BackupRecord.created_at.desc())).first()
        if newest is None or newest.created_at is None:
            return True
        age = datetime.now(timezone.utc) - newest.created_at.replace(tzinfo=timezone.utc)
        return age.total_seconds() >= self.config.backup_interval_hours * 3600

    def create_backup(self, kind: str = "db", note: str = "") -> dict:
        return create_backup(self.config, kind=kind, note=note)

    def list_backups(self) -> list:
        return list_backups(self.config)

    def restore_backup(self, backup_id: str, confirm: bool = False) -> dict:
        with self._state_lock:
            if self._sessions:
                raise ActiveMeetingError(
                    "Ein Backup kann während einer aktiven Aufnahme nicht wiederhergestellt werden.")
        return restore_backup(self.config, backup_id, confirm=confirm)

    def release_storage(self) -> dict:
        """Release application caches without stopping external audio/LLM services."""
        removed_bytes = 0
        removed_paths = []
        for relative in ("cache", "tmp", "transcription-cache"):
            path = (self.config.base_dir / relative).resolve()
            root = self.config.base_dir.resolve()
            if root not in path.parents:
                continue
            if not path.exists():
                continue
            for item in path.rglob("*"):
                if item.is_file():
                    try:
                        removed_bytes += item.stat().st_size
                    except OSError:
                        pass
            try:
                shutil.rmtree(path)
                removed_paths.append(relative)
            except OSError as exc:
                log.warning("cache_release_failed path=%s error=%s", path, exc)

        # Drop only the core's cached provider objects.  An in-flight worker
        # still owns its engine and can finish normally; subsequent work gets a
        # fresh object.  This releases idle model/runtime references without
        # touching installed model files or terminating external services.
        self._providers.clear_cache()
        self._llm_providers.clear_cache()
        self._embed_manager.clear_cache()

        ollama = {"requested": False, "unloaded": False, "note": ""}
        base = (self.config.ollama_base_url or "").rstrip("/")
        if base:
            # SSRF guard: never issue requests to a config-controlled URL that
            # is not confined to this machine, unless the user explicitly
            # enabled external network. This keeps release_storage from being
            # turned into a request to arbitrary / internal / cloud-metadata
            # endpoints via a poisoned ollama_base_url.
            # Ollama is a local lifecycle endpoint.  Even when general network
            # access is enabled, never turn this maintenance action into a
            # request to an arbitrary host or cloud-metadata service.
            if not is_loopback_endpoint(base):
                ollama["note"] = ("Ollama-Endpunkt ist nicht lokal; aus "
                                  "Sicherheitsgründen wurde kein Aufruf ausgeführt.")
            else:
                endpoint = base[:-3] if base.endswith("/v1") else base
                try:
                    ollama["requested"] = True
                    with _NO_REDIRECT_OPENER.open(endpoint + "/api/ps", timeout=2) as response:
                        running = json.loads(response.read().decode() or "{}").get("models", [])
                    for model in running:
                        name = model.get("name") if isinstance(model, dict) else None
                        if not name:
                            continue
                        req = urllib.request.Request(
                            endpoint + "/api/generate",
                            data=json.dumps({"model": name, "keep_alive": 0}).encode(),
                            headers={"Content-Type": "application/json"}, method="POST")
                        with _NO_REDIRECT_OPENER.open(req, timeout=2) as response:
                            ollama["unloaded"] = ollama["unloaded"] or response.status < 300
                except Exception:
                    ollama["note"] = "Ollama war nicht erreichbar; Caches wurden trotzdem freigegeben."
        return {"removed_bytes": removed_bytes, "paths": removed_paths,
                "ollama": ollama,
                "llama_cpp": "Nicht beendet oder neu gestartet (externer Dienst)."}
