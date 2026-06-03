"""AWS live-resources connector.

Discovers and describes live AWS resources across services and regions.
Uses boto3 wrapped in ``asyncio.to_thread`` for async compatibility.

Authentication is resolved from the ``secrets`` dict at call time:
- ``aws_access_key_id`` + ``aws_secret_access_key`` (+ optional ``aws_session_token``)
- ``aws_profile`` for SSO / named AWS profile
- Neither: falls back to the default credential chain (instance role, env vars, etc.)

Each discovered resource is tagged with ``kind="aws_live"`` and its ARN is stored
in ``extra`` metadata to enable ARN-based cross-source linking.

Supported services
------------------
- **s3** — list_buckets + describe each (tags, policy, versioning)  [global]
- **iam** — list_users, list_roles, list_policies                   [global]
- **ec2** — describe_instances, describe_vpcs, describe_security_groups [per-region]
- **organizations** — describe_organization, list roots/OUs (nested), list_accounts
  Opt-in via ``include_organizations=True`` in :class:`AwsConfig`.

URI format: ``aws://{account_id}/{region}/{service}/{resource_type}/{name}``
external_id: ARN (e.g. ``arn:aws:s3:::my-bucket``)

Organizations URI format:
  ``aws://org/{org_id}``
  ``aws://org/{org_id}/ou/{ou_id}``
  ``aws://org/{org_id}/account/{account_id}``
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import boto3
import botocore.exceptions
from pydantic import BaseModel, Field

from omniscience_connectors.base import Connector, DocumentRef, FetchedDocument, WebhookHandler

__all__ = ["AwsConfig", "AwsConnector"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class AwsConfig(BaseModel):
    """Public configuration for the AWS connector (no secrets)."""

    regions: list[str] = Field(default=["us-east-1"])
    """List of AWS regions to scan for regional services like EC2."""

    services: list[str] = Field(default=["s3", "iam", "ec2"])
    """Which AWS services to discover.  Supported: ``"s3"``, ``"iam"``, ``"ec2"``."""

    resource_type_filters: list[str] = Field(default_factory=list)
    """Optional resource-type allow-list (e.g. ``["aws_instance", "aws_vpc"]``).
    Empty means include all resource types for the configured services."""

    include_organizations: bool = Field(default=False)
    """When True, enumerate the AWS Organization, its OUs, and member accounts.

    Requires the caller's principal to have read-only Organizations permissions:
    ``organizations:DescribeOrganization``, ``organizations:ListRoots``,
    ``organizations:ListOrganizationalUnitsForParent``,
    ``organizations:ListAccounts``, ``organizations:ListParents``.

    The Organizations API endpoint is always ``us-east-1`` regardless of the
    ``regions`` setting.  Existing s3/iam/ec2 behavior is unchanged when this
    is False (the default).
    """


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def _build_session(secrets: dict[str, str], region: str | None = None) -> boto3.Session:
    """Build a boto3 Session from runtime secrets.

    Credential resolution order:
    1. Explicit key/secret (``aws_access_key_id`` + ``aws_secret_access_key``).
    2. Named profile (``aws_profile``) for SSO / shared config.
    3. Default credential chain (instance role, env vars, ~/.aws/credentials).
    """
    access_key = secrets.get("aws_access_key_id", "")
    secret_key = secrets.get("aws_secret_access_key", "")
    session_token = secrets.get("aws_session_token", "")
    profile = secrets.get("aws_profile", "")

    if access_key and secret_key:
        return boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token or None,
            region_name=region,
        )
    if profile:
        return boto3.Session(profile_name=profile, region_name=region)
    return boto3.Session(region_name=region)


def _build_client(service: str, secrets: dict[str, str], region: str | None = None) -> Any:
    """Return a boto3 service client."""
    session = _build_session(secrets, region)
    return session.client(service)  # type: ignore[call-overload]


def _get_account_id(secrets: dict[str, str]) -> str:
    """Retrieve the AWS account ID via STS get-caller-identity."""
    sts = _build_client("sts", secrets)
    response: dict[str, Any] = sts.get_caller_identity()
    return str(response["Account"])


# ---------------------------------------------------------------------------
# Resource-type filter helper
# ---------------------------------------------------------------------------


def _allowed(resource_type: str, filters: list[str]) -> bool:
    """Return True if *resource_type* is permitted by the filter list.

    When the filter list is empty all resource types are permitted.
    """
    if not filters:
        return True
    return resource_type in filters


# ---------------------------------------------------------------------------
# Per-service discovery helpers (synchronous — run in asyncio.to_thread)
# ---------------------------------------------------------------------------


def _discover_s3(
    secrets: dict[str, str],
    account_id: str,
    filters: list[str],
) -> list[DocumentRef]:
    """List all S3 buckets and return a DocumentRef per bucket."""
    client = _build_client("s3", secrets)
    refs: list[DocumentRef] = []

    response: dict[str, Any] = client.list_buckets()
    buckets: list[dict[str, Any]] = response.get("Buckets", [])

    for bucket in buckets:
        name: str = bucket.get("Name", "")
        if not name:
            continue

        resource_type = "aws_s3_bucket"
        if not _allowed(resource_type, filters):
            continue

        # S3 buckets are global; region is "us-east-1" by convention in ARNs
        arn = f"arn:aws:s3:::{name}"
        uri = f"aws://{account_id}/global/s3/bucket/{name}"

        # Collect supplementary metadata without failing if permissions are missing
        tags: dict[str, str] = {}
        versioning = ""
        location = ""

        try:
            tag_resp: dict[str, Any] = client.get_bucket_tagging(Bucket=name)
            tags = {t["Key"]: t["Value"] for t in tag_resp.get("TagSet", [])}
        except botocore.exceptions.ClientError:
            pass

        try:
            ver_resp: dict[str, Any] = client.get_bucket_versioning(Bucket=name)
            versioning = ver_resp.get("Status", "")
        except botocore.exceptions.ClientError:
            pass

        try:
            loc_resp: dict[str, Any] = client.get_bucket_location(Bucket=name)
            location = loc_resp.get("LocationConstraint") or "us-east-1"
        except botocore.exceptions.ClientError:
            pass

        refs.append(
            DocumentRef(
                external_id=arn,
                uri=uri,
                metadata={
                    "kind": "aws_live",
                    "resource_type": resource_type,
                    "service": "s3",
                    "region": location or "global",
                    "account_id": account_id,
                    "name": name,
                    "arn": arn,
                    "tags": tags,
                    "versioning": versioning,
                },
            )
        )

    return refs


def _discover_iam(
    secrets: dict[str, str],
    account_id: str,
    filters: list[str],
) -> list[DocumentRef]:
    """List IAM users, roles, and policies (global service)."""
    client = _build_client("iam", secrets)
    refs: list[DocumentRef] = []

    # --- Users ---
    if _allowed("aws_iam_user", filters):
        paginator = client.get_paginator("list_users")
        for page in paginator.paginate():
            for user in page.get("Users", []):
                name: str = user.get("UserName", "")
                arn: str = user.get("Arn", f"arn:aws:iam::{account_id}:user/{name}")
                uri = f"aws://{account_id}/global/iam/user/{name}"
                refs.append(
                    DocumentRef(
                        external_id=arn,
                        uri=uri,
                        metadata={
                            "kind": "aws_live",
                            "resource_type": "aws_iam_user",
                            "service": "iam",
                            "region": "global",
                            "account_id": account_id,
                            "name": name,
                            "arn": arn,
                            "user_id": user.get("UserId", ""),
                            "path": user.get("Path", "/"),
                        },
                    )
                )

    # --- Roles ---
    if _allowed("aws_iam_role", filters):
        paginator = client.get_paginator("list_roles")
        for page in paginator.paginate():
            for role in page.get("Roles", []):
                name = role.get("RoleName", "")
                arn = role.get("Arn", f"arn:aws:iam::{account_id}:role/{name}")
                uri = f"aws://{account_id}/global/iam/role/{name}"
                refs.append(
                    DocumentRef(
                        external_id=arn,
                        uri=uri,
                        metadata={
                            "kind": "aws_live",
                            "resource_type": "aws_iam_role",
                            "service": "iam",
                            "region": "global",
                            "account_id": account_id,
                            "name": name,
                            "arn": arn,
                            "role_id": role.get("RoleId", ""),
                            "path": role.get("Path", "/"),
                        },
                    )
                )

    # --- Customer-managed policies ---
    if _allowed("aws_iam_policy", filters):
        paginator = client.get_paginator("list_policies")
        for page in paginator.paginate(Scope="Local"):
            for policy in page.get("Policies", []):
                name = policy.get("PolicyName", "")
                arn = policy.get("Arn", f"arn:aws:iam::{account_id}:policy/{name}")
                uri = f"aws://{account_id}/global/iam/policy/{name}"
                refs.append(
                    DocumentRef(
                        external_id=arn,
                        uri=uri,
                        metadata={
                            "kind": "aws_live",
                            "resource_type": "aws_iam_policy",
                            "service": "iam",
                            "region": "global",
                            "account_id": account_id,
                            "name": name,
                            "arn": arn,
                            "policy_id": policy.get("PolicyId", ""),
                            "attachment_count": policy.get("AttachmentCount", 0),
                        },
                    )
                )

    return refs


def _discover_ec2(
    secrets: dict[str, str],
    account_id: str,
    region: str,
    filters: list[str],
) -> list[DocumentRef]:
    """Discover EC2 instances, VPCs, and security groups in *region*."""
    client = _build_client("ec2", secrets, region)
    refs: list[DocumentRef] = []

    # --- Instances ---
    if _allowed("aws_instance", filters):
        paginator = client.get_paginator("describe_instances")
        for page in paginator.paginate():
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    instance_id: str = inst.get("InstanceId", "")
                    if not instance_id:
                        continue
                    arn = f"arn:aws:ec2:{region}:{account_id}:instance/{instance_id}"
                    # Extract Name tag if present
                    name_tag = _extract_name_tag(inst.get("Tags", []))
                    display_name = name_tag or instance_id
                    uri = f"aws://{account_id}/{region}/ec2/instance/{instance_id}"
                    refs.append(
                        DocumentRef(
                            external_id=arn,
                            uri=uri,
                            metadata={
                                "kind": "aws_live",
                                "resource_type": "aws_instance",
                                "service": "ec2",
                                "region": region,
                                "account_id": account_id,
                                "name": display_name,
                                "instance_id": instance_id,
                                "arn": arn,
                                "instance_type": inst.get("InstanceType", ""),
                                "state": inst.get("State", {}).get("Name", ""),
                                "vpc_id": inst.get("VpcId", ""),
                                "tags": {t["Key"]: t["Value"] for t in inst.get("Tags", [])},
                            },
                        )
                    )

    # --- VPCs ---
    if _allowed("aws_vpc", filters):
        paginator = client.get_paginator("describe_vpcs")
        for page in paginator.paginate():
            for vpc in page.get("Vpcs", []):
                vpc_id: str = vpc.get("VpcId", "")
                if not vpc_id:
                    continue
                arn = f"arn:aws:ec2:{region}:{account_id}:vpc/{vpc_id}"
                name_tag = _extract_name_tag(vpc.get("Tags", []))
                display_name = name_tag or vpc_id
                uri = f"aws://{account_id}/{region}/ec2/vpc/{vpc_id}"
                refs.append(
                    DocumentRef(
                        external_id=arn,
                        uri=uri,
                        metadata={
                            "kind": "aws_live",
                            "resource_type": "aws_vpc",
                            "service": "ec2",
                            "region": region,
                            "account_id": account_id,
                            "name": display_name,
                            "vpc_id": vpc_id,
                            "arn": arn,
                            "cidr_block": vpc.get("CidrBlock", ""),
                            "is_default": vpc.get("IsDefault", False),
                            "state": vpc.get("State", ""),
                            "tags": {t["Key"]: t["Value"] for t in vpc.get("Tags", [])},
                        },
                    )
                )

    # --- Security groups ---
    if _allowed("aws_security_group", filters):
        paginator = client.get_paginator("describe_security_groups")
        for page in paginator.paginate():
            for sg in page.get("SecurityGroups", []):
                sg_id: str = sg.get("GroupId", "")
                if not sg_id:
                    continue
                arn = f"arn:aws:ec2:{region}:{account_id}:security-group/{sg_id}"
                sg_name: str = sg.get("GroupName", sg_id)
                uri = f"aws://{account_id}/{region}/ec2/security-group/{sg_id}"
                refs.append(
                    DocumentRef(
                        external_id=arn,
                        uri=uri,
                        metadata={
                            "kind": "aws_live",
                            "resource_type": "aws_security_group",
                            "service": "ec2",
                            "region": region,
                            "account_id": account_id,
                            "name": sg_name,
                            "group_id": sg_id,
                            "arn": arn,
                            "description": sg.get("Description", ""),
                            "vpc_id": sg.get("VpcId", ""),
                            "tags": {t["Key"]: t["Value"] for t in sg.get("Tags", [])},
                        },
                    )
                )

    return refs


def _extract_name_tag(tags: list[dict[str, str]]) -> str:
    """Return the value of the ``Name`` tag, or empty string if absent."""
    for tag in tags:
        if tag.get("Key") == "Name":
            return tag.get("Value", "")
    return ""


# ---------------------------------------------------------------------------
# Organizations helpers (synchronous — run in asyncio.to_thread)
# ---------------------------------------------------------------------------

# The Organizations API is region-agnostic; all calls go to us-east-1.
_ORG_REGION = "us-east-1"

# Retry parameters for ThrottlingException / TooManyRequestsException.
_ORG_MAX_RETRIES = 5
_ORG_BACKOFF_BASE = 0.5  # seconds


def _org_client(secrets: dict[str, str]) -> Any:
    """Return a boto3 organizations client pinned to us-east-1."""
    return _build_client("organizations", secrets, _ORG_REGION)


def _paginate_org(client: Any, method: str, key: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Paginate an Organizations list call with exponential-backoff retry.

    Args:
        client:  boto3 organizations client.
        method:  Paginator name (e.g. ``"list_accounts"``).
        key:     Response key holding the result list (e.g. ``"Accounts"``).
        **kwargs: Extra parameters forwarded to ``paginator.paginate()``.

    Returns:
        Flat list of all items across all pages.

    Raises:
        botocore.exceptions.ClientError: On access-denied or unrecoverable errors.
    """
    paginator = client.get_paginator(method)
    items: list[dict[str, Any]] = []
    attempt = 0
    while True:
        try:
            for page in paginator.paginate(**kwargs):
                items.extend(page.get(key, []))
            return items
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("ThrottlingException", "TooManyRequestsException"):
                attempt += 1
                if attempt > _ORG_MAX_RETRIES:
                    logger.warning(
                        "organizations.throttle.max_retries_exceeded",
                        extra={"method": method, "attempts": attempt},
                    )
                    raise
                wait = _ORG_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.debug(
                    "organizations.throttle.retry",
                    extra={"method": method, "attempt": attempt, "wait_s": wait},
                )
                time.sleep(wait)
                items = []  # reset before retry
            else:
                raise


