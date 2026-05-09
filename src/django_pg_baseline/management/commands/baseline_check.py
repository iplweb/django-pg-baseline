"""Print the baseline freshness report; optionally gate CI on a delta threshold.

By default this command is informational and exits 0 (same data as
``baseline_info``, formatted for grep-friendly CI logs). Pass
``--max-delta N`` to make the command exit 1 when any per-app delta
exceeds ``N`` — useful as a CI guard that reminds the team to run
``baseline_update`` once drift crosses a threshold.

Computing the delta does **not** rebuild anything: it diffs the
``last_migration`` map recorded in ``baseline.meta.json`` against
``MigrationLoader.disk_migrations``. Runs in milliseconds.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from django_pg_baseline.conf import get_config
from django_pg_baseline.freshness import check_freshness


class Command(BaseCommand):
    help = (
        "Report baseline freshness (per-app migration delta). "
        "Exits 0 by default; with --max-delta N exits 1 when any "
        "app's delta exceeds N."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-delta",
            type=int,
            default=None,
            help=(
                "Exit 1 when any per-app delta exceeds this value. "
                "Omit for informational mode (always exits 0)."
            ),
        )

    def handle(self, *args, max_delta=None, **options):
        config = get_config()

        if not config.sql_path.exists():
            raise CommandError(
                f"baseline.sql is missing at {config.sql_path}. "
                f"Run `manage.py baseline_rebuild` to generate it, "
                f"or remove PG_BASELINE from settings to disable."
            )

        if not config.meta_path.exists():
            raise CommandError(
                f"baseline.meta.json is missing at {config.meta_path}. "
                f"Run `manage.py baseline_rebuild` to generate it."
            )

        report = check_freshness(config.meta_path)

        self.stdout.write(f"git_sha:     {report.git_sha}")
        self.stdout.write(f"worst_delta: {report.worst_delta} ({report.worst_app})")
        if max_delta is not None:
            self.stdout.write(f"max_delta:   {max_delta}")
        self.stdout.write("\nPer-app deltas (newer on disk than baseline):")
        for app, delta in sorted(report.deltas.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {app:40s} +{delta}")

        if max_delta is not None and report.worst_delta > max_delta:
            raise CommandError(
                f"Baseline is stale: worst per-app delta is "
                f"{report.worst_delta} ({report.worst_app}), "
                f"exceeds --max-delta={max_delta}. "
                f"Run `manage.py baseline_update` to refresh."
            )

        self.stdout.write(self.style.SUCCESS("\nBaseline freshness OK."))
