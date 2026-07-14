#!/usr/bin/env python3
"""Background worker: runs the continuous ASIN discovery loops.

Launched as its own process (see Procfile `worker:`). Shares the SQLite DB with
the web process — it writes the global feed + scanner_state, the web reads them.

Runs two independent loops in this one process (no extra Heroku dyno needed):
  - ScanLoop           (continuous_scanner.py) — the original learned-baseline,
                        multi-dimensional sweep. Always on.
  - MethodScanLoop      (method_scanner.py) — the Method 1 vs Method 2 A/B
                        engine from the board. Runs alongside the original
                        scanner as a second mode; starts paused until enabled
                        via /api/method_test/control (method_engine_state.enabled).
"""

import logging
import threading

from config_loader import load_config
from database import init_db
from continuous_scanner import ScanLoop
from method_scanner import MethodScanLoop

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

    try:
        method_thread = threading.Thread(
            target=lambda: MethodScanLoop(config).run_forever(),
            name="method-scan-loop",
            daemon=True,
        )
        method_thread.start()
        logger.info("[WORKER] Method comparison loop started (paused until enabled)")
    except Exception as e:
        logger.error(f"[WORKER] Failed to start method comparison loop: {e}")

    ScanLoop(config).run_forever()


if __name__ == "__main__":
    main()
