import click

from ..config import load_config, save_config
from ..constants import CONFIG_FILE
from ..utils import console


@click.command("config")
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
        "atom_token":           atom_token,
        "atom_user_id":         atom_user_id,
        "spaceship_api_key":    spaceship_api_key,
        "spaceship_api_secret": spaceship_api_secret,
        "spaceship_first":      spaceship_first,
        "spaceship_last":       spaceship_last,
        "spaceship_email":      spaceship_email,
        "spaceship_phone":      spaceship_phone,
        "spaceship_address":    spaceship_address,
        "spaceship_city":       spaceship_city,
        "spaceship_state":      spaceship_state,
        "spaceship_zip":        spaceship_zip,
        "spaceship_country":    spaceship_country,
    }
    if not any(v is not None for v in new_opts.values()):
        console.print("[yellow]Nothing to set. Pass at least one option.[/yellow]")
        return

    for key, val in new_opts.items():
        if val is not None:
            cfg[key] = val

    save_config(cfg)
    console.print(f"[green]✓ Config saved[/green] → {CONFIG_FILE}")
