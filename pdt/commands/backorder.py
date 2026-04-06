import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import click
from rich import box
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..config import load_config
from ..constants import BACKORDER_LOG_FILE, BACKORDER_PID_FILE, NOTIFY_WINDOW
from ..notifications import send_notification
from ..rdap import rdap_lookup
from ..spaceship import (
    RateLimiter,
    spaceship_check_available,
    spaceship_ensure_contact,
    spaceship_poll_async,
    spaceship_register,
)
from ..storage import load, load_backorders, save, save_backorders
from ..utils import (
    console,
    find_domain,
    fmt_duration,
    remaining,
    resolve_targets,
    set_verbose,
    set_vlog_sink,
    status_style,
    to_local,
    utcnow,
    vlog,
)


@click.command("backorder")
@click.argument("domains", nargs=-1, required=True)
@click.option("-v", "--verbose", is_flag=True, default=False,
              help="Show detailed output (same as pdt -v backorder)")
@click.option("-d", "--detach", is_flag=True, help="Run as background daemon")
@click.option("--daemon", is_flag=True, hidden=True,
              help="Internal: run as daemon process (used by --detach)")
def backorder(domains, verbose, detach, daemon):
    """Snipe one or more domains the moment they become available via Spaceship.

    \b
    Each domain must already be tracked (pdt add). Polls RDAP every 5 s and
    attempts registration up to 10 times once a domain drops.
    Multiple domains run in parallel and share API rate-limit budgets.
    Successful registrations are saved to ~/.pdt/backorders.json.
    Use --detach / -d to run in the background.
    Stop with: pdt backorder-stop
    """
    if verbose:
        set_verbose(True)

    # ── 0. Detach: spawn a background daemon ─────────────────────────────────
    if detach:
        if BACKORDER_PID_FILE.exists():
            try:
                pid = int(BACKORDER_PID_FILE.read_text().strip())
                os.kill(pid, 0)
                console.print(f"[yellow]Backorder daemon already running (PID {pid})[/yellow]")
                return
            except (ProcessLookupError, PermissionError, ValueError):
                BACKORDER_PID_FILE.unlink(missing_ok=True)

        argv0 = sys.argv[0]
        cmd = ([sys.executable, argv0] if argv0.endswith(".py") else [argv0])
        cmd += ["backorder", "--daemon"] + list(domains)

        with open(BACKORDER_LOG_FILE, "a") as log:
            proc = subprocess.Popen(
                cmd,
                start_new_session=True,
                stdout=log,
                stderr=log,
            )
        BACKORDER_PID_FILE.write_text(str(proc.pid))
        console.print(f"[green]✓ Backorder daemon started[/green] (PID [bold]{proc.pid}[/bold])")
        console.print(f"[dim]Domains: {', '.join(domains)}[/dim]")
        console.print(f"[dim]Log → {BACKORDER_LOG_FILE}[/dim]")
        console.print("[dim]Stop with: pdt backorder-stop[/dim]")
        return

    # ── 1. Verify all domains are on the main list ────────────────────────────
    tracked = load()
    domains = resolve_targets(domains, tracked)
    entries: dict = {}
    for domain in domains:
        entry = find_domain(tracked, domain)
        if entry is None:
            console.print(
                f"[red]Not tracked: [bold]{domain}[/bold] — "
                f"add it first with [bold]pdt add {domain} -t TIME[/bold][/red]"
            )
            sys.exit(1)
        if not entry.get("drop_time"):
            console.print(
                f"[red]{domain} has no drop time set.[/red] "
                f"Run: [bold]pdt update {domain} -t TIME[/bold]"
            )
            sys.exit(1)
        entries[domain] = entry

    # ── 1b. Warn if 3+ domains share a close drop window ─────────────────────
    OVERLAP_WINDOW = 300
    drop_times = {
        d: datetime.fromisoformat(entries[d]["drop_time"]) for d in domains
    }
    sorted_drops = sorted(drop_times.items(), key=lambda x: x[1])
    max_overlap  = 1
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
            console.print(
                f"    [dim]·[/dim] [cyan]{d}[/cyan]  "
                f"[dim]{to_local(drop_times[d]).strftime('%b %d  %H:%M:%S')} local[/dim]"
            )
        console.print(
            "  [yellow]Spaceship allows [bold]60 async-operation polls / 300 s[/bold] per account.[/yellow]\n"
            "  [yellow]With 3+ simultaneous drops the shared poll budget may be exhausted,[/yellow]\n"
            "  [yellow]causing some domains to time out before their operation resolves.[/yellow]\n"
            "  [dim]The rate limiter will never exceed the limit — only some domains\n"
            "  may be delayed. Split into separate runs to avoid this.[/dim]"
        )
        console.print()
        if daemon:
            print(f"  [daemon] Rate-limit warning acknowledged — continuing.", flush=True)
        elif not click.confirm("  Continue anyway?", default=False):
            sys.exit(0)
        console.print()

    # ── 2. Verify Spaceship credentials ───────────────────────────────────────
    cfg        = load_config()
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

    # ── 3. Ensure contact exists ───────────────────────────────────────────────
    contact_id = spaceship_ensure_contact(cfg, api_key, api_secret)

    # ── 4. Shared rate limiters ────────────────────────────────────────────────
    reg_rl   = RateLimiter(30,  30.0)
    async_rl = RateLimiter(60, 300.0)

    MAX_ATTEMPTS   = 15
    POLL_INTERVAL  = 5
    ASYNC_TIMEOUT  = 120
    ASYNC_INTERVAL = 5

    MAX_RETRY_DELAY = 900  # 15 minutes

    def attempt_delay(attempt: int) -> int:
        return min(2 ** (attempt - 1), MAX_RETRY_DELAY)

    log_buf: deque = deque(maxlen=50)

    # ── 5. Per-domain state ────────────────────────────────────────────────────
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

    # ── 6. Build table ─────────────────────────────────────────────────────────
    def build_table():
        from ..utils import VERBOSE
        dot   = "[bright_black]·[/bright_black]"
        total = len(domains)

        with state_lock:
            succeeded = sum(1 for s in states.values() if s["phase"] == "success")
            failed    = sum(1 for s in states.values() if s["phase"] in ("failed", "timeout"))
            stopped   = sum(1 for s in states.values() if s["phase"] == "stopped")
            in_prog   = total - succeeded - failed - stopped

        parts = [
            f"[bold white]Backorder Snipe[/bold white]  {dot}  "
            f"[bold cyan]{total} domain{'s' if total > 1 else ''}[/bold cyan]"
        ]
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
        tbl.add_column("Domain",    style="cyan", no_wrap=True)
        tbl.add_column("Phase",     no_wrap=True, min_width=15)
        tbl.add_column("RDAP",      no_wrap=True, min_width=10)
        tbl.add_column("Registrar", style="dim",  no_wrap=True, overflow="ellipsis", min_width=8)
        tbl.add_column("Drop Time", no_wrap=True, min_width=22)
        tbl.add_column("Note",      overflow="ellipsis", ratio=1)

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

            drop_dt   = s["drop_dt"]
            rem       = (drop_dt - utcnow()).total_seconds()
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

        table_lines   = 5 + len(domains)
        panel_overhead = 3
        available_h   = max(1, console.height - table_lines - panel_overhead)

        lines    = list(log_buf)[-available_h:]
        log_text = Text()
        for line in lines:
            log_text.append(f"  [v] {line}\n", style="dim blue")
        log_panel = Panel(log_text, border_style="dim blue", padding=(0, 1))
        return Group(tbl, log_panel)

    # ── 7. Worker thread ───────────────────────────────────────────────────────
    def _interruptible_sleep(secs: float) -> bool:
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline:
            if stop_event.is_set():
                return False
            time.sleep(0.2)
        return True

    def worker(domain: str):
        s = states[domain]

        initial_st, initial_reg = rdap_lookup(domain)
        with state_lock:
            s["rdap_status"]  = initial_st
            s["rdap_checked"] = utcnow()
            if initial_reg is not None:
                s["registrar"] = initial_reg

        if initial_st != "available":
            sleep_secs = (
                s["drop_dt"] - timedelta(seconds=120) - utcnow()
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

            with state_lock:
                s["phase"]         = "polling"
                s["poll_interval"] = POLL_INTERVAL

            is_available = False
            while True:
                if stop_event.is_set():
                    with state_lock:
                        s["phase"] = "stopped"
                    return
                if utcnow() > s["timeout_dt"]:
                    with state_lock:
                        s["phase"]   = "timeout"
                        s["message"] = "1 h past drop — may have been renewed"
                    return

                new_st, new_reg = rdap_lookup(domain)
                _transient = {"fetch-error", "timeout", "fetching…", "—"}
                with state_lock:
                    prev_st = s["rdap_status"]
                    s["rdap_status"]  = new_st
                    s["rdap_checked"] = utcnow()
                    if new_reg is not None:
                        s["registrar"] = new_reg

                if new_st == "available":
                    is_available = True
                    break

                if (prev_st not in _transient
                        and new_st not in _transient
                        and new_st != prev_st):
                    with state_lock:
                        s["phase"]   = "failed"
                        s["message"] = f"rdap changed: '{prev_st}' → '{new_st}'"
                    bos = load_backorders()
                    bos.append({
                        "domain":         domain,
                        "backordered_at": utcnow().isoformat(),
                        "attempts":       0,
                        "result":         "failed",
                        "reason":         f"rdap status changed without purchase: {prev_st!r} → {new_st!r}",
                    })
                    save_backorders(bos)
                    return

                interval = 15 if new_st in ("fetch-error", "timeout") else POLL_INTERVAL
                with state_lock:
                    s["poll_interval"] = interval
                    s["poll_deadline"] = time.monotonic() + interval
                if not _interruptible_sleep(interval):
                    with state_lock:
                        s["phase"] = "stopped"
                    return

            if not is_available:
                return

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

            if not reg_rl.acquire(stop_event):
                with state_lock:
                    s["phase"] = "stopped"
                return

            op_id, err, fatal = spaceship_register(domain, api_key, api_secret, contact_id)
            if err:
                with state_lock:
                    s["message"] = f"attempt {attempt} error: {err}"
                if fatal:
                    rdap_st, _ = rdap_lookup(domain)
                    if rdap_st != "available":
                        with state_lock:
                            s["phase"]   = "failed"
                            s["message"] = f"fatal error (will not retry): {err}"
                        bos = load_backorders()
                        bos.append({
                            "domain":         domain,
                            "backordered_at": utcnow().isoformat(),
                            "attempts":       attempt,
                            "result":         "failed",
                            "reason":         err,
                        })
                        save_backorders(bos)
                        return
                if attempt < MAX_ATTEMPTS:
                    delay = attempt_delay(attempt)
                    with state_lock:
                        s["message"] = f"attempt {attempt} error: {err}  (retry in {delay}s)"
                    if not _interruptible_sleep(delay):
                        with state_lock:
                            s["phase"] = "stopped"
                        return
                continue

            if not op_id:
                if attempt < MAX_ATTEMPTS:
                    delay = attempt_delay(attempt)
                    with state_lock:
                        s["message"] = f"attempt {attempt}: 202 but no operation ID  (retry in {delay}s)"
                    if not _interruptible_sleep(delay):
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
                    "backordered_at": utcnow().isoformat(),
                    "attempts":       attempt,
                    "result":         "success",
                    "registrar":      "spaceship.dev",
                })
                save_backorders(bos)
                break

            reason = "op failed" if op_result == "failed" else "op timed out"
            if attempt < MAX_ATTEMPTS:
                delay = attempt_delay(attempt)
                with state_lock:
                    s["message"] = f"attempt {attempt}: {reason}  (retry in {delay}s)"
                if not _interruptible_sleep(delay):
                    with state_lock:
                        s["phase"] = "stopped"
                    return
            else:
                with state_lock:
                    s["message"] = f"attempt {attempt}: {reason}"

        if not success:
            with state_lock:
                if s["phase"] not in ("stopped", "success"):
                    s["phase"]   = "failed"
                    s["message"] = f"all {MAX_ATTEMPTS} attempts exhausted"
            if s["phase"] == "failed":
                final_st, final_reg = rdap_lookup(domain)
                if final_st != "available" and final_reg:
                    with state_lock:
                        s["registrar"] = final_reg
                        s["message"]   = f"taken by {final_reg}"
                    bos = load_backorders()
                    bos.append({
                        "domain":         domain,
                        "backordered_at": utcnow().isoformat(),
                        "attempts":       MAX_ATTEMPTS,
                        "result":         "failed",
                        "taken_by":       final_reg,
                    })
                    save_backorders(bos)

    # ── 8. Launch threads and display ──────────────────────────────────────────
    threads = [
        threading.Thread(target=worker, args=(d,), daemon=True)
        for d in domains
    ]

    if daemon:
        # Headless daemon: log phase transitions to stdout (→ log file)
        def _ts():
            return utcnow().strftime("%Y-%m-%d %H:%M:%S")

        BACKORDER_PID_FILE.write_text(str(os.getpid()))
        print(
            f"[{_ts()}] PDT backorder daemon started (PID {os.getpid()})"
            f" — {', '.join(domains)}",
            flush=True,
        )
        prev_phases = {d: None for d in domains}
        for t in threads:
            t.start()
        try:
            while any(t.is_alive() for t in threads):
                with state_lock:
                    snapshot = {d: dict(s) for d, s in states.items()}
                for domain in domains:
                    s  = snapshot[domain]
                    ph = s["phase"]
                    if ph != prev_phases[domain]:
                        prev_phases[domain] = ph
                        msg = s.get("message", "")
                        print(
                            f"[{_ts()}] {domain}: {ph}"
                            + (f" — {msg}" if msg else ""),
                            flush=True,
                        )
                time.sleep(1.0)
            # Final summary
            with state_lock:
                for domain in domains:
                    s   = states[domain]
                    ph  = s["phase"]
                    msg = s.get("message", "")
                    print(
                        f"[{_ts()}] FINAL {domain}: {ph}"
                        + (f" — {msg}" if msg else ""),
                        flush=True,
                    )
        except KeyboardInterrupt:
            stop_event.set()
            for t in threads:
                t.join(timeout=2.0)
            print(f"[{_ts()}] PDT backorder daemon stopped.", flush=True)
        finally:
            BACKORDER_PID_FILE.unlink(missing_ok=True)
        print(f"[{_ts()}] PDT backorder daemon finished.", flush=True)

    else:
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


