import sys
from datetime import timedelta

import click
from rich import box
from rich.text import Text

from ..constants import NOTIFY_WINDOW
from ..rdap import rdap_lookup
from ..storage import archive_expired, load, save
from ..utils import (
    console,
    find_domain,
    fmt_duration,
    parse_duration,
    remaining,
    resolve_targets,
    status_style,
    to_local,
    utcnow,
)


@click.command("add")
@click.argument("domains", nargs=-1)
@click.option("-t", "--time", "duration", default=None, metavar="DURATION",
              help="Time until drop, e.g. 1d3h57m")
@click.option("-a", "--appraisal", type=float, default=None, metavar="USD",
              help="Estimated value in USD")
@click.option("-n", "--note", default="", help="Freeform note")
@click.option("-s", "--status", default=None,
              help="RDAP status (auto-fetched if omitted)")
def add(domains, duration, appraisal, note, status):
    """Add one or more domains to track. Reads from stdin if no domains given."""
    if domains:
        names = list(domains)
    elif not sys.stdin.isatty():
        names = [line.strip() for line in sys.stdin if line.strip()]
    else:
        console.print("[red]Provide a domain name or pipe domains via stdin.[/red]")
        sys.exit(1)

    secs = None
    drop_time = None
    if duration:
        try:
            secs = parse_duration(duration)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(1)
        drop_time = (utcnow() + timedelta(seconds=secs)).isoformat()

    tracked = load()

    for name in names:
        domain = name.lower().strip()
        if not domain:
            continue

        if any(d["domain"] == domain for d in tracked):
            console.print(f"[yellow]Already tracking [bold]{domain}[/bold][/yellow]")
            continue

        reg = None
        st = status
        if st is None:
            with console.status(f"[dim]Fetching RDAP for {domain}…[/dim]"):
                st, reg = rdap_lookup(domain)

        tracked.append({
            "domain":    domain,
            "drop_time": drop_time,
            "appraisal": appraisal,
            "note":      note,
            "status":    st,
            "registrar": reg,
            "added_at":  utcnow().isoformat(),
            "notified":  False,
        })

        st_style  = status_style(st)
        drop_info = f"— drops in [white]{fmt_duration(secs)}[/white] " if secs is not None else ""
        console.print(
            f"[green]✓ Added[/green] [bold cyan]{domain}[/bold cyan] "
            f"{drop_info}— status: [{st_style}]{st}[/{st_style}]"
        )

    save(tracked)


@click.command("rm")
@click.argument("targets", nargs=-1, required=True)
def remove(targets):
    """Remove domains by name or list index (e.g. pdt rm 1 3 example.com)."""
    domains = load()
    names = resolve_targets(targets, domains)
    new = [d for d in domains if d["domain"] not in names]
    save(new)
    for name in names:
        console.print(f"[green]✓ Removed[/green] [bold]{name}[/bold]")


@click.command("flag")
@click.argument("targets", nargs=-1, required=True)
def flag(targets):
    """Toggle the flag on one or more domains by name or index (highlighted in list)."""
    tracked = load()
    names = resolve_targets(targets, tracked)
    for name in names:
        entry = find_domain(tracked, name)
        entry["flagged"] = not entry.get("flagged", False)
        state = "[yellow]⚑ flagged[/yellow]" if entry["flagged"] else "[dim]unflagged[/dim]"
        console.print(f"[bold cyan]{name}[/bold cyan] → {state}")
    save(tracked)


@click.command("update")
@click.argument("targets", nargs=-1, required=True)
@click.option("-t", "--time", "duration", default=None, metavar="DURATION",
              help="New drop time, e.g. 45m")
@click.option("-a", "--appraisal", type=float, default=None)
@click.option("-n", "--note", default=None)
@click.option("-s", "--status", default=None)
def update(targets, duration, appraisal, note, status):
    """Update fields for one or more tracked domains by name or index.

    \b
    Examples:
      pdt update example.com -t 3h
      pdt update 1 2 3 -t 3h4m
      pdt update example.com other.net -a 500
    """
    if not any(v is not None for v in (duration, appraisal, note, status)):
        console.print("[yellow]Nothing to update. Pass at least one option.[/yellow]")
        return

    drop_time = None
    secs = None
    if duration is not None:
        try:
            secs = parse_duration(duration)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(1)
        drop_time = (utcnow() + timedelta(seconds=secs)).isoformat()

    domains = load()
    names = resolve_targets(targets, domains)

    for name in names:
        entry = find_domain(domains, name)
        changes = []
        if drop_time is not None:
            entry["drop_time"] = drop_time
            entry["notified"] = False
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
        console.print(
            f"[green]✓ Updated[/green] [bold cyan]{name}[/bold cyan] — " + ", ".join(changes)
        )

    save(domains)


