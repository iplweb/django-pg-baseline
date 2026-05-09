# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-05-09

### Added

- `baseline_update` management command and `rebuild.update_baseline()`
  function. Loads the existing `baseline.sql` into the testcontainer
  before `migrate`, so Django's `post_migrate` preserves existing
  auto-increment IDs (permission / content_type rows) via
  `get_or_create`. Routine refreshes after adding migrations now
  produce minimal git diffs instead of ~1500–2000-line churn from
  auth/contenttype ID drift. See README for the
  `baseline_rebuild` vs `baseline_update` workflow.
- `rebuild._load_baseline_into_container()` helper — streams a SQL
  file into the testcontainer's PG via `docker exec -i ... psql`.

## [0.1.0] - 2026-05-08

### Added

- Initial extraction from BPP (`bpp/src/django_pg_baseline/`).
- `BaselineConfig` typed loader for `settings.PG_BASELINE`.
- `loader.load_baseline()` — stdlib-only `psql` shell-out.
- `loader.baseline_needed()` — `to_regclass('public.django_migrations')` probe.
- `patches.install_test_db_patch()` — idempotent monkey patch on
  `BaseDatabaseCreation._create_test_db`.
- `freshness.check_freshness()` — informational migration delta report
  (no enforcement; the project decides when to rebuild).
- `writer.write_meta()` — emits `baseline.meta.json` with `meta_version=1`,
  git SHA, PG version, per-app last migration.
- `rebuild.rebuild_baseline()` — orchestrates a full regeneration via
  `testcontainers`; runs `pg_dump` inside the container to guarantee
  client/server major-version match; scrubs PG17→PG16-incompatible
  lines.
- Management commands: `baseline_load`, `baseline_info`, `baseline_rebuild`.
- `AppConfig.ready()` auto-installs the test-DB monkey patch when
  `PG_BASELINE` is configured.
- Optional pytest plugin for projects that prefer not to add the app to
  `INSTALLED_APPS`.
- `get_baseline_path()` — top-level stable export for downstream
  consumers (e.g. `pytest-testcontainers-django`).
- `DJANGO_PG_BASELINE_SQL_PATH` environment variable override for
  `get_baseline_path()` resolution.
- `_django_pg_baseline_seeded` marker on `DATABASES['default']['TEST']`
  — explicit coordination flag with `pytest-testcontainers-django`.

[Unreleased]: https://github.com/iplweb/django-pg-baseline/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/iplweb/django-pg-baseline/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/iplweb/django-pg-baseline/releases/tag/v0.1.0