def _collect_ous(
    client: Any,
    parent_id: str,
    parent_path: str,
    org_id: str,
    filters: list[str],
) -> list[DocumentRef]:
    """Recursively collect OUs under *parent_id*.

    Args:
        client:      boto3 organizations client.
        parent_id:   Root or OU id to recurse from.
        parent_path: Human-readable path string for the parent (e.g. ``"/root"``).
        org_id:      Organization id (used in the URI).
        filters:     Resource-type allow-list.

    Returns:
        List of :class:`DocumentRef` for every OU found at any depth.
    """
    refs: list[DocumentRef] = []
    ous = _paginate_org(
        client,
        "list_organizational_units_for_parent",
        "OrganizationalUnits",
        ParentId=parent_id,
    )
    for ou in ous:
        ou_id: str = ou.get("Id", "")
        ou_name: str = ou.get("Name", ou_id)
        ou_arn: str = ou.get("Arn", f"arn:aws:organizations::{org_id}:ou/{org_id}/{ou_id}")
        ou_path = f"{parent_path}/{ou_name}"
        uri = f"aws://org/{org_id}/ou/{ou_id}"

        if _allowed("aws_organizations_ou", filters):
            refs.append(
                DocumentRef(
                    external_id=ou_arn,
                    uri=uri,
                    metadata={
                        "kind": "aws_live",
                        "resource_type": "aws_organizations_ou",
                        "service": "organizations",
                        "region": "global",
                        "org_id": org_id,
                        "ou_id": ou_id,
                        "name": ou_name,
                        "arn": ou_arn,
                        "parent_id": parent_id,
                        "path": ou_path,
                    },
                )
            )

        # Recurse into child OUs
        refs.extend(_collect_ous(client, ou_id, ou_path, org_id, filters))

    return refs


