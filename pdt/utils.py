import re
import sys
from collections import deque
from datetime import datetime, timedelta, timezone

from rich.console import Console

from .constants import NOTIFY_WINDOW

console = Console()

# ── Verbose logging ───────────────────────────────────────────────────────────

VERBOSE    = False
_vlog_sink = None   # set to a deque when inside a Live context


def set_verbose(v: bool):
    global VERBOSE
    VERBOSE = v


def set_vlog_sink(sink):
    global _vlog_sink
    _vlog_sink = sink


def vlog(msg: str):
    """Log a verbose message. Buffered when inside a Live display, printed directly otherwise."""
    if not VERBOSE:
        return
    if _vlog_sink is not None:
        _vlog_sink.append(msg)
    else:
        console.print(f"[dim blue]  [v] {msg}[/dim blue]")


# ── Time helpers ──────────────────────────────────────────────────────────────

def utcnow() -> datetime:
    """Return the current UTC time as a naive datetime (drop-in for datetime.utcnow())."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_duration(s: str) -> float:
    """'1d3h57m' → total seconds as float"""
    m = re.fullmatch(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s.strip())
    if not m or not any(m.groups()):
        raise ValueError(f"Invalid duration '{s}'. Use e.g. 1d3h57m or 45m")
    d, h, mi, sec = (int(x or 0) for x in m.groups())
    return timedelta(days=d, hours=h, minutes=mi, seconds=sec).total_seconds()


def fmt_duration(secs: float) -> str:
    """Total seconds → fixed-width string like '1d 03h 51m 00s'"""
    if secs <= 0:
        return "NOW"
    td = timedelta(seconds=int(secs))
    d = td.days
    h, rem = divmod(td.seconds, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h:02d}h {m:02d}m {s:02d}s"
    if h:
        return f"{h:02d}h {m:02d}m {s:02d}s"
    if m:
        return f"{m:02d}m {s:02d}s"
    return f"{s:02d}s"


def to_local(dt: datetime) -> datetime:
    """Convert a naive UTC datetime to the local timezone."""
    return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def remaining(entry: dict) -> float:
    if not entry.get("drop_time"):
        return float("inf")
    drop = datetime.fromisoformat(entry["drop_time"])
    return (drop - utcnow()).total_seconds()


# ── Domain helpers ────────────────────────────────────────────────────────────

def find_domain(domains: list, name: str):
    """Return the first entry matching name, or None."""
    for d in domains:
        if d["domain"] == name:
            return d
    return None


def resolve_targets(
    targets: tuple, domains: list, allow_untracked: bool = False
) -> list:
    """Resolve a mix of domain names and 1-based indices to domain names.

    Indices match the default 'list' sort order (remaining time, active only).
    Exits on out-of-range index or unknown name (unless allow_untracked=True).
    Deduplicates while preserving order.
    """
    active_sorted = sorted(
        [d for d in domains if not d.get("archived")],
        key=lambda d: remaining(d),
    )
    resolved = []
    seen: set = set()
    for t in targets:
        t = str(t)
        if t.isdigit():
            idx = int(t) - 1
            if idx < 0 or idx >= len(active_sorted):
                console.print(f"[red]Index out of range: [bold]{t}[/bold][/red]")
                sys.exit(1)
            name = active_sorted[idx]["domain"]
        else:
            name = t.lower().strip()
            if not allow_untracked and not any(d["domain"] == name for d in domains):
                console.print(f"[red]Not found: [bold]{name}[/bold][/red]")
                sys.exit(1)
        if name not in seen:
            seen.add(name)
            resolved.append(name)
    return resolved


# ── Display helpers ───────────────────────────────────────────────────────────

def status_style(status: str) -> str:
    s = status.lower()
    if "available" in s:
        return "bold green"
    if "pending" in s:
        return "yellow"
    if "redemption" in s:
        return "bold red"
    if "error" in s or "fetch" in s:
        return "dim red"
    return "dim white"
