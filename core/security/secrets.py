"""Secret handling and network-action gating.

- API keys are never stored in the database or logs.
- Preferred backend: OS keyring. Fallback: a 0600 file (obfuscated, not
  encrypted; real encryption/SQLCipher is explicitly out of MVP scope).
- Any network action (e.g. model downloads) is gated: no network by default,
  and downloads always require explicit confirmation.
"""
from __future__ import annotations

import base64
import ipaddress
import os
import threading
from pathlib import Path
from urllib.parse import urlparse

from core.config import Config, get_config
from core.logging_setup import get_logger

log = get_logger("ma.security")

try:  # optional dependency; keyring may be unavailable in headless envs
    import keyring  # type: ignore
    _HAS_KEYRING = True
except Exception:  # pragma: no cover
    keyring = None  # type: ignore
    _HAS_KEYRING = False

_SERVICE = "meeting-assistant"


class NetworkBlockedError(RuntimeError):
    """Raised when a network action is attempted while disallowed."""


def is_loopback_endpoint(url: str) -> bool:
    """Return whether an HTTP endpoint is safely confined to this machine."""
    try:
        parsed = urlparse((url or "").strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"}:
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False
    except ValueError:
        return False


def require_endpoint_allowed(url: str, config: Config | None = None) -> None:
    """Allow local providers without network opt-in, block remote providers."""
    cfg = config or get_config()
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise NetworkBlockedError("Der Anbieter-Endpunkt muss eine HTTP(S)-URL sein.")
    if is_loopback_endpoint(url):
        return
    if cfg.network_allowed:
        return
    raise NetworkBlockedError(
        "Dieser LLM-Endpunkt liegt nicht auf diesem Rechner. Aktiviere "
        "'externe Anbieter erlauben' ausdrücklich in den Einstellungen."
    )


class SecretStore:
    def __init__(self, config: Config):
        self._config = config
        self._fallback_file = config.state_dir / "secrets.b64"
        self._lock = threading.RLock()

    @property
    def using_keyring(self) -> bool:
        return _HAS_KEYRING

    def _use_keyring(self) -> bool:
        if not _HAS_KEYRING:
            return False
        # probe: keyring may raise in headless environments
        try:
            keyring.get_password(_SERVICE, "__probe__")  # type: ignore
            return True
        except Exception:
            return False

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._config.ensure_dirs()
            if self._use_keyring():
                keyring.set_password(_SERVICE, key, value)  # type: ignore
                return
            self._fallback_file.parent.mkdir(parents=True, exist_ok=True)
            data = self._read_fallback()
            data[key] = base64.b64encode(value.encode()).decode()
            self._atomic_write(_dumps(data))

    def get(self, key: str) -> str | None:
        with self._lock:
            if self._use_keyring():
                return keyring.get_password(_SERVICE, key)  # type: ignore
            data = self._read_fallback()
            enc = data.get(key)
            try:
                return base64.b64decode(enc).decode() if enc else None
            except (ValueError, UnicodeDecodeError):
                return None

    def delete(self, key: str) -> None:
        with self._lock:
            if self._use_keyring():
                try:
                    keyring.delete_password(_SERVICE, key)  # type: ignore
                except Exception:
                    pass
                return
            data = self._read_fallback()
            data.pop(key, None)
            if self._fallback_file.exists():
                self._atomic_write(_dumps(data))

    def _atomic_write(self, raw: str) -> None:
        tmp = self._fallback_file.with_name(self._fallback_file.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._fallback_file)
        try:
            os.chmod(self._fallback_file, 0o600)
        except OSError:
            pass

    def _read_fallback(self) -> dict[str, str]:
        if not self._fallback_file.exists():
            return {}
        try:
            try:
                os.chmod(self._fallback_file, 0o600)
            except OSError:
                pass
            return _loads(self._fallback_file.read_text())
        except Exception:
            return {}


def _dumps(data: dict[str, str]) -> str:
    import json
    return json.dumps(data, indent=2, sort_keys=True)


def _loads(raw: str) -> dict[str, str]:
    import json
    return json.loads(raw)


_secret_service: SecretStore | None = None


def secret_service(config: Config | None = None) -> SecretStore:
    global _secret_service
    if _secret_service is None:
        _secret_service = SecretStore(config or get_config())
    elif config is not None and config is not get_config():
        _secret_service = SecretStore(config)
    return _secret_service


def require_network_action(
    reason: str,
    config: Config | None = None,
    confirmed: bool = False,
) -> None:
    """Gate any network access. Raises if blocked.

    - If network is disabled in config -> always blocked (confirmation alone
      is not enough; the user must enable network first).
    - If the action is a download and confirmation is required -> must pass.
    """
    cfg = config or get_config()
    if not cfg.network_allowed:
        log.warning("network_action_blocked reason=%s (network disabled)", reason)
        raise NetworkBlockedError(
            f"Netzwerk ist deaktiviert. Aktivieren Sie es in den Einstellungen, "
            f"um '{reason}' auszuführen."
        )
    if "download" in reason and cfg.downloads_require_confirmation and not confirmed:
        raise NetworkBlockedError(
            f"Download '{reason}' erfordert eine ausdrückliche Bestätigung."
        )
    log.info("network_action_allowed reason=%s", reason)
