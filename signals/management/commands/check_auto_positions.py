"""
Management command to check open positions with automatic tracking and post exit
when take profit or stop loss level is hit. Run periodically (e.g. every 1–2 minutes)
via cron for real-time tracking, or rely on the background thread started on app load.

Example cron (every 2 minutes) - optional if using background thread:
  */2 * * * * cd /path/to/project && python manage.py check_auto_positions
"""
from django.core.management.base import BaseCommand

from signals.auto_tracking import run_auto_tracking_check


class Command(BaseCommand):
    help = (
        "Check open positions with automatic tracking; post TP/SL exit to Discord "
        "when current price hits take profit or stop loss level."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only log what would be done; do not send Discord or update DB.",
        )

    def handle(self, *args, **options):
        dry_run = options.get("dry_run", False)
        if dry_run:
            self.stdout.write("Dry run: no Discord send or DB updates.")
        run_auto_tracking_check(dry_run=dry_run)
