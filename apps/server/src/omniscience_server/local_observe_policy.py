"""Identities shared by the isolated local Observe seed and read-token broker."""

from uuid import NAMESPACE_URL, UUID, uuid5

PROFILE_ID = "management-local-observe-v1"
BROKER_SCOPE = "local-observe:mint-read"
BROKER_MAX_SECONDS = 900


def fixture_id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"urn:omniscience:{PROFILE_ID}:{name}")


BROKER_ID = fixture_id("broker")
WORKSPACES = {slug: fixture_id(slug) for slug in ("alpha", "beta")}


def workspace_name(slug: str) -> str:
    return f"local-observe-{slug}"
