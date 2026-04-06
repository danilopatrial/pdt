from pathlib import Path

VERSION = "1.0.0"

DATA_DIR        = Path.home() / ".pdt"
DATA_FILE       = DATA_DIR / "domains.json"
CONFIG_FILE     = DATA_DIR / "config.json"
PID_FILE             = DATA_DIR / "daemon.pid"
LOG_FILE             = DATA_DIR / "daemon.log"
BACKORDER_PID_FILE   = DATA_DIR / "backorder.pid"
BACKORDER_LOG_FILE   = DATA_DIR / "backorder.log"
BACKORDERS_FILE      = DATA_DIR / "backorders.json"

NOTIFY_WINDOW = 300        # 5 minutes
ARCHIVE_AFTER = 24 * 3600  # 24 hours past drop time
