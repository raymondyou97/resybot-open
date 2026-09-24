"""Loopback-only server launcher, independent of the launch directory."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.server import app  # noqa: E402
import uvicorn  # noqa: E402

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8000, log_level='warning', access_log=False)
