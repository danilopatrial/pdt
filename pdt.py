#!/usr/bin/env python3
"""PDT — Pending Delete Domain Tracker"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
import requests
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

DATA_DIR        = Path.home() / ".pdt"
DATA_FILE       = DATA_DIR / "domains.json"
CONFIG_FILE     = DATA_DIR / "config.json"
PID_FILE        = DATA_DIR / "daemon.pid"
LOG_FILE        = DATA_DIR / "daemon.log"
BACKORDERS_FILE = DATA_DIR / "backorders.json"

NOTIFY_WINDOW = 300        # 5 minutes
ARCHIVE_AFTER = 24 * 3600  # 24 hours past drop time


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg: dict):
    DATA_DIR.mkdir(exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ── Storage ───────────────────────────────────────────────────────────────────

def archive_expired():
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


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def find_domain(domains: list, name: str):
    """Return the first entry matching name, or None."""
    for d in domains:
        if d["domain"] == name:
            return d
    return None


def to_local(dt: datetime) -> datetime:
    """Convert a naive UTC datetime to the local timezone."""
    return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def remaining(entry: dict) -> float:
    drop = datetime.fromisoformat(entry["drop_time"])
    return (drop - datetime.utcnow()).total_seconds()


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


# ── Spaceship API ─────────────────────────────────────────────────────────────

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


def spaceship_register(domain: str, api_key: str, api_secret: str, contact_id: str):
    """POST a domain registration request.

    Returns (operation_id, None) on HTTP 202, or (None, error_str) otherwise.
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
            return op_id, None
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        vlog(f"register exception: {e}")
        return None, str(e)


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


def send_notification(title: str, msg: str):
    """Desktop notification — tries plyer, then notify-send, then osascript."""
    try:
        from plyer import notification  # type: ignore
        notification.notify(title=title, message=msg, app_name="PDT", timeout=30)
        return
    except Exception:
        pass
    try:
        subprocess.run(["notify-send", "-t", "30000", title, msg], check=False)
        return
    except FileNotFoundError:
        pass
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
            check=False,
        )
    except FileNotFoundError:
        pass


# ── CLI ───────────────────────────────────────────────────────────────────────

VERBOSE    = False
_vlog_sink = None   # set to a deque when inside a Live context


def vlog(msg: str):
    """Log a verbose message. Buffered when inside a Live display, printed directly otherwise."""
    if not VERBOSE:
        return
    if _vlog_sink is not None:
        _vlog_sink.append(msg)
    else:
        console.print(f"[dim blue]  [v] {msg}[/dim blue]")


@click.group()
@click.version_option("1.0.0", prog_name="pdt")
@click.option("-v", "--verbose", is_flag=True, default=False,
              help="Show detailed output including API requests and responses.")
def cli(verbose):
    """PDT — Pending Delete Domain Tracker

    Track domains in pending-delete and get desktop notifications
    5 minutes before they become available.
    """
    global VERBOSE
    VERBOSE = verbose


# ── add ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("domain")
@click.option("-t", "--time", "duration", required=True, metavar="DURATION",
              help="Time until drop, e.g. 1d3h57m")
@click.option("-a", "--appraisal", type=float, default=None, metavar="USD",
              help="Estimated value in USD")
@click.option("-n", "--note", default="", help="Freeform note")
@click.option("-s", "--status", default=None,
              help="RDAP status (auto-fetched if omitted)")
def add(domain, duration, appraisal, note, status):
    """Add a domain to track."""
    domain = domain.lower().strip()
    domains = load()

    if any(d["domain"] == domain for d in domains):
        console.print(f"[red]Already tracking [bold]{domain}[/bold][/red]")
        sys.exit(1)

    try:
        secs = parse_duration(duration)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    drop_time = (datetime.utcnow() + timedelta(seconds=secs)).isoformat()

    registrar = None
    if status is None:
        with console.status(f"[dim]Fetching RDAP for {domain}…[/dim]"):
            status, registrar = rdap_lookup(domain)

    domains.append({
        "domain": domain,
        "drop_time": drop_time,
        "appraisal": appraisal,
        "note": note,
        "status": status,
        "registrar": registrar,
        "added_at": datetime.utcnow().isoformat(),
        "notified": False,
    })
    save(domains)

    st = status_style(status)
    console.print(
        f"[green]✓ Added[/green] [bold cyan]{domain}[/bold cyan] "
        f"— drops in [white]{fmt_duration(secs)}[/white] "
        f"— status: [{st}]{status}[/{st}]"
    )


# ── remove ────────────────────────────────────────────────────────────────────

@cli.command("rm")
@click.argument("domain")
def remove(domain):
    """Remove a domain from tracking."""
    domain = domain.lower().strip()
    domains = load()
    new = [d for d in domains if d["domain"] != domain]
    if len(new) == len(domains):
        console.print(f"[red]Not found: [bold]{domain}[/bold][/red]")
        sys.exit(1)
    save(new)
    console.print(f"[green]✓ Removed[/green] [bold]{domain}[/bold]")


# ── flag ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("domains", nargs=-1, required=True)
def flag(domains):
    """Toggle the flag on one or more domains (highlighted in list)."""
    tracked = load()
    changed = False
    for name in domains:
        name = name.lower().strip()
        entry = find_domain(tracked, name)
        if entry is None:
            console.print(f"[red]Not found: [bold]{name}[/bold][/red]")
            continue
        entry["flagged"] = not entry.get("flagged", False)
        state = "[yellow]⚑ flagged[/yellow]" if entry["flagged"] else "[dim]unflagged[/dim]"
        console.print(f"[bold cyan]{name}[/bold cyan] → {state}")
        changed = True
    if changed:
        save(tracked)


# ── update ────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("domain")
@click.option("-t", "--time", "duration", default=None, metavar="DURATION",
              help="New drop time, e.g. 45m")