@click.command("register")
@click.argument("domain")
def register_domain(domain):
    """Register a domain immediately via Spaceship and record it in backorders.

    DOMAIN can be a domain name or a 1-based index from 'pdt list'.
    The domain must already be available. On success it is saved to the
    successful backorders list (visible with pdt list --backorders).
    """
    cfg = load_config()
    if domain.isdigit():
        tracked  = load()
        resolved = resolve_targets((domain,), tracked)
        domain   = resolved[0]
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

    console.print(
        "[dim]Note: domain registration requires the domains:billing scope on your "
        "Spaceship API key. If registration fails with HTTP 403, verify your key's "
        "scopes at https://www.spaceship.com/application/api-manager/[/dim]"
    )

    contact_id = spaceship_ensure_contact(cfg, api_key, api_secret)

    avail, avail_result, _ = spaceship_check_available(domain, api_key, api_secret)
    if not avail:
        console.print(
            f"[red]Domain [cyan]{domain}[/cyan] is not available for registration:[/red] {avail_result}"
        )
        sys.exit(1)

    MAX_ATTEMPTS = 15
    console.print(f"[bold]Registering[/bold] [cyan]{domain}[/cyan]…")
    op_id = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        op_id, err, fatal = spaceship_register(domain, api_key, api_secret, contact_id)
        if op_id:
            break
        if fatal:
            console.print(
                f"[red]Registration failed with a fatal error (will not retry):[/red] {err}"
            )
            sys.exit(1)
        if err:
            if attempt == MAX_ATTEMPTS:
                console.print(
                    f"[red]Registration failed after {MAX_ATTEMPTS} attempts:[/red] {err}"
                )
                sys.exit(1)
            delay = min(2 ** (attempt - 1), 900)
            console.print(
                f"[yellow]Attempt {attempt}/{MAX_ATTEMPTS} failed:[/yellow] {err}  "
                f"[dim](retry in {delay}s)[/dim]"
            )
            time.sleep(delay)
    if not op_id:
        console.print("[red]Got 202 but no operation ID in response.[/red]")
        sys.exit(1)

    console.print(f"[dim]Waiting for async operation [bold]{op_id}[/bold]…[/dim]")
    ASYNC_TIMEOUT  = 120
    ASYNC_INTERVAL = 5
    deadline = time.monotonic() + ASYNC_TIMEOUT
    result = None
    while time.monotonic() < deadline:
        op_st = spaceship_poll_async(op_id, api_key, api_secret)
        if op_st in ("success", "failed"):
            result = op_st
            break
        time.sleep(ASYNC_INTERVAL)

    if result == "success":
        console.print(f"[bold green]Registered[/bold green] [cyan]{domain}[/cyan] successfully.")
        bos = load_backorders()
        bos.append({
            "domain":         domain,
            "backordered_at": utcnow().isoformat(),
            "attempts":       1,
            "result":         "success",
            "registrar":      "spaceship.dev",
        })
        save_backorders(bos)
        tracked = load()
        entry   = find_domain(tracked, domain)
        if entry is not None:
            entry["status"] = "registered"
            save(tracked)
    elif result == "failed":
        console.print(f"[red]Registration operation failed for[/red] [cyan]{domain}[/cyan].")
        sys.exit(1)
    else:
        console.print(
            f"[yellow]Operation timed out after {ASYNC_TIMEOUT}s.[/yellow] "
            "Check Spaceship dashboard."
        )
        sys.exit(1)


