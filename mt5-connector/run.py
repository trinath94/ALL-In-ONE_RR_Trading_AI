"""Entrypoint for the Windows service (see windows/install-service.ps1)."""
from __future__ import annotations

import logging
import os
import sys

import uvicorn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from connector.adapter import MT5Adapter  # noqa: E402
from connector.app import ConnectorConfig, create_app  # noqa: E402


def main() -> None:
    logging.basicConfig(level=os.environ.get("RR_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.environ.get("RR_LOG_FILE", "connector.log"))])
    cfg = ConnectorConfig.from_env()
    app = create_app(cfg, MT5Adapter())
    uvicorn.run(app, host=os.environ.get("RR_BIND", "0.0.0.0"), port=int(os.environ.get("RR_PORT", "8443")),
                ssl_certfile=os.environ["RR_TLS_CERT"], ssl_keyfile=os.environ["RR_TLS_KEY"], log_level="info",
                workers=1)  # single worker: MT5 API is process-bound


if __name__ == "__main__":
    main()
