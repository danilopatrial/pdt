import re
import sys
import threading
import time

import requests

from .config import save_config
from .utils import console, vlog


# ── Rate limiter ──────────────────────────────────────────────────────────────

class RateLimiter:
    """Sliding-window rate limiter, safe for use across multiple threads."""

    def __init__(self, max_calls: int, window: float):
        self._max  = max_calls
        self._win  = window
        self._hits: list = []
        self._lock = threading.Lock()

    def acquire(self, abort_event=None) -> bool:
        """Block until a call slot is available.

        Returns True when the slot is granted, False if *abort_event* is set
        before the slot becomes available.
        """
        while True:
            if abort_event and abort_event.is_set():
                return False
            now = time.time()
            with self._lock:
                cutoff = now - self._win
                self._hits = [t for t in self._hits if t > cutoff]
                if len(self._hits) < self._max:
                    self._hits.append(now)
                    return True
            time.sleep(0.1)


# ── Atom appraisal ────────────────────────────────────────────────────────────

def atom_appraise(domain: str, api_token: str, user_id: int) -> float | None:
    """Fetch estimated market value from Atom's appraisal API. Returns None on failure."""
    url = "https://www.atom.com/api/marketplace/domain-appraisal"
    params = {"api_token": api_token, "user_id": user_id, "domain_name": domain}
    vlog(f"GET {url}")
    vlog(f"params: user_id={user_id}, domain_name={domain}, api_token={api_token[:4]}****")
    try:
        r = requests.get(url, params=params, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; pdt/1.0)"})
        vlog(f"HTTP {r.status_code} ({len(r.content)} bytes)")
        r.raise_for_status()
        data = r.json()
        vlog(f"response: {r.text[:200]}")
        value = data.get("atom_appraisal")
        if value is not None:
            vlog(f"atom_appraisal={value!r}")
            return float(value)
        vlog("atom_appraisal field missing from response")
        return None
    except requests.HTTPError as e:
        vlog(f"HTTP error: {e} — body: {e.response.text[:200]}")
        return None
    except Exception as e:
        vlog(f"exception: {e}")
        return None


# ── Spaceship contact ─────────────────────────────────────────────────────────

def spaceship_ensure_contact(cfg: dict, api_key: str, api_secret: str) -> str:
    """Return a Spaceship contact ID, creating one via the API if not yet stored."""
    contact_id = cfg.get("spaceship_contact_id")
    if contact_id:
        return contact_id

    contact_field_map = [
        ("spaceship_first",   "firstName"),
        ("spaceship_last",    "lastName"),
        ("spaceship_email",   "email"),
        ("spaceship_phone",   "phone"),
        ("spaceship_address", "address1"),
        ("spaceship_city",    "city"),
        ("spaceship_state",   "stateProvince"),
        ("spaceship_zip",     "postalCode"),
        ("spaceship_country", "country"),
    ]
    for cfg_key, _ in contact_field_map:
        if not cfg.get(cfg_key):
            flag = "--" + cfg_key.replace("_", "-")
            console.print(
                f"[red]Missing {cfg_key}.[/red] "
                f"Run: [bold]pdt config {flag} VALUE[/bold]"
            )
            sys.exit(1)

    # Validate and normalise country code (must be ^[A-Z]{2}$)
    country_raw = cfg["spaceship_country"]
    country_upper = country_raw.upper()
    if not re.match(r"^[A-Z]{2}$", country_upper):
        console.print(
            f"[red]Invalid country code '{country_raw}'.[/red] "
            "Required format: 2 uppercase ASCII letters, e.g. [bold]US[/bold] or [bold]BR[/bold]"
        )
        sys.exit(1)
    cfg["spaceship_country"] = country_upper

    # Normalise phone to +{cc}.{subscriber}  (^\+\d{1,3}\.\d{4,}$)
    raw_phone = cfg["spaceship_phone"]
    _digits = re.sub(r"[\s\-\(\)\.]", "", raw_phone).lstrip("+")
    _ONE_CC  = {"US", "CA", "RU", "KZ"}
    _TWO_CC  = {
        "BR", "AR", "CO", "CL", "VE", "PE", "EC", "BO", "PY", "UY",
        "GY", "SR", "FR", "DE", "GB", "IT", "ES", "PT", "NL", "BE",
        "CH", "AT", "SE", "NO", "DK", "FI", "PL", "CZ", "HU", "RO",
        "CN", "JP", "KR", "IN", "SG", "AU", "NZ", "ZA", "MX", "IL",
        "NG", "EG",
    }
    if country_upper in _ONE_CC:
        _cc_len = 1
    elif country_upper in _TWO_CC:
        _cc_len = 2
    else:
        _cc_len = next((l for l in (1, 2, 3) if len(_digits) - l >= 4), None)
        if _cc_len is None:
            console.print(
                f"[red]Phone number '{raw_phone}' is too short to parse.[/red] "
                "Required format: [bold]+CC.SUBSCRIBER[/bold] e.g. [bold]+55.11999999999[/bold]"
            )
            sys.exit(1)
    phone_normalized = f"+{_digits[:_cc_len]}.{_digits[_cc_len:]}"
    if not re.match(r"^\+\d{1,3}\.\d{4,}$", phone_normalized):
        console.print(
            f"[red]Phone number '{raw_phone}' could not be normalised.[/red] "
            "Required format: [bold]+CC.SUBSCRIBER[/bold] e.g. [bold]+55.11999999999[/bold] "
            "(calling code 1–3 digits, subscriber ≥ 4 digits)"
        )
        sys.exit(1)
    cfg["spaceship_phone"] = phone_normalized

    body = {api_field: cfg[cfg_key] for cfg_key, api_field in contact_field_map}
    url = "https://spaceship.dev/api/v1/contacts"
    headers = {
        "X-API-Key": api_key,
        "X-API-Secret": api_secret,
        "Content-Type": "application/json",
    }
    vlog(f"PUT {url}")
    r = requests.put(url, headers=headers, json=body, timeout=30)
    vlog(f"HTTP {r.status_code} ({len(r.content)} bytes)")
    if not r.ok:
        console.print(f"[red]Contact creation failed ({r.status_code}):[/red] {r.text}")
        sys.exit(1)
    data = r.json()
    contact_id = data.get("contactId") or data.get("id")
    if not contact_id:
        console.print(f"[red]Contact created but no contactId in response:[/red] {r.text}")
        sys.exit(1)
    cfg["spaceship_contact_id"] = contact_id
    save_config(cfg)
    console.print(f"[green]✓ Spaceship contact created[/green] (ID: {contact_id})")
    return contact_id


