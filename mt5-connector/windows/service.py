"""Windows service wrapper for the RR MT5 connector (pywin32).

Install/start via windows/install.ps1. The service runs under a dedicated local user account
(not LocalSystem) because the MT5 terminal stores its data per user profile.
"""
from __future__ import annotations

import os
import sys
import threading

import servicemanager  # type: ignore
import win32event  # type: ignore
import win32service  # type: ignore
import win32serviceutil  # type: ignore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class RRConnectorService(win32serviceutil.ServiceFramework):
    _svc_name_ = "RRMT5Connector"
    _svc_display_name_ = "RR Trading Engine - MT5 Connector"
    _svc_description_ = "Authenticated HTTPS bridge between RR Trading Engine (Ubuntu) and the MetaTrader 5 terminal."

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.server = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self.server is not None:
            self.server.should_exit = True
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED, (self._svc_name_, ""))
        os.chdir(ROOT)
        from dotenv_loader import load_env  # local helper: reads connector.env (non-secret settings)
        load_env(os.path.join(ROOT, "connector.env"))
        import logging

        import uvicorn

        from connector.adapter import MT5Adapter
        from connector.app import ConnectorConfig, create_app
        logging.basicConfig(level=os.environ.get("RR_LOG_LEVEL", "INFO"), filename=os.environ.get("RR_LOG_FILE", os.path.join(ROOT, "logs", "connector.log")),
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
        app = create_app(ConnectorConfig.from_env(), MT5Adapter())
        cfg = uvicorn.Config(app, host=os.environ.get("RR_BIND", "0.0.0.0"), port=int(os.environ.get("RR_PORT", "8443")),
                             ssl_certfile=os.environ["RR_TLS_CERT"], ssl_keyfile=os.environ["RR_TLS_KEY"], workers=1, log_config=None)
        self.server = uvicorn.Server(cfg)
        t = threading.Thread(target=self.server.run, daemon=True)
        t.start()
        win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
        t.join(timeout=20)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(RRConnectorService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(RRConnectorService)
