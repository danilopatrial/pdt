"""
Persistent daily file logger for PDT.

Log files are written to ~/.pdt/logs/pdt-YYYY-MM-DD.log (UTC date).
A new file is created each day; old files are kept indefinitely.

Usage:
    from .logger import get_logger
    log = get_logger()
    log.info("something happened")

Convenience helpers:
    log_cmd(name, args)                      — log a CLI invocation
    log_event(msg, level="info")             — log a general event
    log_api_req(method, url, *, body=None)   — log an outgoing API request
    log_api_resp(method, url, status, nbytes, elapsed_ms, *, error=None)
"""

import logging
from datetime import datetime, timezone

from .constants import LOGS_DIR

_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    """Return the singleton PDT logger, initialising it on first call."""
    global _logger
    if _logger is not None:
        return _logger

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = LOGS_DIR / f"pdt-{today}.log"

    logger = logging.getLogger("pdt")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        fmt = logging.Formatter(
            "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(fmt)
        logger.addHandler(handler)

    _logger = logger
    return logger


# ── Convenience helpers ───────────────────────────────────────────────────────

def log_cmd(name: str, args: dict) -> None:
    """Log a CLI command invocation with its arguments."""
    filtered = {k: v for k, v in args.items() if v not in (None, False, (), "")}
    arg_str  = "  ".join(f"{k}={v!r}" for k, v in filtered.items()) if filtered else "(no args)"
    get_logger().info(f"CMD  {name}  {arg_str}")


def log_event(msg: str, level: str = "info") -> None:
    """Log a general application event."""
    getattr(get_logger(), level)(msg)


def log_api_req(method: str, url: str, *, body: object = None) -> None:
    """Log an outgoing API request before it is sent."""
    log = get_logger()
    log.info(f"API  → {method.upper()} {url}")
    if body is not None:
        import json
        try:
            body_str = json.dumps(body)
        except Exception:
            body_str = str(body)
        log.debug(f"API  body: {body_str[:400]}")


def log_api_resp(
    method: str,
    url: str,
    status: int,
    nbytes: int,
    elapsed_ms: float,
    *,
    error: str | None = None,
    body_excerpt: str | None = None,
) -> None:
    """Log the result of an API call."""
    log   = get_logger()
    level = "warning" if status >= 400 else "info"
    msg   = f"API  ← {method.upper()} {url}  HTTP {status}  {nbytes}B  {elapsed_ms:.0f}ms"
    if error:
        msg += f"  error={error}"
    getattr(log, level)(msg)
    if body_excerpt:
        log.debug(f"API  body: {body_excerpt[:400]}")


def log_api_error(method: str, url: str, exc: Exception) -> None:
    """Log an exception raised during an API call."""
    get_logger().error(f"API  ✗ {method.upper()} {url}  {type(exc).__name__}: {exc}")