def _discover_organizations(
    secrets: dict[str, str],
    filters: list[str],
) -> list[DocumentRef]:
    """Discover the Organization, all OUs (nested), and member accounts.

    Gracefully handles ``AccessDeniedException`` (the caller may lack
    organizations permissions in a member account without delegated access)
    by logging a warning and returning an empty list.

    Args:
        secrets: Runtime credential dict.
        filters: Resource-type allow-list.

    Returns:
        List of :class:`DocumentRef` for the org, every OU, and every account.
    """
    client = _org_client(secrets)
    refs: list[DocumentRef] = []

    # --- Describe the organization itself ---
    try:
        org_resp: dict[str, Any] = client.describe_organization()
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("AccessDeniedException", "AWSOrganizationsNotInUseException"):
            logger.warning(
                "organizations.discover.skipped",
                extra={"reason": code},
            )
            return []
        raise

    org: dict[str, Any] = org_resp.get("Organization", {})
    org_id: str = org.get("Id", "")
    if not org_id:
        logger.warning("organizations.discover.empty_org_id")
        return []

    org_arn: str = org.get("Arn", f"arn:aws:organizations::{org_id}:organization/{org_id}")
    master_account: str = org.get("MasterAccountId", "")
    feature_set: str = org.get("FeatureSet", "")

    if _allowed("aws_organization", filters):
        refs.append(
            DocumentRef(
                external_id=org_arn,
                uri=f"aws://org/{org_id}",
                metadata={
                    "kind": "aws_live",
                    "resource_type": "aws_organization",
                    "service": "organizations",
                    "region": "global",
                    "org_id": org_id,
                    "name": org_id,
                    "arn": org_arn,
                    "master_account_id": master_account,
                    "feature_set": feature_set,
                },
            )
        )

    # --- Enumerate roots then collect OUs recursively ---
    try:
        roots = _paginate_org(client, "list_roots", "Roots")
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        logger.warning("organizations.list_roots.failed", extra={"code": code})
        roots = []

    for root in roots:
        root_id: str = root.get("Id", "")
        if not root_id:
            continue
        try:
            refs.extend(_collect_ous(client, root_id, "/root", org_id, filters))
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            logger.warning(
                "organizations.list_ous.failed",
                extra={"root_id": root_id, "code": code},
            )

    # --- Member accounts ---
    if _allowed("aws_organizations_account", filters):
        try:
            accounts = _paginate_org(client, "list_accounts", "Accounts")
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            logger.warning("organizations.list_accounts.failed", extra={"code": code})
            accounts = []

        for acct in accounts:
            acct_id: str = acct.get("Id", "")
            if not acct_id:
                continue
            acct_name: str = acct.get("Name", acct_id)
            _fallback = f"arn:aws:organizations::{org_id}:account/{org_id}/{acct_id}"
            acct_arn: str = acct.get("Arn", _fallback)
            acct_email: str = acct.get("Email", "")
            acct_status: str = acct.get("Status", "")
            joined: str = str(acct.get("JoinedTimestamp", ""))

            # Resolve the immediate OU parent for this account
            parent_id = ""
            try:
                parents = _paginate_org(
                    client, "list_parents", "Parents", ChildId=acct_id
                )
                if parents:
                    parent_id = parents[0].get("Id", "")
            except botocore.exceptions.ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                logger.warning(
                    "organizations.list_parents.failed",
                    extra={"account_id": acct_id, "code": code},
                )

            refs.append(
                DocumentRef(
                    external_id=acct_arn,
                    uri=f"aws://org/{org_id}/account/{acct_id}",
                    metadata={
                        "kind": "aws_live",
                        "resource_type": "aws_organizations_account",
                        "service": "organizations",
                        "region": "global",
                        "org_id": org_id,
                        "account_id": acct_id,
                        "name": acct_name,
                        "arn": acct_arn,
                        "email": acct_email,
                        "status": acct_status,
                        "joined_timestamp": joined,
                        "parent_id": parent_id,
                    },
                )
            )

    return refs


