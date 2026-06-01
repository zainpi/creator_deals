#!/usr/bin/env python3
"""Background worker: runs the continuous category-aware ASIN discovery loop.

Launched as its own process (see Procfile `worker:`). Shares the SQLite DB with
the web process — it writes the global feed + scanner_state, the web reads them.
"""

import logging

from config_loader import load_config
from database import init_db
from continuous_scanner import ScanLoop

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)


def main():
    config = load_config()
    try:
        init_db()
        logger.info("[WORKER] Database ready")
    except Exception as e:
        logger.error(f"[WORKER] Database init error: {e}")
    ScanLoop(config).run_forever()


if __name__ == "__main__":
    main()