# ── Spaceship registration ────────────────────────────────────────────────────

def spaceship_register(domain: str, api_key: str, api_secret: str, contact_id: str):
    """POST a domain registration request.

    Returns (operation_id, None, False) on HTTP 202,
    or (None, error_str, fatal) otherwise where fatal=True means retrying won't help.
    """
    url = f"https://spaceship.dev/api/v1/domains/{domain}"
    headers = {
        "X-API-Key": api_key,
        "X-API-Secret": api_secret,
        "Content-Type": "application/json",
    }
    body = {
        "autoRenew": False,
        "years": 1,
        "privacyProtection": {"level": "high", "userConsent": True},
        "contacts": {
            "registrant": contact_id,
            "admin": contact_id,
            "tech": contact_id,
            "billing": contact_id,
        },
    }
    vlog(f"POST {url}")
    try:
        r = requests.post(url, headers=headers, json=body, timeout=30)
        vlog(f"HTTP {r.status_code} ({len(r.content)} bytes)")
        if r.status_code == 202:
            op_id = r.headers.get("spaceship-async-operationid")
            return op_id, None, False
        if r.status_code == 422:
            try:
                detail = r.json().get("detail", "")
                fatal = "not available for registration" not in detail.lower()
            except Exception:
                fatal = True
        else:
            # 404 can be a propagation lag — retryable
            fatal = False
        return None, f"HTTP {r.status_code}: {r.text[:200]}", fatal
    except Exception as e:
        vlog(f"register exception: {e}")
        return None, str(e), False


def spaceship_poll_async(operation_id: str, api_key: str, api_secret: str):
    """GET async-operations/{id}. Returns the status string or None on error."""
    url = f"https://spaceship.dev/api/v1/async-operations/{operation_id}"
    headers = {"X-API-Key": api_key, "X-API-Secret": api_secret}
    vlog(f"GET {url}")
    try:
        r = requests.get(url, headers=headers, timeout=15)
        vlog(f"HTTP {r.status_code} ({len(r.content)} bytes)")
        r.raise_for_status()
        return r.json().get("status")
    except Exception as e:
        vlog(f"async poll exception: {e}")
        return None


def spaceship_check_available(domain: str, api_key: str, api_secret: str) -> tuple:
    """Calls GET /v1/domains/{domain}/available.

    Returns (is_available, result_string, is_premium).
    - is_available is True only when result == "available".
    - is_premium is True when the response includes premiumPricing.
    - On non-2xx: returns (False, f"http-{status_code}", False).
    - On exception: returns (False, "check-error", False).
    """
    url = f"https://spaceship.dev/api/v1/domains/{domain}/available"
    headers = {
        "X-API-Key": api_key,
        "X-API-Secret": api_secret,
    }
    vlog(f"GET {url}")
    try:
        r = requests.get(url, headers=headers, timeout=15)
        vlog(f"HTTP {r.status_code} ({len(r.content)} bytes)")
        if not r.ok:
            return False, f"http-{r.status_code}", False
        data       = r.json()
        result     = data.get("result", "unknown")
        is_premium = bool(data.get("premiumPricing"))
        if result == "available":
            return True, "available", is_premium
        return False, result, is_premium
    except Exception as e:
        vlog(f"availability check exception: {e}")
        return False, "check-error", False