@click.command("backorder-stop")
def backorder_stop():
    """Stop the background backorder daemon."""
    import signal
    if not BACKORDER_PID_FILE.exists():
        console.print("[yellow]No backorder daemon running.[/yellow]")
        return
    try:
        pid = int(BACKORDER_PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        BACKORDER_PID_FILE.unlink(missing_ok=True)
        console.print(f"[green]✓ Backorder daemon stopped[/green] (PID {pid})")
    except ProcessLookupError:
        console.print("[yellow]Backorder daemon was not running (stale PID file cleared).[/yellow]")
        BACKORDER_PID_FILE.unlink(missing_ok=True)
    except PermissionError:
        console.print("[red]Cannot stop backorder daemon — process owned by another user.[/red]")
    except ValueError:
        console.print("[yellow]Backorder daemon was not running (stale PID file cleared).[/yellow]")
        BACKORDER_PID_FILE.unlink(missing_ok=True)


@click.command("backorder-logs")
@click.option("-n", "--lines", default=50, show_default=True, metavar="N",
              help="Number of recent log lines to show")
@click.option("-f", "--follow", is_flag=True,
              help="Follow the log in real time (like tail -f)")
def backorder_logs(lines, follow):
    """Show or follow the backorder daemon log.

    \b
    Examples:
      pdt backorder-logs
      pdt backorder-logs -n 100
      pdt backorder-logs -f
    """
    if not BACKORDER_LOG_FILE.exists():
        console.print(
            "[dim]No backorder log found. Start the daemon with: pdt backorder -d DOMAIN[/dim]"
        )
        return

    if follow:
        try:
            subprocess.run(["tail", "-f", "-n", str(lines), str(BACKORDER_LOG_FILE)])
        except FileNotFoundError:
            console.print("[red]tail not found — cannot follow log.[/red]")
        except KeyboardInterrupt:
            pass
        return

    text       = BACKORDER_LOG_FILE.read_text()
    tail_lines = text.splitlines()[-lines:]
    for line in tail_lines:
        console.print(line)


@click.command("available")
@click.argument("targets", nargs=-1, required=True)
@click.option("-m", "--machine", is_flag=True, default=False,
              help="Machine-readable CSV output (no Rich markup).")
def available(targets, machine):
    """Check domain availability via Spaceship.

    TARGETS can be domain names, 1-based list indices, or a mix of both.

    \b
    Examples:
      pdt available erisis.com example.net
      pdt available 1 3
      pdt av erisis.com 2
    """
    cfg        = load_config()
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

    tracked = load()
    domains = resolve_targets(targets, tracked, allow_untracked=True)

    if machine:
        print("domain,result,is_premium")

    for i, domain in enumerate(domains):
        if i > 0:
            time.sleep(1)

        is_avail, result, is_premium = spaceship_check_available(domain, api_key, api_secret)

        if machine:
            print(f"{domain},{result},{str(is_premium).lower()}")
        else:
            premium_suffix = "  [dim](premium)[/dim]" if is_premium else ""
            if is_avail:
                console.print(
                    f"[bold green]✓[/bold green]  {domain:<30} [bold green]available[/bold green]{premium_suffix}"
                )
            elif result == "check-error" or result.startswith("http-"):
                console.print(
                    f"[dim red]?[/dim red]  {domain:<30} [dim red]{result}[/dim red]{premium_suffix}"
                )
            else:
                console.print(
                    f"[bold red]✗[/bold red]  {domain:<30} [bold red]{result}[/bold red]{premium_suffix}"
                )
