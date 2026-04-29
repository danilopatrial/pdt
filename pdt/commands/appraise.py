import sys

import click

from ..spaceship import atom_appraise
from ..rdap import rdap_lookup
from ..logger import log_event
from ..storage import load, save
from ..utils import console, find_domain, redact_domain, resolve_targets, status_style
from ..config import load_config


@click.command("appraise")
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
    cfg     = load_config()
    token   = token   or cfg.get("atom_token")
    user_id = user_id or cfg.get("atom_user_id")

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
        targets = resolve_targets(domains, tracked)
    else:
        console.print("[red]Provide at least one domain, or use --all[/red]")
        sys.exit(1)

    changed = False
    for name in targets:
        entry = find_domain(tracked, name)
        if entry and entry.get("appraisal"):
            console.print(
                f"  [bold cyan]{redact_domain(name)}[/bold cyan] — already appraised at "
                f"[green]${entry['appraisal']:,.0f}[/green], skipping"
            )
            continue

        with console.status(f"[dim]  Appraising {redact_domain(name)}…[/dim]"):
            value = atom_appraise(name, token, user_id)

        result_str = (
            f"[green]${value:,.0f}[/green]"
            if value is not None
            else "[dim red]no value returned[/dim red]"
        )
        console.print(f"  [bold cyan]{redact_domain(name)}[/bold cyan] → {result_str}")

        if entry and value is not None:
            entry["appraisal"] = value
            changed = True
        log_event(
            f"appraise  {name}  value={value}"
            if value is not None else f"appraise  {name}  value=None (no result)"
        )

    if changed:
        save(tracked)
        console.print("\n[green]✓ Saved appraisals[/green]")


@click.command("rdap")
@click.argument("domains", nargs=-1, metavar="[DOMAIN]...")
@click.option("-a", "--all", "all_tracked", is_flag=True,
              help="Fetch status for all tracked domains")
@click.option("--archived", "archived", is_flag=True,
              help="Fetch status for all archived domains")
@click.option("--missing", "only_missing", is_flag=True,
              help="Only domains with no status, a fetch error, or a 429 rate-limit error")
@click.option("--redemption", "only_redemption", is_flag=True,
              help="Only domains currently in redemption period")
@click.option("--available", "only_available", is_flag=True,
              help="Only domains whose stored status is available")
def rdap(domains, all_tracked, archived, only_missing, only_redemption, only_available):
    """Fetch and update RDAP status.

    \b
    Examples:
      pdt rdap example.com
      pdt rdap example.com other.net
      pdt rdap --all
      pdt rdap --all --missing
      pdt rdap --all --redemption
      pdt rdap --all --available
      pdt rdap --archived
    """
    tracked = load()

    if archived:
        pool = [d for d in tracked if d.get("archived")]
        if not pool:
            console.print("[dim]No archived domains.[/dim]")
            return
        targets = [d["domain"] for d in pool]
    elif all_tracked:
        active = [d for d in tracked if not d.get("archived")]
        if not active:
            console.print("[dim]No domains tracked.[/dim]")
            return
        targets = [d["domain"] for d in active]
    elif domains:
        targets = resolve_targets(domains, tracked)
    else:
        console.print("[red]Provide at least one domain, or use --all[/red]")
        sys.exit(1)

    if only_missing or only_redemption or only_available:
        def _match(name):
            entry = find_domain(tracked, name)
            st = (entry.get("status") or "").lower() if entry else ""
            if only_missing:
                return not st or "error" in st or "429" in st or "fetch" in st or "timeout" in st
            if only_redemption:
                return "redemption" in st
            if only_available:
                return "available" in st
        targets = [t for t in targets if _match(t)]
        if not targets:
            console.print("[dim]No domains match that filter.[/dim]")
            return

    changed = False
    for name in targets:
        with console.status(f"[dim]  {redact_domain(name)}…[/dim]"):
            status, registrar = rdap_lookup(name)
        entry      = find_domain(tracked, name)
        untracked  = "" if entry else " [dim](not tracked)[/dim]"
        st         = status_style(status)
        reg_str    = f"  [dim]({registrar})[/dim]" if registrar else ""
        console.print(f"  [bold cyan]{redact_domain(name)}[/bold cyan]{untracked} → [{st}]{status}[/{st}]{reg_str}")
        if entry:
            entry["status"]    = status
            entry["registrar"] = registrar
            changed = True
        log_event(f"rdap  {name}  status={status!r}  registrar={registrar!r}")

    if changed:
        save(tracked)
        if len(targets) > 1:
            console.print(f"\n[green]✓ Updated {len(targets)} domains[/green]")
