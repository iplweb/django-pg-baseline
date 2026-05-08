# `django-pg-baseline` — Extraction Specification

**Status:** draft
**Source:** `bpp/src/django_pg_baseline/` (already cleanly isolated as a
local Django app)
**Target:** standalone OSS package on PyPI as `django-pg-baseline`
**Package #3 of 3** in the testcontainers-for-Django family:

1. `pytest-testcontainers` — generic pytest plugin, session-scoped Docker
   container lifecycle. Framework-agnostic.
2. `pytest-testcontainers-django` — Django bridge on top of #1. Injects
   env vars before Django imports settings; supports init-script mounts
   and `DATABASES['default']['TEST']['TEMPLATE']` for fast test-db clone.
3. **`django-pg-baseline`** (this package) — Django app for managing a
   `baseline.sql` artifact (a `pg_dump` of seed-data + post-migrate
   schema). Two consumption modes:
   - (a) standalone: monkey-patch Django's test DB creation to load the
     baseline via `psql` after `CREATE DATABASE`,
   - (b) testcontainer-bridged: provide the baseline path so package
     #2 mounts it as a PG init script and clones via `WITH TEMPLATE`.

This spec describes the extraction. Code-wise the package is already
clean — the spec is mostly about packaging, integration contracts,
and the public-API surface we commit to before publishing.

---

## 1. Purpose

A Django suite with hundreds of migrations and/or non-trivial seed data
spends many minutes per test run on `migrate`. The fix is well-known
in principle:

1. Apply migrations once against a clean PG.
2. `pg_dump` the result.
3. On every subsequent test run, `psql -f` (or `CREATE DATABASE …
   WITH TEMPLATE`) the dump into the test DB, then let `migrate` apply
   only the small delta of migrations added since the dump was taken.

`django-pg-baseline` packages this pattern as a reusable Django app with
all the operational bits people forget the first time:

- a deterministic, version-controlled `baseline.sql` (with timestamp
  freezing for diff stability),
- a sidecar `baseline.meta.json` recording the highest migration name
  per app + git SHA + PG version,
- automatic loading on test DB creation (monkey patch) when no faster
  template-clone path is available,
- compatibility with the testcontainer/template-clone path for
  consumers using package #2.

When to rebuild the baseline is the project's decision, not the
package's — we provide `baseline_info` so the user can see how stale
the dump is, but no exit-1 gate enforcing freshness.

In real BPP runs this turns a ~6-minute `migrate` into a ~3-second
`psql` import (or a sub-second `CREATE DATABASE WITH TEMPLATE` clone).

---

## 2. Scope

### In scope

- `BaselineConfig` + `settings.PG_BASELINE` typed loader.
- `loader.load_baseline()` — stdlib-only (no Django at import time)
  shell-out to `psql` to load a dump into a DSN.
- `loader.baseline_needed()` — probe `to_regclass('public.django_migrations')`.
- `patches.install_test_db_patch()` — idempotent monkey patch on
  `BaseDatabaseCreation._create_test_db` that:
  - kicks other sessions off the template DB before `CREATE DATABASE
    WITH TEMPLATE` (Postgres requires zero connections on the source),
  - on the non-template path, post-`CREATE DATABASE` runs `psql -f
    baseline.sql` only when the new DB is empty.
- `freshness.check_freshness()` — compares disk migrations (via
  `MigrationLoader.graph`) vs. the per-app `last_migration` recorded
  in `baseline.meta.json` and produces a typed report. Used by
  `baseline_info` for human-readable output. **Informational only**
  — does not enforce any threshold. The project decides when to
  rebuild.
- `writer.write_meta()` — emits `baseline.meta.json` with a
  `meta_version` field (currently `1`), git SHA, PG version, per-app
  last migration. The `meta_version` field lets future package
  releases detect and migrate older formats; readers must check
  `meta_version <= MAX_SUPPORTED` and fail with a clear message
  otherwise.
- `rebuild.rebuild_baseline()` — orchestrates a full regeneration:
  spins an isolated PG via `testcontainers`, redirects the
  `default` Django connection at it, runs `migrate`, freezes
  configurable timestamp columns, runs `pg_dump` *inside* the
  container (to guarantee client/server major-version match),
  scrubs PG17→PG16-incompatible lines, writes the dump and meta.
- Management commands: `baseline_load`, `baseline_info`,
  `baseline_rebuild`.
- Auto-install of the monkey patch via `AppConfig.ready()` when
  `django_pg_baseline` is in `INSTALLED_APPS`.
- Optional pytest plugin (`pytest_plugin.py`) for projects that
  prefer not to add the app to `INSTALLED_APPS`.

### Out of scope

- The actual `baseline.sql` and `baseline.meta.json` artifacts —
  consumers ship their own under `settings.PG_BASELINE['BASELINE_DIR']`.
  This package ships the machinery, never the data.
- Non-Postgres backends. `psql`/`pg_dump`/template-clone are PG-specific.
  We do not pretend to be portable.
- Any "build a testcontainer for the test DB" logic — that lives in
  package #2 (`pytest-testcontainers-django`).
- Image-distribution of pre-loaded PG (e.g. `iplweb/bpp_dbserver`
  with the dump baked into `/docker-entrypoint-initdb.d/`). That's a
  consumer concern, not ours.

---

## 3. Public API

### 3.1 `settings.PG_BASELINE`

