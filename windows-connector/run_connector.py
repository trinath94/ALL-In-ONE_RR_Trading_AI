"""PyInstaller entry script (RR_MT5_Connector.exe)."""
import sys

from rr_connector.app import main

if __name__ == "__main__":
    sys.exit(main())