@click.command("list")
@click.option("-m", "--machine", is_flag=True, help="CSV output for scripting")
@click.option("--sort", default="time",
              type=click.Choice(["time", "appraisal", "domain", "status"]),
              show_default=True, help="Sort order")
@click.option("-A", "--archived", "show_archived", is_flag=True,
              help="Show archived domains (dropped >24h ago)")
@click.option("-b", "--backorders", "show_backorders", is_flag=True,
              help="Show registered/attempted backorders")
def list_domains(machine, sort, show_archived, show_backorders):
    """List all tracked domains."""
    from ..storage import load_backorders
    from datetime import datetime

    if show_backorders:
        bos = load_backorders()
        if not bos:
            console.print("[dim]No backorder records yet.[/dim]")
            return
        if machine:
            click.echo("domain,result,attempts,registered_at,registrar,taken_by,reason")
            for b in bos:
                registrar = (b.get("registrar") or "").replace(",", ";")
                taken_by  = (b.get("taken_by")  or "").replace(",", ";")
                reason    = (b.get("reason")    or "").replace(",", ";")
                click.echo(
                    f"{b['domain']},{b['result']},{b.get('attempts', '')},"
                    f"{b['backordered_at']},{registrar},{taken_by},{reason}"
                )
            return
        table = _build_backorders_table(bos)
        console.print(table)
        total = len(bos)
        succ  = sum(1 for b in bos if b["result"] == "success")
        console.print(f"[dim]  {total} record{'s' if total != 1 else ''} · {succ} successful[/dim]")
        return

    archive_expired()
    all_domains = load()

    domains = [d for d in all_domains if bool(d.get("archived")) == show_archived]

    if not domains:
        if show_archived:
            console.print("[dim]No archived domains.[/dim]")
        else:
            console.print("[dim]No domains tracked. Use [bold]pdt add[/bold] to get started.[/dim]")
        return

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
                ago       = int(-remaining(d))
                appr      = d.get("appraisal") or ""
                note      = (d.get("note")      or "").replace(",", ";")
                reg       = (d.get("registrar") or "").replace(",", ";")
                drop_str  = d.get("drop_time") or ""
                click.echo(f"{d['domain']},{ago},{drop_str},{appr},{d['status']},{reg},{note}")
        else:
            click.echo("domain,remaining_seconds,drop_time_utc,appraisal,status,registrar,note")
            for d in domains:
                r        = int(remaining(d))
                appr     = d.get("appraisal") or ""
                note     = (d.get("note")      or "").replace(",", ";")
                reg      = (d.get("registrar") or "").replace(",", ";")
                drop_str = d.get("drop_time") or ""
                click.echo(f"{d['domain']},{r},{drop_str},{appr},{d['status']},{reg},{note}")
        return

    w = console.width

    tiny  = w < 60
    small = w < 85

    show_index     = not tiny
    show_drop_time = not small
    show_appraisal = w >= 68
    show_registrar = w >= 110
    show_note      = w >= 130

    table_box   = box.SIMPLE_HEAD if tiny else box.ROUNDED
    table_title = (
        None if tiny else
        ("[bold white]Archived Domains[/bold white]" if show_archived
         else "[bold white]Tracked Domains[/bold white]")
    )

    table = _build_domain_table(
        domains, show_archived, show_index, show_drop_time, show_appraisal,
        show_registrar, show_note, table_box, table_title, tiny,
    )
    console.print(table)
    total = len(domains)
    label = "archived domain" if show_archived else "domain"
    console.print(f"[dim]  {total} {label}{'s' if total != 1 else ''} · times in local timezone[/dim]")


