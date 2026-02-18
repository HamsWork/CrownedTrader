"""
Management command to sync positions between CrownedTrader and Interactive Brokers.

Fetches current positions from TWS/IB Gateway and reports drift vs our system positions.
Use this to keep the system and IBKR aligned (e.g. after manual trades in TWS or missed pushes).

Requires: IBKR running (TWS or IB Gateway) with API enabled.
Environment: IBKR_HOST, IBKR_PORT, IBKR_CLIENT_ID (IBKR_ENABLED not required for sync).

Example:
  python manage.py sync_ibkr
  python manage.py sync_ibkr --user myuser
"""
from django.core.management.base import BaseCommand

from signals.ibkr import sync_positions_from_ibkr


class Command(BaseCommand):
    help = (
        "Fetch positions from IBKR and report drift vs system Position records. "
        "Does not place orders."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--user",
            type=str,
            default=None,
            help="Username to compare system positions for (optional).",
        )

    def handle(self, *args, **options):
        username = options.get("user")
        user = None
        if username:
            from django.contrib.auth.models import User
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                self.stderr.write(self.style.ERROR(f"User '{username}' not found."))
                return

        result = sync_positions_from_ibkr(user=user)
        ibkr_positions = result.get("ibkr_positions") or []
        drift = result.get("drift") or []
        errors = result.get("errors") or []

        if errors:
            for e in errors:
                self.stderr.write(self.style.ERROR(str(e)))

        self.stdout.write(f"IBKR positions: {len(ibkr_positions)}")
        for p in ibkr_positions:
            self.stdout.write(
                f"  {p.get('symbol')} ({p.get('asset_class')}) pos={p.get('position')} avgCost={p.get('avgCost')} account={p.get('account')}"
            )

        if drift:
            self.stdout.write(self.style.WARNING("Drift (system vs IBKR):"))
            for d in drift:
                self.stdout.write(self.style.WARNING(f"  {d}"))
        else:
            self.stdout.write(self.style.SUCCESS("No drift reported."))
