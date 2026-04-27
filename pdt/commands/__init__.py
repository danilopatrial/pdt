import sys

import click

from ..constants import VERSION
from ..logger import get_logger
from ..utils import set_verbose, set_redacted


@click.group()
@click.version_option(VERSION, prog_name="pdt")
@click.option("-v", "--verbose", is_flag=True, default=False,
              help="Show detailed output including API requests and responses.")
@click.option("-R", "--redacted", is_flag=True, default=False,
              help="Redact domain names in output (for screenshots).")
@click.pass_context
def cli(ctx, verbose, redacted):
    """PDT — Pending Delete Domain Tracker

    Track domains in pending-delete and get desktop notifications
    5 minutes before they become available.
    """
    set_verbose(verbose)
    set_redacted(redacted)
    # Log every invocation (skip --version which exits before subcommand)
    if ctx.invoked_subcommand:
        argv = " ".join(sys.argv[1:])
        get_logger().info(f"INVOKE  pdt {argv}")


# Register all commands
from .domains import add, flag, list_domains, next_cmd, remove, update
from .appraise import appraise, rdap
from .watch import logs, poll, status, stop, watch
from .backorder import available, backorder, backorder_logs, backorder_stop, register_domain
from .config import config

cli.add_command(add)
cli.add_command(remove,          name="rm")
cli.add_command(flag)
cli.add_command(update)
cli.add_command(list_domains,    name="list")
cli.add_command(next_cmd,        name="next")
cli.add_command(appraise)
cli.add_command(rdap)
cli.add_command(poll)
cli.add_command(watch)
cli.add_command(stop)
cli.add_command(status)
cli.add_command(logs)
cli.add_command(backorder)
cli.add_command(backorder_stop)
cli.add_command(backorder_logs)
cli.add_command(register_domain, name="register")
cli.add_command(available)
cli.add_command(available,       name="av")
cli.add_command(config)