# ---------------------------------------------------------------------------
# Per-resource fetch helpers (synchronous — run in asyncio.to_thread)
# ---------------------------------------------------------------------------


def _fetch_s3_resource(client: Any, name: str) -> dict[str, Any]:
    """Describe a single S3 bucket in detail."""
    detail: dict[str, Any] = {"name": name}

    try:
        loc = client.get_bucket_location(Bucket=name)
        detail["location"] = loc.get("LocationConstraint") or "us-east-1"
    except botocore.exceptions.ClientError:
        pass

    try:
        ver = client.get_bucket_versioning(Bucket=name)
        detail["versioning_status"] = ver.get("Status", "")
        detail["versioning_mfa_delete"] = ver.get("MFADelete", "")
    except botocore.exceptions.ClientError:
        pass

    try:
        tag_resp = client.get_bucket_tagging(Bucket=name)
        detail["tags"] = {t["Key"]: t["Value"] for t in tag_resp.get("TagSet", [])}
    except botocore.exceptions.ClientError:
        detail["tags"] = {}

    try:
        policy_resp = client.get_bucket_policy(Bucket=name)
        raw_policy = policy_resp.get("Policy", "{}")
        detail["policy"] = json.loads(raw_policy)
    except botocore.exceptions.ClientError:
        detail["policy"] = {}

    return detail


