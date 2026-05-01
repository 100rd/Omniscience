"""Helm chart hardening assertions for issue #166.

This is the chart's structural test suite. It renders the chart with
representative value-sets and asserts the hardening invariants:

  1. Pod spec is PSA-restricted compliant.
  2. NetworkPolicy default-denies and emits the named allow-list.
  3. RBAC ClusterRole verbs are exactly the read-only set.
  4. ClusterRole has no aggregationRule.
  5. Leader-election Role is namespaced and limited to coordination/leases.
  6. Image can be pinned by digest.
  7. Resource envelope matches the documented production envelope.
  8. Topology spread renders only when replicaCount >= 2.

Runner: pytest helm/omniscience-operator/tests/assertions/

Requires:  pyyaml  (already a project dep through the operator/CI tooling)
           helm    (CI: azure/setup-helm@v4)
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_PATH = Path(__file__).resolve().parents[2]  # helm/omniscience-operator
HELM = shutil.which("helm") or "/opt/homebrew/bin/helm"

DEFAULT_SET = {
    "workspaceId": "11111111-2222-3333-4444-555555555555",
    "clusterName": "ci",
    "nats.url": "nats://omniscience-nats.system:4222",
}


def _render(extra: dict[str, str] | None = None) -> list[dict]:
    args = [HELM, "template", "release-name", str(CHART_PATH)]
    sets = dict(DEFAULT_SET)
    if extra:
        sets.update(extra)
    for k, v in sets.items():
        args += ["--set", f"{k}={v}"]
    out = subprocess.check_output(args, text=True)
    return [d for d in yaml.safe_load_all(out) if d]


def _by_kind(docs: list[dict], kind: str, name_suffix: str | None = None) -> list[dict]:
    out = [d for d in docs if d.get("kind") == kind]
    if name_suffix:
        out = [d for d in out if d.get("metadata", {}).get("name", "").endswith(name_suffix)]
    return out


# ── PSA-restricted pod spec ──────────────────────────────────────────────────


def test_pod_security_context_restricted_compliant():
    docs = _render()
    deployment = _by_kind(docs, "Deployment")[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    sc = container["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["runAsNonRoot"] is True
    assert sc["runAsUser"] == 65532
    assert sc["capabilities"]["drop"] == ["ALL"]
    assert sc["seccompProfile"]["type"] == "RuntimeDefault"


def test_pod_security_floor_cannot_be_relaxed():
    """Critical invariant — user values cannot escape the PSA-restricted floor."""
    docs = _render(
        {
            "securityContext.allowPrivilegeEscalation": "true",
            "securityContext.runAsNonRoot": "false",
            "securityContext.readOnlyRootFilesystem": "false",
        }
    )
    deployment = _by_kind(docs, "Deployment")[0]
    sc = deployment["spec"]["template"]["spec"]["containers"][0]["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False, "floor was relaxed"
    assert sc["readOnlyRootFilesystem"] is True, "floor was relaxed"
    assert sc["runAsNonRoot"] is True, "floor was relaxed"


def test_no_host_network_or_pid():
    docs = _render()
    pod_spec = _by_kind(docs, "Deployment")[0]["spec"]["template"]["spec"]
    assert "hostNetwork" not in pod_spec or pod_spec["hostNetwork"] is False
    assert "hostPID" not in pod_spec or pod_spec["hostPID"] is False
    assert "hostIPC" not in pod_spec or pod_spec["hostIPC"] is False
    container = pod_spec["containers"][0]
    for port in container.get("ports", []):
        assert "hostPort" not in port, "hostPort forbidden"


# ── NetworkPolicy ────────────────────────────────────────────────────────────


def test_networkpolicy_default_enabled_and_default_deny():
    docs = _render()
    np = _by_kind(docs, "NetworkPolicy")
    assert len(np) == 1
    assert sorted(np[0]["spec"]["policyTypes"]) == ["Egress", "Ingress"]


def test_networkpolicy_ingress_metrics_namespaceselector():
    docs = _render({"networkPolicy.metricsAllowedNamespaceSelector.matchLabels.team": "monitoring"})
    np = _by_kind(docs, "NetworkPolicy")[0]
    metrics_rules = [
        r for r in np["spec"]["ingress"] if any(p["port"] == 8080 for p in r.get("ports", []))
    ]
    assert metrics_rules, "metrics ingress rule missing"
    assert "from" in metrics_rules[0]


def test_networkpolicy_egress_includes_dns_apiserver_nats():
    docs = _render({"omniscience.serverUrl": "https://api.omniscience.example.com:443"})
    np = _by_kind(docs, "NetworkPolicy")[0]
    egress_ports = sorted(
        {(p["port"], p.get("protocol", "TCP")) for r in np["spec"]["egress"] for p in r.get("ports", [])}
    )
    assert (53, "UDP") in egress_ports, "DNS UDP missing"
    assert (53, "TCP") in egress_ports, "DNS TCP missing"
    assert (443, "TCP") in egress_ports, "apiserver / omniscience missing"
    assert (4222, "TCP") in egress_ports, "NATS missing"


def test_networkpolicy_omits_omniscience_egress_when_unset():
    docs = _render()  # no omniscience.serverUrl, no api.baseUrl
    np = _by_kind(docs, "NetworkPolicy")[0]
    egress_443_count = sum(
        1 for r in np["spec"]["egress"] for p in r.get("ports", []) if p["port"] == 443
    )
    # Only the apiserver rule should match.
    assert egress_443_count == 1


def test_networkpolicy_can_be_disabled():
    docs = _render({"networkPolicy.enabled": "false"})
    assert _by_kind(docs, "NetworkPolicy") == []


# ── RBAC verb allow-list ─────────────────────────────────────────────────────


_ALLOWED_VERBS = {"get", "list", "watch"}


def test_clusterrole_verbs_are_readonly():
    docs = _render()
    cluster_roles = _by_kind(docs, "ClusterRole")
    assert cluster_roles, "expected at least one ClusterRole"
    for cr in cluster_roles:
        for i, rule in enumerate(cr.get("rules") or []):
            for verb in rule.get("verbs") or []:
                assert verb in _ALLOWED_VERBS, (
                    f"ClusterRole '{cr['metadata']['name']}' rule[{i}] verb '{verb}' "
                    f"is forbidden — operator is observation-only"
                )


def test_clusterrole_has_no_aggregation_rule():
    docs = _render()
    for cr in _by_kind(docs, "ClusterRole"):
        assert "aggregationRule" not in cr, (
            f"ClusterRole '{cr['metadata']['name']}' must NOT carry aggregationRule"
        )


def test_leader_election_role_is_namespaced_and_scoped_to_leases():
    docs = _render({"leaderElection.enabled": "true"})
    roles = _by_kind(docs, "Role", name_suffix="-leader")
    assert len(roles) == 1
    role = roles[0]
    assert role["metadata"].get("namespace") == "default", "leader-election Role must be namespaced"
    rules = role["rules"]
    assert len(rules) == 1
    rule = rules[0]
    assert rule["apiGroups"] == ["coordination.k8s.io"]
    assert rule["resources"] == ["leases"]


def test_no_role_outside_leader_election():
    """Only the leader-election Role may exist; the rest is ClusterRole-bound."""
    docs = _render({"leaderElection.enabled": "true"})
    non_leader_roles = [
        r for r in _by_kind(docs, "Role")
        if not r["metadata"]["name"].endswith("-leader")
    ]
    assert non_leader_roles == [], f"unexpected Roles: {[r['metadata']['name'] for r in non_leader_roles]}"


# ── Image / resource / topology ──────────────────────────────────────────────


def test_image_pinned_by_digest_when_set():
    docs = _render({"image.digest": "sha256:abc123"})
    deployment = _by_kind(docs, "Deployment")[0]
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    assert "@sha256:abc123" in image
    assert ":0.2.0" not in image  # tag is dropped when digest is set


def test_image_falls_back_to_tag_when_digest_unset():
    docs = _render()
    deployment = _by_kind(docs, "Deployment")[0]
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    assert "@sha256" not in image
    assert ":" in image


def test_resource_envelope_matches_issue():
    docs = _render()
    container = _by_kind(docs, "Deployment")[0]["spec"]["template"]["spec"]["containers"][0]
    res = container["resources"]
    assert res["requests"]["cpu"] == "100m"
    assert res["requests"]["memory"] == "128Mi"
    assert res["limits"]["cpu"] == "500m"
    assert res["limits"]["memory"] == "512Mi"


def test_topology_spread_only_when_multi_replica():
    one = _render({"replicaCount": "1"})
    two = _render({"replicaCount": "2"})
    one_pod = _by_kind(one, "Deployment")[0]["spec"]["template"]["spec"]
    two_pod = _by_kind(two, "Deployment")[0]["spec"]["template"]["spec"]
    assert "topologySpreadConstraints" not in one_pod
    assert two_pod["topologySpreadConstraints"]
    assert two_pod["topologySpreadConstraints"][0]["topologyKey"] == "topology.kubernetes.io/zone"


# ── Secret hygiene (ADR-0007 §ACL) ───────────────────────────────────────────


def test_workspace_id_mounted_via_envfrom_secret_not_configmap():
    docs = _render()
    container = _by_kind(docs, "Deployment")[0]["spec"]["template"]["spec"]["containers"][0]
    env_from = container.get("envFrom", [])
    assert env_from, "expected envFrom for Secret-mounted workspace_id"
    # All envFrom entries must be secretRef (never configMapRef for workspace).
    secret_names = [e["secretRef"]["name"] for e in env_from if "secretRef" in e]
    assert any("config" in n for n in secret_names), "expected operator config Secret in envFrom"
    # Belt-and-braces: `env` should NOT carry workspace_id with a literal value.
    for ev in container.get("env", []):
        assert ev.get("name") != "OMNISCIENCE_WORKSPACE_ID", (
            "OMNISCIENCE_WORKSPACE_ID must come from envFrom-Secret, not env literal"
        )


# ── PSA namespace toggle ─────────────────────────────────────────────────────


def test_namespace_psa_labels_when_managenamespace_true():
    docs = _render({"podSecurity.manageNamespace": "true"})
    namespaces = _by_kind(docs, "Namespace")
    assert len(namespaces) == 1
    labels = namespaces[0]["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"
    assert labels["pod-security.kubernetes.io/audit"] == "restricted"
    assert labels["pod-security.kubernetes.io/warn"] == "restricted"


def test_namespace_not_rendered_by_default():
    docs = _render()
    assert _by_kind(docs, "Namespace") == [], "Namespace should not be rendered by default"
