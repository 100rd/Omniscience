"""Posture-label separation assertions (AC-HA-5, REQ-OPS-6, issue #350).

`omniscience.io/posture` (evaluation | governed | production-ha) is a derived
label — see helm/omniscience/templates/_helpers.tpl `omniscience.posture` and
the fail-closed guard in `_production_guards.tpl`. A caller cannot claim
production-ha via `--set` without satisfying every AC-HA-1/2/3/REQ-OPS-6
guard first, because the guard `fail()`s the render before the posture
helper would ever compute "production-ha" for an unqualified install.

Rendered-label assertions require `helm template`, which renders directly —
`Chart.yaml` declares no subchart dependencies, so no `helm dependency build`
step is needed. They self-skip via the shared `_needs_helm` marker only when
the `helm` binary itself is unavailable on PATH.

Runner: pytest tests/test_helm_posture.py
"""

from __future__ import annotations

from tests.test_production_ha_render import (
    BASELINE_SET,
    VALID_PRODUCTION_SET,
    _by_kind,
    _needs_helm,
    _render,
)


@_needs_helm
class TestPostureLabel:
    def test_default_bundled_install_is_evaluation_posture(self):
        """REQ-OPS-6 fallback: no production.enabled and no evidence signal → evaluation."""
        docs = _render(BASELINE_SET)
        deployment = _by_kind(docs, "Deployment")[0]
        assert deployment["metadata"]["labels"]["omniscience.io/posture"] == "evaluation"

    def test_external_wiring_without_production_enabled_is_governed_posture(self):
        """REQ-OPS-6 intermediate signal: one evidence field set, production still disabled."""
        overrides = {
            **BASELINE_SET,
            "production.evidence.rds.endpoint": "external-rds.example.internal",
        }
        docs = _render(overrides)
        deployment = _by_kind(docs, "Deployment")[0]
        assert deployment["metadata"]["labels"]["omniscience.io/posture"] == "governed"

    def test_fully_qualified_production_is_production_ha_posture(self):
        docs = _render(VALID_PRODUCTION_SET)
        deployment = _by_kind(docs, "Deployment")[0]
        assert deployment["metadata"]["labels"]["omniscience.io/posture"] == "production-ha"
        # AC-HA-5 groundTruth: production-ha posture must co-occur with the
        # full HA scheduling policy, not just the label.
        assert _by_kind(docs, "PodDisruptionBudget")
        assert _by_kind(docs, "HorizontalPodAutoscaler")
        assert _by_kind(docs, "PriorityClass")
        pod_spec = deployment["spec"]["template"]["spec"]
        assert pod_spec["topologySpreadConstraints"]

    def test_service_object_also_carries_posture_label(self):
        docs = _render(VALID_PRODUCTION_SET)
        service = _by_kind(docs, "Service")[0]
        assert service["metadata"]["labels"]["omniscience.io/posture"] == "production-ha"

    def test_posture_boundary_progression_per_req_ops_6(self):
        """REQ-OPS-6: no signal -> evaluation; one evidence field -> governed;
        fully evidenced + production.enabled -> production-ha. Verifies the
        three postures are reached by strictly increasing configuration
        commitment, not by any single unrelated --set flag."""
        eval_docs = _render(BASELINE_SET)
        eval_posture = _by_kind(eval_docs, "Deployment")[0]["metadata"]["labels"][
            "omniscience.io/posture"
        ]

        governed_overrides = {
            **BASELINE_SET,
            "production.evidence.neo4j.topology": "causal-cluster",
        }
        governed_docs = _render(governed_overrides)
        governed_posture = _by_kind(governed_docs, "Deployment")[0]["metadata"]["labels"][
            "omniscience.io/posture"
        ]

        prod_docs = _render(VALID_PRODUCTION_SET)
        prod_posture = _by_kind(prod_docs, "Deployment")[0]["metadata"]["labels"][
            "omniscience.io/posture"
        ]

        assert eval_posture == "evaluation"
        assert governed_posture == "governed"
        assert prod_posture == "production-ha"