def _build_backorders_table(bos):
    from datetime import datetime
    from rich.table import Table

    tbl = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold magenta",
        border_style="bright_black",
        title="[bold white]Backorder Records[/bold white]",
        title_style="bold",
        expand=True,
    )
    tbl.add_column("Domain", style="bold cyan", no_wrap=True, min_width=14)
    tbl.add_column("Result", no_wrap=True, min_width=8)
    tbl.add_column("Attempts", justify="right", no_wrap=True, min_width=8)
    tbl.add_column("Registered At", justify="center", no_wrap=True, min_width=18)
    tbl.add_column("Detail", style="dim", ratio=1, overflow="ellipsis")
    for b in bos:
        result = b["result"]
        result_cell = (
            Text("success", style="bold green")
            if result == "success"
            else Text("failed", style="bold red")
        )
        detail = b.get("registrar") or b.get("taken_by") or b.get("reason") or "—"
        ts = b.get("backordered_at", "")
        try:
            dt = datetime.fromisoformat(ts)
            ts = to_local(dt).strftime("%b %d  %H:%M:%S")
        except Exception:
            pass
        tbl.add_row(b["domain"], result_cell, str(b.get("attempts", "—")), ts, detail)
    return tbl


def _build_domain_table(
    domains, show_archived, show_index, show_drop_time, show_appraisal,
    show_registrar, show_note, table_box, table_title, tiny,
):
    from datetime import datetime
    from rich.table import Table

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

    domain_max = max(16, console.width // 3)
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
        has_drop_time = bool(d.get("drop_time"))
        drop_dt = datetime.fromisoformat(d["drop_time"]) if has_drop_time else None
        appraisal = f"${d['appraisal']:,.0f}" if d.get("appraisal") else "—"
        st = d.get("status", "unknown")

        local_dt = to_local(drop_dt) if drop_dt else None
        if show_archived:
            ago       = -rem
            time_cell = Text(fmt_duration(ago) + " ago", style="dim")
            date_str  = local_dt.strftime("%b %d  %H:%M") if local_dt else "—"
        else:
            if not has_drop_time:
                time_cell = Text("—", style="dim")
            elif rem <= 0:
                time_cell = Text("AVAILABLE", style="bold green blink")
            elif rem < NOTIFY_WINDOW:
                time_cell = Text(fmt_duration(rem), style="bold red")
            elif rem < 3600:
                time_cell = Text(fmt_duration(rem), style="yellow")
            else:
                time_cell = Text(fmt_duration(rem), style="white")
            date_str = local_dt.strftime("%b %d  %H:%M") if local_dt else "—"

        flagged     = d.get("flagged", False)
        domain_cell = Text(
            ("⚑ " if flagged else "") + d["domain"],
            style="bold yellow" if flagged else "bold cyan",
        )

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

    return table


@click.command("next")
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
    actual  = len(domains)

    if machine:
        click.echo("domain,remaining_seconds,drop_time_utc,appraisal,status")
        for d in domains:
            drop_str = d.get("drop_time") or ""
            click.echo(
                f"{d['domain']},{int(remaining(d))},{drop_str},"
                f"{d.get('appraisal', '')},{d['status']}"
            )
        return

    from rich.panel import Panel

    w = console.width
    show_appraisal = w >= 65
    show_status    = w >= 55
    show_note      = w >= 90

    console.print(
        Panel(
            f"[bold white]Next {actual} Dropping[/bold white]",
            style="cyan",
            expand=True,
        )
    )
    for i, d in enumerate(domains, 1):
        rem     = remaining(d)
        rem_str = fmt_duration(rem)
        st      = d.get("status", "unknown")

        parts = [
            f"  [dim]{i}.[/dim] [bold cyan]{d['domain']}[/bold cyan]",
            f"[white]{rem_str}[/white]",
        ]
        if show_appraisal:
            appraisal = (
                f"[green]${d['appraisal']:,.0f}[/green]"
                if d.get("appraisal") else "[dim]—[/dim]"
            )
            parts.append(appraisal)
        if show_status:
            parts.append(f"[{status_style(st)}]{st}[/{status_style(st)}]")
        if show_note and d.get("note"):
            parts.append(f"[dim]{d['note']}[/dim]")

        console.print("  ".join(parts))
