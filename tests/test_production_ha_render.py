"""Helm chart assertions for the production-HA qualification profile (issue #350).

Covers AC-HA-1 (fail-closed environment evidence), AC-HA-2 (deterministic HA
scheduling policy), AC-HA-3 (per-stateful-dependency topology + entitlement
evidence), and REQ-OPS-6 (reconciliation/retention cannot be disabled while
claiming production-ha). AC-HA-5 posture-label assertions live in
test_helm_posture.py.

Two independent verification layers, both always runnable — `Chart.yaml`
declares no subchart dependencies (see docs/runbooks/production-ha.md), so
`helm template`/`helm install` render directly without a
`helm dependency build` step:

  1. Guard-message tests (`test_guard_fires_*`, `test_valid_production_config_*`)
     run `helm lint` and parse the `funcMap fail` log line it emits when a
     template `fail()` call fires. `helm lint` does NOT propagate a fired
     `fail()` into its own exit code.
  2. Rendered-object tests (`TestRenderedObjects`) run `helm template` and
     assert on the actual manifest field values. These only self-skip via
     `pytest.mark.skipif` when the `helm` binary itself is unavailable on
     PATH — AC-HA-4 (live fault-domain probe) is the only acceptance
     criterion in this profile that genuinely requires a live cluster, and
     it has no assertions in this file.

Runner: pytest tests/test_production_ha_render.py
Requires: pyyaml, helm (CI: azure/setup-helm@v4)
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_PATH = Path(__file__).resolve().parents[1] / "helm" / "omniscience"
HELM = shutil.which("helm") or "helm"

_FAIL_LOG_RE = re.compile(r'msg="funcMap fail" message="(?P<msg>.*)"\s*$')

# Routes around pre-existing nil-pointer gaps in values.yaml that are
# unrelated to production-HA (serviceAccount/postgres/retention keys the
# templates reference but values.yaml never defaulted — see the runbook's
# "Known pre-existing chart defects" section). Not a fix for those gaps,
# just the minimum needed for *any* render to complete.
BASELINE_SET = {
    "secrets.postgresPassword": "test-password",
    "serviceAccount.create": "false",
    "postgres.enabled": "false",
    "retention.enabled": "true",
}

# A fully evidenced, guard-satisfying production-HA configuration.
VALID_PRODUCTION_SET = {
    **BASELINE_SET,
    "production.enabled": "true",
    "postgresql.enabled": "false",
    "neo4j.enabled": "false",
    "qdrant.enabled": "false",
    "nats.enabled": "false",
    "replicaCount": "3",
    "production.evidence.account": "111111111111",
    "production.evidence.cluster": "omniscience-prod-eks",
    "production.evidence.region": "us-east-1",
    "production.evidence.zones[0]": "us-east-1a",
    "production.evidence.zones[1]": "us-east-1b",
    "production.evidence.zones[2]": "us-east-1c",
    "production.evidence.rds.endpoint": "omniscience-prod.abcdef.us-east-1.rds.amazonaws.com",
    "production.evidence.rds.multiAz": "true",
    "production.evidence.jetstream.externalUrl": "nats://jetstream-prod.internal:4222",
    "production.evidence.jetstream.replicas": "3",
    "production.evidence.neo4j.topology": "causal-cluster",
    "production.evidence.neo4j.entitlementRef": "secret/neo4j-license#key",
    "production.evidence.qdrant.topology": "distributed",
    "production.evidence.qdrant.nodes": "3",
}


def _set_args(overrides: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in overrides.items():
        args += ["--set", f"{key}={value}"]
    return args


def _set_string_args(overrides: dict[str, str]) -> list[str]:
    """Like `_set_args`, but forces every value through `--set-string`.

    Unlike `--set`, `--set-string` never auto-types `true`/`false` into a Go
    bool — the value always arrives at the template as the literal string.
    This is what a type-unsafe `not $x` guard would treat as truthy even for
    the string "false", and what GitOps tools (Terraform `helm_release` with
    `type = "auto"` unset, Helmfile/ArgoCD string-serialized values) commonly
    produce in practice.
    """
    args: list[str] = []
    for key, value in overrides.items():
        args += ["--set-string", f"{key}={value}"]
    return args


def _lint_fail_message(
    overrides: dict[str, str], *, string_keys: frozenset[str] = frozenset()
) -> str | None:
    """Return the `fail()` guard message `helm lint` logged, if any fired.

    `string_keys` selects which override keys go through `--set-string`
    (literal-string, no auto-typing) instead of `--set` (Helm auto-types
    bare `true`/`false`). Only the field(s) under test should go through
    `--set-string` — sending the *whole* overrides dict that way would also
    turn every other `"false"` value (e.g. the AC-HA-5 bundled-subchart
    toggles, which must stay real bool `false`) into a truthy string and
    trip an unrelated guard first, masking the one under test.
    """
    plain = {k: v for k, v in overrides.items() if k not in string_keys}
    stringy = {k: v for k, v in overrides.items() if k in string_keys}
    proc = subprocess.run(
        [HELM, "lint", str(CHART_PATH), *_set_args(plain), *_set_string_args(stringy)],
        text=True,
        capture_output=True,
        check=False,
    )
    for line in proc.stderr.splitlines():
        match = _FAIL_LOG_RE.search(line)
        if match:
            return match.group("msg")
    return None


def _helm_available() -> bool:
    return shutil.which("helm") is not None


def _render(overrides: dict[str, str]) -> list[dict]:
    out = subprocess.check_output(
        [HELM, "template", "release-name", str(CHART_PATH), *_set_args(overrides)],
        text=True,
    )
    return [doc for doc in yaml.safe_load_all(out) if doc]


def _by_kind(docs: list[dict], kind: str) -> list[dict]:
    return [doc for doc in docs if doc.get("kind") == kind]


_needs_helm = pytest.mark.skipif(
    not _helm_available(),
    reason="helm binary not found on PATH — install helm to run rendered-object assertions",
)


# ── Fail-closed guard messages (AC-HA-1, AC-HA-2, AC-HA-3, AC-HA-5) ──────────
# Deps-independent: uses `helm lint`, which tolerates a missing charts/ dir.


def test_valid_production_config_triggers_no_guard():
    assert _lint_fail_message(VALID_PRODUCTION_SET) is None


@pytest.mark.parametrize(
    ("override", "expected_substring"),
    [
        # AC-HA-1: dedicated-account / cluster / region / zone evidence
        ({"production.evidence.account": ""}, "production.evidence.account is required"),
        ({"production.evidence.cluster": ""}, "production.evidence.cluster is required"),
        ({"production.evidence.region": ""}, "production.evidence.region is required"),
        # AC-HA-2: scheduling policy floor
        ({"replicaCount": "2"}, "replicaCount must be >= 3"),
        (
            {"production.autoscaling.minReplicas": "2"},
            "production.autoscaling.minReplicas must be >= 3",
        ),
        # AC-HA-5: bundled stateful subcharts are evaluation-only
        ({"postgresql.enabled": "true"}, "postgresql.enabled must be false"),
        ({"neo4j.enabled": "true"}, "neo4j.enabled must be false"),
        ({"qdrant.enabled": "true"}, "qdrant.enabled must be false"),
        ({"nats.enabled": "true"}, "nats.enabled must be false"),
        # AC-HA-1/5: the *real* local-Postgres toggle (single "l", consumed by
        # templates/pvc.yaml) — distinct from the dead bitnami-style
        # `postgresql.enabled` above.
        ({"postgres.enabled": "true"}, "postgres.enabled must be false"),
        # AC-HA-3: RDS Multi-AZ authority
        ({"production.evidence.rds.endpoint": ""}, "production.evidence.rds.endpoint"),
        ({"production.evidence.rds.multiAz": "false"}, "production.evidence.rds.multiAz"),
        # AC-HA-3: JetStream — external, >= 3 replicas
        (
            {"production.evidence.jetstream.externalUrl": ""},
            "production.evidence.jetstream.externalUrl",
        ),
        (
            {"production.evidence.jetstream.replicas": "2"},
            "production.evidence.jetstream.replicas must be >= 3",
        ),
        # AC-HA-3: Neo4j — approved topology + entitlement reference
        ({"production.evidence.neo4j.topology": ""}, "production.evidence.neo4j.topology"),
        (
            {"production.evidence.neo4j.entitlementRef": ""},
            "production.evidence.neo4j.entitlementRef",
        ),
        # AC-HA-3: Qdrant — distributed, >= 3 nodes
        ({"production.evidence.qdrant.topology": ""}, "production.evidence.qdrant.topology"),
        (
            {"production.evidence.qdrant.nodes": "2"},
            "production.evidence.qdrant.nodes must be >= 3",
        ),
        # REQ-OPS-6: reconciliation/retention cannot be disabled under production-ha
        (
            {"retention.enabled": "false"},
            "cannot claim production-ha with retention/reconciliation disabled (REQ-OPS-6)",
        ),
        (
            {"config.reconcileEnabled": "false"},
            "cannot claim production-ha with retention/reconciliation disabled (REQ-OPS-6)",
        ),
    ],
)
def test_guard_fires_for_invalid_or_missing_evidence(override, expected_substring):
    overrides = {**VALID_PRODUCTION_SET, **override}
    message = _lint_fail_message(overrides)
    assert message is not None, f"expected a fail() guard for override {override}"
    assert expected_substring in message


def test_guard_fires_when_zones_are_not_exactly_three():
    overrides = {
        k: v
        for k, v in VALID_PRODUCTION_SET.items()
        if not k.startswith("production.evidence.zones")
    }
    overrides["production.evidence.zones[0]"] = "us-east-1a"
    overrides["production.evidence.zones[1]"] = "us-east-1b"
    message = _lint_fail_message(overrides)
    assert message is not None
    assert "production.evidence.zones must list exactly 3" in message


def test_guard_fires_when_zones_are_three_empty_strings():
    """AC-HA-1: length-3 alone is not "evidence" — content must be non-empty."""
    overrides = {
        k: v
        for k, v in VALID_PRODUCTION_SET.items()
        if not k.startswith("production.evidence.zones")
    }
    overrides["production.evidence.zones[0]"] = ""
    overrides["production.evidence.zones[1]"] = ""
    overrides["production.evidence.zones[2]"] = ""
    zone_keys = frozenset(
        [
            "production.evidence.zones[0]",
            "production.evidence.zones[1]",
            "production.evidence.zones[2]",
        ]
    )
    message = _lint_fail_message(overrides, string_keys=zone_keys)
    assert message is not None
    assert "production.evidence.zones must not contain an empty zone name" in message


# ── String-typed boolean bypass closure (REQ-OPS-6 / AC-HA-3) ───────────────
# Go templates treat any non-empty string, including the literal "false", as
# truthy. A bare `not $x`/`if $x` guard on a bool-shaped field therefore
# no-ops when the value arrives as the *string* "false" instead of the YAML
# boolean `false` — a realistic path via `--set-string`, Terraform
# `helm_release` `set {}` blocks (string by default), or Helmfile/ArgoCD
# values serialization. `--set` alone (used above) auto-types true/false and
# cannot exercise this bypass.


@pytest.mark.parametrize(
    ("override", "expected_substring"),
    [
        (
            {"production.evidence.rds.multiAz": "false"},
            "production.evidence.rds.multiAz must be true",
        ),
        (
            {"retention.enabled": "false"},
            "cannot claim production-ha with retention/reconciliation disabled (REQ-OPS-6)",
        ),
        (
            {"config.reconcileEnabled": "false"},
            "cannot claim production-ha with retention/reconciliation disabled (REQ-OPS-6)",
        ),
    ],
)
def test_guard_closes_string_typed_false_bypass(override, expected_substring):
    overrides = {**VALID_PRODUCTION_SET, **override}
    message = _lint_fail_message(overrides, string_keys=frozenset(override))
    assert message is not None, (
        f"expected a fail() guard for --set-string override {override} "
        "(string-typed boolean bypass)"
    )
    assert expected_substring in message


@pytest.mark.parametrize(
    "field",
    ["production.evidence.rds.multiAz", "retention.enabled", "config.reconcileEnabled"],
)
def test_real_boolean_false_still_fails_control(field):
    """Control: a genuine boolean `false` via `--set` (Helm auto-types) must
    still fail — the string-typed fix must not weaken this existing path."""
    overrides = {**VALID_PRODUCTION_SET, field: "false"}
    message = _lint_fail_message(overrides)
    assert message is not None


# ── Rendered objects (AC-HA-2, AC-HA-3): exact scheduling-policy values ─────


@_needs_helm
class TestRenderedObjects:
    def test_valid_production_renders_ha_scheduling_objects_with_exact_values(self):
        docs = _render(VALID_PRODUCTION_SET)
        deployment = _by_kind(docs, "Deployment")[0]
        pod_spec = deployment["spec"]["template"]["spec"]

        tsc = pod_spec["topologySpreadConstraints"][0]
        assert tsc["maxSkew"] == 1
        assert tsc["topologyKey"] == "topology.kubernetes.io/zone"
        assert tsc["whenUnsatisfiable"] == "DoNotSchedule"

        pdb = _by_kind(docs, "PodDisruptionBudget")[0]
        assert pdb["spec"]["maxUnavailable"] == 1

        hpa = _by_kind(docs, "HorizontalPodAutoscaler")[0]
        assert hpa["spec"]["minReplicas"] == 3
        assert hpa["spec"]["scaleTargetRef"]["kind"] == "Deployment"

        priority_class = _by_kind(docs, "PriorityClass")[0]
        assert priority_class["metadata"]["name"].endswith("-critical-read")
        assert pod_spec["priorityClassName"] == priority_class["metadata"]["name"]

    def test_default_install_renders_no_ha_scheduling_objects(self):
        docs = _render(BASELINE_SET)
        deployment = _by_kind(docs, "Deployment")[0]
        pod_spec = deployment["spec"]["template"]["spec"]
        assert "topologySpreadConstraints" not in pod_spec
        assert "priorityClassName" not in pod_spec
        assert _by_kind(docs, "PodDisruptionBudget") == []
        assert _by_kind(docs, "HorizontalPodAutoscaler") == []
        assert _by_kind(docs, "PriorityClass") == []

    def test_render_fails_when_replica_count_below_three(self):
        overrides = {**VALID_PRODUCTION_SET, "replicaCount": "2"}
        with pytest.raises(subprocess.CalledProcessError):
            _render(overrides)

    def test_render_fails_when_bundled_subchart_still_enabled(self):
        overrides = {**VALID_PRODUCTION_SET, "postgresql.enabled": "true"}
        with pytest.raises(subprocess.CalledProcessError):
            _render(overrides)
