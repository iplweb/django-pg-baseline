"""Show a readable summary of the current baseline state.

Informational only — always exits 0. The package no longer enforces
freshness; when to rebuild is the project's call.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from django_pg_baseline.conf import get_config
from django_pg_baseline.freshness import check_freshness


class Command(BaseCommand):
    help = "Print a summary of the baseline state and current per-app deltas."

    def handle(self, *args, **options):
        config = get_config()
        self.stdout.write(f"sql_path:         {config.sql_path}")
        self.stdout.write(f"meta_path:        {config.meta_path}")

        if not config.sql_path.exists():
            self.stdout.write(
                self.style.WARNING(f"\nbaseline.sql is missing at {config.sql_path}.")
            )
            self.stdout.write(
                "Run `manage.py baseline_rebuild` to generate it, or "
                "remove PG_BASELINE from settings to disable."
            )

        if not config.meta_path.exists():
            self.stdout.write(
                self.style.WARNING(
                    f"\nbaseline.meta.json is missing at {config.meta_path}."
                )
            )
            self.stdout.write(
                "Run `manage.py baseline_rebuild` to generate it. "
                "(Without meta.json there is no per-app delta to report.)"
            )
            return

        report = check_freshness(config.meta_path)
        self.stdout.write(f"git_sha:          {report.git_sha}")
        self.stdout.write(f"postgres_version: {report.meta.get('postgres_version')}")
        self.stdout.write(f"meta_version:     {report.meta.get('meta_version', 1)}")
        self.stdout.write(
            f"\nWorst delta:      {report.worst_delta} ({report.worst_app})"
        )
        self.stdout.write("\nPer-app deltas (newer on disk than baseline):")
        for app, delta in sorted(report.deltas.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {app:40s} +{delta}")
