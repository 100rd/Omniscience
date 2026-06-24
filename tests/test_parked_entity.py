import uuid
from datetime import UTC, datetime

import pytest
from omniscience_core.storage.graph import EntityNodeView
from omniscience_retrieval.probabilistic_scoring import (
    calculate_probabilistic_confidence,
    calculate_probabilistic_incident_confidence,
)


def test_entity_node_view_parked():
    node = EntityNodeView(
        id=uuid.uuid4(),
        name="test_entity",
        kind="resource",
        source="aws",
        chunk_text=None,
        is_parked=True,
    )
    assert node.is_parked


def test_scoring_penalized_when_parked():
    score_normal = calculate_probabilistic_confidence(
        source="aws", valid_from=datetime.now(UTC), is_parked=False
    )
    score_parked = calculate_probabilistic_confidence(
        source="aws", valid_from=datetime.now(UTC), is_parked=True
    )
    assert score_parked < score_normal
    assert score_parked == pytest.approx(score_normal * 0.1, abs=0.01)


class DummyNode:
    def __init__(self, is_parked=False):
        self.is_parked = is_parked


class DummyClassified:
    def __init__(self, pr=None, target=None):
        self.responsible_pr = pr
        self.target_resource = target


def test_incident_scoring_penalized_when_parked():
    # Normal case
    alert = DummyNode(is_parked=False)
    target = DummyNode(is_parked=False)
    classified = DummyClassified(target=target)

    conf_normal, _ = calculate_probabilistic_incident_confidence(
        alert=alert, classified=classified, max_depth=3
    )

    # Parked alert case
    alert_parked = DummyNode(is_parked=True)
    conf_parked_alert, prov_parked_alert = calculate_probabilistic_incident_confidence(
        alert=alert_parked, classified=classified, max_depth=3
    )

    assert conf_parked_alert < conf_normal
    assert prov_parked_alert is True

    # Parked resource case
    target_parked = DummyNode(is_parked=True)
    classified_parked = DummyClassified(target=target_parked)
    conf_parked_res, prov_parked_res = calculate_probabilistic_incident_confidence(
        alert=alert, classified=classified_parked, max_depth=3
    )

    assert conf_parked_res < conf_normal
    assert prov_parked_res is True