def _fetch_iam_user(client: Any, name: str, account_id: str) -> dict[str, Any]:
    """Describe a single IAM user."""
    detail: dict[str, Any] = {"user_name": name}
    try:
        resp = client.get_user(UserName=name)
        user = resp.get("User", {})
        detail.update(
            {
                "user_id": user.get("UserId", ""),
                "arn": user.get("Arn", f"arn:aws:iam::{account_id}:user/{name}"),
                "path": user.get("Path", "/"),
                "create_date": str(user.get("CreateDate", "")),
            }
        )
    except botocore.exceptions.ClientError:
        pass
    return detail


def _fetch_iam_role(client: Any, name: str, account_id: str) -> dict[str, Any]:
    """Describe a single IAM role."""
    detail: dict[str, Any] = {"role_name": name}
    try:
        resp = client.get_role(RoleName=name)
        role = resp.get("Role", {})
        detail.update(
            {
                "role_id": role.get("RoleId", ""),
                "arn": role.get("Arn", f"arn:aws:iam::{account_id}:role/{name}"),
                "path": role.get("Path", "/"),
                "create_date": str(role.get("CreateDate", "")),
                "assume_role_policy": role.get("AssumeRolePolicyDocument", {}),
            }
        )
    except botocore.exceptions.ClientError:
        pass
    return detail