@click.option("-a", "--appraisal", type=float, default=None)
@click.option("-n", "--note", default=None)
@click.option("-s", "--status", default=None)
def update(domain, duration, appraisal, note, status):
    """Update fields for a tracked domain."""
    domain = domain.lower().strip()
    domains = load()
    entry = find_domain(domains, domain)
    if entry is None:
        console.print(f"[red]Not found: [bold]{domain}[/bold][/red]")
        sys.exit(1)

    changes = []
    if duration is not None:
        try:
            secs = parse_duration(duration)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(1)
        entry["drop_time"] = (datetime.utcnow() + timedelta(seconds=secs)).isoformat()
        entry["notified"] = False  # reset notification flag on time update
        changes.append(f"drop time → [white]{fmt_duration(secs)}[/white]")
    if appraisal is not None:
        entry["appraisal"] = appraisal
        changes.append(f"appraisal → [green]${appraisal:,.0f}[/green]")
    if note is not None:
        entry["note"] = note
        changes.append(f"note → [dim]{note}[/dim]")
    if status is not None:
        entry["status"] = status
        changes.append(f"status → [{status_style(status)}]{status}[/{status_style(status)}]")

    if not changes:
        console.print("[yellow]Nothing to update. Pass at least one option.[/yellow]")
        return

    save(domains)
    console.print(f"[green]✓ Updated[/green] [bold cyan]{domain}[/bold cyan] — " + ", ".join(changes))


# ── list ──────────────────────────────────────────────────────────────────────

@cli.command("list")
@click.option("-m", "--machine", is_flag=True, help="CSV output for scripting")
@click.option("--sort", default="time",
              type=click.Choice(["time", "appraisal", "domain", "status"]),
              show_default=True, help="Sort order")
@click.option("-A", "--archived", "show_archived", is_flag=True,
              help="Show archived domains (dropped >24h ago)")
