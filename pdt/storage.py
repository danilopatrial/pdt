import json

from .constants import DATA_DIR, DATA_FILE, BACKORDERS_FILE, ARCHIVE_AFTER
from .utils import remaining


# ── Domains ───────────────────────────────────────────────────────────────────

def ensure_data():
    DATA_DIR.mkdir(exist_ok=True)
    if not DATA_FILE.exists():
        DATA_FILE.write_text("[]")


def load() -> list:
    ensure_data()
    return json.loads(DATA_FILE.read_text())


def save(domains: list):
    ensure_data()
    DATA_FILE.write_text(json.dumps(domains, indent=2))


def archive_expired() -> int:
    """Mark domains whose drop time passed >24h ago as archived. Returns count newly archived."""
    domains = load()
    newly = 0
    for d in domains:
        if not d.get("archived") and remaining(d) < -ARCHIVE_AFTER:
            d["archived"] = True
            newly += 1
    if newly:
        save(domains)
    return newly


# ── Backorders ────────────────────────────────────────────────────────────────

def ensure_backorders():
    DATA_DIR.mkdir(exist_ok=True)
    if not BACKORDERS_FILE.exists():
        BACKORDERS_FILE.write_text("[]")


def load_backorders() -> list:
    ensure_backorders()
    return json.loads(BACKORDERS_FILE.read_text())


def save_backorders(backorders: list):
    ensure_backorders()
    BACKORDERS_FILE.write_text(json.dumps(backorders, indent=2))