def _fetch_iam_policy(client: Any, arn: str) -> dict[str, Any]:
    """Describe a single customer-managed IAM policy."""
    detail: dict[str, Any] = {"arn": arn}
    try:
        resp = client.get_policy(PolicyArn=arn)
        policy = resp.get("Policy", {})
        detail.update(
            {
                "policy_name": policy.get("PolicyName", ""),
                "policy_id": policy.get("PolicyId", ""),
                "attachment_count": policy.get("AttachmentCount", 0),
                "create_date": str(policy.get("CreateDate", "")),
                "update_date": str(policy.get("UpdateDate", "")),
            }
        )
    except botocore.exceptions.ClientError:
        pass
    return detail


def _fetch_ec2_instance(client: Any, instance_id: str) -> dict[str, Any]:
    """Describe a single EC2 instance."""
    detail: dict[str, Any] = {"instance_id": instance_id}
    try:
        resp = client.describe_instances(InstanceIds=[instance_id])
        reservations = resp.get("Reservations", [])
        if reservations:
            instances = reservations[0].get("Instances", [])
            if instances:
                inst = instances[0]
                detail.update(
                    {
                        "instance_type": inst.get("InstanceType", ""),
                        "state": inst.get("State", {}).get("Name", ""),
                        "vpc_id": inst.get("VpcId", ""),
                        "subnet_id": inst.get("SubnetId", ""),
                        "private_ip": inst.get("PrivateIpAddress", ""),
                        "public_ip": inst.get("PublicIpAddress", ""),
                        "image_id": inst.get("ImageId", ""),
                        "launch_time": str(inst.get("LaunchTime", "")),
                        "tags": {t["Key"]: t["Value"] for t in inst.get("Tags", [])},
                    }
                )
    except botocore.exceptions.ClientError:
        pass
    return detail


def _fetch_ec2_vpc(client: Any, vpc_id: str) -> dict[str, Any]:
    """Describe a single VPC."""
    detail: dict[str, Any] = {"vpc_id": vpc_id}
    try:
        resp = client.describe_vpcs(VpcIds=[vpc_id])
        vpcs = resp.get("Vpcs", [])
        if vpcs:
            vpc = vpcs[0]
            detail.update(
                {
                    "cidr_block": vpc.get("CidrBlock", ""),
                    "is_default": vpc.get("IsDefault", False),
                    "state": vpc.get("State", ""),
                    "dhcp_options_id": vpc.get("DhcpOptionsId", ""),
                    "tags": {t["Key"]: t["Value"] for t in vpc.get("Tags", [])},
                }
            )
    except botocore.exceptions.ClientError:
        pass
    return detail


