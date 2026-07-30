"""AC-SP95-1 / AC-SP95-6: static invariants of the namespaced local Compose
fragment -- exact owner-prefixed service/network/volume inventory, digest-pinned
datastore images (no mutable tags), a single loopback host binding, and the
egress-isolated private network. Text-based (no YAML dependency) so it runs in
any environment the rest of the suite runs in.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = ROOT / "deploy" / "compose" / "management-readonly-local"
COMPOSE = FRAGMENT / "compose.yaml"
COMPOSE_SOURCE = FRAGMENT / "compose.source.yaml"

EXPECTED_SERVICES = {
    "omniscience-api",
    "omniscience-admin",
    "omniscience-postgres",
    "omniscience-nats",
    "omniscience-neo4j",
    "omniscience-qdrant",
    "omniscience-migrate",
}


def _compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def test_exact_owner_prefixed_service_inventory() -> None:
    text = _compose_text()
    for svc in EXPECTED_SERVICES:
        assert re.search(rf"^  {re.escape(svc)}:", text, re.MULTILINE), f"missing service {svc}"


def test_no_generic_mock_omnius_or_sibling_resources() -> None:
    text = _compose_text()
    for forbidden in ("mock-", "omnius", "selected-owner", "barbarossa-", "portal-"):
        assert forbidden not in text, f"forbidden token present: {forbidden}"
    # No bare generic service keys.
    for generic in ("\n  app:", "\n  postgres:", "\n  nats:", "\n  neo4j:", "\n  qdrant:"):
        assert generic not in text


def test_datastore_images_are_digest_pinned_no_mutable_tags() -> None:
    text = _compose_text()
    assert ":latest" not in text
    for line in text.splitlines():
        m = re.search(r"image:\s*(\S+)", line)
        if not m:
            continue
        ref = m.group(1)
        if any(store in ref for store in ("postgres", "nats:", "neo4j", "qdrant")):
            assert "@sha256:" in ref, f"datastore image not digest-pinned: {ref}"


def test_single_loopback_host_binding_admin_only() -> None:
    text = _compose_text()
    bindings = re.findall(r'"(127\.0\.0\.1:[^"]+)"', text)
    assert len(bindings) == 1, f"expected exactly one host binding, got {bindings}"
    assert bindings[0].endswith(":80"), bindings[0]
    # No non-loopback publish and no store/broker host port.
    assert "0.0.0.0:" not in text
    for port in ("5432:", "6333:", "6334:", "7687:", "7474:", "4222:", "8000:"):
        assert f"127.0.0.1:{port}" not in text


def test_private_network_is_egress_isolated() -> None:
    text = _compose_text()
    assert "omniscience-private:" in text
    assert "omniscience-readnet:" in text
    # Scope to the top-level `networks:` section (services also reference these
    # names). The private store network is internal (no external egress).
    networks_block = text.split("\nnetworks:", 1)[1].split("\nvolumes:", 1)[0]
    private_block = networks_block.split("omniscience-private:", 1)[1]
    assert "internal: true" in private_block


def test_source_override_builds_owner_images_only() -> None:
    src = COMPOSE_SOURCE.read_text(encoding="utf-8")
    for svc in ("omniscience-api", "omniscience-migrate", "omniscience-admin"):
        assert svc in src
    assert "build:" in src
    assert "INSTALL_LOCAL_EMBEDDINGS" in src


def test_migrate_is_a_one_shot_not_a_steady_owner() -> None:
    text = _compose_text()
    migrate_block = text.split("omniscience-migrate:", 1)[1].split("omniscience-admin:", 1)[0]
    assert 'restart: "no"' in migrate_block
    assert "alembic" in migrate_block
