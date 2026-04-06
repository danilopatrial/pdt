import requests

from .utils import vlog


def rdap_lookup(domain: str) -> tuple:
    """Return (status, registrar) from RDAP. Returns ('available', None) on 404."""
    url = f"https://rdap.org/domain/{domain}"
    vlog(f"GET {url}")
    try:
        r = requests.get(
            url,
            timeout=10,
            allow_redirects=True,
            headers={"Accept": "application/rdap+json, application/json"},
        )
        vlog(f"HTTP {r.status_code} ({len(r.content)} bytes)")
        if r.status_code == 404:
            vlog("404 → available")
            return "available", None
        r.raise_for_status()
        data = r.json()
        statuses = data.get("status", [])
        result = ", ".join(statuses) if statuses else "active"
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
        vlog(f"registrar: {registrar!r}")
        return result, registrar
    except requests.HTTPError as e:
        vlog(f"HTTP error: {e}")
        return f"http-error-{e.response.status_code}", None
    except requests.Timeout:
        vlog("request timed out")
        return "timeout", None
    except Exception as e:
        vlog(f"exception: {e}")
        return "fetch-error", None
