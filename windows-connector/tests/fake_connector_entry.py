"""TEST-ONLY launcher: the real headless connector (rr_connector.headless) attached to the FakeMT5 terminal.

Never packaged or shipped. Used to verify RR Trading One end to end on machines without MetaTrader 5:
desktop supervisor -> this process -> outbound relay over the local gateway -> engine stack.

* ``FakeMT5`` (mt5-connector/tests) replaces the MetaTrader5 package; ``FAKE_MT5_LOGIN`` / ``FAKE_MT5_SERVER``
  choose the account the fake terminal is logged in to.
* A file-backed test keyring in ``RRMT5_HOME`` replaces the Windows Credential Manager so that linking (one process)
  and running (another process) share credentials, as they do on Windows.

Same command line as the real executable: ``--headless PROFILE``, ``--register-stdin URL``, ``--detect-json``.
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "mt5-connector"))
sys.path.insert(0, str(HERE.parent.parent / "mt5-connector" / "tests"))

FAKE_EXE = os.environ.get("FAKE_MT5_EXE", "C:/MT5/terminal64.exe")


class FileKeyring:
    """Test keyring persisted as JSON (0600). Duck-typed like rr_connector.vault.MemoryKeyring."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def _save(self, d: dict) -> None:
        self.path.write_text(json.dumps(d))
        os.chmod(self.path, 0o600)

    def get_password(self, service, username):
        return self._load().get(f"{service}/{username}")

    def set_password(self, service, username, password):
        d = self._load()
        d[f"{service}/{username}"] = password
        self._save(d)

    def delete_password(self, service, username):
        d = self._load()
        d.pop(f"{service}/{username}", None)
        self._save(d)


def main() -> int:
    from rr_connector import headless
    base = headless.home()
    from rr_connector import app as app_mod
    from rr_connector import terminals, vault
    ring = FileKeyring(base / "test-keyring.json")
    vault._default_backend = lambda: ring
    from connector.adapter import MT5Adapter
    from fake_mt5 import FakeMT5
    fake = FakeMT5(login=int(os.environ.get("FAKE_MT5_LOGIN", "1001")), server=os.environ.get("FAKE_MT5_SERVER", "Demo-Server"))
    for name in filter(None, os.environ.get("FAKE_MT5_DROP", "").split(",")):   # simulate a broker no longer offering it
        fake.symbols.pop(name, None)
    app_mod.make_adapter = lambda: MT5Adapter(fake)
    terminals.find_terminals = lambda *a, **k: [terminals.Terminal(folder=os.path.dirname(FAKE_EXE), exe=FAKE_EXE,
                                                                   running=True, sources=["test"])]
    return headless.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
