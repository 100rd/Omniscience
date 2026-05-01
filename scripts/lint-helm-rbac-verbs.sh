#!/usr/bin/env bash
# scripts/lint-helm-rbac-verbs.sh
#
# Verb-allowlist lint check for the omniscience-operator Helm chart (#166).
#
# Renders the chart and asserts:
#   1. Every ClusterRole rule uses ONLY verbs from the read-only allow-list:
#        get, list, watch
#      Any of {create, update, patch, delete, deletecollection, *} is a hard
#      failure — the operator is observation-only per ADR-0007.
#   2. ClusterRoles do NOT carry an `aggregationRule` (operator does not
#      participate in role aggregation).
#   3. The leader-election Role (namespaced, name suffix `-leader`) is the
#      ONLY object permitted to carry write verbs, and only on the
#      `coordination.k8s.io/leases` resource.
#
# Exit codes:
#   0 — all checks pass
#   1 — forbidden verb on a ClusterRole
#   2 — aggregationRule present on a ClusterRole
#   3 — leader-election Role rules other than coordination/leases
#   4 — helm not installed / chart render failed / parser unavailable
#
# Usage:
#   scripts/lint-helm-rbac-verbs.sh [chart-path]
#
# Default chart path: helm/omniscience-operator
#
# This script runs in CI (.github/workflows/operator.yml) and locally. It is
# the canonical CI gate for issue #166's "no write verbs leak in" invariant.
set -euo pipefail

CHART_PATH="${1:-helm/omniscience-operator}"
ALLOWED_VERBS_REGEX='^(get|list|watch)$'

# Required chart values to render (chart fails closed without them).
WORKSPACE_ID="11111111-2222-3333-4444-555555555555"
CLUSTER_NAME="ci-lint"
NATS_URL="nats://nats:4222"

if ! command -v helm >/dev/null 2>&1; then
  echo "FAIL: helm not installed" >&2
  exit 4
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "FAIL: python3 not installed (needed for YAML parser)" >&2
  exit 4
fi

# Render the chart with leader-election ON so the namespaced Role is exercised.
RENDERED=$(helm template release-name "$CHART_PATH" \
  --set "workspaceId=$WORKSPACE_ID" \
  --set "clusterName=$CLUSTER_NAME" \
  --set "nats.url=$NATS_URL" \
  --set "leaderElection.enabled=true" 2>/dev/null) || {
  echo "FAIL: helm template failed for $CHART_PATH" >&2
  exit 4
}

# Hand off to a Python YAML walker — bash + grep is too lossy for nested rules.
# The Python script reads the rendered manifest from $RENDERED via an env var
# rather than stdin (avoids stdin conflict between the heredoc and the
# rendered manifest data).
RENDERED="$RENDERED" ALLOWED_VERBS_REGEX="$ALLOWED_VERBS_REGEX" python3 <<'PY'
import os
import re
import sys
import yaml

allowed = re.compile(os.environ["ALLOWED_VERBS_REGEX"])
errors = []

for doc in yaml.safe_load_all(os.environ["RENDERED"]):
    if not doc or not isinstance(doc, dict):
        continue
    kind = doc.get("kind")
    name = (doc.get("metadata") or {}).get("name", "<no-name>")

    if kind == "ClusterRole":
        # Invariant: no aggregationRule.
        if "aggregationRule" in doc:
            errors.append((2, f"ClusterRole '{name}' must NOT carry aggregationRule (operator does not aggregate)"))
        # Invariant: every verb in every rule is in the read-only allow-list.
        for i, rule in enumerate(doc.get("rules") or []):
            verbs = rule.get("verbs") or []
            for verb in verbs:
                if not allowed.match(verb):
                    resources = ",".join(rule.get("resources") or ["?"])
                    api_groups = ",".join(rule.get("apiGroups") or [""])
                    errors.append((
                        1,
                        f"ClusterRole '{name}' rule[{i}] has FORBIDDEN verb '{verb}' "
                        f"on apiGroups=[{api_groups}] resources=[{resources}] "
                        f"(allowed: get,list,watch)"
                    ))

    elif kind == "Role":
        # Only the leader-election Role is permitted to carry write verbs,
        # and only on the coordination.k8s.io/leases resource.
        is_leader_role = name.endswith("-leader")
        for i, rule in enumerate(doc.get("rules") or []):
            api_groups = rule.get("apiGroups") or []
            resources = rule.get("resources") or []
            verbs = rule.get("verbs") or []
            if is_leader_role:
                # Leader-election Role: must be exclusively coordination/leases.
                bad_groups = [g for g in api_groups if g != "coordination.k8s.io"]
                bad_resources = [r for r in resources if r != "leases"]
                if bad_groups or bad_resources:
                    errors.append((
                        3,
                        f"Leader-election Role '{name}' rule[{i}] permitted only on "
                        f"coordination.k8s.io/leases (got apiGroups={api_groups} "
                        f"resources={resources})"
                    ))
            else:
                # Any other Role must use the read-only verb set too.
                for verb in verbs:
                    if not allowed.match(verb):
                        errors.append((
                            1,
                            f"Role '{name}' rule[{i}] has FORBIDDEN verb '{verb}' "
                            f"(only the leader-election Role may carry write verbs)"
                        ))

if errors:
    # First non-zero code wins (preserves the most-specific signal).
    code = errors[0][0]
    print("RBAC VERB LINT — FAIL", file=sys.stderr)
    for _, msg in errors:
        print(f"  - {msg}", file=sys.stderr)
    sys.exit(code)

print("RBAC VERB LINT — PASS (all ClusterRole verbs in {get,list,watch}, no aggregationRule, leader-election Role scoped to coordination/leases)")
PY
