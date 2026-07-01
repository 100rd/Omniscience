"""Tests for slimming omniscience-core (review action point P2).

Contract under test:

omniscience-core is the shared base package inherited transitively by every
workspace member — including omniscience-cli, omniscience-parsers and
omniscience-embeddings, which only ever import ``omniscience_core.config``
and ``omniscience_core.errors``.  Yet core's base ``dependencies`` couple
sqlalchemy/asyncpg/alembic, nats-py, argon2-cffi and the full OTel stack
(SDK + OTLP exporter + FastAPI instrumentation), so a plain
``pip install omniscience-cli`` drags in Postgres drivers and a message
broker client it never uses.

Acceptance criteria encoded here:

1. core's base ``[project.dependencies]`` contains none of the heavy
   DB/broker/OTel-SDK distributions.
2. core's base dependencies are drawn from an explicit lightweight
   allowlist (pydantic, pydantic-settings, structlog, opentelemetry-api,
   prometheus-client) so new heavy deps cannot sneak back in.
3. Any heavy third-party library still imported somewhere under
   ``packages/core/src`` must be declared either in core's base deps or in
   a ``[project.optional-dependencies]`` extra — i.e. modules that keep
   DB/queue/auth code inside core must stage their deps behind extras
   (modules moved out of core need nothing).
4. Per uv.lock, the transitive closure of omniscience-cli,
   omniscience-parsers and omniscience-embeddings contains no heavy dep —
   the slimming must be reflected in the regenerated lockfile, not just in
   pyproject.
5. Regression guards for the stated trade-off: omniscience-server still
   resolves the runtime deps it relies on through core today, alembic
   stays available for ``make migrate``, and the public
   ``omniscience_core.*`` import paths keep working in the full dev
   environment.
"""

from __future__ import annotations

import ast
import importlib
import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
CORE_DIR = REPO_ROOT / "packages" / "core"
CORE_SRC = CORE_DIR / "src" / "omniscience_core"
UV_LOCK = REPO_ROOT / "uv.lock"

# Distributions that must not ship in the shared base install.
DB_DEPS = {"sqlalchemy", "asyncpg", "alembic"}
BROKER_AUTH_DEPS = {"nats-py", "argon2-cffi"}
OTEL_HEAVY_DEPS = {
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp",
    "opentelemetry-instrumentation-fastapi",
}
HEAVY_DEPS = DB_DEPS | BROKER_AUTH_DEPS | OTEL_HEAVY_DEPS

# The slim base may only require these lightweight distributions.
# opentelemetry-api (unlike the SDK/exporter) is a no-op shim by design and
# prometheus-client is pure Python — both are safe for CLI-class consumers.
ALLOWED_BASE_DEPS = {
    "pydantic",
    "pydantic-settings",
    "structlog",
    "opentelemetry-api",
    "prometheus-client",
}

# Import-name prefix (dotted-segment match) -> distribution name.
HEAVY_IMPORT_TO_DIST = {
    "sqlalchemy": "sqlalchemy",
    "asyncpg": "asyncpg",
    "alembic": "alembic",
    "nats": "nats-py",
    "argon2": "argon2-cffi",
    "opentelemetry.sdk": "opentelemetry-sdk",
    "opentelemetry.exporter": "opentelemetry-exporter-otlp",
    "opentelemetry.instrumentation": "opentelemetry-instrumentation-fastapi",
}

LIGHT_CONSUMERS = ["omniscience-cli", "omniscience-parsers", "omniscience-embeddings"]


