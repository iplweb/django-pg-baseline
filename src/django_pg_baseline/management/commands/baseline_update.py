"""Update ``baseline.sql`` in place by replaying migrations on top of it.

See :func:`django_pg_baseline.rebuild.update_baseline` for the design
rationale (preserves auto-increment IDs by seeding the testcontainer
with the prior dump before ``migrate`` runs).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from django_pg_baseline.conf import get_config
from django_pg_baseline.rebuild import update_baseline, with_overrides


class Command(BaseCommand):
    help = (
        "Update baseline.sql in place, preserving auto-increment IDs. "
        "Loads the existing baseline into a testcontainer, applies "
        "migrations, re-dumps. Run baseline_rebuild instead for a full "
        "from-scratch reset."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--image",
            default=None,
            help="Override PG_BASELINE['REBUILD_IMAGE'].",
        )
        parser.add_argument(
            "--baseline-dir",
            default=None,
            help="Override PG_BASELINE['BASELINE_DIR'].",
        )

    def handle(self, *args, image=None, baseline_dir=None, **options):
        config = with_overrides(
            get_config(),
            rebuild_image=image,
            baseline_dir=baseline_dir,
        )
        if not config.sql_path.exists():
            raise CommandError(
                f"baseline_update requires an existing {config.sql_path}; "
                f"run `manage.py baseline_rebuild` first to create it."
            )
        self.stdout.write(f"Updating baseline using image {config.rebuild_image}...")
        update_baseline(config)
        self.stdout.write(self.style.SUCCESS("Baseline updated."))
        self.stdout.write(f"  sql:  {config.sql_path}")
        self.stdout.write(f"  meta: {config.meta_path}")
