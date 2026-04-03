#!/usr/bin/env python3
"""PDT — Pending Delete Domain Tracker"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import click
import requests
from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

DATA_DIR = Path.home() / ".pdt"
DATA_FILE = DATA_DIR / "domains.json"
PID_FILE = DATA_DIR / "daemon.pid"
LOG_FILE = DATA_DIR / "daemon.log"

NOTIFY_WINDOW = 300        # 5 minutes
ARCHIVE_AFTER = 24 * 3600  # 24 hours past drop time


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


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_duration(s: str) -> float:
    """'1d3h57m' → total seconds as float"""
    m = re.fullmatch(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s.strip())
    if not m or not any(m.groups()):
        raise ValueError(f"Invalid duration '{s}'. Use e.g. 1d3h57m or 45m")
    d, h, mi, sec = (int(x or 0) for x in m.groups())
    return timedelta(days=d, hours=h, minutes=mi, seconds=sec).total_seconds()


def fmt_duration(secs: float) -> str:
    """Total seconds → '1d3h57m'"""
    if secs <= 0:
        return "NOW"
    td = timedelta(seconds=int(secs))
    d = td.days
    h, rem = divmod(td.seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return "".join(parts)


def find_domain(domains: list, name: str):
    """Return the first entry matching name, or None."""
    for d in domains:
        if d["domain"] == name:
            return d
    return None


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


def rdap_lookup(domain: str) -> str:
    """Return RDAP status string. Returns 'available' on 404."""
    try:
        r = requests.get(
            f"https://rdap.org/domain/{domain}",
            timeout=10,
            allow_redirects=True,
            headers={"Accept": "application/rdap+json, application/json"},
        )
        if r.status_code == 404:
            return "available"
        r.raise_for_status()
        data = r.json()
        statuses = data.get("status", [])
        return ", ".join(statuses) if statuses else "active"
    except requests.HTTPError as e:
        return f"http-error-{e.response.status_code}"
    except requests.Timeout:
        return "timeout"
    except Exception:
        return "fetch-error"


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

@click.group()
@click.version_option("1.0.0", prog_name="pdt")
def cli():
    """PDT — Pending Delete Domain Tracker

    Track domains in pending-delete and get desktop notifications
    5 minutes before they become available.
    """


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

    if status is None:
        with console.status(f"[dim]Fetching RDAP for {domain}…[/dim]"):
            status = rdap_lookup(domain)

    domains.append({
        "domain": domain,
        "drop_time": drop_time,
        "appraisal": appraisal,
        "note": note,
        "status": status,
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
            click.echo("domain,dropped_seconds_ago,drop_time_utc,appraisal,status,note")
            for d in domains:
                ago = int(-remaining(d))
                appr = d.get("appraisal") or ""
                note = (d.get("note") or "").replace(",", ";")
                click.echo(f"{d['domain']},{ago},{d['drop_time']},{appr},{d['status']},{note}")
        else:
            click.echo("domain,remaining_seconds,drop_time_utc,appraisal,status,note")
            for d in domains:
                r = int(remaining(d))
                appr = d.get("appraisal") or ""
                note = (d.get("note") or "").replace(",", ";")
                click.echo(f"{d['domain']},{r},{d['drop_time']},{appr},{d['status']},{note}")
        return

    w = console.width

    # Responsive breakpoints
    tiny  = w < 60
    small = w < 85
    mid   = w < 105

    show_index     = not tiny
    show_drop_time = not small
    show_appraisal = w >= 68
    show_note      = not mid

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
            table.add_column("Dropped At (UTC)", justify="center", no_wrap=True, min_width=12)
    else:
        table.add_column("Drops In", justify="right", no_wrap=True, min_width=6)
        if show_drop_time:
            table.add_column("Drop Time (UTC)", justify="center", no_wrap=True, min_width=12)

    if show_appraisal:
        table.add_column("Appraisal", justify="right", no_wrap=True, min_width=8)

    table.add_column("Status", no_wrap=True, overflow="ellipsis", min_width=8)

    if show_note:
        table.add_column("Note", style="dim", ratio=1, overflow="ellipsis", no_wrap=True)

    for i, d in enumerate(domains, 1):
        rem = remaining(d)
        drop_dt = datetime.fromisoformat(d["drop_time"])
        appraisal = f"${d['appraisal']:,.0f}" if d.get("appraisal") else "—"
        st = d.get("status", "unknown")

        if show_archived:
            ago = -rem  # positive = seconds since drop
            time_cell = Text(fmt_duration(ago) + " ago", style="dim")
            date_str  = drop_dt.strftime("%b %d  %H:%M")
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
            date_str = drop_dt.strftime("%b %d  %H:%M")

        row = []
        if show_index:
            row.append(str(i))
        row.append(d["domain"])
        row.append(time_cell)
        if show_drop_time:
            row.append(date_str)
        if show_appraisal:
            row.append(appraisal)
        row.append(Text(st, style=status_style(st)))
        if show_note:
            row.append(d.get("note", ""))

        table.add_row(*row)

    console.print(table)
    total = len(domains)
    label = "archived domain" if show_archived else "domain"
    console.print(f"[dim]  {total} {label}{'s' if total != 1 else ''} · times in UTC[/dim]")


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
            status = rdap_lookup(name)
        entry = find_domain(tracked, name)
        untracked = "" if entry else " [dim](not tracked)[/dim]"
        st = status_style(status)
        console.print(f"  [bold cyan]{name}[/bold cyan]{untracked} → [{st}]{status}[/{st}]")
        if entry:
            entry["status"] = status
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
            new_st = rdap_lookup(d)
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
def watch(detach):
    """Watch for dropping domains and notify 5 min before they drop.

    Use --detach / -d to run in the background.
    Stop with: pdt stop
    """
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


if __name__ == "__main__":
    cli()
