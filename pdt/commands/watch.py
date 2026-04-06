import os
import signal
import subprocess
import sys
import time
from datetime import datetime

import click
from rich import box
from rich.table import Table
from rich.text import Text

from ..constants import LOG_FILE, NOTIFY_WINDOW, PID_FILE
from ..notifications import send_notification
from ..rdap import rdap_lookup
from ..storage import archive_expired, load, save
from ..utils import (
    console,
    fmt_duration,
    remaining,
    resolve_targets,
    set_verbose,
    status_style,
    to_local,
    utcnow,
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
    from rich.live import Live

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


@click.command("watch")
@click.option("-d", "--detach", is_flag=True, help="Run as background daemon")
@click.option("-v", "--verbose", is_flag=True, default=False,
              help="Show detailed output (same as pdt -v watch)")
def watch(detach, verbose):
    """Watch for dropping domains and notify 5 min before they drop.

    Use --detach / -d to run in the background.
    Stop with: pdt stop
    """
    if verbose:
        set_verbose(True)
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