def list_domains(machine, sort, show_archived):
    """List all tracked domains."""
    archive_expired()
    all_domains = load()

    domains = [d for d in all_domains if bool(d.get("archived")) == show_archived]

    if not domains:
        if show_archived:
            console.print("[dim]No archived domains.[/dim]")
        else:
            console.print("[dim]No domains tracked. Use [bold]pdt add[/bold] to get started.[/dim]")
        return

    if show_archived:
        key_fns = {
            "time":      lambda d: remaining(d),
            "appraisal": lambda d: -(d.get("appraisal") or 0),
            "domain":    lambda d: d["domain"],
            "status":    lambda d: d.get("status", ""),
        }
    else:
        key_fns = {
            "time":      lambda d: remaining(d),
            "appraisal": lambda d: -(d.get("appraisal") or 0),
            "domain":    lambda d: d["domain"],
            "status":    lambda d: d.get("status", ""),
        }
    domains = sorted(domains, key=key_fns[sort])

    if machine:
        if show_archived:
            click.echo("domain,dropped_seconds_ago,drop_time_utc,appraisal,status,registrar,note")
            for d in domains:
                ago = int(-remaining(d))
                appr = d.get("appraisal") or ""
                note = (d.get("note") or "").replace(",", ";")
                reg = (d.get("registrar") or "").replace(",", ";")
                click.echo(f"{d['domain']},{ago},{d['drop_time']},{appr},{d['status']},{reg},{note}")
        else:
            click.echo("domain,remaining_seconds,drop_time_utc,appraisal,status,registrar,note")
            for d in domains:
                r = int(remaining(d))
                appr = d.get("appraisal") or ""
                note = (d.get("note") or "").replace(",", ";")
                reg = (d.get("registrar") or "").replace(",", ";")
                click.echo(f"{d['domain']},{r},{d['drop_time']},{appr},{d['status']},{reg},{note}")
        return

    w = console.width

    # Responsive breakpoints
    tiny  = w < 60
    small = w < 85
    mid   = w < 105

    show_index     = not tiny
    show_drop_time = not small
    show_appraisal = w >= 68
    show_registrar = w >= 110
    show_note      = w >= 130

    table_box   = box.SIMPLE_HEAD if tiny else box.ROUNDED
    table_title = (
        None if tiny else
        ("[bold white]Archived Domains[/bold white]" if show_archived else "[bold white]Tracked Domains[/bold white]")
    )

    table = Table(
        box=table_box,
        show_header=True,
        header_style="bold magenta",
        border_style="bright_black",
        title=table_title,
        title_style="bold",
        expand=True,
        pad_edge=not tiny,
    )

    if show_index:
        table.add_column("#", style="dim", width=3, justify="right", no_wrap=True)

    domain_max = max(16, w // 3)
    table.add_column("Domain", style="bold cyan", no_wrap=True,
                     overflow="ellipsis", min_width=12, max_width=domain_max)

    if show_archived:
        table.add_column("Dropped", justify="right", no_wrap=True, min_width=8)
        if show_drop_time:
            table.add_column("Dropped At", justify="center", no_wrap=True, min_width=12)
    else:
        table.add_column("Drops In", justify="right", no_wrap=True, min_width=6)
        if show_drop_time:
            table.add_column("Drop Time", justify="center", no_wrap=True, min_width=12)

    if show_appraisal:
        table.add_column("Appraisal", justify="right", no_wrap=True, min_width=8)

    table.add_column("Status", no_wrap=True, overflow="ellipsis", min_width=8)

    if show_registrar:
        table.add_column("Registrar", style="dim", no_wrap=True, overflow="ellipsis", min_width=10)

    if show_note:
        table.add_column("Note", style="dim", ratio=1, overflow="ellipsis", no_wrap=True)

    for i, d in enumerate(domains, 1):
        rem = remaining(d)
        drop_dt = datetime.fromisoformat(d["drop_time"])
        appraisal = f"${d['appraisal']:,.0f}" if d.get("appraisal") else "—"
        st = d.get("status", "unknown")

        local_dt = to_local(drop_dt)
        if show_archived:
            ago = -rem  # positive = seconds since drop
            time_cell = Text(fmt_duration(ago) + " ago", style="dim")
            date_str  = local_dt.strftime("%b %d  %H:%M")
        else:
            rem_str = fmt_duration(rem)
            if rem <= 0:
                time_cell = Text("AVAILABLE", style="bold green blink")
            elif rem < NOTIFY_WINDOW:
                time_cell = Text(rem_str, style="bold red")
            elif rem < 3600:
                time_cell = Text(rem_str, style="yellow")
            else:
                time_cell = Text(rem_str, style="white")
            date_str = local_dt.strftime("%b %d  %H:%M")

        flagged = d.get("flagged", False)
        domain_cell = Text(("⚑ " if flagged else "") + d["domain"],
                           style="bold yellow" if flagged else "bold cyan")

        row = []
        if show_index:
            row.append(str(i))
        row.append(domain_cell)
        row.append(time_cell)
        if show_drop_time:
            row.append(date_str)
        if show_appraisal:
            row.append(appraisal)
        row.append(Text(st, style=status_style(st)))
        if show_registrar:
            row.append(d.get("registrar") or "—")
        if show_note:
            row.append(d.get("note", ""))

        table.add_row(*row)

    console.print(table)
    total = len(domains)
    label = "archived domain" if show_archived else "domain"
    console.print(f"[dim]  {total} {label}{'s' if total != 1 else ''} · times in local timezone[/dim]")


# ── next ──────────────────────────────────────────────────────────────────────

@cli.command("next")
@click.option("-n", "--count", default=5, show_default=True, metavar="N")
@click.option("-m", "--machine", is_flag=True, help="CSV output")
def next_cmd(count, machine):
    """Show the N domains dropping soonest."""
    archive_expired()
    domains = [d for d in load() if not d.get("archived")]
    if not domains:
        console.print("[dim]No domains tracked.[/dim]")
        return

    domains = sorted(domains, key=lambda d: remaining(d))[:count]

    if machine:
        click.echo("domain,remaining_seconds,drop_time_utc,appraisal,status")
        for d in domains:
            click.echo(
                f"{d['domain']},{int(remaining(d))},{d['drop_time']},"
                f"{d.get('appraisal','')},{d['status']}"
            )
        return

    w = console.width
    show_appraisal = w >= 65
    show_status    = w >= 55
    show_note      = w >= 90

    console.print(
        Panel(
            f"[bold white]Next {count} Dropping[/bold white]",
            style="cyan",
            expand=True,
        )
    )
    for i, d in enumerate(domains, 1):
        rem = remaining(d)
        rem_str = fmt_duration(rem)
        st = d.get("status", "unknown")

        parts = [
            f"  [dim]{i}.[/dim] [bold cyan]{d['domain']}[/bold cyan]",
            f"[white]{rem_str}[/white]",
        ]
        if show_appraisal:
            appraisal = f"[green]${d['appraisal']:,.0f}[/green]" if d.get("appraisal") else "[dim]—[/dim]"
            parts.append(appraisal)
        if show_status:
            parts.append(f"[{status_style(st)}]{st}[/{status_style(st)}]")
        if show_note and d.get("note"):
            parts.append(f"[dim]{d['note']}[/dim]")

        console.print("  ".join(parts))


# ── appraise ──────────────────────────────────────────────────────────────────

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
        vlog(f"response: {json.dumps(data)}")
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


@cli.command()
@click.option("--atom-token", default=None, metavar="TOKEN", help="Atom API token")
@click.option("--atom-user-id", default=None, type=int, metavar="ID", help="Atom user ID")
@click.option("--spaceship-api-key", default=None, metavar="VALUE", help="Spaceship API key")
@click.option("--spaceship-api-secret", default=None, metavar="VALUE", help="Spaceship API secret")
@click.option("--spaceship-first", default=None, metavar="VALUE", help="Contact first name")
@click.option("--spaceship-last", default=None, metavar="VALUE", help="Contact last name")
@click.option("--spaceship-email", default=None, metavar="VALUE", help="Contact email")
@click.option("--spaceship-phone", default=None, metavar="VALUE",
              help="Contact phone (+countrycode.number, e.g. +55.11999999999)")
@click.option("--spaceship-address", default=None, metavar="VALUE", help="Contact street address")
@click.option("--spaceship-city", default=None, metavar="VALUE", help="Contact city")
@click.option("--spaceship-state", default=None, metavar="VALUE", help="Contact state/province")
@click.option("--spaceship-zip", default=None, metavar="VALUE", help="Contact ZIP/postal code")
@click.option("--spaceship-country", default=None, metavar="VALUE", help="Contact country code (e.g. US)")
@click.option("--show", is_flag=True, help="Print current config")
def config(atom_token, atom_user_id, spaceship_api_key, spaceship_api_secret,
           spaceship_first, spaceship_last, spaceship_email, spaceship_phone,
           spaceship_address, spaceship_city, spaceship_state, spaceship_zip,
           spaceship_country, show):
    """Get or set PDT configuration.

    \b
    Examples:
      pdt config --atom-token abc123 --atom-user-id 456
      pdt config --spaceship-api-key KEY --spaceship-api-secret SECRET
      pdt config --show
    """
    cfg = load_config()

    if show:
        if not cfg:
            console.print("[dim]No config set. Use pdt config --atom-token TOKEN --atom-user-id ID[/dim]")
            return

        def _mask(val):
            if val and val != "—":
                return val[:4] + "*" * (len(val) - 4)
            return val

        token_display  = _mask(cfg.get("atom_token", "—"))
        key_display    = _mask(cfg.get("spaceship_api_key", "—"))
        secret_display = _mask(cfg.get("spaceship_api_secret", "—"))

        console.print(f"  [dim]atom_token[/dim]           : [cyan]{token_display}[/cyan]")
        console.print(f"  [dim]atom_user_id[/dim]         : [cyan]{cfg.get('atom_user_id', '—')}[/cyan]")
        console.print(f"  [dim]spaceship_api_key[/dim]    : [cyan]{key_display}[/cyan]")
        console.print(f"  [dim]spaceship_api_secret[/dim] : [cyan]{secret_display}[/cyan]")
        console.print(f"  [dim]spaceship_contact_id[/dim] : [cyan]{cfg.get('spaceship_contact_id', '—')}[/cyan]")
        console.print(f"  [dim]spaceship_first[/dim]      : [cyan]{cfg.get('spaceship_first', '—')}[/cyan]")
        console.print(f"  [dim]spaceship_last[/dim]       : [cyan]{cfg.get('spaceship_last', '—')}[/cyan]")
        console.print(f"  [dim]spaceship_email[/dim]      : [cyan]{cfg.get('spaceship_email', '—')}[/cyan]")
        console.print(f"  [dim]spaceship_phone[/dim]      : [cyan]{cfg.get('spaceship_phone', '—')}[/cyan]")
        console.print(f"  [dim]spaceship_address[/dim]    : [cyan]{cfg.get('spaceship_address', '—')}[/cyan]")
        console.print(f"  [dim]spaceship_city[/dim]       : [cyan]{cfg.get('spaceship_city', '—')}[/cyan]")
        console.print(f"  [dim]spaceship_state[/dim]      : [cyan]{cfg.get('spaceship_state', '—')}[/cyan]")
        console.print(f"  [dim]spaceship_zip[/dim]        : [cyan]{cfg.get('spaceship_zip', '—')}[/cyan]")
        console.print(f"  [dim]spaceship_country[/dim]    : [cyan]{cfg.get('spaceship_country', '—')}[/cyan]")
        return

    new_opts = {
        "atom_token":          atom_token,
        "atom_user_id":        atom_user_id,
        "spaceship_api_key":   spaceship_api_key,
        "spaceship_api_secret": spaceship_api_secret,
        "spaceship_first":     spaceship_first,
        "spaceship_last":      spaceship_last,
        "spaceship_email":     spaceship_email,
        "spaceship_phone":     spaceship_phone,
        "spaceship_address":   spaceship_address,
        "spaceship_city":      spaceship_city,
        "spaceship_state":     spaceship_state,
        "spaceship_zip":       spaceship_zip,
        "spaceship_country":   spaceship_country,
    }
    if not any(v is not None for v in new_opts.values()):
        console.print("[yellow]Nothing to set. Pass at least one option.[/yellow]")
        return

    for key, val in new_opts.items():
        if val is not None:
            cfg[key] = val

    save_config(cfg)
    console.print(f"[green]✓ Config saved[/green] → {CONFIG_FILE}")


@cli.command()
@click.argument("domains", nargs=-1, metavar="[DOMAIN]...")
@click.option("-a", "--all", "all_tracked", is_flag=True,
              help="Appraise all tracked domains missing a value")
@click.option("--token", envvar="ATOM_API_TOKEN", default=None, metavar="TOKEN",
              help="Atom API token (overrides config; or set ATOM_API_TOKEN env var)")
@click.option("--user-id", envvar="ATOM_USER_ID", default=None, type=int, metavar="ID",
              help="Atom user ID (overrides config; or set ATOM_USER_ID env var)")
def appraise(domains, all_tracked, token, user_id):
    """Fetch Atom appraisals for domains missing a value.

    \b
    Skips domains that already have an appraisal. On API failure the value
    stays as None.

    \b
    Examples:
      pdt appraise example.com other.net --token abc --user-id 123
      pdt appraise --all --token abc --user-id 123
      ATOM_API_TOKEN=abc ATOM_USER_ID=123 pdt appraise --all
    """
    cfg = load_config()
    token   = token   or cfg.get("atom_token")
    user_id = user_id or cfg.get("atom_user_id")
    vlog(f"credentials source: token={'flag/env' if token else 'missing'}, user_id={'flag/env/config' if user_id else 'missing'}")

    if not token or not user_id:
        console.print(
            "[red]Atom credentials required.[/red] "
            "Run [bold]pdt config --atom-token TOKEN --atom-user-id ID[/bold] "
            "or pass --token / --user-id flags."
        )
        sys.exit(1)

    tracked = load()

    if all_tracked:
        targets = [d["domain"] for d in tracked if not d.get("archived") and not d.get("appraisal")]
        if not targets:
            console.print("[dim]All tracked domains already have appraisals.[/dim]")
            return
    elif domains:
        targets = [d.lower().strip() for d in domains]
    else:
        console.print("[red]Provide at least one domain, or use --all[/red]")
        sys.exit(1)

    changed = False
    for name in targets:
        entry = find_domain(tracked, name)
        if entry and entry.get("appraisal"):
            console.print(
                f"  [bold cyan]{name}[/bold cyan] — already appraised at "
                f"[green]${entry['appraisal']:,.0f}[/green], skipping"
            )
            continue

        with console.status(f"[dim]  Appraising {name}…[/dim]"):
            value = atom_appraise(name, token, user_id)

        if value is not None:
            result_str = f"[green]${value:,.0f}[/green]"
        else:
            result_str = "[dim red]no value returned[/dim red]"

        console.print(f"  [bold cyan]{name}[/bold cyan] → {result_str}")

        if entry and value is not None:
            entry["appraisal"] = value
            changed = True

    if changed:
        save(tracked)
        console.print(f"\n[green]✓ Saved appraisals[/green]")


# ── rdap ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("domains", nargs=-1, metavar="[DOMAIN]...")
@click.option("-a", "--all", "all_tracked", is_flag=True,
              help="Fetch status for all tracked domains")
def rdap(domains, all_tracked):
    """Fetch and update RDAP status.

    \b
    Examples:
      pdt rdap example.com
      pdt rdap example.com other.net
      pdt rdap --all
    """
    tracked = load()

    if all_tracked:
        active = [d for d in tracked if not d.get("archived")]
        if not active:
            console.print("[dim]No domains tracked.[/dim]")
            return
        targets = [d["domain"] for d in active]
    elif domains:
        targets = [d.lower().strip() for d in domains]
    else:
        console.print("[red]Provide at least one domain, or use --all[/red]")
        sys.exit(1)

    changed = False
    for name in targets:
        with console.status(f"[dim]  {name}…[/dim]"):
            status, registrar = rdap_lookup(name)
        entry = find_domain(tracked, name)
        untracked = "" if entry else " [dim](not tracked)[/dim]"
        st = status_style(status)
        reg_str = f"  [dim]({registrar})[/dim]" if registrar else ""
        console.print(f"  [bold cyan]{name}[/bold cyan]{untracked} → [{st}]{status}[/{st}]{reg_str}")
        if entry:
            entry["status"] = status
            entry["registrar"] = registrar
            changed = True

    if changed:
        save(tracked)
        if len(targets) > 1:
            console.print(f"\n[green]✓ Updated {len(targets)} domains[/green]")


# ── poll ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("domains", nargs=-1, metavar="[DOMAIN]...")
@click.option("-n", "--next", "use_next", type=int, default=None, metavar="N",
              help="Poll the next N tracked domains instead of specifying names")
@click.option("-i", "--interval", default=10, show_default=True, metavar="SECS",
              help="Seconds between RDAP polls")
def poll(domains, use_next, interval):
    """Live-poll RDAP status, refreshing every SECS seconds.

    \b
    Examples:
      pdt poll example.com other.net
      pdt poll --next 5
      pdt poll example.com -i 30
    """
    if not domains and use_next is None:
        console.print("[red]Provide at least one domain, or use --next N[/red]")
        sys.exit(1)

    tracked_all = load()
    tracked_map = {d["domain"]: d for d in tracked_all}

    if use_next is not None:
        pool = sorted(
            (d for d in tracked_all if not d.get("archived")),
            key=lambda d: remaining(d),
        )[:use_next]
        if not pool:
            console.print("[red]No tracked domains.[/red]")
            sys.exit(1)
        domain_list = [d["domain"] for d in pool]
    else:
        domain_list = [d.lower().strip() for d in domains]

    # per-domain state
    states = {
        d: {"status": "fetching…", "prev": None, "checked": None, "changed": False, "notified": False}
        for d in domain_list
    }

    def build_table(time_left: float) -> Table:
        now = datetime.utcnow()
        w = console.width
        has_drops = any(d in tracked_map for d in domain_list)
        has_prev  = any(states[d]["prev"] is not None for d in domain_list)

        secs = int(time_left)
        bar_width = max(4, min(12, w // 10))
        filled = round(bar_width * (1 - time_left / interval)) if interval > 0 else bar_width
        bar = "[green]" + "█" * filled + "[/green]" + "[bright_black]" + "░" * (bar_width - filled) + "[/bright_black]"
        title = f"[bold white]RDAP Poll[/bold white]  [bright_black]·[/bright_black]  every {interval}s  [bright_black]·[/bright_black]  {bar} [dim]{secs}s[/dim]"

        tbl = Table(
            box=box.ROUNDED,
            expand=True,
            header_style="bold magenta",
            border_style="bright_black",
            title=title,
            title_style="",
        )
        tbl.add_column("Domain", style="bold cyan", no_wrap=True, overflow="ellipsis", min_width=12)
        tbl.add_column("Status", no_wrap=True, overflow="ellipsis", min_width=8)
        if has_prev:
            tbl.add_column("Was", style="dim", no_wrap=True, overflow="ellipsis", min_width=6)
        tbl.add_column("Checked", justify="right", no_wrap=True, min_width=8, style="dim")
        if has_drops:
            tbl.add_column("Drops In", justify="right", no_wrap=True, min_width=6)

        for d in domain_list:
            s = states[d]
            st = s["status"]

            if s["checked"]:
                delta = int((now - s["checked"]).total_seconds())
                checked_str = "just now" if delta < 2 else f"{delta}s ago"
            else:
                checked_str = "—"

            changed = s["changed"]
            st_text = Text(("● " if changed else "  ") + st,
                           style=status_style(st) + (" bold" if changed else ""))

            row: list = [d, st_text]
            if has_prev:
                row.append(s["prev"] or "")
            row.append(checked_str)
            if has_drops:
                if d in tracked_map:
                    rem = remaining(tracked_map[d])
                    if rem <= 0:
                        row.append(Text("AVAILABLE", style="bold green blink"))
                    elif rem < NOTIFY_WINDOW:
                        row.append(Text(fmt_duration(rem), style="bold red"))
                    elif rem < 3600:
                        row.append(Text(fmt_duration(rem), style="yellow"))
                    else:
                        row.append(Text(fmt_duration(rem), style="white"))
                else:
                    row.append(Text("—", style="dim"))

            tbl.add_row(*row)

        return tbl

    def do_poll():
        for d in domain_list:
            new_st, _ = rdap_lookup(d)
            s = states[d]
            old_st = s["status"]
            if old_st not in ("fetching…", new_st):
                s["prev"] = old_st
                s["changed"] = True
                if "available" in new_st.lower() and not s["notified"]:
                    send_notification(f"● {d} is available!", f"Status: {new_st}")
                    s["notified"] = True
            s["status"] = new_st
            s["checked"] = datetime.utcnow()

    console.print("[dim]Watching… [bold]Ctrl+C[/bold] to stop.[/dim]")
    try:
        with Live(build_table(interval), refresh_per_second=8, screen=False) as live:
            do_poll()
            while True:
                deadline = time.monotonic() + interval
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    live.update(build_table(left))
                    time.sleep(0.12)
                do_poll()
                live.update(build_table(interval))
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


# ── watch / daemon ────────────────────────────────────────────────────────────

def _watch_loop():
    ts = lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts()}] PDT watch started (PID {os.getpid()})", flush=True)
    while True:
        try:
            newly = archive_expired()
            if newly:
                print(f"[{ts()}] Archived {newly} expired domain{'s' if newly != 1 else ''}", flush=True)
            domains = [d for d in load() if not d.get("archived")]
            changed = False
            for d in domains:
                rem = remaining(d)
                if 0 < rem <= NOTIFY_WINDOW and not d.get("notified"):
                    title = f"Domain dropping: {d['domain']}"
                    lines = [f"Available in {fmt_duration(rem)}"]
                    if d.get("appraisal"):
                        lines.append(f"Appraisal: ${d['appraisal']:,.0f}")
                    if d.get("note"):
                        lines.append(d["note"])
                    send_notification(title, "\n".join(lines))
                    d["notified"] = True
                    changed = True
                    print(
                        f"[{ts()}] Notified: {d['domain']} drops in {fmt_duration(rem)}",
                        flush=True,
                    )
            if changed:
                save(domains)
        except Exception as e:
            print(f"[{ts()}] ERROR: {e}", flush=True)
        time.sleep(60)


@cli.command()
@click.option("-d", "--detach", is_flag=True, help="Run as background daemon")
@click.option("-v", "--verbose", is_flag=True, default=False,
              help="Show detailed output (same as pdt -v watch)")
def watch(detach, verbose):
    """Watch for dropping domains and notify 5 min before they drop.

    Use --detach / -d to run in the background.
    Stop with: pdt stop
    """
    global VERBOSE
    if verbose:
        VERBOSE = True
    if detach:
        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text().strip())
                os.kill(pid, 0)
                console.print(f"[yellow]Daemon already running (PID {pid})[/yellow]")
                return
            except (ProcessLookupError, ValueError):
                PID_FILE.unlink(missing_ok=True)

        argv0 = sys.argv[0]
        cmd = ([sys.executable, argv0] if argv0.endswith(".py") else [argv0]) + ["watch"]

        with open(LOG_FILE, "a") as log:
            proc = subprocess.Popen(
                cmd,
                start_new_session=True,
                stdout=log,
                stderr=log,
            )
        PID_FILE.write_text(str(proc.pid))
        console.print(f"[green]✓ Daemon started[/green] (PID [bold]{proc.pid}[/bold])")
        console.print(f"[dim]Log → {LOG_FILE}[/dim]")
        console.print(f"[dim]Stop with: pdt stop[/dim]")
        return

    # Foreground mode
    PID_FILE.write_text(str(os.getpid()))
    try:
        _watch_loop()
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
    finally:
        PID_FILE.unlink(missing_ok=True)