def _fetch_ec2_sg(client: Any, sg_id: str) -> dict[str, Any]:
    """Describe a single security group."""
    detail: dict[str, Any] = {"group_id": sg_id}
    try:
        resp = client.describe_security_groups(GroupIds=[sg_id])
        groups = resp.get("SecurityGroups", [])
        if groups:
            sg = groups[0]
            detail.update(
                {
                    "group_name": sg.get("GroupName", ""),
                    "description": sg.get("Description", ""),
                    "vpc_id": sg.get("VpcId", ""),
                    "ip_permissions": sg.get("IpPermissions", []),
                    "ip_permissions_egress": sg.get("IpPermissionsEgress", []),
                    "tags": {t["Key"]: t["Value"] for t in sg.get("Tags", [])},
                }
            )
    except botocore.exceptions.ClientError:
        pass
    return detail


# ---------------------------------------------------------------------------
# Organizations fetch helpers (synchronous — run in asyncio.to_thread)
# ---------------------------------------------------------------------------


def _fetch_org_organization(client: Any, org_id: str) -> dict[str, Any]:
    """Describe the Organization."""
    detail: dict[str, Any] = {"org_id": org_id}
    try:
        resp = client.describe_organization()
        org = resp.get("Organization", {})
        detail.update(
            {
                "id": org.get("Id", org_id),
                "arn": org.get("Arn", ""),
                "feature_set": org.get("FeatureSet", ""),
                "master_account_id": org.get("MasterAccountId", ""),
                "master_account_email": org.get("MasterAccountEmail", ""),
                "available_policy_types": [
                    pt.get("Type", "") for pt in org.get("AvailablePolicyTypes", [])
                ],
            }
        )
    except botocore.exceptions.ClientError:
        pass
    return detail


def _fetch_org_ou(client: Any, ou_id: str) -> dict[str, Any]:
    """Describe a single Organizational Unit."""
    detail: dict[str, Any] = {"ou_id": ou_id}
    try:
        resp = client.describe_organizational_unit(OrganizationalUnitId=ou_id)
        ou = resp.get("OrganizationalUnit", {})
        detail.update(
            {
                "id": ou.get("Id", ou_id),
                "arn": ou.get("Arn", ""),
                "name": ou.get("Name", ""),
            }
        )
    except botocore.exceptions.ClientError:
        pass
    return detail


def _fetch_org_account(client: Any, account_id: str) -> dict[str, Any]:
    """Describe a single member account."""
    detail: dict[str, Any] = {"account_id": account_id}
    try:
        resp = client.describe_account(AccountId=account_id)
        acct = resp.get("Account", {})
        detail.update(
            {
                "id": acct.get("Id", account_id),
                "arn": acct.get("Arn", ""),
                "name": acct.get("Name", ""),
                "email": acct.get("Email", ""),
                "status": acct.get("Status", ""),
                "joined_method": acct.get("JoinedMethod", ""),
                "joined_timestamp": str(acct.get("JoinedTimestamp", "")),
            }
        )
    except botocore.exceptions.ClientError:
        pass
    return detail


# ---------------------------------------------------------------------------
# AwsConnector
# ---------------------------------------------------------------------------


