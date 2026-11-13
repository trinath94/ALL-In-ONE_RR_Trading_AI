"""Secret storage in the Windows Credential Manager (per-user, DPAPI-protected) via ``keyring``.

Stored here, never in files: the agent API key, the per-agent bridge HMAC secret, the device's Ed25519 private
key and the key that encrypts the local configuration database. Trading passwords are never requested or stored:
the connector uses the MT5 terminal's own authorised session.
"""
from __future__ import annotations

import sys

import keyring
from keyring.backend import KeyringBackend

SERVICE = "RR MT5 Connector"


def _default_backend() -> KeyringBackend | None:
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring  # Windows Credential Manager
        return WinVaultKeyring()
    return None


class Vault:
    def __init__(self, backend=None):
        self.backend = backend or _default_backend() or keyring.get_keyring()

    def get(self, name: str) -> str | None:
        return self.backend.get_password(SERVICE, name)

    def set(self, name: str, value: str) -> None:
        self.backend.set_password(SERVICE, name, value)

    def delete(self, name: str) -> None:
        try:
            self.backend.delete_password(SERVICE, name)
        except Exception:  # noqa: BLE001 - absent entry
            pass


class MemoryKeyring:
    """In-memory store for tests. Deliberately NOT a ``KeyringBackend`` subclass, so keyring's automatic
    backend discovery can never select it in the application."""

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)
