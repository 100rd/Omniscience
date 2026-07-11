"""Regression contracts for scheduled workflows that provide operational evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _workflow(name: str) -> dict[str, Any]:
    with (ROOT / ".github" / "workflows" / name).open(encoding="utf-8") as stream:
        parsed = yaml.safe_load(stream)
    assert isinstance(parsed, dict)
    return parsed


def _step(workflow: dict[str, Any], job: str, name: str) -> dict[str, Any]:
    steps = workflow["jobs"][job]["steps"]
    return next(step for step in steps if step.get("name") == name)


def test_dr_migrations_run_from_alembic_project() -> None:
    workflow = _workflow("dr-drill.yml")
    step = _step(workflow, "dr-drill", "Run database migrations")

    assert step["working-directory"] == "packages/core"
    assert "alembic upgrade head" in step["run"]


def test_dr_rebuild_uses_the_configured_local_embedding_provider() -> None:
    workflow = _workflow("dr-drill.yml")
    rebuild = _step(workflow, "dr-drill", "Run DR rebuild with RTO enforcement")
    verify = _step(workflow, "dr-drill", "Verify-only pass (idempotency check)")
    integration = _step(workflow, "dr-drill", "Run DR unit + integration tests")

    assert rebuild["env"]["EMBEDDING_PROVIDER"] == "local"
    assert verify["env"]["EMBEDDING_PROVIDER"] == "local"
    assert integration["env"]["EMBEDDING_PROVIDER"] == "local"
    assert "OMNISCIENCE_EMBEDDING_PROVIDER" not in rebuild["env"]
    assert "--recompute-embeddings" in rebuild["run"]


def test_holmes_image_is_official_and_digest_pinned() -> None:
    versions = json.loads((ROOT / "bench" / "vendors" / "versions.json").read_text())
    holmes = versions["vendors"]["holmesgpt"]

    assert holmes["ref"].startswith(
        "us-central1-docker.pkg.dev/genuine-flight-317411/devel/holmes@sha256:"
    )
    assert holmes["ref"].endswith(holmes["digest"])
    assert holmes["digest"] != "sha256:" + "0" * 64


def test_holmes_workflow_uses_the_pinned_server_contract() -> None:
    workflow = _workflow("benchmark.yml")
    run = _step(workflow, "nightly-matrix", "Start HolmesGPT (Docker) for live calls")["run"]

    assert "-p 8090:5050" in run
    assert "--entrypoint python" in run
    assert "/app/server.py" in run
    assert "curl -sf http://localhost:8090/docs" in run
    assert "docker logs holmes" in run


def test_omniscience_workflow_materializes_env_and_fails_loudly() -> None:
    workflow = _workflow("benchmark.yml")
    run = _step(
        workflow,
        "nightly-matrix",
        "Start Omniscience via docker compose (for omniscience vendor only)",
    )["run"]

    assert "cp .env.example .env" in run
    assert "curl -sf http://localhost:8000/health" in run
    assert "docker compose logs" in run
    assert "exit 1" in run
