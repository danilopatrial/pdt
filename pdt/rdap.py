import threading
import time
from collections import deque

import requests

from .logger import log_api_error, log_api_req, log_api_resp
from .utils import vlog


class RdapThrottle:
    """Sliding-window rate limiter: at most 3 RDAP calls per 5-second window.

    Only active when more than 3 domains are being watched simultaneously.
    Workers call acquire(stop_event) before every rdap_lookup(); it blocks
    until a slot opens or stop_event is set (returns False when stopped).
    """
    GROUP_SIZE     = 3
    GROUP_INTERVAL = 5.0

    def __init__(self, total_domains: int):
        self._enabled  = total_domains > self.GROUP_SIZE
        self._lock     = threading.Lock()
        self._times: deque = deque()

    def acquire(self, stop_event: threading.Event) -> bool:
        if not self._enabled:
            return True
        while not stop_event.is_set():
            with self._lock:
                now = time.monotonic()
                while self._times and now - self._times[0] >= self.GROUP_INTERVAL:
                    self._times.popleft()
                if len(self._times) < self.GROUP_SIZE:
                    self._times.append(now)
                    return True
            time.sleep(0.05)
        return False


def _distill_status(statuses: list[str]) -> str:
    """Keep only lifecycle-relevant RDAP statuses; discard registrar policy noise."""
    kept = [
        s for s in statuses
        if any(kw in s.lower() for kw in ("redemption", "pending", "active", "available"))
    ]
    return ", ".join(kept) if kept else "active"


def rdap_lookup(domain: str) -> tuple:
    """Return (status, registrar) from RDAP. Returns ('available', None) on 404."""
    url = f"https://rdap.org/domain/{domain}"
    vlog(f"GET {url}", domain=domain)
    log_api_req("GET", url)
    t0 = time.monotonic()
    try:
        r = requests.get(
            url,
            timeout=10,
            allow_redirects=True,
            headers={"Accept": "application/rdap+json, application/json"},
        )
        elapsed = (time.monotonic() - t0) * 1000
        vlog(f"HTTP {r.status_code} ({len(r.content)} bytes)")
        if r.status_code == 404:
            vlog("404 → available")
            log_api_resp("GET", url, r.status_code, len(r.content), elapsed)
            return "available", None
        log_api_resp("GET", url, r.status_code, len(r.content), elapsed)
        r.raise_for_status()
        data = r.json()
        statuses = data.get("status", [])
        result = _distill_status(statuses)
        vlog(f"statuses: {statuses!r} → {result!r}")

        # Extract registrar from entities
        registrar = None
        for entity in data.get("entities", []):
            if "registrar" in entity.get("roles", []):
                vcard = entity.get("vcardArray", [])
                if len(vcard) > 1 and isinstance(vcard[1], list):
                    for prop in vcard[1]:
                        if isinstance(prop, list) and len(prop) >= 4 and prop[0] == "fn":
                            registrar = prop[3]
                            break
                if not registrar:
                    registrar = entity.get("handle") or entity.get("ldhName")
                if registrar:
                    break
        if registrar:
            registrar = " ".join(registrar.split()[:2])
        vlog(f"registrar: {registrar!r}")
        return result, registrar
    except requests.HTTPError as e:
        elapsed = (time.monotonic() - t0) * 1000
        vlog(f"HTTP error: {e}")
        log_api_resp("GET", url, e.response.status_code, len(e.response.content), elapsed,
                     error=str(e))
        return f"http-error-{e.response.status_code}", None
    except requests.Timeout:
        elapsed = (time.monotonic() - t0) * 1000
        vlog("request timed out")
        log_api_resp("GET", url, 0, 0, elapsed, error="timeout")
        return "timeout", None
    except Exception as e:
        vlog(f"exception: {e}")
        log_api_error("GET", url, e)
        return "fetch-error", None