def _canonical(name: str) -> str:
    """PEP 503 canonical form of a distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str) -> str:
    """Extract the canonical distribution name from a PEP 508 requirement."""
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    assert match, f"unparseable requirement: {requirement!r}"
    return _canonical(match.group(1))


def _core_pyproject() -> dict:
    return tomllib.loads((CORE_DIR / "pyproject.toml").read_text(encoding="utf-8"))


def _core_base_dep_names() -> set[str]:
    project = _core_pyproject()["project"]
    return {_requirement_name(req) for req in project.get("dependencies", [])}


def _core_extra_dep_names() -> set[str]:
    project = _core_pyproject()["project"]
    extras = project.get("optional-dependencies", {})
    return {_requirement_name(req) for reqs in extras.values() for req in reqs}


def _dist_for_module(module: str) -> str | None:
    """Map a dotted import name to a heavy distribution, if it is one."""
    parts = module.split(".")
    for i in range(len(parts), 0, -1):
        dist = HEAVY_IMPORT_TO_DIST.get(".".join(parts[:i]))
        if dist is not None:
            return dist
    return None


def _heavy_imports_in_core_source() -> dict[str, set[str]]:
    """Heavy distributions imported anywhere under packages/core/src.

    Returns ``{distribution: {relative file paths importing it}}``.  Both
    module-level and lazy in-function imports count: either way the
    distribution must be installable via core itself for the import to
    ever succeed.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(CORE_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                dist = _dist_for_module(module)
                if dist is not None:
                    found.setdefault(dist, set()).add(str(path.relative_to(REPO_ROOT)))
    return found


def _lock_packages() -> dict[str, dict]:
    lock = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    return {_canonical(pkg["name"]): pkg for pkg in lock.get("package", [])}


def _lock_closure(root: str) -> set[str]:
    """Transitive runtime dependency closure of ``root`` per uv.lock.

    Follows ``dependencies`` plus any extras explicitly requested by a
    depender (``{ name = "...", extra = [...] }`` entries).
    """
    packages = _lock_packages()
    assert root in packages, f"{root} missing from uv.lock"
    closure: set[str] = set()
    pending: list[tuple[str, tuple[str, ...]]] = [(root, ())]
    processed: set[tuple[str, tuple[str, ...]]] = set()
    while pending:
        name, extras = pending.pop()
        if (name, extras) in processed:
            continue
        processed.add((name, extras))
        closure.add(name)
        pkg = packages.get(name)
        if pkg is None:
            continue
        deps = list(pkg.get("dependencies", []))
        optional = pkg.get("optional-dependencies", {})
        for extra in extras:
            deps.extend(optional.get(extra, []))
        for dep in deps:
            pending.append(
                (_canonical(dep["name"]), tuple(sorted(dep.get("extra", []))))
            )
    return closure


@pytest.mark.parametrize("dep", sorted(HEAVY_DEPS))
def test_core_base_dependencies_exclude_heavy_dep(dep: str):
    base = _core_base_dep_names()
    assert dep not in base, (
        f"omniscience-core base dependencies still include {dep!r}; DB/broker/"
        "OTel-SDK deps must move out of the shared base (into the consumers "
        "that need them or a core extra)"
    )


def test_core_base_dependencies_are_lightweight_only():
    base = _core_base_dep_names()
    unexpected = base - ALLOWED_BASE_DEPS
    assert not unexpected, (
        "omniscience-core base dependencies must stay lightweight "
        f"(allowed: {sorted(ALLOWED_BASE_DEPS)}), but found {sorted(unexpected)}; "
        "heavy runtime deps belong to the consumers or to core extras"
    )


def test_heavy_imports_in_core_source_are_covered_by_base_or_extras():
    """Modules kept inside core may import heavy libs only if core declares
    them (base or ``[project.optional-dependencies]``), so the public import
    paths stay resolvable once the matching extra is installed.  Modules
    moved out of core (e.g. into omniscience-index) trivially satisfy this.
    """
    declared = _core_base_dep_names() | _core_extra_dep_names()
    undeclared = {
        dist: sorted(files)
        for dist, files in _heavy_imports_in_core_source().items()
        if dist not in declared
    }
    assert not undeclared, (
        "omniscience-core source imports distributions that core no longer "
        f"declares anywhere: {undeclared}; either move those modules out of "
        "core or stage the deps behind a core extra"
    )


@pytest.mark.parametrize("consumer", LIGHT_CONSUMERS)
def test_light_consumer_lock_closure_has_no_heavy_deps(consumer: str):
    """cli/parsers/embeddings must not transitively resolve Postgres, NATS,
    argon2 or the full OTel stack — the whole point of slimming core."""
    closure = _lock_closure(consumer)
    leaked = sorted(closure & HEAVY_DEPS)
    assert not leaked, (
        f"{consumer} transitively resolves heavy deps {leaked} per uv.lock; "
        "slim omniscience-core (and regenerate uv.lock) so light consumers "
        "stop inheriting them"
    )


def test_server_lock_closure_still_resolves_runtime_deps():
    """Regression guard for the trade-off: the server really uses core's
    db/queue/auth/telemetry modules, so slimming core must not silently drop
    their runtime deps from the server's resolution."""
    closure = _lock_closure("omniscience-server")
    required = {
        "sqlalchemy",
        "asyncpg",
        "nats-py",
        "argon2-cffi",
        "opentelemetry-sdk",
        "opentelemetry-exporter-otlp",
    }
    missing = sorted(required - closure)
    assert not missing, (
        f"omniscience-server no longer resolves {missing} per uv.lock; "
        "re-add them to the server (directly or via core extras) before "
        "stripping them from core's base"
    )


def test_alembic_still_available_for_migrations():
    """``make migrate`` runs ``uv run alembic upgrade head`` and the CLI
    shells out to ``python -m alembic`` — alembic must stay resolvable from
    the server or the workspace dev group after leaving core's base."""
    packages = _lock_packages()
    root = packages.get("omniscience", {})
    dev_groups = root.get("dev-dependencies", {})
    dev_names = {
        _canonical(dep["name"]) for deps in dev_groups.values() for dep in deps
    }
    reachable = dev_names | _lock_closure("omniscience-server")
    assert "alembic" in reachable, (
        "alembic is no longer resolvable from omniscience-server or the root "
        "dev group; migrations (make migrate / omniscience CLI) would break"
    )


PUBLIC_IMPORT_PATHS = [
    "omniscience_core",
    "omniscience_core.config",
    "omniscience_core.errors",
    "omniscience_core.logging",
    "omniscience_core.secrets",
    "omniscience_core.freshness",
    "omniscience_core.audit",
    "omniscience_core.audit.fingerprint",
    "omniscience_core.auth.middleware",
    "omniscience_core.auth.scopes",
    "omniscience_core.auth.tokens",
    "omniscience_core.auth.workspace",
    "omniscience_core.db",
    "omniscience_core.db.models",
    "omniscience_core.db.schemas",
    "omniscience_core.queue",
    "omniscience_core.queue.consumer",
    "omniscience_core.queue.messages",
    "omniscience_core.queue.producer",
    "omniscience_core.stats",
    "omniscience_core.stats.service",
    "omniscience_core.storage",
    "omniscience_core.storage.graph",
    "omniscience_core.storage.vector",
    "omniscience_core.telemetry",
    "omniscience_core.telemetry.clients",
    "omniscience_core.telemetry.metrics",
]


@pytest.mark.parametrize("module", PUBLIC_IMPORT_PATHS)
def test_public_import_path_preserved(module: str):
    """Regression guard: every ``omniscience_core.*`` path imported by
    workspace consumers today must keep importing in the full dev
    environment (all extras installed), whether the code stays in core
    behind extras or is re-exported after a move."""
    importlib.import_module(module)