@cli.command()
def stop():
    """Stop the background watch daemon."""
    if not PID_FILE.exists():
        console.print("[yellow]No daemon running.[/yellow]")
        return
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        PID_FILE.unlink(missing_ok=True)
        console.print(f"[green]✓ Daemon stopped[/green] (PID {pid})")
    except (ProcessLookupError, ValueError):
        console.print("[yellow]Daemon was not running (stale PID file cleared).[/yellow]")
        PID_FILE.unlink(missing_ok=True)


@cli.command()
def status():
    """Show daemon status."""
    if not PID_FILE.exists():
        console.print("[dim]Daemon: [red]not running[/red][/dim]")
        return
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        console.print(f"[dim]Daemon: [green]running[/green] (PID {pid})[/dim]")
    except (ProcessLookupError, ValueError):
        console.print("[dim]Daemon: [red]not running[/red] (stale PID file)[/dim]")


# ── backorder ─────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("domains", nargs=-1, required=True)
@click.option("-v", "--verbose", is_flag=True, default=False,
              help="Show detailed output (same as pdt -v backorder)")
def backorder(domains, verbose):
    """Snipe one or more domains the moment they become available via Spaceship.

    \b
    Each domain must already be tracked (pdt add). Polls RDAP every 5 s and
    attempts registration up to 10 times once a domain drops.
    Multiple domains run in parallel and share API rate-limit budgets.
    Successful registrations are saved to ~/.pdt/backorders.json.
    """
    global VERBOSE
    if verbose:
        VERBOSE = True
    domains = [d.lower().strip() for d in domains]

    # ── 1. Verify all domains are on the main list ────────────────────────────
    tracked = load()
    entries: dict = {}
    for domain in domains:
        entry = find_domain(tracked, domain)
        if entry is None:
            console.print(
                f"[red]Not tracked: [bold]{domain}[/bold] — "
                f"add it first with [bold]pdt add {domain} -t TIME[/bold][/red]"
            )
            sys.exit(1)
        entries[domain] = entry

    # ── 1b. Warn if 3+ domains share a close drop window ─────────────────────
    # Spaceship allows 60 async-operation polls per 300 s (user-wide).
    # Each domain attempt polls every 5 s for up to 120 s → 24 polls/attempt.
    # 3 domains polling simultaneously = up to 72 polls in 300 s, exceeding
    # the budget.  Our RateLimiter won't violate the limit, but will throttle
    # async polls, potentially causing artificial timeouts.
    OVERLAP_WINDOW = 300  # seconds — drops within this window are "simultaneous"
    drop_times = {
        d: datetime.fromisoformat(entries[d]["drop_time"]) for d in domains
    }
    sorted_drops = sorted(drop_times.items(), key=lambda x: x[1])
    max_overlap = 1
    for i, (_, t_i) in enumerate(sorted_drops):
        cluster = sum(
            1 for _, t_j in sorted_drops
            if abs((t_j - t_i).total_seconds()) <= OVERLAP_WINDOW
        )
        if cluster > max_overlap:
            max_overlap = cluster
    if max_overlap >= 3:
        overlapping = [
            d for d, t in sorted_drops
            if abs((t - sorted_drops[0][1]).total_seconds()) <= OVERLAP_WINDOW
        ]
        console.print()
        console.print(
            "[bold yellow]⚠  Rate-limit warning[/bold yellow]\n"
            f"  [yellow]{max_overlap} domains are expected to drop within a {OVERLAP_WINDOW//60}-minute window:[/yellow]"
        )
        for d in overlapping:
            console.print(f"    [dim]·[/dim] [cyan]{d}[/cyan]  [dim]{to_local(drop_times[d]).strftime('%b %d  %H:%M:%S')} local[/dim]")
        console.print(
            "  [yellow]Spaceship allows [bold]60 async-operation polls / 300 s[/bold] per account.[/yellow]\n"
            "  [yellow]With 3+ simultaneous drops the shared poll budget may be exhausted,[/yellow]\n"
            "  [yellow]causing some domains to time out before their operation resolves.[/yellow]\n"
            "  [dim]The rate limiter will never exceed the limit — only some domains\n"
            "  may be delayed. Split into separate runs to avoid this.[/dim]"
        )
        console.print()
        if not click.confirm("  Continue anyway?", default=False):
            sys.exit(0)
        console.print()

    # ── 2. Verify Spaceship credentials ───────────────────────────────────────
    cfg = load_config()
    api_key    = cfg.get("spaceship_api_key")
    api_secret = cfg.get("spaceship_api_secret")
    if not api_key:
        console.print(
            "[red]Missing spaceship_api_key.[/red] "
            "Run: [bold]pdt config --spaceship-api-key VALUE[/bold]"
        )
        sys.exit(1)
    if not api_secret:
        console.print(
            "[red]Missing spaceship_api_secret.[/red] "
            "Run: [bold]pdt config --spaceship-api-secret VALUE[/bold]"
        )
        sys.exit(1)

    # ── 3. Ensure contact exists once (shared across all domains) ─────────────
    contact_id = spaceship_ensure_contact(cfg, api_key, api_secret)

    # ── 4. Shared rate limiters (Spaceship API limits) ────────────────────────
    # Registration:    30 requests / 30 s per user
    # Async-operation: 60 requests / 300 s per user
    reg_rl   = RateLimiter(30,  30.0)
    async_rl = RateLimiter(60, 300.0)

    MAX_ATTEMPTS   = 10
    POLL_INTERVAL  = 5    # s between RDAP polls
    ATTEMPT_DELAY  = 3    # s between registration attempts
    ASYNC_TIMEOUT  = 120  # s to wait for each async operation
    ASYNC_INTERVAL = 5    # s between async-operation polls

    # ── 5a. Verbose log buffer (rendered inside Live; avoids cursor corruption) ─
    log_buf: deque = deque(maxlen=50)

    # ── 5. Per-domain state ───────────────────────────────────────────────────
    states: dict = {}
    for domain in domains:
        drop_dt = datetime.fromisoformat(entries[domain]["drop_time"])
        states[domain] = {
            "phase":          "init",
            "rdap_status":    "—",
            "rdap_checked":   None,
            "registrar":      entries[domain].get("registrar"),
            "drop_dt":        drop_dt,
            "timeout_dt":     drop_dt + timedelta(seconds=3600),
            "sleep_deadline": None,
            "sleep_total":    0.0,
            "poll_deadline":  None,
            "poll_interval":  POLL_INTERVAL,
            "attempt":        0,
            "max_attempts":   MAX_ATTEMPTS,
            "op_id":          None,
            "op_status":      None,
            "op_deadline":    None,
            "op_total":       ASYNC_TIMEOUT,
            "message":        "",
        }

    state_lock = threading.Lock()
    stop_event = threading.Event()

    # ── 6. Build table ────────────────────────────────────────────────────────
    def build_table() -> Table:
        dot   = "[bright_black]·[/bright_black]"
        total = len(domains)

        with state_lock:
            succeeded = sum(1 for s in states.values() if s["phase"] == "success")
            failed    = sum(1 for s in states.values() if s["phase"] in ("failed", "timeout"))
            stopped   = sum(1 for s in states.values() if s["phase"] == "stopped")
            in_prog   = total - succeeded - failed - stopped

        parts = [f"[bold white]Backorder Snipe[/bold white]  {dot}  "
                 f"[bold cyan]{total} domain{'s' if total > 1 else ''}[/bold cyan]"]
        if succeeded:
            parts.append(f"[bold green]{succeeded} registered[/bold green]")
        if failed:
            parts.append(f"[bold red]{failed} failed[/bold red]")
        if in_prog > 0:
            parts.append(f"[white]{in_prog} active[/white]")
        title = f"  {dot}  ".join(parts)

        tbl = Table(
            box=box.ROUNDED,
            expand=True,
            header_style="bold magenta",
            border_style="bright_black",
            title=title,
            title_style="",
            show_header=True,
            pad_edge=True,
        )
        tbl.add_column("Domain",     style="cyan",  no_wrap=True)
        tbl.add_column("Phase",      no_wrap=True,  min_width=15)
        tbl.add_column("RDAP",       no_wrap=True,  min_width=10)
        tbl.add_column("Registrar",  style="dim",   no_wrap=True, overflow="ellipsis", min_width=8)
        tbl.add_column("Drop Time",  no_wrap=True,  min_width=22)
        tbl.add_column("Note",       overflow="ellipsis", ratio=1)

        with state_lock:
            snapshot = {d: dict(s) for d, s in states.items()}

        for domain in domains:
            s  = snapshot[domain]
            ph = s["phase"]

            phase_map = {
                "init":        "[dim]init[/dim]",
                "sleeping":    "[blue]sleeping[/blue]",
                "polling":     "[yellow]polling RDAP[/yellow]",
                "registering": "[bold yellow]★ registering[/bold yellow]",
                "success":     "[bold green]✓ registered[/bold green]",
                "failed":      "[bold red]✗ failed[/bold red]",
                "timeout":     "[yellow]✗ timed out[/yellow]",
                "stopped":     "[dim]stopped[/dim]",
            }
            phase_cell = phase_map.get(ph, ph)

            rdap_st   = s["rdap_status"]
            rdap_cell = (
                Text(rdap_st, style=status_style(rdap_st))
                if rdap_st != "—"
                else Text("—", style="dim")
            )

            drop_dt = s["drop_dt"]
            rem     = (drop_dt - datetime.utcnow()).total_seconds()
            drop_cell = Text(
                to_local(drop_dt).strftime("%b %d  %H:%M:%S"),
                style="white" if rem > 0 else "dim",
            )
            if rem > 0:
                drop_cell.append(f"  in {fmt_duration(rem)}", style="dim")
            else:
                drop_cell.append(f"  +{fmt_duration(-rem)} past", style="dim")

            note = s["message"]
            if ph == "sleeping" and s["sleep_deadline"]:
                left = max(0.0, s["sleep_deadline"] - time.monotonic())
                note = f"waking in {fmt_duration(left)}"
            elif ph == "polling" and s["poll_deadline"]:
                left = max(0.0, s["poll_deadline"] - time.monotonic())
                note = f"next check in {int(left)}s"
            elif ph == "registering":
                note = f"attempt {s['attempt']}/{s['max_attempts']}"
                if s["op_id"]:
                    op_label = s["op_status"] or "pending"
                    note += f"  ·  op:{s['op_id'][:8]}…  [{op_label}]"

            reg = s.get("registrar") or "—"
            tbl.add_row(domain, phase_cell, rdap_cell, reg, drop_cell,
                        Text(note, style="dim"))

        if not VERBOSE or not log_buf:
            return tbl

        lines = list(log_buf)
        log_text = Text()
        for line in lines:
            log_text.append(f"  [v] {line}\n", style="dim blue")
        log_panel = Panel(log_text, border_style="dim blue", padding=(0, 1))
        return Group(tbl, log_panel)

    # ── 7. Worker thread ──────────────────────────────────────────────────────
    def _interruptible_sleep(secs: float) -> bool:
        """Sleep in small increments; return False if stop_event fires."""
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline:
            if stop_event.is_set():
                return False
            time.sleep(0.2)
        return True

    def worker(domain: str):
        s = states[domain]

        # Initial RDAP check
        initial_st, initial_reg = rdap_lookup(domain)
        with state_lock:
            s["rdap_status"]  = initial_st
            s["rdap_checked"] = datetime.utcnow()
            if initial_reg is not None:
                s["registrar"] = initial_reg

        if initial_st != "available":
            # Sleep until 2 min before the scheduled drop
            sleep_secs = (
                s["drop_dt"] - timedelta(seconds=120) - datetime.utcnow()
            ).total_seconds()
            if sleep_secs > 0:
                with state_lock:
                    s["phase"]          = "sleeping"
                    s["sleep_total"]    = sleep_secs
                    s["sleep_deadline"] = time.monotonic() + sleep_secs
                if not _interruptible_sleep(sleep_secs):
                    with state_lock:
                        s["phase"] = "stopped"
                    return

            # Active RDAP polling
            with state_lock:
                s["phase"]         = "polling"
                s["poll_interval"] = POLL_INTERVAL

            available = False
            while True:
                if stop_event.is_set():
                    with state_lock:
                        s["phase"] = "stopped"
                    return
                if datetime.utcnow() > s["timeout_dt"]:
                    with state_lock:
                        s["phase"]   = "timeout"
                        s["message"] = "1 h past drop — may have been renewed"
                    return

                new_st, new_reg = rdap_lookup(domain)
                with state_lock:
                    s["rdap_status"]  = new_st
                    s["rdap_checked"] = datetime.utcnow()
                    if new_reg is not None:
                        s["registrar"] = new_reg

                if new_st == "available":
                    available = True
                    break

                interval = 15 if new_st in ("fetch-error", "timeout") else POLL_INTERVAL
                with state_lock:
                    s["poll_interval"] = interval
                    s["poll_deadline"] = time.monotonic() + interval
                if not _interruptible_sleep(interval):
                    with state_lock:
                        s["phase"] = "stopped"
                    return

            if not available:
                return

        # Registration
        with state_lock:
            s["phase"] = "registering"

        success = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if stop_event.is_set():
                with state_lock:
                    s["phase"] = "stopped"
                return

            with state_lock:
                s["attempt"]     = attempt
                s["op_id"]       = None
                s["op_status"]   = None
                s["op_deadline"] = None
                s["message"]     = ""

            # Rate-limited registration slot
            if not reg_rl.acquire(stop_event):
                with state_lock:
                    s["phase"] = "stopped"
                return

            op_id, err = spaceship_register(domain, api_key, api_secret, contact_id)
            if err:
                with state_lock:
                    s["message"] = f"attempt {attempt} error: {err}"
                if attempt < MAX_ATTEMPTS and not _interruptible_sleep(ATTEMPT_DELAY):
                    with state_lock:
                        s["phase"] = "stopped"
                    return
                continue

            if not op_id:
                with state_lock:
                    s["message"] = f"attempt {attempt}: 202 but no operation ID"
                if attempt < MAX_ATTEMPTS and not _interruptible_sleep(ATTEMPT_DELAY):
                    with state_lock:
                        s["phase"] = "stopped"
                    return
                continue

            with state_lock:
                s["op_id"]       = op_id
                s["op_deadline"] = time.monotonic() + ASYNC_TIMEOUT

            op_result = None
            while True:
                with state_lock:
                    op_deadline = s["op_deadline"]
                if time.monotonic() >= op_deadline:
                    break
                if stop_event.is_set():
                    with state_lock:
                        s["phase"] = "stopped"
                    return

                # Rate-limited async-operation poll
                if not async_rl.acquire(stop_event):
                    with state_lock:
                        s["phase"] = "stopped"
                    return

                op_st = spaceship_poll_async(op_id, api_key, api_secret)
                with state_lock:
                    s["op_status"] = op_st
                if op_st in ("success", "failed"):
                    op_result = op_st
                    break
                if not _interruptible_sleep(ASYNC_INTERVAL):
                    with state_lock:
                        s["phase"] = "stopped"
                    return

            if op_result == "success":
                success = True
                with state_lock:
                    s["phase"]   = "success"
                    s["message"] = f"registered on attempt {attempt}/{MAX_ATTEMPTS}"
                bos = load_backorders()
                bos.append({
                    "domain":         domain,
                    "backordered_at": datetime.utcnow().isoformat(),
                    "attempts":       attempt,
                    "result":         "success",
                    "registrar":      "spaceship.dev",
                })
                save_backorders(bos)
                break

            with state_lock:
                s["message"] = (
                    f"attempt {attempt}: op failed"
                    if op_result == "failed"
                    else f"attempt {attempt}: op timed out"
                )
            if attempt < MAX_ATTEMPTS and not _interruptible_sleep(ATTEMPT_DELAY):
                with state_lock:
                    s["phase"] = "stopped"
                return

        if not success:
            with state_lock:
                if s["phase"] not in ("stopped", "success"):
                    s["phase"]   = "failed"
                    s["message"] = f"all {MAX_ATTEMPTS} attempts exhausted"
            # Check if someone else registered it (outside lock to avoid holding it during network I/O)
            if s["phase"] == "failed":
                final_st, final_reg = rdap_lookup(domain)
                if final_st != "available" and final_reg:
                    with state_lock:
                        s["registrar"] = final_reg
                        s["message"]   = f"taken by {final_reg}"
                    bos = load_backorders()
                    bos.append({
                        "domain":         domain,
                        "backordered_at": datetime.utcnow().isoformat(),
                        "attempts":       MAX_ATTEMPTS,
                        "result":         "failed",
                        "taken_by":       final_reg,
                    })
                    save_backorders(bos)

    # ── 8. Launch threads and Live display ────────────────────────────────────
    threads = [
        threading.Thread(target=worker, args=(d,), daemon=True)
        for d in domains
    ]

    console.print("[dim]Watching… [bold]Ctrl+C[/bold] to abort.[/dim]")
    global _vlog_sink
    _vlog_sink = log_buf
    try:
        with Live(build_table(), refresh_per_second=8, screen=False) as live:
            for t in threads:
                t.start()
            while any(t.is_alive() for t in threads):
                live.update(build_table())
                time.sleep(0.12)
            live.update(build_table())

    except KeyboardInterrupt:
        stop_event.set()
        for t in threads:
            t.join(timeout=2.0)
        console.print("\n[dim]Stopped.[/dim]")
    finally:
        _vlog_sink = None


if __name__ == "__main__":
    cli()
