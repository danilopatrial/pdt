import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

import click
from rich import box
from rich.live import Live
from rich.panel import Panel
from rich.console import Group
from rich.table import Table
from rich.text import Text

from ..constants import LOG_FILE, NOTIFY_WINDOW, PID_FILE
from ..notifications import send_notification
from ..rdap import rdap_lookup
from ..storage import archive_expired, load, save
from ..utils import (
    console,
    find_domain,
    fmt_duration,
    redact_domain,
    remaining,
    resolve_targets,
    set_verbose,
    set_vlog_sink,
    status_style,
    to_local,
    utcnow,
    vlog,
)


@click.command("poll")
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
        domain_list = resolve_targets(domains, tracked_all)

    states = {
        d: {"status": "fetching…", "prev": None, "checked": None, "changed": False, "notified": False}
        for d in domain_list
    }

    def build_table(time_left: float) -> Table:
        now = utcnow()
        w = console.width
        has_drops = any(d in tracked_map for d in domain_list)
        has_prev  = any(states[d]["prev"] is not None for d in domain_list)

        secs      = int(time_left)
        bar_width = max(4, min(12, w // 10))
        filled    = round(bar_width * (1 - time_left / interval)) if interval > 0 else bar_width
        bar = (
            "[green]" + "█" * filled + "[/green]"
            + "[bright_black]" + "░" * (bar_width - filled) + "[/bright_black]"
        )
        title = (
            f"[bold white]RDAP Poll[/bold white]  [bright_black]·[/bright_black]  "
            f"every {interval}s  [bright_black]·[/bright_black]  {bar} [dim]{secs}s[/dim]"
        )

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
            s  = states[d]
            st = s["status"]

            if s["checked"]:
                delta       = int((now - s["checked"]).total_seconds())
                checked_str = "just now" if delta < 2 else f"{delta}s ago"
            else:
                checked_str = "—"

            changed = s["changed"]
            st_text = Text(
                ("● " if changed else "  ") + st,
                style=status_style(st) + (" bold" if changed else ""),
            )

            row: list = [d, st_text]
            if has_prev:
                row.append(s["prev"] or "")
            row.append(checked_str)
            if has_drops:
                if d in tracked_map:
                    rem = remaining(tracked_map[d])
                    if rem <= 0:
                        if "available" in states[d]["status"].lower():
                            row.append(Text("AVAILABLE", style="bold green blink"))
                        else:
                            row.append(Text("MISSED", style="bold red"))
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
            s      = states[d]
            old_st = s["status"]
            if old_st not in ("fetching…", new_st):
                s["prev"]    = old_st
                s["changed"] = True
                if "available" in new_st.lower() and not s["notified"]:
                    send_notification(f"● {d} is available!", f"Status: {new_st}")
                    s["notified"] = True
            s["status"]  = new_st
            s["checked"] = utcnow()

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


def _watch_domains_live(domain_names: list, tracked: list):
    """Interactive live UI: countdown to drop, then RDAP-poll until available."""
    POLL_INTERVAL = 5

    # Validate all domains have a drop time
    entries = {}
    for domain in domain_names:
        entry = find_domain(tracked, domain)
        if entry is None:
            console.print(
                f"[red]Not tracked: [bold]{domain}[/bold] — "
                f"add it first with [bold]pdt add {domain}[/bold][/red]"
            )
            sys.exit(1)
        if not entry.get("drop_time"):
            console.print(
                f"[red]{domain} has no drop time set.[/red] "
                f"Run: [bold]pdt update {domain} -t TIME[/bold]"
            )
            sys.exit(1)
        entries[domain] = entry

    states: dict = {}
    for domain in domain_names:
        drop_dt = datetime.fromisoformat(entries[domain]["drop_time"])
        states[domain] = {
            "phase":         "waiting",   # waiting | monitoring | available
            "rdap_status":   "—",
            "rdap_checked":  None,
            "registrar":     entries[domain].get("registrar"),
            "drop_dt":       drop_dt,
            "message":       "",
            "poll_deadline": None,
        }

    state_lock = threading.Lock()
    stop_event = threading.Event()
    log_buf: deque = deque(maxlen=50)

    # ── Build table ────────────────────────────────────────────────────────────
    def build_table():
        from ..utils import VERBOSE
        dot   = "[bright_black]·[/bright_black]"
        total = len(domain_names)

        with state_lock:
            n_avail = sum(1 for s in states.values() if s["phase"] == "available")
            n_monit = sum(1 for s in states.values() if s["phase"] == "monitoring")
            n_wait  = sum(1 for s in states.values() if s["phase"] == "waiting")

        parts = [
            f"[bold white]Watch[/bold white]  {dot}  "
            f"[bold cyan]{total} domain{'s' if total > 1 else ''}[/bold cyan]"
        ]
        if n_avail:
            parts.append(f"[bold green]{n_avail} available[/bold green]")
        if n_monit:
            parts.append(f"[yellow]{n_monit} monitoring[/yellow]")
        if n_wait:
            parts.append(f"[dim]{n_wait} waiting[/dim]")
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
        tbl.add_column("Domain",    style="cyan", no_wrap=True)
        tbl.add_column("Phase",     no_wrap=True, min_width=15)
        tbl.add_column("RDAP",      no_wrap=True, min_width=10)
        tbl.add_column("Registrar", style="dim",  no_wrap=True, overflow="ellipsis", min_width=8)
        tbl.add_column("Drop Time", no_wrap=True, min_width=22)
        tbl.add_column("Note",      overflow="ellipsis", ratio=1)

        with state_lock:
            snapshot = {d: dict(s) for d, s in states.items()}

        for domain in domain_names:
            s  = snapshot[domain]
            ph = s["phase"]

            phase_map = {
                "waiting":    "[dim]waiting[/dim]",
                "monitoring": "[yellow]monitoring RDAP[/yellow]",
                "available":  "[bold green]✓ available[/bold green]",
            }
            phase_cell = phase_map.get(ph, ph)

            rdap_st   = s["rdap_status"]
            rdap_cell = (
                Text(rdap_st, style=status_style(rdap_st))
                if rdap_st != "—"
                else Text("—", style="dim")
            )

            drop_dt = s["drop_dt"]
            rem     = (drop_dt - utcnow()).total_seconds()
            drop_cell = Text(
                to_local(drop_dt).strftime("%b %d  %H:%M:%S"),
                style="white" if rem > 0 else "dim",
            )
            if rem > 0:
                drop_cell.append(f"  in {fmt_duration(rem)}", style="dim")
            else:
                drop_cell.append(f"  +{fmt_duration(-rem)} past", style="dim")

            note = s["message"]
            if ph == "monitoring" and s["poll_deadline"]:
                left = max(0.0, s["poll_deadline"] - time.monotonic())
                note = f"next check in {int(left)}s"

            reg = s.get("registrar") or "—"
            tbl.add_row(
                redact_domain(domain), phase_cell, rdap_cell, reg, drop_cell,
                Text(note, style="dim"),
            )

        if not VERBOSE or not log_buf:
            return tbl

        table_lines    = 5 + len(domain_names)
        panel_overhead = 3
        available_h    = max(1, console.height - table_lines - panel_overhead)
        lines    = list(log_buf)[-available_h:]
        log_text = Text()
        for line in lines:
            log_text.append(f"  [v] {line}\n", style="dim blue")
        log_panel = Panel(log_text, border_style="dim blue", padding=(0, 1))
        return Group(tbl, log_panel)

    # ── Worker ─────────────────────────────────────────────────────────────────
    def _interruptible_sleep(secs: float) -> bool:
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline:
            if stop_event.is_set():
                return False
            time.sleep(0.2)
        return True

    def worker(domain: str):
        s = states[domain]

        # Initial RDAP lookup on first run
        vlog(f"watch.start  {domain}  drop_time={entries[domain].get('drop_time')!r}", domain=domain)
        initial_st, initial_reg = rdap_lookup(domain)
        with state_lock:
            s["rdap_status"]  = initial_st
            s["rdap_checked"] = utcnow()
            if initial_reg is not None:
                s["registrar"] = initial_reg
        vlog(f"watch.rdap_initial  {domain}  status={initial_st!r}  registrar={initial_reg!r}", domain=domain)

        # Phase 1: sleep until drop time
        sleep_secs = (s["drop_dt"] - utcnow()).total_seconds()
        if sleep_secs > 0:
            vlog(f"watch.sleeping  {domain}  secs={sleep_secs:.0f}", domain=domain)
            if not _interruptible_sleep(sleep_secs):
                return

        # Notify: drop time elapsed
        send_notification(
            f"Drop time elapsed: {domain}",
            "Monitoring RDAP for availability…",
        )
        with state_lock:
            s["phase"]   = "monitoring"
            s["message"] = "drop time elapsed"
        vlog(f"watch.monitoring  {domain}  drop time elapsed", domain=domain)

        # Phase 2: poll RDAP until available
        while not stop_event.is_set():
            new_st, new_reg = rdap_lookup(domain)
            with state_lock:
                prev_st = s["rdap_status"]
                s["rdap_status"]  = new_st
                s["rdap_checked"] = utcnow()
                if new_reg:
                    s["registrar"] = new_reg
                s["message"] = ""
            if new_st != prev_st:
                vlog(f"watch.rdap_change  {domain}  {prev_st!r} → {new_st!r}", domain=domain)

            if "available" in new_st.lower():
                with state_lock:
                    s["phase"]   = "available"
                    s["message"] = "RDAP confirmed available"
                vlog(f"watch.available  {domain}", domain=domain)
                send_notification(f"✓ {domain} is available!", f"RDAP: {new_st}")
                return

            with state_lock:
                s["poll_deadline"] = time.monotonic() + POLL_INTERVAL
            if not _interruptible_sleep(POLL_INTERVAL):
                return

    # ── Launch ─────────────────────────────────────────────────────────────────
    threads = [
        threading.Thread(target=worker, args=(d,), daemon=True)
        for d in domain_names
    ]

    console.print("[dim]Watching… [bold]Ctrl+C[/bold] to abort.[/dim]")
    set_vlog_sink(log_buf)
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
        set_vlog_sink(None)


def _watch_loop():
    def ts():
        return utcnow().strftime("%Y-%m-%d %H:%M:%S")

    print(f"[{ts()}] PDT watch started (PID {os.getpid()})", flush=True)
    while True:
        try:
            newly = archive_expired()
            if newly:
                print(
                    f"[{ts()}] Archived {newly} expired domain{'s' if newly != 1 else ''}",
                    flush=True,
                )
            domains = [d for d in load() if not d.get("archived")]
            changed = False
            for d in domains:
                rem = remaining(d)

                # Notify 5 min before drop
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

                # Notify once when RDAP shows available after drop
                if rem <= 0 and not d.get("rdap_notified"):
                    rdap_st, _ = rdap_lookup(d["domain"])
                    if "available" in rdap_st.lower():
                        send_notification(
                            f"✓ {d['domain']} is available!",
                            f"RDAP: {rdap_st}",
                        )
                        d["rdap_notified"] = True
                        changed = True
                        print(
                            f"[{ts()}] RDAP available: {d['domain']} ({rdap_st})",
                            flush=True,
                        )

            if changed:
                save(domains)
        except Exception as e:
            print(f"[{ts()}] ERROR: {e}", flush=True)
        time.sleep(60)


@click.command("watch")
@click.argument("domains", nargs=-1, required=False)
@click.option("-d", "--detach", is_flag=True, help="Run as background daemon")
@click.option("-v", "--verbose", is_flag=True, default=False,
              help="Show detailed output (same as pdt -v watch)")
def watch(domains, detach, verbose):
    """Watch for dropping domains and notify when they drop and become available.

    With no arguments, runs (or detaches) a background daemon that notifies
    5 minutes before each tracked domain drops, and again once RDAP confirms
    it is available.

    With DOMAIN arguments, opens an interactive live display that counts down
    to each domain's drop time, notifies when the timer elapses, then polls
    RDAP every 5 seconds and notifies again once the domain becomes available.

    \b
    DOMAIN can be a domain name or a 1-based index from 'pdt list'.

    \b
    Examples:
      pdt watch                      # start daemon (foreground)
      pdt watch -d                   # start daemon in background
      pdt watch example.com          # interactive watch for one domain
      pdt watch 1 3 example.net      # interactive watch for multiple domains
    """
    if verbose:
        set_verbose(True)

    if domains:
        # Interactive live watch mode for specific domains
        tracked = load()
        domain_names = resolve_targets(domains, tracked)
        _watch_domains_live(domain_names, tracked)
        return

    # ── Daemon mode (no domain args) ──────────────────────────────────────────
    if detach:
        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text().strip())
                os.kill(pid, 0)
                console.print(f"[yellow]Daemon already running (PID {pid})[/yellow]")
                return
            except (ProcessLookupError, PermissionError, ValueError):
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
        console.print("[dim]Stop with: pdt stop[/dim]")
        return

    PID_FILE.write_text(str(os.getpid()))
    try:
        _watch_loop()
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
    finally:
        PID_FILE.unlink(missing_ok=True)


@click.command("stop")
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
    except ProcessLookupError:
        console.print("[yellow]Daemon was not running (stale PID file cleared).[/yellow]")
        PID_FILE.unlink(missing_ok=True)
    except PermissionError:
        console.print(
            f"[red]Cannot stop daemon — process exists but is owned by another user.[/red]"
        )
    except ValueError:
        console.print("[yellow]Daemon was not running (stale PID file cleared).[/yellow]")
        PID_FILE.unlink(missing_ok=True)


@click.command("status")
def status():
    """Show daemon status."""
    if not PID_FILE.exists():
        console.print("[dim]Daemon: [red]not running[/red][/dim]")
        return
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        console.print(f"[dim]Daemon: [green]running[/green] (PID {pid})[/dim]")
    except ProcessLookupError:
        console.print("[dim]Daemon: [red]not running[/red] (stale PID file)[/dim]")
    except PermissionError:
        console.print(
            f"[dim]Daemon: [yellow]process exists but owned by another user[/yellow] (PID {pid})[/dim]"
        )
    except ValueError:
        console.print("[dim]Daemon: [red]not running[/red] (stale PID file)[/dim]")


@click.command("logs")
@click.option("-n", "--lines", default=50, show_default=True, metavar="N",
              help="Number of recent log lines to show")
@click.option("-f", "--follow", is_flag=True,
              help="Follow the log in real time (like tail -f)")
def logs(lines, follow):
    """Show or follow the daemon log.

    \b
    Examples:
      pdt logs
      pdt logs -n 100
      pdt logs -f
    """
    if not LOG_FILE.exists():
        console.print("[dim]No daemon log found. Start the daemon with: pdt watch -d[/dim]")
        return

    if follow:
        try:
            subprocess.run(["tail", "-f", "-n", str(lines), str(LOG_FILE)])
        except FileNotFoundError:
            console.print("[red]tail not found — cannot follow log.[/red]")
        except KeyboardInterrupt:
            pass
        return

    text       = LOG_FILE.read_text()
    tail_lines = text.splitlines()[-lines:]
    for line in tail_lines:
        console.print(line)
