# django-pg-baseline

[![tests](https://github.com/iplweb/django-pg-baseline/actions/workflows/tests.yml/badge.svg)](https://github.com/iplweb/django-pg-baseline/actions/workflows/tests.yml)
[![PyPI version](https://img.shields.io/pypi/v/django-pg-baseline.svg)](https://pypi.org/project/django-pg-baseline/)
[![Python versions](https://img.shields.io/pypi/pyversions/django-pg-baseline.svg)](https://pypi.org/project/django-pg-baseline/)
[![Django versions](https://img.shields.io/badge/Django-5.0%20%7C%205.1%20%7C%205.2%20%7C%206.1-blue.svg)](https://pypi.org/project/django-pg-baseline/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> Manage a baseline `pg_dump` for Django test databases — turn N-minute
> `migrate` boots into a few-second `psql` import.

A reusable Django app that manages a `baseline.sql` artifact (a
`pg_dump` of the post-migrate schema + seed data) and loads it
automatically whenever Django creates a test database. `migrate` then
applies only the small delta of migrations added since the dump was
taken.

In real projects with hundreds of migrations this turns a ~6-minute
`migrate` into a ~3-second `psql` import — or sub-second when paired
with a testcontainer that clones from a populated template DB.

## Why

A Django suite with hundreds of migrations and/or non-trivial seed
data spends many minutes per test run on `migrate`. The fix is
well-known in principle:

1. Apply migrations once against a clean PG.
2. `pg_dump` the result.
3. On every subsequent test run, `psql -f` (or
   `CREATE DATABASE ... WITH TEMPLATE`) the dump into the test DB,
   then let `migrate` apply only the small delta of migrations added
   since the dump was taken.

Every Django shop with a heavy migration history rediscovers this
pattern independently. `django-pg-baseline` packages it as a reusable
app with all the operational bits people forget the first time:

- a deterministic, version-controlled `baseline.sql` (with timestamp
  freezing for diff stability),
- a sidecar `baseline.meta.json` recording the highest migration name
  per app, plus git SHA and PG version,
- automatic loading on test DB creation when no faster
  template-clone path is available,
- explicit coordination with [pytest-testcontainers-django] for the
  template-clone path,
- a one-shot `manage.py baseline_rebuild` that spins an isolated PG
  via `testcontainers`, runs `migrate`, and emits the dump.

[pytest-testcontainers-django]: https://github.com/iplweb/pytest-testcontainers-django

## Features

- **One-line setup**: add `"django_pg_baseline"` to `INSTALLED_APPS`,
  set `PG_BASELINE['BASELINE_DIR']`, done.
- **Three usage modes**: standalone (host `psql`), with
  testcontainers (template clone), or rebuild-only (CI cron).
- **Optional pytest plugin** for projects that don't want to add the
  app to `INSTALLED_APPS`.
- **Deterministic dumps**: built-in timestamp freezing produces
  byte-stable diffs across rebuilds.
- **Cross-major PG support**: runs `pg_dump` *inside* the rebuild
  container to guarantee client/server version match; scrubs known
  PG17→PG16 incompatibilities.
- **Stale baseline is fine**: if the dump lags behind HEAD, Django's
  `migrate` applies the delta on top. `manage.py baseline_info` shows
  per-app deltas; the package itself never gates on freshness.
- **psycopg v2 *and* v3** compatible. No runtime psycopg dep — uses
  whichever the host project already pulled in for Django's PG
  backend.

## Installation

### Using uv (recommended)

```bash
uv add django-pg-baseline
```

### Using pip

```bash
pip install django-pg-baseline
```

This package depends on `Django>=5.0` and `testcontainers[postgres]`.
It does **not** declare a runtime psycopg dependency — your Django
project already has either `psycopg`, `psycopg-binary`, `psycopg2`, or
`psycopg2-binary` installed (Django's PG backend requires one), and
forcing a flavor would conflict with that choice.

## Quick start

### 1. Configure

```python
# settings.py
INSTALLED_APPS = [
    ...,
    "django_pg_baseline",
]

PG_BASELINE = {
    "BASELINE_DIR": BASE_DIR / "baseline-sql",
}
```

The directory should be tracked in git — it holds `baseline.sql` and
`baseline.meta.json`, both produced by the `baseline_rebuild`
command.

### 2. Generate the baseline

```bash
python manage.py baseline_rebuild
git add baseline-sql/baseline.sql baseline-sql/baseline.meta.json
git commit -m "chore(baseline): refresh after migrations"
```

This spins a fresh Postgres testcontainer, runs `migrate`, freezes
configured timestamp columns, runs `pg_dump` inside the container,
scrubs PG-version-specific lines, and writes the dump + meta file.

### 3. Run tests

```bash
pytest
```

Django creates the test DB; the monkey patch loads `baseline.sql` via
`psql`; `migrate` applies any post-baseline delta. That's it.

## Three modes of use

### Mode A — Standalone (host `psql`)

The simplest case. Useful when:

- the consumer runs tests against a long-lived PG (host PG, a
  `docker compose` service, a CI service container),
- `psql` is on `PATH`.

What happens at test time:

1. `AppConfig.ready()` installs the `_create_test_db` patch.
2. Django's runner calls `_create_test_db` → `CREATE DATABASE
   test_<name>`.
3. Patch sees `django_migrations` is missing in the new DB →
   `psql -f baseline.sql --single-transaction --quiet -v ON_ERROR_STOP=1`.
4. Django's `migrate` applies any post-baseline delta.

**Note:** if you use a `TEMPLATE` DB (set `TEST.TEMPLATE` in your
`DATABASES`), the test DB user must be granted the `pg_signal_backend`
role — Postgres needs zero connections on the source DB before
`CREATE DATABASE WITH TEMPLATE` is allowed, and the patch terminates
leftover sessions to enforce that.

### Mode B — With pytest-testcontainers-django

Faster (sub-second test-DB creation via template clone). Useful when:

- you accept Docker as a test dependency,
- you want the test DB to be a *clone* of a populated template
  rather than a `psql` reload.

Setup is identical to Mode A. Once
[pytest-testcontainers-django] is installed, it auto-detects this
package via `get_baseline_path()`, mounts `baseline.sql` into the PG
container as `/docker-entrypoint-initdb.d/01-baseline.sql`, and sets
`DATABASES['default']['TEST']['TEMPLATE']` so Django runs
`CREATE DATABASE … WITH TEMPLATE …`.

In Mode B the host `psql` shell-out is **never** invoked. We still
own:

- the patch's "kick sessions off template" prelude,
- `settings.PG_BASELINE` and `get_baseline_path()`,
- `manage.py baseline_rebuild`.

### Mode C — Build/rebuild the baseline (CI or local)

```bash
python manage.py baseline_rebuild
git add path/to/baseline-sql/
git commit -m "chore(baseline): refresh after migrations …"
```

Recommended downstream wiring: a GitHub Action that runs
`baseline_rebuild` whenever `**/migrations/**` changes on the main
branch and opens a PR with the refreshed dump. The package itself
does not enforce any "freshness" policy — when to rebuild is the
project's decision; we just provide the tooling.

## Configuration reference

```python
PG_BASELINE = {
    # REQUIRED. Directory holding baseline.sql + baseline.meta.json.
    "BASELINE_DIR": BASE_DIR / "baseline-sql",

    # Optional, defaults shown.
    "SQL_FILENAME": "baseline.sql",
    "META_FILENAME": "baseline.meta.json",

    # Which Django connection to load into / dump from.
    "DATABASE_ALIAS": "default",

    # Auto-install the _create_test_db monkey patch in
    # AppConfig.ready(). Set to False for manual control (e.g. only
    # under pytest, only on certain CI hosts).
    "AUTO_LOAD_ON_TEST_DB": True,

    # Image used by `baseline_rebuild`. Override for plpython3u,
    # custom locales, extensions, etc.
    "REBUILD_IMAGE": "postgres:16",

    # Extra args appended to the built-in pg_dump invocation. The
    # default invocation already includes --no-owner --no-acl
    # --no-privileges --no-comments and --exclude-table-data=django_session.
    "PG_DUMP_EXTRA_ARGS": ["--exclude-table-data=audit_log"],

    # Stacks ON TOP of the default exclusions. Each entry becomes
    # --exclude-table-data=<pattern>. Cleaner than spelling out
    # --exclude-table-data=... in PG_DUMP_EXTRA_ARGS.
    "PG_DUMP_EXTRA_EXCLUDE_TABLE_DATA": ["django_cache*", "easy_thumbnails_*"],

    # Tables/columns whose timestamps are frozen before pg_dump,
    # for deterministic diffs across rebuilds.
    "FREEZE_TIMESTAMPS": [("django_migrations", ["applied"])],
    "FREEZE_TIMESTAMPS_EXTRA": [("django_template", ["creation_date"])],
    "FREEZE_TIMESTAMP_VALUE": "2000-01-01 00:00:00+00",
}
```

## Management commands

| Command | What it does |
| --- | --- |
| `baseline_load` | Load `baseline.sql` into the configured DB. Skips when `django_migrations` already exists, unless `--force`. |
| `baseline_info` | Human summary: git SHA, PG version, sql/meta paths, plus per-app deltas. Always exits 0. |
| `baseline_rebuild` | **Full rebuild from scratch.** Spins a `testcontainers` PG, runs `migrate` against an *empty* DB, freezes timestamps, runs in-container `pg_dump`, scrubs, writes meta. Auto-increment IDs (permissions, content_types) end up at whatever values `post_migrate` assigns this run — typically drifts between rebuilds, producing large diffs in `baseline.sql`. Use when starting fresh or when the prior dump is no longer loadable. Flags: `--image`, `--baseline-dir`. |
| `baseline_update` | **Incremental update preserving IDs.** Same as `baseline_rebuild`, but loads the existing `baseline.sql` into the testcontainer *before* `migrate`. Django's `post_migrate` then sees existing content_types/permissions via `get_or_create` and keeps their IDs; only genuinely-new rows (for models added by new migrations) get fresh sequential IDs. Use this for routine refreshes after adding migrations — produces minimal git diffs. Errors out when no prior `baseline.sql` exists; run `baseline_rebuild` first. Flags: `--image`, `--baseline-dir`. |

### `baseline_rebuild` vs `baseline_update`

The two commands solve different problems:

- **`baseline_rebuild`** answers the question "what does a fresh
  database look like after running every migration?" Reproducible from
  source code alone. The auto-increment IDs are an implementation
  detail of `post_migrate`'s iteration order; they are stable *within*
  one rebuild but typically shift between rebuilds (different Python
  hash seed, different app-registry traversal, third-party app
  reordering). When this happens, `git diff baseline.sql` shows ~1500–
  2000 lines of churn for the auth/contenttype tables even when no
  models actually changed, drowning the meaningful diff.

- **`baseline_update`** answers the question "what changes when I
  apply only the new migrations on top of the prior baseline?" The
  prior dump is loaded first, so `django_migrations` already records
  every migration baked into it; `migrate` applies only the delta.
  Existing permission/content_type rows keep their IDs (Django's
  `update_contenttypes` and `create_permissions` use `get_or_create`),
  and any FK references to them in other dump rows stay valid. New
  rows for newly-introduced models append at the end of the sequence.
  Net effect: rebuilds with no migration changes produce a
  byte-identical `baseline.sql` (modulo the freeze-timestamp pass);
  rebuilds adding migrations produce a minimal diff confined to the
  actual changes.

Recommended workflow: use `baseline_update` for routine refreshes
after `makemigrations`. Reach for `baseline_rebuild` only when you
genuinely want a clean slate (e.g. you've removed a model and want
its content_type/permission rows pruned, or a Django/Postgres major
upgrade made the prior dump unloadable).

The two commands operate on the same `baseline.sql` artifact; nothing
in the file format distinguishes one from the other. The choice is
purely about the *generation strategy*.

## Pytest plugin (alternative to `INSTALLED_APPS`)

If you'd rather not add the app to `INSTALLED_APPS`, the package
ships a pytest plugin that installs the same monkey patch via
`pytest_configure`:

```toml
# pyproject.toml — pytest auto-discovers the plugin via the
# pytest11 entry point. Nothing else needed.
```

Behaviour matches the `INSTALLED_APPS` route exactly:

- no-op when `DJANGO_SETTINGS_MODULE` is unset,
- no-op when `PG_BASELINE` is unset,
- raises `pytest.UsageError` when `BASELINE_DIR` is configured but
  `baseline.sql` is missing (matching `AppConfig.ready()` policy —
  loud failure beats silent slowness in CI).

Use one route or the other, not both. (Both are idempotent; double
install is safe but pointless.)

## Public API

Stable from v0.1 (the contract surface for downstream tooling such
as `pytest-testcontainers-django`):

```python
from django_pg_baseline import get_baseline_path  # Path | None
```

Reachable via submodules but **not yet locked under semver**
(stabilised at v1.0):

```python
from django_pg_baseline.conf import get_config, BaselineConfig
from django_pg_baseline.patches import install_test_db_patch
from django_pg_baseline.loader import load_baseline, baseline_needed
from django_pg_baseline.freshness import check_freshness, FreshnessReport
```

## Environment variables

| Variable | Effect |
| --- | --- |
| `DJANGO_PG_BASELINE_SQL_PATH` | Override `get_baseline_path()` resolution. Points at a dump file directly, bypassing `settings.PG_BASELINE['BASELINE_DIR']`. Useful for CI pinning a specific baseline. |

## Security note

The dump captures all data present in the testcontainer after
`migrate()`. If your data migrations seed users, fixtures, or any
other content that ends up in the dump, *that data lands in version
control*. Review the dump before committing, especially on the first
rebuild. Use `PG_DUMP_EXTRA_EXCLUDE_TABLE_DATA` to skip tables whose
row data should not ship (e.g. `auth_user` when you have real test
passwords). The package does **not** exclude `auth_user` by default —
projects that intentionally seed admin fixtures rely on that data
being in the baseline.

## Supported versions

### Python

| Python | 3.10 | 3.11 | 3.12 | 3.13 |
|--------|:----:|:----:|:----:|:----:|
|        | ✓    | ✓    | ✓    | ✓    |

### Django × Python

| Django  | 3.10 | 3.11 | 3.12 | 3.13 | Status                                  |
|---------|:----:|:----:|:----:|:----:|-----------------------------------------|
| 5.0     | ✓    | ✓    | ✓    | —    | EOL Apr 2025 — supported on a best-effort basis |
| 5.1     | ✓    | ✓    | ✓    | ✓    | EOL Dec 2025 — supported on a best-effort basis |
| 5.2 LTS | ✓    | ✓    | ✓    | ✓    | Active LTS (extended support to Apr 2028) |
| 6.1     | —    | —    | ✓    | ✓    | Current release — Django 6.x requires Python >= 3.12 |

Django 4.2 is out of scope (LTS goes EOL in April 2026 — the project
targets current Django).

Django 6.0 is not in the CI matrix: this project went straight from
5.2 to 6.1. It very likely works (nothing here touches the APIs 6.0
changed), but it is untested, so it is not claimed as supported.

### PostgreSQL

PostgreSQL 16 and 17. Older PG versions (14, 15) are out of scope:
they're already EOL on the ladder and would complicate
`_scrub_dump` (the list of cross-major incompatibilities to scrub
grows with every PG release we keep alive).

### psycopg

`psycopg2`, `psycopg2-binary`, and `psycopg[binary]>=3` all work —
the package uses whichever your Django project already pulled in for
its PG backend. CI tests both `psycopg2-binary` and `psycopg[binary]`
in separate matrix cells.

### Operating system

Linux is the supported CI target. macOS works in practice for local
development. Windows is not supported — the package shells out to
`psql`/`pg_dump` and assumes POSIX path conventions and a Linux-style
Docker daemon for the rebuild path.

## How it fits with related packages

`django-pg-baseline` is package #3 of the testcontainers-for-Django
family:

1. `pytest-testcontainers` — generic pytest plugin,
   session-scoped Docker container lifecycle. Framework-agnostic.
2. `pytest-testcontainers-django` — Django bridge on top of #1.
   Injects env vars before Django imports settings; supports
   init-script mounts and `DATABASES['default']['TEST']['TEMPLATE']`
   for fast test-DB clone.
3. **`django-pg-baseline`** (this package) — manages the
   `baseline.sql` artifact and provides the patch /
   `get_baseline_path()` contract that #2 reads.

Each package can be used standalone. Pair #3 with #2 for the
fastest test-DB creation; use #3 alone with a host `psql` if you
prefer no Docker dependency.

## Contributing

Issues and PRs welcome at
<https://github.com/iplweb/django-pg-baseline>.

Local development:

```bash
git clone https://github.com/iplweb/django-pg-baseline
cd django-pg-baseline
uv sync --extra test
pre-commit install
pytest
```

## License

MIT — see [LICENSE](LICENSE).
