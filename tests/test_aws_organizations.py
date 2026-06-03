"""Tests for the AWS Organizations ingestion path (Issue #90 / feat/aws-organizations-connector).

Coverage:
- AwsConfig.include_organizations flag (default False, opt-in)
- _collect_ous: flat OUs, nested recursion, filter exclusion
- _discover_organizations: org + OU + accounts happy path
- _discover_organizations: AccessDeniedException degrades gracefully (returns [])
- _discover_organizations: AWSOrganizationsNotInUseException degrades gracefully
- _discover_organizations: empty org_id returns []
- _discover_organizations: list_roots ClientError logs warning + continues
- _discover_organizations: list_accounts ClientError logs warning + continues
- _discover_organizations: list_parents failure logs warning, account still emitted
- _paginate_org: ThrottlingException triggers backoff retry then succeeds
- _paginate_org: ThrottlingException exhausts retries and re-raises
- account→OU parent mapping in metadata
- URI and ARN format for org, OU, account
- AwsConnector.discover: organizations NOT called when include_organizations=False
- AwsConnector.discover: organizations called when include_organizations=True
- AwsConnector.fetch: aws_organization dispatches to _fetch_org_organization
- AwsConnector.fetch: aws_organizations_ou dispatches to _fetch_org_ou
- AwsConnector.fetch: aws_organizations_account dispatches to _fetch_org_account
- _fetch_org_organization: success + ClientError silenced
- _fetch_org_ou: success + ClientError silenced
- _fetch_org_account: success + ClientError silenced

Total: 22 tests
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from omniscience_connectors.aws.connector import (
    AwsConfig,
    AwsConnector,
    _collect_ous,
    _discover_organizations,
    _fetch_org_account,
    _fetch_org_organization,
    _fetch_org_ou,
    _paginate_org,
)
from omniscience_connectors.base import DocumentRef

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ACCOUNT_ID = "123456789012"
ORG_ID = "o-exampleorgid"
OU_ID_1 = "ou-root-aaaa"
OU_ID_2 = "ou-root-bbbb"
OU_ID_CHILD = "ou-aaaa-cccc"

SECRETS: dict[str, str] = {
    "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
}


def _client_error(code: str, message: str = "error") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Operation")


def _org_arn(org_id: str) -> str:
    return f"arn:aws:organizations::{org_id}:organization/{org_id}"


def _ou_arn(org_id: str, ou_id: str) -> str:
    return f"arn:aws:organizations::{org_id}:ou/{org_id}/{ou_id}"


def _acct_arn(org_id: str, acct_id: str) -> str:
    return f"arn:aws:organizations::{org_id}:account/{org_id}/{acct_id}"


def _make_org(org_id: str = ORG_ID, master: str = ACCOUNT_ID) -> dict[str, Any]:
    return {
        "Id": org_id,
        "Arn": _org_arn(org_id),
        "FeatureSet": "ALL",
        "MasterAccountId": master,
        "MasterAccountEmail": "master@example.com",
        "AvailablePolicyTypes": [{"Type": "SERVICE_CONTROL_POLICY", "Status": "ENABLED"}],
    }


def _make_ou(ou_id: str, name: str, org_id: str = ORG_ID) -> dict[str, Any]:
    return {"Id": ou_id, "Name": name, "Arn": _ou_arn(org_id, ou_id)}


def _make_account(
    acct_id: str,
    name: str,
    org_id: str = ORG_ID,
    status: str = "ACTIVE",
) -> dict[str, Any]:
    return {
        "Id": acct_id,
        "Name": name,
        "Arn": _acct_arn(org_id, acct_id),
        "Email": f"{name}@example.com",
        "Status": status,
        "JoinedMethod": "INVITED",
        "JoinedTimestamp": "2023-01-01T00:00:00+00:00",
    }


def _make_org_client(
    org: dict[str, Any] | None = None,
    roots: list[dict[str, Any]] | None = None,
    ous_by_parent: dict[str, list[dict[str, Any]]] | None = None,
    accounts: list[dict[str, Any]] | None = None,
    parents_by_child: dict[str, list[dict[str, Any]]] | None = None,
) -> MagicMock:
    """Build a fully mocked boto3 organizations client.

    Args:
        org:             Result of describe_organization (without wrapping key).
        roots:           Items returned by list_roots.
        ous_by_parent:   Mapping parent_id -> list of OUs.
        accounts:        Items returned by list_accounts.
        parents_by_child: Mapping child_id -> list of parents.
    """
    client = MagicMock()

    org_data = org or _make_org()
    client.describe_organization.return_value = {"Organization": org_data}

    def _paginator(method: str) -> MagicMock:
        pag = MagicMock()

        if method == "list_roots":
            pag.paginate.return_value = iter([{"Roots": roots or [{"Id": "r-root"}]}])

        elif method == "list_organizational_units_for_parent":
            def _ou_pages(**kwargs: Any) -> Any:
                parent_id = kwargs.get("ParentId", "")
                items = (ous_by_parent or {}).get(parent_id, [])
                return iter([{"OrganizationalUnits": items}])
            pag.paginate.side_effect = _ou_pages

        elif method == "list_accounts":
            pag.paginate.return_value = iter([{"Accounts": accounts or []}])

        elif method == "list_parents":
            def _parent_pages(**kwargs: Any) -> Any:
                child_id = kwargs.get("ChildId", "")
                items = (parents_by_child or {}).get(child_id, [])
                return iter([{"Parents": items}])
            pag.paginate.side_effect = _parent_pages

        return pag

    client.get_paginator.side_effect = _paginator
    return client


# ---------------------------------------------------------------------------
# AwsConfig.include_organizations
# ---------------------------------------------------------------------------


class TestAwsConfigOrganizations:
    def test_default_is_false(self) -> None:
        cfg = AwsConfig()
        assert cfg.include_organizations is False

    def test_can_be_enabled(self) -> None:
        cfg = AwsConfig(include_organizations=True)
        assert cfg.include_organizations is True

    def test_existing_defaults_unchanged(self) -> None:
        cfg = AwsConfig(include_organizations=True)
        assert cfg.services == ["s3", "iam", "ec2"]
        assert cfg.regions == ["us-east-1"]


# ---------------------------------------------------------------------------
# _paginate_org
# ---------------------------------------------------------------------------


class TestPaginateOrg:
    def test_returns_flat_list_across_pages(self) -> None:
        client = MagicMock()
        pag = MagicMock()
        pag.paginate.return_value = iter(
            [{"Accounts": [{"Id": "111"}]}, {"Accounts": [{"Id": "222"}]}]
        )
        client.get_paginator.return_value = pag

        result = _paginate_org(client, "list_accounts", "Accounts")
        assert [r["Id"] for r in result] == ["111", "222"]

    def test_throttle_retries_then_succeeds(self) -> None:
        """First call raises ThrottlingException; second succeeds."""
        client = MagicMock()
        pag = MagicMock()
        throttle_error = _client_error("ThrottlingException")
        ok_page = [{"Accounts": [{"Id": "111"}]}]

        call_count = 0

        def _paginate(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise throttle_error
            return iter(ok_page)

        pag.paginate.side_effect = _paginate
        client.get_paginator.return_value = pag

        with patch("omniscience_connectors.aws.connector.time.sleep") as mock_sleep:
            result = _paginate_org(client, "list_accounts", "Accounts")

        assert [r["Id"] for r in result] == ["111"]
        mock_sleep.assert_called_once()

    def test_throttle_exhausts_retries_raises(self) -> None:
        """Persistent ThrottlingException re-raises after max retries."""
        client = MagicMock()
        pag = MagicMock()
        pag.paginate.side_effect = _client_error("ThrottlingException")
        client.get_paginator.return_value = pag

        with (
            patch("omniscience_connectors.aws.connector.time.sleep"),
            pytest.raises(ClientError),
        ):
            _paginate_org(client, "list_accounts", "Accounts")


# ---------------------------------------------------------------------------
# _collect_ous
# ---------------------------------------------------------------------------


class TestCollectOus:
    def test_flat_ous_returned(self) -> None:
        client = _make_org_client(
            ous_by_parent={"r-root": [_make_ou(OU_ID_1, "Engineering")]},
        )
        refs = _collect_ous(client, "r-root", "/root", ORG_ID, [])
        assert len(refs) == 1
        ref = refs[0]
        assert ref.metadata["resource_type"] == "aws_organizations_ou"
        assert ref.metadata["ou_id"] == OU_ID_1
        assert ref.metadata["name"] == "Engineering"
        assert ref.uri == f"aws://org/{ORG_ID}/ou/{OU_ID_1}"

    def test_nested_recursion(self) -> None:
        """Root OU has one child OU which has a grandchild OU."""
        client = _make_org_client(
            ous_by_parent={
                "r-root": [_make_ou(OU_ID_1, "Engineering")],
                OU_ID_1: [_make_ou(OU_ID_CHILD, "Backend")],
                OU_ID_CHILD: [],
            },
        )
        refs = _collect_ous(client, "r-root", "/root", ORG_ID, [])
        ou_ids = {r.metadata["ou_id"] for r in refs}
        assert ou_ids == {OU_ID_1, OU_ID_CHILD}

    def test_path_is_built_correctly(self) -> None:
        client = _make_org_client(
            ous_by_parent={
                "r-root": [_make_ou(OU_ID_1, "Engineering")],
                OU_ID_1: [_make_ou(OU_ID_CHILD, "Backend")],
                OU_ID_CHILD: [],
            },
        )
        refs = _collect_ous(client, "r-root", "/root", ORG_ID, [])
        paths = {r.metadata["path"] for r in refs}
        assert "/root/Engineering" in paths
        assert "/root/Engineering/Backend" in paths

    def test_filter_excludes_ou(self) -> None:
        client = _make_org_client(
            ous_by_parent={"r-root": [_make_ou(OU_ID_1, "Engineering")]},
        )
        refs = _collect_ous(client, "r-root", "/root", ORG_ID, ["aws_organization"])
        assert refs == []

    def test_empty_parent_returns_empty(self) -> None:
        client = _make_org_client(ous_by_parent={"r-root": []})
        refs = _collect_ous(client, "r-root", "/root", ORG_ID, [])
        assert refs == []


# ---------------------------------------------------------------------------
# _discover_organizations
# ---------------------------------------------------------------------------


class TestDiscoverOrganizations:
    def test_happy_path_yields_org_ou_account(self) -> None:
        acct = _make_account("222222222222", "production")
        client = _make_org_client(
            ous_by_parent={
                "r-root": [_make_ou(OU_ID_1, "Engineering")],
                OU_ID_1: [],
            },
            accounts=[acct],
            parents_by_child={"222222222222": [{"Id": OU_ID_1, "Type": "ORGANIZATIONAL_UNIT"}]},
        )

        with patch(
            "omniscience_connectors.aws.connector._org_client", return_value=client
        ):
            refs = _discover_organizations(SECRETS, [])

        resource_types = {r.metadata["resource_type"] for r in refs}
        assert "aws_organization" in resource_types
        assert "aws_organizations_ou" in resource_types
        assert "aws_organizations_account" in resource_types

    def test_org_uri_format(self) -> None:
        client = _make_org_client(accounts=[])
        with patch("omniscience_connectors.aws.connector._org_client", return_value=client):
            refs = _discover_organizations(SECRETS, [])

        org_refs = [r for r in refs if r.metadata["resource_type"] == "aws_organization"]
        assert len(org_refs) == 1
        assert org_refs[0].uri == f"aws://org/{ORG_ID}"
        assert org_refs[0].external_id == _org_arn(ORG_ID)

    def test_account_uri_format(self) -> None:
        acct_id = "333333333333"
        acct = _make_account(acct_id, "staging")
        client = _make_org_client(accounts=[acct])
        with patch("omniscience_connectors.aws.connector._org_client", return_value=client):
            refs = _discover_organizations(SECRETS, [])

        acct_refs = [r for r in refs if r.metadata["resource_type"] == "aws_organizations_account"]
        assert len(acct_refs) == 1
        assert acct_refs[0].uri == f"aws://org/{ORG_ID}/account/{acct_id}"

    def test_account_parent_id_mapped(self) -> None:
        acct_id = "444444444444"
        acct = _make_account(acct_id, "dev")
        client = _make_org_client(
            accounts=[acct],
            parents_by_child={acct_id: [{"Id": OU_ID_1, "Type": "ORGANIZATIONAL_UNIT"}]},
        )
        with patch("omniscience_connectors.aws.connector._org_client", return_value=client):
            refs = _discover_organizations(SECRETS, [])

        acct_refs = [r for r in refs if r.metadata["resource_type"] == "aws_organizations_account"]
        assert acct_refs[0].metadata["parent_id"] == OU_ID_1

    def test_access_denied_returns_empty(self) -> None:
        client = MagicMock()
        client.describe_organization.side_effect = _client_error("AccessDeniedException")

        with patch("omniscience_connectors.aws.connector._org_client", return_value=client):
            refs = _discover_organizations(SECRETS, [])

        assert refs == []

    def test_not_in_use_returns_empty(self) -> None:
        client = MagicMock()
        client.describe_organization.side_effect = _client_error(
            "AWSOrganizationsNotInUseException"
        )

        with patch("omniscience_connectors.aws.connector._org_client", return_value=client):
            refs = _discover_organizations(SECRETS, [])

        assert refs == []

    def test_empty_org_id_returns_empty(self) -> None:
        client = MagicMock()
        # Return an org dict missing the Id field
        client.describe_organization.return_value = {"Organization": {"FeatureSet": "ALL"}}

        with patch("omniscience_connectors.aws.connector._org_client", return_value=client):
            refs = _discover_organizations(SECRETS, [])

        assert refs == []

    def test_list_roots_failure_logged_and_continues(self) -> None:
        client = MagicMock()
        client.describe_organization.return_value = {"Organization": _make_org()}

        # list_roots raises; list_accounts succeeds with one account
        acct = _make_account("555555555555", "security")
        list_roots_pag = MagicMock()
        list_roots_pag.paginate.side_effect = _client_error("AccessDeniedException")

        list_accounts_pag = MagicMock()
        list_accounts_pag.paginate.return_value = iter([{"Accounts": [acct]}])

        list_parents_pag = MagicMock()
        list_parents_pag.paginate.side_effect = lambda **kwargs: iter([{"Parents": []}])

        def _pag(method: str) -> MagicMock:
            if method == "list_roots":
                return list_roots_pag
            if method == "list_accounts":
                return list_accounts_pag
            if method == "list_parents":
                return list_parents_pag
            return MagicMock()

        client.get_paginator.side_effect = _pag

        with patch("omniscience_connectors.aws.connector._org_client", return_value=client):
            refs = _discover_organizations(SECRETS, [])

        # Should still yield the org and the account despite roots failing
        rtypes = {r.metadata["resource_type"] for r in refs}
        assert "aws_organization" in rtypes
        assert "aws_organizations_account" in rtypes

    def test_list_accounts_failure_logged_and_continues(self) -> None:
        client = _make_org_client()  # no accounts key → default empty
        # Override list_accounts paginator to raise
        real_pag_factory = client.get_paginator.side_effect

        def _pag_with_acct_error(method: str) -> MagicMock:
            pag = real_pag_factory(method)
            if method == "list_accounts":
                pag.paginate.side_effect = _client_error("AccessDeniedException")
            return pag

        client.get_paginator.side_effect = _pag_with_acct_error

        with patch("omniscience_connectors.aws.connector._org_client", return_value=client):
            refs = _discover_organizations(SECRETS, [])

        # Org should still be discovered; just no account refs
        rtypes = {r.metadata["resource_type"] for r in refs}
        assert "aws_organization" in rtypes
        assert "aws_organizations_account" not in rtypes

    def test_list_parents_failure_account_still_emitted(self) -> None:
        acct_id = "666666666666"
        acct = _make_account(acct_id, "sandbox")
        client = _make_org_client(accounts=[acct])

        # Override list_parents paginator to raise
        real_pag_factory = client.get_paginator.side_effect

        def _pag_with_parents_error(method: str) -> MagicMock:
            pag = real_pag_factory(method)
            if method == "list_parents":
                pag.paginate.side_effect = _client_error("AccessDeniedException")
            return pag

        client.get_paginator.side_effect = _pag_with_parents_error

        with patch("omniscience_connectors.aws.connector._org_client", return_value=client):
            refs = _discover_organizations(SECRETS, [])

        acct_refs = [r for r in refs if r.metadata["resource_type"] == "aws_organizations_account"]
        assert len(acct_refs) == 1
        assert acct_refs[0].metadata["account_id"] == acct_id
        assert acct_refs[0].metadata["parent_id"] == ""


# ---------------------------------------------------------------------------
# AwsConnector.discover integration
# ---------------------------------------------------------------------------


class TestAwsConnectorDiscoverOrganizations:
    @pytest.fixture()
    def connector(self) -> AwsConnector:
        return AwsConnector()

    async def test_organizations_not_called_by_default(self, connector: AwsConnector) -> None:
        config = AwsConfig(services=[])  # no standard services

        with (
            patch(
                "omniscience_connectors.aws.connector._get_account_id",
                return_value=ACCOUNT_ID,
            ),
            patch(
                "omniscience_connectors.aws.connector._discover_organizations"
            ) as mock_org,
        ):
            refs = [ref async for ref in connector.discover(config, SECRETS)]

        mock_org.assert_not_called()
        assert refs == []

    async def test_organizations_called_when_enabled(self, connector: AwsConnector) -> None:
        config = AwsConfig(services=[], include_organizations=True)

        org_ref = DocumentRef(
            external_id=_org_arn(ORG_ID),
            uri=f"aws://org/{ORG_ID}",
            metadata={"kind": "aws_live", "resource_type": "aws_organization"},
        )

        with (
            patch(
                "omniscience_connectors.aws.connector._get_account_id",
                return_value=ACCOUNT_ID,
            ),
            patch(
                "omniscience_connectors.aws.connector._discover_organizations",
                return_value=[org_ref],
            ) as mock_org,
        ):
            refs = [ref async for ref in connector.discover(config, SECRETS)]

        mock_org.assert_called_once()
        assert len(refs) == 1
        assert refs[0].metadata["resource_type"] == "aws_organization"


# ---------------------------------------------------------------------------
# AwsConnector.fetch for Organizations resources
# ---------------------------------------------------------------------------


class TestAwsConnectorFetchOrganizations:
    @pytest.fixture()
    def connector(self) -> AwsConnector:
        return AwsConnector()

    @pytest.fixture()
    def config(self) -> AwsConfig:
        return AwsConfig(include_organizations=True)

    def _make_ref(self, resource_type: str, **extra: Any) -> DocumentRef:
        meta: dict[str, Any] = {
            "kind": "aws_live",
            "resource_type": resource_type,
            "service": "organizations",
            "region": "global",
            **extra,
        }
        return DocumentRef(
            external_id=meta.get("arn", f"arn:aws:organizations::test:{resource_type}"),
            uri=f"aws://org/{ORG_ID}/test",
            metadata=meta,
        )

    async def test_fetch_organization(
        self, connector: AwsConnector, config: AwsConfig
    ) -> None:
        mock_client = MagicMock()
        mock_client.describe_organization.return_value = {"Organization": _make_org()}
        ref = self._make_ref("aws_organization", org_id=ORG_ID)

        with patch(
            "omniscience_connectors.aws.connector._org_client", return_value=mock_client
        ):
            doc = await connector.fetch(config, SECRETS, ref)

        data = json.loads(doc.content_bytes)
        assert data["id"] == ORG_ID
        assert data["master_account_id"] == ACCOUNT_ID
        assert doc.content_type == "application/json"

    async def test_fetch_ou(self, connector: AwsConnector, config: AwsConfig) -> None:
        mock_client = MagicMock()
        mock_client.describe_organizational_unit.return_value = {
            "OrganizationalUnit": _make_ou(OU_ID_1, "Engineering")
        }
        ref = self._make_ref("aws_organizations_ou", ou_id=OU_ID_1)

        with patch(
            "omniscience_connectors.aws.connector._org_client", return_value=mock_client
        ):
            doc = await connector.fetch(config, SECRETS, ref)

        data = json.loads(doc.content_bytes)
        assert data["id"] == OU_ID_1
        assert data["name"] == "Engineering"

    async def test_fetch_account(self, connector: AwsConnector, config: AwsConfig) -> None:
        acct_id = "777777777777"
        mock_client = MagicMock()
        mock_client.describe_account.return_value = {
            "Account": _make_account(acct_id, "prod")
        }
        ref = self._make_ref("aws_organizations_account", account_id=acct_id)

        with patch(
            "omniscience_connectors.aws.connector._org_client", return_value=mock_client
        ):
            doc = await connector.fetch(config, SECRETS, ref)

        data = json.loads(doc.content_bytes)
        assert data["id"] == acct_id
        assert data["name"] == "prod"
        assert data["status"] == "ACTIVE"


# ---------------------------------------------------------------------------
# _fetch_org_* helpers
# ---------------------------------------------------------------------------


class TestFetchOrgHelpers:
    def test_fetch_org_organization_success(self) -> None:
        client = MagicMock()
        client.describe_organization.return_value = {"Organization": _make_org()}
        result = _fetch_org_organization(client, ORG_ID)
        assert result["id"] == ORG_ID
        assert result["feature_set"] == "ALL"
        assert "SERVICE_CONTROL_POLICY" in result["available_policy_types"]

    def test_fetch_org_organization_client_error_silenced(self) -> None:
        client = MagicMock()
        client.describe_organization.side_effect = _client_error("AccessDeniedException")
        result = _fetch_org_organization(client, ORG_ID)
        assert result == {"org_id": ORG_ID}

    def test_fetch_org_ou_success(self) -> None:
        client = MagicMock()
        client.describe_organizational_unit.return_value = {
            "OrganizationalUnit": _make_ou(OU_ID_1, "Security")
        }
        result = _fetch_org_ou(client, OU_ID_1)
        assert result["id"] == OU_ID_1
        assert result["name"] == "Security"

    def test_fetch_org_ou_client_error_silenced(self) -> None:
        client = MagicMock()
        client.describe_organizational_unit.side_effect = _client_error("AccessDeniedException")
        result = _fetch_org_ou(client, OU_ID_1)
        assert result == {"ou_id": OU_ID_1}

    def test_fetch_org_account_success(self) -> None:
        acct_id = "888888888888"
        client = MagicMock()
        client.describe_account.return_value = {"Account": _make_account(acct_id, "payments")}
        result = _fetch_org_account(client, acct_id)
        assert result["id"] == acct_id
        assert result["name"] == "payments"
        assert result["email"] == "payments@example.com"

    def test_fetch_org_account_client_error_silenced(self) -> None:
        client = MagicMock()
        client.describe_account.side_effect = _client_error("AccountNotFoundException")
        result = _fetch_org_account(client, "999999999999")
        assert result == {"account_id": "999999999999"}