class AwsConnector(Connector):
    """Connector for live AWS resources.

    Discovers resources across S3, IAM, EC2, and optionally AWS Organizations
    (configurable) and returns them as :class:`~omniscience_connectors.base.DocumentRef`
    objects tagged with ``kind="aws_live"``.  The ARN is stored as ``external_id``
    and in ``metadata["arn"]`` to support ARN-based cross-source linking.

    Stateless: all state derives from config + secrets at call time.
    """

    connector_type: ClassVar[str] = "aws"
    config_schema: ClassVar[type[BaseModel]] = AwsConfig

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Verify AWS credentials via STS get-caller-identity.

        Raises:
            PermissionError: On access-denied errors.
            RuntimeError: On any other AWS / connectivity error.
        """
        cfg: AwsConfig = config  # type: ignore[assignment]

        def _check() -> None:
            try:
                _get_account_id(secrets)
            except botocore.exceptions.ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("AccessDenied", "403"):
                    raise PermissionError(
                        f"AWS credentials lack sts:GetCallerIdentity permission: {exc}"
                    ) from exc
                raise RuntimeError(f"AWS validation failed: {exc}") from exc
            except botocore.exceptions.BotoCoreError as exc:
                raise RuntimeError(f"AWS connectivity error during validation: {exc}") from exc

        del cfg  # not needed for STS validation
        await asyncio.to_thread(_check)

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Yield a DocumentRef for every discovered live AWS resource.

        Enumerates all configured services across all configured regions.
        Global services (S3, IAM) are enumerated once regardless of the
        ``regions`` list.  When ``include_organizations`` is True, the
        Organization tree (org, OUs, accounts) is also enumerated once.

        Args:
            config: Validated :class:`AwsConfig`.
            secrets: Runtime-resolved credential values.

        Yields:
            :class:`~omniscience_connectors.base.DocumentRef` for each resource.
        """
        cfg: AwsConfig = config  # type: ignore[assignment]

        account_id = await asyncio.to_thread(_get_account_id, secrets)
        filters = cfg.resource_type_filters

        # Global services (no region iteration)
        if "s3" in cfg.services:
            refs = await asyncio.to_thread(_discover_s3, secrets, account_id, filters)
            for ref in refs:
                yield ref

        if "iam" in cfg.services:
            refs = await asyncio.to_thread(_discover_iam, secrets, account_id, filters)
            for ref in refs:
                yield ref

        # Regional services
        if "ec2" in cfg.services:
            for region in cfg.regions:
                refs = await asyncio.to_thread(_discover_ec2, secrets, account_id, region, filters)
                for ref in refs:
                    yield ref

        # Organizations (opt-in, global)
        if cfg.include_organizations:
            refs = await asyncio.to_thread(_discover_organizations, secrets, filters)
            for ref in refs:
                yield ref

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Describe the AWS resource referenced by *ref* and return its JSON detail.

        The ``ref.metadata`` fields are used to route the call to the correct
        boto3 describe API.  Falls back to returning the ref metadata as JSON
        when the resource type is unrecognised.

        Args:
            config: Validated :class:`AwsConfig`.
            secrets: Runtime-resolved credential values.
            ref: The reference returned by :meth:`discover`.

        Returns:
            :class:`~omniscience_connectors.base.FetchedDocument` with JSON content.
        """
        cfg: AwsConfig = config  # type: ignore[assignment]
        del cfg

        meta = ref.metadata
        resource_type: str = meta.get("resource_type", "")
        region: str = meta.get("region", "us-east-1")
        account_id: str = meta.get("account_id", "")
        arn: str = meta.get("arn", ref.external_id)

        def _describe() -> dict[str, Any]:
            if resource_type == "aws_s3_bucket":
                client = _build_client("s3", secrets)
                name: str = meta.get("name", "")
                return _fetch_s3_resource(client, name)

            if resource_type == "aws_iam_user":
                client = _build_client("iam", secrets)
                name = meta.get("name", "")
                return _fetch_iam_user(client, name, account_id)

            if resource_type == "aws_iam_role":
                client = _build_client("iam", secrets)
                name = meta.get("name", "")
                return _fetch_iam_role(client, name, account_id)

            if resource_type == "aws_iam_policy":
                client = _build_client("iam", secrets)
                return _fetch_iam_policy(client, arn)

            if resource_type == "aws_instance":
                client = _build_client("ec2", secrets, region)
                return _fetch_ec2_instance(client, meta.get("instance_id", ""))

            if resource_type == "aws_vpc":
                client = _build_client("ec2", secrets, region)
                return _fetch_ec2_vpc(client, meta.get("vpc_id", ""))

            if resource_type == "aws_security_group":
                client = _build_client("ec2", secrets, region)
                return _fetch_ec2_sg(client, meta.get("group_id", ""))

            if resource_type == "aws_organization":
                client = _org_client(secrets)
                return _fetch_org_organization(client, meta.get("org_id", ""))

            if resource_type == "aws_organizations_ou":
                client = _org_client(secrets)
                return _fetch_org_ou(client, meta.get("ou_id", ""))

            if resource_type == "aws_organizations_account":
                client = _org_client(secrets)
                return _fetch_org_account(client, meta.get("account_id", ""))

            # Unknown type: return metadata as-is
            return dict(meta)

        detail = await asyncio.to_thread(_describe)
        content = json.dumps(detail, default=str).encode()

        return FetchedDocument(
            ref=ref,
            content_bytes=content,
            content_type="application/json",
        )

    def webhook_handler(self) -> WebhookHandler | None:
        """Return None — AWS live resources use scheduler-based polling."""
        return None