The single Django settings entry point. Schema (already in `conf.py`):

```python
PG_BASELINE = {
    # REQUIRED. Directory holding baseline.sql + baseline.meta.json.
    "BASELINE_DIR": "/path/to/your/baseline-sql",

    # Optional, defaults shown.
    "SQL_FILENAME": "baseline.sql",
    "META_FILENAME": "baseline.meta.json",

    # Which Django connection to load into / dump from.
    "DATABASE_ALIAS": "default",

    # Auto-install the _create_test_db monkey patch in AppConfig.ready().
    # Set to False if you want to control patch installation manually
    # (e.g. only under pytest, only on certain CI hosts).
    "AUTO_LOAD_ON_TEST_DB": True,

    # Image used by `baseline_rebuild`. Override if you need
    # plpython3u, custom locales, extensions, etc.
    "REBUILD_IMAGE": "postgres:16",

    # Extra args appended to the built-in pg_dump invocation. The
    # default invocation already includes --no-owner --no-acl
    # --no-privileges --no-comments and --exclude-table-data=django_session
    # — use this for project-specific additions. Always additive; there
    # is no "replace defaults" option (fork the package if you need it).
    "PG_DUMP_EXTRA_ARGS": ["--exclude-table-data=audit_log"],

    # Convenience: stacks ON TOP of the default exclusions. Each entry
    # becomes --exclude-table-data=<pattern>. Equivalent to listing
    # --exclude-table-data=... in PG_DUMP_EXTRA_ARGS, but cleaner.
    "PG_DUMP_EXTRA_EXCLUDE_TABLE_DATA": ["django_cache*", "easy_thumbnails_*"],

    # Tables/columns whose timestamps should be set to a fixed value
    # before pg_dump, for deterministic diffs across rebuilds.
    "FREEZE_TIMESTAMPS": [("django_migrations", ["applied"])],
    # Stack on top of the default freeze list:
    "FREEZE_TIMESTAMPS_EXTRA": [("django_template", ["creation_date"])],
    "FREEZE_TIMESTAMP_VALUE": "2000-01-01 00:00:00+00",
}
```

**Required:** `BASELINE_DIR`. Everything else has a default. Missing
or empty `PG_BASELINE` raises `ImproperlyConfigured` with a clear
message.

`get_config()` returns a `@dataclass(frozen=True)` `BaselineConfig`.
CLI overrides (e.g. `--image` for `baseline_rebuild`) construct a
new instance via `dataclasses.replace(cfg, rebuild_image=...)`
rather than mutating in place — this avoids time-of-check /
time-of-use bugs when multiple call sites read the same config.

### 3.2 Management commands

| Command | What it does |
| --- | --- |
| `baseline_load` | Load `baseline.sql` into the configured DB. Skips when `django_migrations` already exists, unless `--force`. Flags: `--database <alias>`, `--force`. |
| `baseline_info` | Human summary: git SHA, PG version, sql/meta paths, plus per-app deltas (computed via `MigrationLoader.graph`, so `replaces=` squashes are counted correctly). Always exits 0 — informational, no gating. |
| `baseline_rebuild` | Regenerate `baseline.sql` + `baseline.meta.json`. Spins a `testcontainers` PG, runs `migrate`, freezes timestamps, runs in-container `pg_dump`, scrubs, writes meta. Flags: `--image <ref>`, `--baseline-dir <path>`. |

All commands instantiate via `get_config()` and respect overrides
on the loaded `BaselineConfig`.

### 3.3 `AppConfig.ready()` auto-patch

Adding `"django_pg_baseline"` to `INSTALLED_APPS` is enough for the
common case. `DjangoPgBaselineConfig.ready()`:

1. Calls `get_config()`. If `settings.PG_BASELINE` is **completely
   unset**, returns silently (app is in `INSTALLED_APPS` but the user
   hasn't configured it yet — fine).
2. If `auto_load_on_test_db` is False, returns silently. The user
   explicitly opted out of the auto-patch, so configuration problems
   below are not our concern.
3. If `sql_path` doesn't exist, raises `ImproperlyConfigured` with
   a message of the form:
   ```
   django-pg-baseline: BASELINE_DIR is configured ({path}) but
   {sql_filename} is missing. Run `manage.py baseline_rebuild` to
   generate it, or remove PG_BASELINE from settings to disable.
   ```
   Rationale: a typo in `BASELINE_DIR` should fail loudly, not
   produce silent slowness in CI (everyone migrates from scratch and
   nobody knows why).
4. Installs the `_create_test_db` monkey patch.

The patch itself is idempotent (`_already_patched` flag) — safe under
double-import / autoreload.

**Stale baseline is fine.** If the dump on disk is older than HEAD
(some app's last migration is past what `meta.json` recorded), the
patch still loads it — Django's `migrate` then applies the delta on
top. Staleness surfaces via `baseline_info` (informational only); the
loading path itself is permissive by design. When to rebuild is the
project's call, not the package's.

### 3.4 Pytest plugin entry point (optional)

The package exposes a pytest entry point pointing at
`django_pg_baseline.pytest_plugin`. The plugin's `pytest_configure`:

1. If `DJANGO_SETTINGS_MODULE` is unset, no-op (pytest may be running
   in a non-Django project that picked us up transitively).
2. Calls `django.setup()`.
3. Calls `get_config()`. If `PG_BASELINE` is unset, no-op.
4. If `auto_load_on_test_db` is False, no-op (user opted out of the
   auto-patch).
5. If `sql_path` doesn't exist, raises `pytest.UsageError` with the
   same message as `AppConfig.ready()` (see §3.3). Same rationale:
   loud failure beats silent slowness.
6. Calls `install_test_db_patch(cfg)`.

Same effect as the `INSTALLED_APPS` route, but available to users who
prefer not to list the app. **Mutually exclusive in practice with the
`INSTALLED_APPS` path** — both are idempotent so double-install is
fine, but documenting "use one or the other" prevents confusion.

See §10 for the decision on whether this plugin stays in #3 or moves
to #2.

### 3.5 Programmatic helpers (for package #2 and downstream tooling)

**Stable from v0.1** (commit-to-not-break, contract surface for
package #2):

```python
from django_pg_baseline import get_baseline_path  # Path | None
```

**Available but not yet stable in 0.x** (subject to change before
v1.0; import from submodules at your own risk):

```python
from django_pg_baseline.conf import get_config, BaselineConfig
from django_pg_baseline.patches import install_test_db_patch
from django_pg_baseline.loader import load_baseline, baseline_needed
from django_pg_baseline.freshness import check_freshness, FreshnessReport
```

The full set is re-exported from the top-level module starting at
v1.0.0, when the public API is locked under semver. Keeping
`BaselineConfig` out of the top-level re-exports during 0.x leaves
room to refactor the dataclass shape (e.g. for multi-DB, see §3.6)
without breaking downstreams.

`get_baseline_path()` is a thin wrapper around `get_config().sql_path`
that returns `Path | None` — `None` when `settings.PG_BASELINE` is unset
or the file is missing, instead of raising. This is the documented
contract surface for package #2.

### 3.6 Multi-database forward compatibility

v1 supports a single database alias (`DATABASE_ALIAS`, default
`"default"`). To leave room for v2 multi-DB without breaking
changes, the internals are already shaped for it:

- All internal helpers accept an explicit `alias` parameter (default
  = `cfg.database_alias`):
  ```python
  def load_baseline(cfg: BaselineConfig, alias: str | None = None) -> None: ...
  def baseline_needed(cfg: BaselineConfig, alias: str | None = None) -> bool: ...
  def get_baseline_path(alias: str = "default") -> Path | None: ...
  ```
- Internal config resolution returns a list, even if one-element:
  ```python
  def _get_configs() -> list[BaselineConfig]:
      return [get_config()]  # v1: single-element list
  ```
- The `_create_test_db` patch dispatches by `self.connection.alias`
  — in v1 the lookup always hits the same single config, in v2 it
  picks per-alias.

The user-facing `settings.PG_BASELINE` shape in v1 is the flat dict
documented in §3.1. v2 may introduce a dict-of-dicts form
(`PG_BASELINE = {"default": {...}, "analytics": {...}}`); the
detector for "old vs. new shape" is "are the top-level keys
connection aliases?" — heuristic but unambiguous in practice.

### 3.7 Environment variable overrides

| Variable | Effect |
| --- | --- |
| `DJANGO_PG_BASELINE_SQL_PATH` | Overrides `get_baseline_path()` resolution. When set, points at the dump file directly, bypassing `settings.PG_BASELINE['BASELINE_DIR']` + `SQL_FILENAME`. Missing-file behavior is the same as a configured-but-missing baseline (loud error in `AppConfig.ready()` / pytest plugin). |

The override exists so CI can pin a specific baseline without editing
settings. It is the rename target for the legacy BPP-flavored
`BPP_BASELINE_SQL_PATH` from `testcontainers_bpp` (§4.3, §11); the
old name is not honored by this package.

---

## 4. Integration contract with `pytest-testcontainers-django` (#2)

The two packages must agree on **who loads the baseline and how**, so
there is no double-load and no silent mismatch.

### 4.1 The contract

`django-pg-baseline` exposes `get_baseline_path() -> Path | None`.
`pytest-testcontainers-django` calls it (best-effort import — no hard
dependency on us) and:

- if it returns a path: mounts the file into the PG container as
  `/docker-entrypoint-initdb.d/01-baseline.sql`, **and** writes
  `DATABASES['default']['TEST']['TEMPLATE']` to the seed DB name so
  Django runs `CREATE DATABASE … WITH TEMPLATE …` instead of running
  migrate from scratch.
- if it returns `None`: falls back to an empty PG; tests run a normal
  `migrate`. Nothing in #3 fires either, because the monkey patch
  short-circuits when `sql_path` doesn't exist.

When the bridge is active, package #2 sets a private marker on the
DB settings:

```python
DATABASES['default']['TEST']['TEMPLATE'] = '<seed-db-name>'
DATABASES['default']['TEST']['_django_pg_baseline_seeded'] = True
```

Our patch reads `_django_pg_baseline_seeded` (not `TEMPLATE`
directly) to decide whether to skip the `psql` reload. This avoids
confusing the patch when a user has set `TEMPLATE` for unrelated
reasons — e.g. a manually configured template DB with their own
seed. The marker is the explicit coordination flag between #2 and
#3; `TEMPLATE` alone is not.

The patch's other job — kicking other sessions off the template DB
before `CREATE DATABASE WITH TEMPLATE` (via `pg_terminate_backend`)
— runs whenever `TEMPLATE` is set, marker or no marker. That part
is generic Postgres hygiene and benefits any consumer that sets
`TEMPLATE`. It requires the `pg_signal_backend` role; in Mode B
(testcontainers) the test user is superuser, so this is satisfied
automatically. Mode A users running against a shared PG must grant
the role to the test DB user — documented in the README.

### 4.2 Why this shape (and not the alternatives)

- **Why not let #2 read `settings.PG_BASELINE` directly?** It would
  couple #2 to our settings schema. With `get_baseline_path()` we own
  the resolution rule (env override, fallback paths, file existence
  check) — and #2 stays focused on container lifecycle.
- **Why a function and not just a setting?** Because the resolution
  may grow. The vendor-neutral env override
  `DJANGO_PG_BASELINE_SQL_PATH` (replacing the legacy
  BPP-flavored `BPP_BASELINE_SQL_PATH` from `testcontainers_bpp`)
  is centralized here behind the same call. A function is
  forward-compatible with future resolution rules.
- **Why optional / soft?** #2 must work without #3 installed. If `import
  django_pg_baseline` fails, #2 logs and continues with an empty PG.

### 4.3 Side note: the existing `find_baseline_sql()` in `testcontainers_bpp`

`bpp/src/testcontainers_bpp/containers.py::find_baseline_sql()`
currently hardcodes the convention path
`<src>/baseline-sql/baseline.sql` and respects
`$BPP_BASELINE_SQL_PATH`. After extraction, that function is
replaced by `django_pg_baseline.get_baseline_path()`, and the env
override is renamed to `DJANGO_PG_BASELINE_SQL_PATH` (clean cut,
no deprecation period — the old name leaves the codebase with the
extraction). This is part of the migration path for BPP (§11) and
the integration contract for #2.

---

## 5. Three modes of use

### Mode A — Standalone, no testcontainer

The simplest case. Useful when:

- the consumer runs tests against a long-lived dev PG (e.g. a host PG,
  a `docker compose` service, a CI service container),
- `psql` is on `PATH` on the host running pytest.

Setup:

```python
INSTALLED_APPS = [..., "django_pg_baseline"]
PG_BASELINE = {"BASELINE_DIR": BASE_DIR / "baseline-sql"}
```

What happens at test time:

1. `AppConfig.ready()` installs the `_create_test_db` patch.
2. Django's runner calls `_create_test_db` → `CREATE DATABASE
   test_<name>`.
3. Patch sees `django_migrations` is missing in the new DB →
   `psql -f baseline.sql --single-transaction --quiet -v ON_ERROR_STOP=1`.
4. Django's `migrate` then applies only the post-baseline delta.

### Mode B — With `pytest-testcontainers-django` (#2)

Faster. Useful when:

- the consumer accepts a Docker daemon as a test dependency,
- they want the test DB to be a *clone* of a populated template
  rather than a `psql` reload (sub-second vs. seconds).

Setup:

```python
INSTALLED_APPS = [..., "django_pg_baseline"]
PG_BASELINE = {"BASELINE_DIR": BASE_DIR / "baseline-sql"}
```

`pytest-testcontainers-django` is auto-loaded as a pytest plugin. It:

1. Reads `get_baseline_path()` from #3.
2. Starts a PG container with the baseline mounted as init script.
3. Sets `DATABASES['default']['TEST']['TEMPLATE'] = '<seed-db>'`.

What happens at test time:

1. PG comes up; init script loads `baseline.sql` into the seed DB.
2. Django runs `_create_test_db`. Our patch sees the
   `_django_pg_baseline_seeded` marker on `TEST` (set by #2 alongside
   `TEMPLATE`), closes the default connection, kicks other backends
   off the template, then delegates to Django's original
   `_create_test_db` which issues `CREATE DATABASE test_… WITH
   TEMPLATE seed`.
3. The new DB is already populated — `migrate` applies only the delta.

In Mode B the `psql` shell-out from #3's `loader.py` is **never**
invoked. We still own:
- the patch's "kick sessions off template" prelude (runs whenever
  `TEMPLATE` is set, with or without our marker — generic PG hygiene),
- the marker-driven "skip psql reload" decision,
- `settings.PG_BASELINE` and `get_baseline_path()`.

### Mode C — Build/rebuild the baseline (CI or local)

Run when the user decides the baseline needs refreshing (typically
after adding migrations):

```bash
uv run python manage.py baseline_rebuild
git add path/to/baseline-sql/baseline.sql baseline.meta.json
git commit -m "chore(baseline): refresh after migrations …"
```

`rebuild_baseline()`:

1. Spins a fresh `PostgresContainer(image=cfg.rebuild_image)`.
2. **Validates** that `pg_dump` is present in the image by running
   `pg_dump --version` inside the container. If absent (e.g. a
   cut-down/distroless image), aborts with a clear error before any
   migrations run, instead of failing cryptically mid-rebuild.
3. Swaps `connections.databases[cfg.database_alias]` to point at
   the testcontainer (and evicts the cached `DatabaseWrapper` so
   subsequent access reconnects). This is necessary because data
   migrations commonly grab `django.db.connection` directly, ignoring
   the `database=` arg passed to `call_command("migrate", database=…)`.
4. `migrate(interactive=False)`.
5. `_freeze_timestamps()` against the configured tables/columns.
6. `pg_dump` *inside* the container (so `pg_dump` major version
   matches the server, avoiding e.g. PG17 emitting `SET
   transaction_timeout = 0;` for a PG16 target).
7. `_scrub_dump()` removes `\restrict`/`\unrestrict` (random tokens,
   nondeterministic) and `SET transaction_timeout = …` (PG17→<17
   incompatibility). **This is ongoing maintenance**: every new
   PG major release may emit new directives that need scrubbing for
   forward/backward compatibility. The scrubbing pass is treated as
   a list-of-known-incompatibilities, kept current as new PG ships.
8. `write_meta()` writes the JSON sidecar (with `meta_version`).
9. Restores the original `default` connection in `finally`.

**Security note.** The dump captures all data present in the
testcontainer after `migrate()`. If your data migrations seed users,
fixtures, or other content that ends up in the dump, *that data
lands in version control*. Review the dump before committing,
especially on the first rebuild. Use `PG_DUMP_EXTRA_EXCLUDE_TABLE_DATA`
to skip tables whose row data should not ship (e.g. `auth_user`
when you have real test passwords). The package does not exclude
`auth_user` by default — projects that intentionally seed admin
fixtures rely on that data being in the baseline.

Recommended downstream wiring: a GitHub Action that runs
`baseline_rebuild` whenever `**/migrations/**` changes on the main
branch and opens a PR with the refreshed dump. The package itself
does not enforce any "freshness" policy — when to rebuild is the
project's decision; we just provide the tooling.

---

## 6. Repo layout

```
django-pg-baseline/
├── pyproject.toml              # src layout, [test] extra
├── README.md                   # quickstart, three modes, troubleshooting
├── CHANGELOG.md                # keep-a-changelog format, towncrier optional
├── LICENSE                     # MIT (matches BPP's reusable-app convention)
├── .pre-commit-config.yaml     # ruff format + ruff check (changed-files-only)
├── .github/
│   └── workflows/
│       ├── tests.yml           # pytest matrix (see §8)
│       └── release.yml         # tag → build sdist+wheel → twine upload
├── src/
│   └── django_pg_baseline/
│       ├── __init__.py         # re-exports public API (see §3.5)
│       ├── apps.py
│       ├── conf.py
│       ├── loader.py
│       ├── patches.py
│       ├── writer.py
│       ├── freshness.py
│       ├── rebuild.py          # gated behind [rebuild] extra
│       ├── pytest_plugin.py
│       └── management/
│           ├── __init__.py
│           └── commands/
│               ├── __init__.py
│               ├── baseline_load.py
│               ├── baseline_info.py
│               └── baseline_rebuild.py
└── tests/
    ├── conftest.py
    ├── test_apps.py
    ├── test_conf.py
    ├── test_loader.py
    ├── test_patches.py
    ├── test_freshness.py
    ├── test_writer.py
    ├── test_rebuild.py
    ├── test_management_commands.py
    └── test_pytest_plugin.py
```

`pyproject.toml` extras:

```toml
[project.optional-dependencies]
test = ["pytest", "pytest-django", "django>=5.0"]
```

`testcontainers[postgres]` is a **regular runtime dependency**, not
an optional extra — `baseline_rebuild` is core functionality, not
opt-in. The cost (one extra Python dep) is far smaller than the
cost of users discovering at runtime that their `pip install` left
out the rebuild path.

`pyproject.toml` entry point for the pytest plugin:

```toml
[project.entry-points.pytest11]
django_pg_baseline = "django_pg_baseline.pytest_plugin"
```

(See §10 for the open question on whether to register the pytest11
entry point at all, or only ship the module and require the user to opt
in via `-p django_pg_baseline.pytest_plugin`.)

Runtime dependencies:
- `Django>=5.0`
- `testcontainers[postgres]>=4.14.2`

**No `psycopg` runtime dep.** This is a Django app — by definition
the host project already has either `psycopg2`, `psycopg2-binary`,
or `psycopg[binary]>=3` installed (Django's Postgres backend
requires one). Forcing a flavor would conflict with the host's
choice and is wrong for a library.

Internally, a small `_backend.py` module hides the differences
between v2 and v3 behind the narrow API we actually use (`connect`,
`cursor.execute`, `commit`, `close`). The public surface is one
function:

```python
# _backend.py
def connect(dsn: str):
    """Open a Postgres connection using whichever psycopg is installed.

    Prefers psycopg (v3); falls back to psycopg2. Returned connection
    supports the v2-style cursor protocol our callers rely on.
    """
    try:
        import psycopg
        return psycopg.connect(dsn)
    except ImportError:
        import psycopg2
        return psycopg2.connect(dsn)
```

The naive aliasing trick `import psycopg2 as psycopg` is
**deliberately avoided** — psycopg2 and psycopg3 are not
API-compatible (connection-string parsing, cursor methods,
`sql.SQL`, and the async story all differ). `_backend.py` documents
the subset we rely on so future callers don't accidentally use a
v3-only feature and break v2 users.

`loader.py` only shells out to `psql` and doesn't need a backend at
all. `patches.py` (the only consumer of `_backend`) imports it
lazily at patch-execution time, after Django has already loaded its
own DB driver — so by then we know one of the two is available.

Both flavors are exercised in CI (see §8).

---

## 7. Coupling-to-BPP audit

`grep -rn "from bpp\|import bpp\|from django_bpp\|import django_bpp"
src/django_pg_baseline/` returns **zero matches**. Tests don't import
BPP fixtures (the local `tests/conftest.py` defines its own
`tmp_baseline_dir`, `fake_sql_file`, `fake_meta_dict`).

What I did find that mentions BPP, and what to do:

| Location | Mention | Action |
| --- | --- | --- |
| `loader.py` docstring | "Postgres testcontainer started by `testcontainers_bpp`" | Reword to refer to "any external mechanism that pre-populates the test DB via PG init scripts and `TEST.TEMPLATE`" — and mention package #2 by its public name. |
| `rebuild.py::PostgresContainer(...)` | hardcoded `username="bpp"`, `password="password"`, `dbname="bpp_baseline"` | Replace with vendor-neutral defaults (`postgres`/`postgres`/`baseline`) or read from config. These never escape the throwaway testcontainer so the values don't matter functionally — but the BPP names are confusing in OSS docs. |
| `tests/conftest.py::fake_meta_dict` | `"bpp": "0500_something"` in fake migration name | Cosmetic — change to `"myapp": "0050_initial"` or similar. Doesn't affect logic. |
| `src/baseline-sql/README.md` | BPP-specific troubleshooting (`plpython3u`, `pl_PL.UTF-8`) | Move into the new package's `README.md` as **examples** of REBUILD_IMAGE customization. Keep wording vendor-neutral. |
| `pytest_plugin.py` docstring | "in bpp we install the monkey patch via INSTALLED_APPS" | Reword. |

None of these are coupling — they're cosmetic strings/docstrings. The
package can be lifted as-is and these can be cleaned up in a single
"vendor-neutral docstrings" commit before first release.

**Verdict:** zero-coupling. This is by far the cleanest of the three
extractions.

---

## 8. CI strategy

Test matrix (GitHub Actions):

- **Python:** 3.10, 3.11, 3.12, 3.13.
- **Django:** 5.0, 5.1, 5.2 (current LTS). Django 4.2 is out of
  scope — the project targets current Django, and 4.2 LTS goes EOL
  in April 2026 anyway. Excluded combo: Django 5.0 + Python 3.13
  (Django 5.0 supports Python 3.10–3.12 only).
- **Postgres:** 16, 17 — as a service container in CI. Older PG is
  out of scope: the project targets current Postgres, and supporting
  EOL versions (14/15) is dead weight that complicates `_scrub_dump`
  and the `pg_signal_backend` story.
- **psycopg driver:** `psycopg2-binary` and `psycopg[binary]>=3` —
  separate cells to validate the lazy-import contract from §6.
- **OS:** Linux only. Windows is not part of the matrix — the
  package shells out to `psql` / `pg_dump`, uses POSIX path
  conventions, and the testcontainer story assumes a Docker daemon
  reachable the way it is on Linux runners. macOS works in practice
  for local development but is not CI-tested.

Total cells = (4 × 3 − 1 excluded) × 2 × 2 = 44. Trim to a sensible
diagonal:

- Full matrix only on push to `main`.
- PRs run a smaller set: `{3.10, 3.13} × {5.1, 5.2} × {16, 17} ×
  {psycopg2, psycopg3}` = 16 cells (5.1 and 5.2 both support the
  full Python range, so no exclusions needed). Still under 5 minutes
  wall time.

Test layers:

1. **Unit tests** (no DB, no Docker). The vast majority of the existing
   suite is already at this layer — fakes for `psycopg`/`psycopg2`,
   `subprocess`, `connections`, `MigrationLoader`. Runs in seconds
   across the full matrix. Keep this property.
2. **Integration test for `rebuild_baseline`** — needs Docker. Gate
   behind a marker (`@pytest.mark.requires_docker`) and run on
   **every push and every PR** (GitHub Actions Linux runners ship
   with Docker — no reason to skip on forks). Catching rebuild
   regressions pre-merge is the whole point.
3. **End-to-end "fake project"** — a tiny throwaway Django project
   under `tests/fake_project/` with a couple of migrations. Test that
   `baseline_rebuild` produces a working dump and that `baseline_load`
   reloads it cleanly. Run on the integration job.
4. **Django `_create_test_db` signature contract test** — for each
   Django version in the matrix, inspect
   `BaseDatabaseCreation._create_test_db.__signature__` (via
   `inspect.signature`) and assert positional/keyword arguments
   match what the patch expects. Fails loudly when a new Django
   release changes the private signature, so we catch breakage at
   our CI rather than at a downstream user's `manage.py test`.

Linting/formatting:

- `ruff check` + `ruff format` (mirror BPP's setup, line length 88).
- `pre-commit` with hooks scoped to changed files (per
  `feedback_no_mass_reformat.md`).

Release pipeline:

- Tag `vX.Y.Z` → workflow builds sdist + wheel via `python -m build`,
  validates with `twine check`, uploads via OIDC to PyPI (no API
  tokens). Match the pattern from BPP's other extracted packages.

---

## 9. Resolved decisions log

All questions raised during spec review have been folded into the
relevant sections as decisions. This log is the single index:

| Topic | Decision | Section |
| --- | --- | --- |
| psycopg2 vs psycopg3 | No psycopg runtime dep; lazy import of `psycopg` (v3) with fallback to `psycopg2`; CI tests both | §6 |
| `HOST` fallback to `localhost` | Pass-through, no fallback (preserves Django's unix-socket case) | loader.py impl note |
| Freshness on squashed migrations | Use `MigrationLoader.graph` (knows `replaces=`), not `max(names)` | §3.2 |
| `_create_test_db` signature drift | Dedicated CI contract test per Django version | §8 layer 4 |
| Silent no-op on missing `BASELINE_DIR` | Hard `ImproperlyConfigured` / `pytest.UsageError` with rebuild instructions when `PG_BASELINE` is set but sql is missing | §3.3, §3.4, §10 |
| `meta.json` schema evolution | `meta_version` field from v0.1 | §3.1, §5 Mode C |
| Postgres version range | Drop PG 14/15; matrix is PG 16 + PG 17 | §8 |
| Multi-DB | Internal helpers already accept `alias`; settings shape stays flat in v1; v2 may introduce dict-of-dicts | §3.6 |
| Coordination flag with #2 | Explicit `_django_pg_baseline_seeded` marker on `TEST` dict, not `TEMPLATE` itself | §4.1 |
| `BaselineConfig` mutability | `frozen=True` + `dataclasses.replace()` for CLI overrides | §3.1 |
| 0.x stable subset | Only `get_baseline_path` re-exported from top-level; rest accessed via submodules until v1.0 | §3.5, §13 |
| Integration tests on PR forks | Run on every push and PR (Linux GHA runners have Docker) | §8 layer 2 |
| `_scrub_dump` maintenance | Treated as a living list; new PG majors will add directives we'll keep up with | §5 Mode C |
| `[rebuild]` extra | Dropped — `testcontainers[postgres]` is a regular runtime dep, rebuild is core not opt-in | §6 |
| Freshness gate / `FRESHNESS_MAX_DELTA` | Removed — when to rebuild is the project's call, not the package's. `baseline_check` command dropped; `baseline_info` still shows per-app deltas, informational only | §1, §3.2 |
| `pg_terminate_backend` privileges | Satisfied automatically in Mode B (testcontainers superuser); Mode A users must grant `pg_signal_backend` role to the test DB user | §4.1 |
| `BPP_BASELINE_SQL_PATH` rename | Clean cut to `DJANGO_PG_BASELINE_SQL_PATH`; no deprecation shim — the old name leaves the codebase together with the extraction | §4.2, §4.3, §11 |

---

## 10. `pytest_plugin.py` — stays in #3 or moves to #2?

**Decision: stays in #3.**

Re-reading `pytest_plugin.py`:

```python
def pytest_configure(config):
    import django
    django.setup()
    from .conf import get_config
    from .patches import install_test_db_patch

    cfg = get_config()
    if cfg.auto_load_on_test_db and cfg.sql_path.exists():
        install_test_db_patch(cfg)
```

It is purely an alternative way to install the same monkey patch as
`AppConfig.ready()`. It depends on:
- `get_config()` (this package),
- `install_test_db_patch()` (this package),
- `django.setup()` and a valid `DJANGO_SETTINGS_MODULE` (the user's
  project, not us).

It does **not** depend on testcontainers, container lifecycle, or
anything package #2 owns. Moving it to #2 would force users who don't
use testcontainers (Mode A) to install #2 just to get the
no-`INSTALLED_APPS` registration path. That's a bad bargain.

The plugin's existence is also useful for the consumer's "I have a
weird stack and I don't want to add `django_pg_baseline` to
`INSTALLED_APPS`" case — totally unrelated to whether #2 is involved.

**However**, there is a related question: should #3 register the
pytest11 entry point at all, or just ship `pytest_plugin.py` as a
module the user opts into via `-p django_pg_baseline.pytest_plugin`?

- Pro entry-point registration: easier for users — `pip install
  django-pg-baseline` and pytest auto-loads the plugin.
- Con: the plugin calls `django.setup()` unconditionally on
  `pytest_configure`. If a user installs us as a transitive dep
  through some other library and runs pytest in a project that
  doesn't have `DJANGO_SETTINGS_MODULE`, the plugin will crash.

**Recommendation:** **do** register the entry point. The plugin
no-ops gracefully when there is no Django context, but fails loudly
when there *is* a Django context with `PG_BASELINE` configured but
the dump is missing — matching the `AppConfig.ready()` policy:

```python
def pytest_configure(config):
    import os
    if not os.environ.get("DJANGO_SETTINGS_MODULE"):
        return  # not a Django project — leave it alone
    import django
    django.setup()
    from django.core.exceptions import ImproperlyConfigured
    import pytest
    try:
        cfg = get_config()
    except ImproperlyConfigured:
        return  # PG_BASELINE not set — fine
    if not cfg.auto_load_on_test_db:
        return  # user opted out of auto-patch — don't validate further
    if not cfg.sql_path.exists():
        raise pytest.UsageError(
            f"django-pg-baseline: BASELINE_DIR is configured "
            f"({cfg.baseline_dir}) but {cfg.sql_filename} is missing. "
            f"Run `manage.py baseline_rebuild` to generate it, or "
            f"remove PG_BASELINE from settings to disable."
        )
    install_test_db_patch(cfg)
```

That mirrors the defensiveness already in `AppConfig.ready()` and
keeps the plugin friendly to non-Django-aware test runs while still
catching configuration mistakes loudly.

---

## 11. Migration path for BPP

After `django-pg-baseline` is on PyPI, the BPP repo becomes a
consumer. Concrete steps, in order:

1. **Add a runtime dep** to `bpp/pyproject.toml`:
   ```toml
   dependencies = [
       ...,
       "django-pg-baseline>=0.1",
   ]
   ```
   `testcontainers[postgres]` is now transitive via
   `django-pg-baseline` — drop the local line. No optional-extras
   plumbing in BPP for the rebuild path; everyone who installs BPP
   gets `baseline_rebuild` available.

2. **Delete** `bpp/src/django_pg_baseline/`. The `INSTALLED_APPS` entry
   stays (`"django_pg_baseline"`); it now resolves to the PyPI package.

3. **`settings/base.py`**: `PG_BASELINE` stays unchanged. BPP's custom
   values (`REBUILD_IMAGE = "iplweb/bpp_dbserver:psql-16.13"`,
   `PG_DUMP_EXTRA_EXCLUDE_TABLE_DATA`, `FREEZE_TIMESTAMPS_EXTRA`) are
   already expressed as the published settings keys and need no edit.

4. **`bpp/src/testcontainers_bpp/containers.py`**: replace
   `find_baseline_sql()` with a call to
   `django_pg_baseline.get_baseline_path()`. The
   `BPP_BASELINE_SQL_PATH` env override is dropped; users who relied
   on it set `DJANGO_PG_BASELINE_SQL_PATH` instead (clean cut — the
   old name leaves the codebase with the extraction, no deprecation
   shim in either #2 or #3). This is also part of the package #2
   extraction work — the two extractions need to land in the right
   order:
   - publish #3 (this package),
   - publish #1 + #2,
   - update BPP to consume both.

5. **Tests in `bpp/src/django_pg_baseline/tests/`**: move to the new
   package's repo (already covered in §6). BPP's repo loses them — and
   that's fine, they tested package internals, not BPP.

6. **`src/baseline-sql/`** stays in BPP. It's data, not code.
   BPP's `PG_BASELINE['BASELINE_DIR']` continues to point at it.

7. **CI**: BPP's `.github/workflows/refresh-baseline.yml` stays —
   it just calls `manage.py baseline_rebuild`, which now resolves to
   the published package's command. No edits needed.

8. **Docs**: update `docs/CODEBASE_MAP.md` and the `src/baseline-sql/`
   README to reference the new package and link to its repo.

Net BPP diff: a few lines of dep changes + one file rename in
`testcontainers_bpp/`. No settings change. No test changes (other
than removing the package-internal tests). No production behavior
change.

---

## 12. Naming & PyPI

PyPI availability check (via `https://pypi.org/pypi/<name>/json`,
HTTP 404 = available):

| Name | Status |
| --- | --- |
| `django-pg-baseline` | **404 — available** |
| `django-pg-dump` | 404 — available |
| `django-baseline-sql` | 404 — available |
| `django-postgres-baseline` | 404 — available |

**Recommended name: `django-pg-baseline`.** Reasons:
- mirrors the existing module name (`django_pg_baseline`), so the
  import path stays identical and the BPP migration is a one-line
  dep change;
- reads naturally ("Django Postgres baseline");
- shorter than `django-postgres-baseline`;
- the keyword "baseline" is the unique signal — "pg-dump" is too
  generic, "baseline-sql" inverts the natural word order.

GitHub repo: `iplweb/django-pg-baseline` (matches BPP's other
extracted-package repos).

---

## 13. Release plan (sketch)

1. **v0.1.0** — minimum viable extraction. Ship as-is from BPP,
   minus the cosmetic BPP names from §7. Document Modes A and C.
   `get_baseline_path()` is **the only stable top-level export** and
   is the contract surface for package #2 (even before #2 ships).
   Everything else is reachable via submodules but explicitly
   marked as 0.x-unstable in §3.5.
2. **v0.2.0** — once package #2 is published, document Mode B and
   the explicit `_django_pg_baseline_seeded` marker contract (§4.1).
   May coincide with widening the test matrix.
3. **v1.0.0** — once we've used it in BPP for a release cycle and
   one external consumer has adopted it. Re-export the full helper
   set from the top-level module (§3.5) and lock the API under
   semver. Schema-bump rules for `meta.json` and breaking changes
   from this point follow strict semver.

Towncrier fragments for changelog (matches BPP's convention):
`changes/<issue>.{added,changed,fixed,removed}.md`.

---

## 14. Why this package is worth open-sourcing

The pattern (baseline `pg_dump` + load on test-DB creation) is rediscovered
independently by every Django shop with a heavy migration history.
The off-the-shelf alternatives are all partial:

- `pytest-django --create-db` + `--reuse-db` — speeds up *subsequent*
  runs but not the first one in CI; doesn't solve the "100s of
  migrations" boot cost.
- `pytest-django` `django_db_setup` fixture override — DIY territory,
  every project rolls its own.
- `pgcopy` / `pgloader` — wrong layer, those are bulk-data tools, not
  test-DB bootstrap.
- The standard advice "squash your migrations" — works once, decays
  again, and squashing has its own correctness pitfalls
  (data migrations, custom `RunPython`, etc.).

`django-pg-baseline` is the missing rung: a small, focused Django app
whose entire job is "manage a baseline.sql artifact and use it
automatically." Pairs with package #2 for the testcontainer crowd,
works standalone for everyone else.
