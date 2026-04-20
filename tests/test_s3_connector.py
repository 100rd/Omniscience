"""Tests for the S3 source connector (Issue #87).

Coverage:
- S3Config model validation (4 tests)
- _build_s3_client credential resolution (3 tests)
- S3Connector.validate — head_bucket success, 403, 404, generic error (4 tests)
- S3Connector.discover — single page, multi-page, include/exclude filters,
  empty bucket, directory-key skipping, ETag external_id (7 tests)
- S3Connector.fetch — success, size limit exceeded, empty content,
  key derivation from URI, S3 content-type preference (5 tests)
- S3Connector.webhook_handler returns S3WebhookHandler (1 test)
- Registry integration — S3Connector is registered as "s3" (1 test)
- S3WebhookHandler.parse_payload — direct event, SNS-wrapped, SNS subscription,
  SNS bad JSON, URL-encoded keys, delete events, no records (7 tests)
- S3WebhookHandler.verify_signature — no secret (plain), HMAC correct/wrong,
  SNS path returns True (falls back gracefully) (4 tests)
- _extract_s3_refs — deduplication, missing bucket/key, ETag stripping (4 tests)

Total: 40+ tests
"""

from __future__ import annotations

import io
import json
import urllib.parse
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from omniscience_connectors.base import DocumentRef
from omniscience_connectors.s3.connector import (
    S3Config,
    S3Connector,
    _build_s3_client,
    _guess_content_type,
    _matches_patterns,
)
from omniscience_connectors.s3.webhook import (
    S3WebhookHandler,
    _extract_s3_refs,
    _verify_hmac_sha256,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BUCKET = "my-test-bucket"
REGION = "us-east-1"


def _make_config(**kwargs: Any) -> S3Config:
    return S3Config(bucket=BUCKET, region=REGION, **kwargs)


def _client_error(code: str, message: str = "error") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Operation")


def _s3_object(
    key: str,
    etag: str = '"abc123"',
    size: int = 100,
    last_modified: datetime | None = None,
    storage_class: str = "STANDARD",
) -> dict[str, Any]:
    if last_modified is None:
        last_modified = datetime(2024, 1, 1, tzinfo=UTC)
    return {
        "Key": key,
        "ETag": etag,
        "Size": size,
        "LastModified": last_modified,
        "StorageClass": storage_class,
    }


def _s3_event_record(
    bucket: str = BUCKET,
    key: str = "docs/file.txt",
    etag: str = "abc123",
    size: int = 1024,
    event_name: str = "ObjectCreated:Put",
) -> dict[str, Any]:
    return {
        "eventName": event_name,
        "s3": {
            "bucket": {"name": bucket},
            "object": {
                "key": urllib.parse.quote_plus(key),
                "eTag": etag,
                "size": size,
            },
        },
    }


def _s3_event(records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"Records": records or [_s3_event_record()]}


# ---------------------------------------------------------------------------
# S3Config validation
# ---------------------------------------------------------------------------


def test_config_defaults() -> None:
    cfg = S3Config(bucket="my-bucket")
    assert cfg.prefix == ""
    assert cfg.region == "us-east-1"
    assert cfg.path_include == []
    assert cfg.path_exclude == []
    assert cfg.max_file_size_bytes == 10_000_000


def test_config_custom_values() -> None:
    cfg = S3Config(
        bucket="custom-bucket",
        prefix="data/",
        region="eu-west-1",
        path_include=["*.json"],
        path_exclude=["*.tmp"],
        max_file_size_bytes=5_000_000,
    )
    assert cfg.bucket == "custom-bucket"
    assert cfg.prefix == "data/"
    assert cfg.region == "eu-west-1"
    assert cfg.path_include == ["*.json"]
    assert cfg.path_exclude == ["*.tmp"]
    assert cfg.max_file_size_bytes == 5_000_000


def test_config_connector_type() -> None:
    assert S3Connector.connector_type == "s3"


def test_config_schema_is_s3_config() -> None:
    assert S3Connector.config_schema is S3Config


# ---------------------------------------------------------------------------
# _build_s3_client credential resolution
# ---------------------------------------------------------------------------


@patch("omniscience_connectors.s3.connector.boto3.Session")
def test_build_client_explicit_keys(mock_session_cls: MagicMock) -> None:
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    _build_s3_client(
        "us-east-1",
        {
            "aws_access_key_id": "AKID",
            "aws_secret_access_key": "secret",
            "aws_session_token": "token",
        },
    )

    mock_session_cls.assert_called_once_with(
        aws_access_key_id="AKID",
        aws_secret_access_key="secret",
        aws_session_token="token",
        region_name="us-east-1",
    )
    mock_session.client.assert_called_once_with("s3")


@patch("omniscience_connectors.s3.connector.boto3.Session")
def test_build_client_named_profile(mock_session_cls: MagicMock) -> None:
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    _build_s3_client("eu-west-1", {"aws_profile": "my-sso-profile"})

    mock_session_cls.assert_called_once_with(
        profile_name="my-sso-profile",
        region_name="eu-west-1",
    )


@patch("omniscience_connectors.s3.connector.boto3.Session")
def test_build_client_default_chain(mock_session_cls: MagicMock) -> None:
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session

    # Empty secrets → default credential chain
    _build_s3_client("ap-southeast-1", {})

    mock_session_cls.assert_called_once_with(region_name="ap-southeast-1")


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------


def test_matches_patterns_single_match() -> None:
    assert _matches_patterns("docs/readme.md", ["*.md"]) is True


def test_matches_patterns_no_match() -> None:
    assert _matches_patterns("data.json", ["*.md", "*.txt"]) is False


def test_matches_patterns_empty_patterns() -> None:
    assert _matches_patterns("anything.py", []) is False


def test_guess_content_type_known() -> None:
    assert _guess_content_type("file.json") == "application/json"


def test_guess_content_type_unknown() -> None:
    assert _guess_content_type("file.unknown_ext_xyz") == "application/octet-stream"


# ---------------------------------------------------------------------------
# S3Connector.validate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_success() -> None:
    connector = S3Connector()
    cfg = _make_config()

    mock_client = MagicMock()
    mock_client.head_bucket.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

    with patch("omniscience_connectors.s3.connector._build_s3_client", return_value=mock_client):
        await connector.validate(cfg, {})

    mock_client.head_bucket.assert_called_once_with(Bucket=BUCKET)


@pytest.mark.asyncio
async def test_validate_access_denied_raises_permission_error() -> None:
    connector = S3Connector()
    cfg = _make_config()

    mock_client = MagicMock()
    mock_client.head_bucket.side_effect = _client_error("403")

    with (
        patch("omniscience_connectors.s3.connector._build_s3_client", return_value=mock_client),
        pytest.raises(PermissionError, match="Access denied"),
    ):
        await connector.validate(cfg, {})


@pytest.mark.asyncio
async def test_validate_bucket_not_found_raises_runtime_error() -> None:
    connector = S3Connector()
    cfg = _make_config()

    mock_client = MagicMock()
    mock_client.head_bucket.side_effect = _client_error("NoSuchBucket")

    with (
        patch("omniscience_connectors.s3.connector._build_s3_client", return_value=mock_client),
        pytest.raises(RuntimeError, match="does not exist"),
    ):
        await connector.validate(cfg, {})


@pytest.mark.asyncio
async def test_validate_generic_client_error_raises_runtime_error() -> None:
    connector = S3Connector()
    cfg = _make_config()

    mock_client = MagicMock()
    mock_client.head_bucket.side_effect = _client_error("500", "Internal error")

    with (
        patch("omniscience_connectors.s3.connector._build_s3_client", return_value=mock_client),
        pytest.raises(RuntimeError, match="Failed to access"),
    ):
        await connector.validate(cfg, {})


# ---------------------------------------------------------------------------
# S3Connector.discover
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_single_page_no_filter() -> None:
    connector = S3Connector()
    cfg = _make_config()

    page = {
        "Contents": [
            _s3_object("docs/a.md", etag='"etag1"', size=100),
            _s3_object("docs/b.txt", etag='"etag2"', size=200),
        ],
        "IsTruncated": False,
    }

    mock_client = MagicMock()
    mock_client.list_objects_v2.return_value = page

    with patch("omniscience_connectors.s3.connector._build_s3_client", return_value=mock_client):
        refs = [r async for r in connector.discover(cfg, {})]

    assert len(refs) == 2
    assert refs[0].external_id == "etag1"  # ETag stripped of quotes
    assert refs[0].uri == f"s3://{BUCKET}/docs/a.md"
    assert refs[0].metadata["key"] == "docs/a.md"
    assert refs[0].metadata["size"] == 100
    assert refs[1].external_id == "etag2"


@pytest.mark.asyncio
async def test_discover_multi_page_pagination() -> None:
    connector = S3Connector()
    cfg = _make_config()

    page1 = {
        "Contents": [_s3_object("file1.txt", etag='"e1"')],
        "IsTruncated": True,
        "NextContinuationToken": "token123",
    }
    page2 = {
        "Contents": [_s3_object("file2.txt", etag='"e2"')],
        "IsTruncated": False,
    }

    mock_client = MagicMock()
    mock_client.list_objects_v2.side_effect = [page1, page2]

    with patch("omniscience_connectors.s3.connector._build_s3_client", return_value=mock_client):
        refs = [r async for r in connector.discover(cfg, {})]

    assert len(refs) == 2
    assert refs[0].external_id == "e1"
    assert refs[1].external_id == "e2"
    # Second call should include continuation token
    second_call_kwargs = mock_client.list_objects_v2.call_args_list[1][1]
    assert second_call_kwargs.get("ContinuationToken") == "token123"


@pytest.mark.asyncio
async def test_discover_path_include_filter() -> None:
    connector = S3Connector()
    cfg = _make_config(path_include=["*.json"])

    page = {
        "Contents": [
            _s3_object("data.json", etag='"e1"'),
            _s3_object("readme.md", etag='"e2"'),
            _s3_object("config.json", etag='"e3"'),
        ],
        "IsTruncated": False,
    }

    mock_client = MagicMock()
    mock_client.list_objects_v2.return_value = page

    with patch("omniscience_connectors.s3.connector._build_s3_client", return_value=mock_client):
        refs = [r async for r in connector.discover(cfg, {})]

    assert len(refs) == 2
    keys = [r.metadata["key"] for r in refs]
    assert "data.json" in keys
    assert "config.json" in keys
    assert "readme.md" not in keys


@pytest.mark.asyncio
async def test_discover_path_exclude_filter() -> None:
    connector = S3Connector()
    cfg = _make_config(path_exclude=["*.tmp", "*.log"])

    page = {
        "Contents": [
            _s3_object("file.txt", etag='"e1"'),
            _s3_object("cache.tmp", etag='"e2"'),
            _s3_object("app.log", etag='"e3"'),
        ],
        "IsTruncated": False,
    }

    mock_client = MagicMock()
    mock_client.list_objects_v2.return_value = page

    with patch("omniscience_connectors.s3.connector._build_s3_client", return_value=mock_client):
        refs = [r async for r in connector.discover(cfg, {})]

    assert len(refs) == 1
    assert refs[0].metadata["key"] == "file.txt"


@pytest.mark.asyncio
async def test_discover_empty_bucket() -> None:
    connector = S3Connector()
    cfg = _make_config()

    page = {"IsTruncated": False}  # No Contents key

    mock_client = MagicMock()
    mock_client.list_objects_v2.return_value = page

    with patch("omniscience_connectors.s3.connector._build_s3_client", return_value=mock_client):
        refs = [r async for r in connector.discover(cfg, {})]

    assert refs == []


@pytest.mark.asyncio
async def test_discover_skips_directory_keys() -> None:
    connector = S3Connector()
    cfg = _make_config()

    page = {
        "Contents": [
            _s3_object("docs/", etag='"dir"', size=0),  # directory-like key
            _s3_object("docs/file.txt", etag='"e1"'),
        ],
        "IsTruncated": False,
    }

    mock_client = MagicMock()
    mock_client.list_objects_v2.return_value = page

    with patch("omniscience_connectors.s3.connector._build_s3_client", return_value=mock_client):
        refs = [r async for r in connector.discover(cfg, {})]

    assert len(refs) == 1
    assert refs[0].metadata["key"] == "docs/file.txt"


@pytest.mark.asyncio
async def test_discover_etag_as_external_id() -> None:
    """ETag (stripped of quotes) is used as external_id for idempotent tracking."""
    connector = S3Connector()
    cfg = _make_config()

    page = {
        "Contents": [_s3_object("report.pdf", etag='"deadbeef"')],
        "IsTruncated": False,
    }

    mock_client = MagicMock()
    mock_client.list_objects_v2.return_value = page

    with patch("omniscience_connectors.s3.connector._build_s3_client", return_value=mock_client):
        refs = [r async for r in connector.discover(cfg, {})]

    assert refs[0].external_id == "deadbeef"


@pytest.mark.asyncio
async def test_discover_with_prefix() -> None:
    connector = S3Connector()
    cfg = _make_config(prefix="datasets/")

    page = {"Contents": [], "IsTruncated": False}
    mock_client = MagicMock()
    mock_client.list_objects_v2.return_value = page

    with patch("omniscience_connectors.s3.connector._build_s3_client", return_value=mock_client):
        _ = [r async for r in connector.discover(cfg, {})]

    call_kwargs = mock_client.list_objects_v2.call_args[1]
    assert call_kwargs.get("Prefix") == "datasets/"


# ---------------------------------------------------------------------------
# S3Connector.fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_success() -> None:
    connector = S3Connector()
    cfg = _make_config()

    ref = DocumentRef(
        external_id="old-etag",
        uri=f"s3://{BUCKET}/docs/readme.md",
        metadata={"bucket": BUCKET, "key": "docs/readme.md"},
    )

    mock_client = MagicMock()
    mock_client.head_object.return_value = {"ContentLength": 50}
    body = io.BytesIO(b"# Hello World")
    mock_client.get_object.return_value = {
        "Body": body,
        "ETag": '"new-etag"',
        "ContentType": "text/markdown",
    }

    with patch("omniscience_connectors.s3.connector._build_s3_client", return_value=mock_client):
        doc = await connector.fetch(cfg, {}, ref)

    assert doc.content_bytes == b"# Hello World"
    assert doc.content_type == "text/markdown"
    assert doc.ref.external_id == "new-etag"  # updated to fresh ETag


@pytest.mark.asyncio
async def test_fetch_size_limit_exceeded() -> None:
    connector = S3Connector()
    cfg = _make_config(max_file_size_bytes=100)

    ref = DocumentRef(
        external_id="e",
        uri=f"s3://{BUCKET}/bigfile.bin",
        metadata={"bucket": BUCKET, "key": "bigfile.bin"},
    )

    mock_client = MagicMock()
    mock_client.head_object.return_value = {"ContentLength": 9_999_999}

    with (
        patch("omniscience_connectors.s3.connector._build_s3_client", return_value=mock_client),
        pytest.raises(ValueError, match="exceeds"),
    ):
        await connector.fetch(cfg, {}, ref)


@pytest.mark.asyncio
async def test_fetch_empty_content_raises() -> None:
    connector = S3Connector()
    cfg = _make_config()

    ref = DocumentRef(
        external_id="e",
        uri=f"s3://{BUCKET}/empty.txt",
        metadata={"bucket": BUCKET, "key": "empty.txt"},
    )

    mock_client = MagicMock()
    mock_client.head_object.return_value = {"ContentLength": 0}
    mock_client.get_object.return_value = {
        "Body": io.BytesIO(b""),
        "ETag": '"e"',
        "ContentType": "text/plain",
    }

    with (
        patch("omniscience_connectors.s3.connector._build_s3_client", return_value=mock_client),
        pytest.raises(ValueError, match="empty content"),
    ):
        await connector.fetch(cfg, {}, ref)


@pytest.mark.asyncio
async def test_fetch_key_derived_from_uri_when_metadata_missing() -> None:
    """When ref.metadata lacks 'key', key is derived from the URI."""
    connector = S3Connector()
    cfg = _make_config()

    ref = DocumentRef(
        external_id="e",
        uri=f"s3://{BUCKET}/derived/path/file.txt",
        metadata={"bucket": BUCKET},  # no 'key'
    )

    mock_client = MagicMock()
    mock_client.head_object.return_value = {"ContentLength": 5}
    mock_client.get_object.return_value = {
        "Body": io.BytesIO(b"hello"),
        "ETag": '"e"',
        "ContentType": "text/plain",
    }

    with patch("omniscience_connectors.s3.connector._build_s3_client", return_value=mock_client):
        doc = await connector.fetch(cfg, {}, ref)

    assert doc.content_bytes == b"hello"
    mock_client.head_object.assert_called_once_with(Bucket=BUCKET, Key="derived/path/file.txt")


@pytest.mark.asyncio
async def test_fetch_falls_back_to_extension_content_type_when_s3_returns_binary() -> None:
    """Connector falls back to extension-based type when S3 returns generic binary type."""
    connector = S3Connector()
    cfg = _make_config()

    ref = DocumentRef(
        external_id="e",
        uri=f"s3://{BUCKET}/data.json",
        metadata={"bucket": BUCKET, "key": "data.json"},
    )

    mock_client = MagicMock()
    mock_client.head_object.return_value = {"ContentLength": 20}
    mock_client.get_object.return_value = {
        "Body": io.BytesIO(b'{"key": "value"}'),
        "ETag": '"e"',
        "ContentType": "binary/octet-stream",  # generic S3 default
    }

    with patch("omniscience_connectors.s3.connector._build_s3_client", return_value=mock_client):
        doc = await connector.fetch(cfg, {}, ref)

    assert doc.content_type == "application/json"


# ---------------------------------------------------------------------------
# S3Connector.webhook_handler
# ---------------------------------------------------------------------------


def test_webhook_handler_returns_s3_handler() -> None:
    connector = S3Connector()
    handler = connector.webhook_handler()
    assert isinstance(handler, S3WebhookHandler)


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_s3_connector_is_registered() -> None:
    from omniscience_connectors import get_connector

    connector = get_connector("s3")
    assert isinstance(connector, S3Connector)


# ---------------------------------------------------------------------------
# _extract_s3_refs
# ---------------------------------------------------------------------------


def test_extract_refs_single_record() -> None:
    event = _s3_event([_s3_event_record(key="folder/file.txt", etag="etag1")])
    refs = _extract_s3_refs(event)

    assert len(refs) == 1
    assert refs[0].external_id == "etag1"
    assert refs[0].uri == f"s3://{BUCKET}/folder/file.txt"
    assert refs[0].metadata["key"] == "folder/file.txt"
    assert refs[0].metadata["action"] == "updated"


def test_extract_refs_deduplication() -> None:
    """Two records with the same key are deduplicated to one ref."""
    record = _s3_event_record(key="dup.txt")
    event = _s3_event([record, record])
    refs = _extract_s3_refs(event)
    assert len(refs) == 1


def test_extract_refs_delete_event() -> None:
    record = _s3_event_record(key="old.txt", event_name="ObjectRemoved:Delete")
    event = _s3_event([record])
    refs = _extract_s3_refs(event)
    assert len(refs) == 1
    assert refs[0].metadata["action"] == "deleted"


def test_extract_refs_url_encoded_key() -> None:
    """URL-encoded keys (S3 event standard) are decoded."""
    # S3 encodes space as '+' and special chars as %XX in event notifications
    encoded_key = urllib.parse.quote_plus("folder with spaces/my file.txt")
    record: dict[str, Any] = {
        "eventName": "ObjectCreated:Put",
        "s3": {
            "bucket": {"name": BUCKET},
            "object": {"key": encoded_key, "eTag": "abc", "size": 100},
        },
    }
    refs = _extract_s3_refs({"Records": [record]})
    assert len(refs) == 1
    assert refs[0].metadata["key"] == "folder with spaces/my file.txt"


def test_extract_refs_missing_bucket_or_key_skipped() -> None:
    records: list[dict[str, Any]] = [
        # Missing bucket name
        {
            "eventName": "ObjectCreated:Put",
            "s3": {"bucket": {}, "object": {"key": "file.txt"}},
        },
        # Missing key
        {
            "eventName": "ObjectCreated:Put",
            "s3": {"bucket": {"name": BUCKET}, "object": {}},
        },
    ]
    refs = _extract_s3_refs({"Records": records})
    assert refs == []


def test_extract_refs_no_records_key() -> None:
    refs = _extract_s3_refs({"SomeOtherKey": []})
    assert refs == []


def test_extract_refs_etag_stripped_of_quotes() -> None:
    record = _s3_event_record(etag='"quoted-etag"')
    refs = _extract_s3_refs(_s3_event([record]))
    # eTag in event has no surrounding quotes (raw from record), but strip defensively
    # In our helper, etag is passed without quotes already
    assert refs[0].external_id == "quoted-etag"


# ---------------------------------------------------------------------------
# S3WebhookHandler.parse_payload — direct S3 events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_direct_s3_event() -> None:
    handler = S3WebhookHandler()
    payload = json.dumps(_s3_event()).encode()
    result = await handler.parse_payload(payload, {})

    assert result.source_name == "s3"
    assert len(result.affected_refs) == 1
    assert result.affected_refs[0].uri == f"s3://{BUCKET}/docs/file.txt"


@pytest.mark.asyncio
async def test_parse_sns_wrapped_notification() -> None:
    handler = S3WebhookHandler()
    inner = _s3_event([_s3_event_record(key="reports/q1.csv")])
    envelope = {
        "Type": "Notification",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:my-topic",
        "Message": json.dumps(inner),
    }
    payload = json.dumps(envelope).encode()
    headers = {"x-amz-sns-message-type": "Notification"}

    result = await handler.parse_payload(payload, headers)

    assert "my-topic" in result.source_name
    assert len(result.affected_refs) == 1
    assert result.affected_refs[0].metadata["key"] == "reports/q1.csv"


@pytest.mark.asyncio
async def test_parse_sns_subscription_confirmation() -> None:
    handler = S3WebhookHandler()
    envelope = {
        "Type": "SubscriptionConfirmation",
        "SubscribeURL": "https://sns.amazonaws.com/...",
        "Token": "abc",
    }
    payload = json.dumps(envelope).encode()
    headers = {"x-amz-sns-message-type": "SubscriptionConfirmation"}

    result = await handler.parse_payload(payload, headers)

    assert result.affected_refs == []


@pytest.mark.asyncio
async def test_parse_invalid_json_raises() -> None:
    handler = S3WebhookHandler()
    with pytest.raises(ValueError, match="not valid JSON"):
        await handler.parse_payload(b"not-json", {})


@pytest.mark.asyncio
async def test_parse_sns_notification_with_bad_inner_json() -> None:
    handler = S3WebhookHandler()
    envelope = {"Type": "Notification", "Message": "not-json-at-all"}
    payload = json.dumps(envelope).encode()
    headers = {"x-amz-sns-message-type": "Notification"}

    with pytest.raises(ValueError, match="not valid JSON"):
        await handler.parse_payload(payload, headers)


@pytest.mark.asyncio
async def test_parse_unsupported_sns_type_raises() -> None:
    handler = S3WebhookHandler()
    envelope = {"Type": "UnknownType"}
    payload = json.dumps(envelope).encode()
    headers = {"x-amz-sns-message-type": "UnknownType"}

    with pytest.raises(ValueError, match="Unsupported SNS"):
        await handler.parse_payload(payload, headers)


@pytest.mark.asyncio
async def test_parse_empty_records() -> None:
    handler = S3WebhookHandler()
    payload = json.dumps({"Records": []}).encode()
    result = await handler.parse_payload(payload, {})
    assert result.affected_refs == []


# ---------------------------------------------------------------------------
# S3WebhookHandler.verify_signature
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_no_secret_and_no_sns_returns_true() -> None:
    """Plain S3 events with no secret configured accept unconditionally."""
    handler = S3WebhookHandler()
    payload = json.dumps(_s3_event()).encode()
    result = await handler.verify_signature(payload, {}, secret="")
    assert result is True


@pytest.mark.asyncio
async def test_verify_hmac_correct_signature() -> None:
    handler = S3WebhookHandler()
    import hashlib
    import hmac as hmac_mod

    secret = "mysecret"
    payload = b'{"Records":[]}'
    digest = hmac_mod.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    headers = {"x-hub-signature-256": f"sha256={digest}"}

    result = await handler.verify_signature(payload, headers, secret=secret)
    assert result is True


@pytest.mark.asyncio
async def test_verify_hmac_wrong_signature() -> None:
    handler = S3WebhookHandler()
    headers = {"x-hub-signature-256": "sha256=deadbeef"}
    result = await handler.verify_signature(b"payload", headers, secret="secret")
    assert result is False


@pytest.mark.asyncio
async def test_verify_sns_path_called_with_invalid_cert_returns_false() -> None:
    """SNS verification with an invalid certificate URL gracefully returns False."""
    handler = S3WebhookHandler()
    envelope = {
        "Type": "Notification",
        "Message": "{}",
        "MessageId": "mid",
        "Timestamp": "2024-01-01T00:00:00Z",
        "TopicArn": "arn:aws:sns:us-east-1:123:t",
        "Signature": "aW52YWxpZA==",  # base64("invalid")
        "SigningCertURL": "https://sns.amazonaws.com/fake-cert.pem",
    }
    payload = json.dumps(envelope).encode()
    headers = {"x-amz-sns-message-type": "Notification"}

    # The cert fetch will fail; expect graceful False
    with patch(
        "omniscience_connectors.s3.webhook._fetch_url",
        side_effect=Exception("connection refused"),
    ):
        result = await handler.verify_signature(payload, headers, secret="")

    assert result is False


# ---------------------------------------------------------------------------
# _verify_hmac_sha256 unit tests
# ---------------------------------------------------------------------------


def test_verify_hmac_sha256_with_prefix() -> None:
    import hashlib
    import hmac as hmac_mod

    secret = "key"
    data = b"hello"
    digest = hmac_mod.new(secret.encode(), data, hashlib.sha256).hexdigest()
    assert _verify_hmac_sha256(data, f"sha256={digest}", secret) is True


def test_verify_hmac_sha256_without_prefix() -> None:
    import hashlib
    import hmac as hmac_mod

    secret = "key"
    data = b"hello"
    digest = hmac_mod.new(secret.encode(), data, hashlib.sha256).hexdigest()
    # No prefix — raw hex treated as the value to compare
    assert _verify_hmac_sha256(data, digest, secret) is True


def test_verify_hmac_sha256_wrong_digest_returns_false() -> None:
    assert _verify_hmac_sha256(b"data", "sha256=deadbeef", "secret") is False
