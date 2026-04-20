"""S3 source connector.

Discovers and fetches objects from an Amazon S3 bucket.
Uses boto3 wrapped in ``asyncio.to_thread`` for async compatibility.

Authentication is resolved from the ``secrets`` dict at call time:
- ``aws_access_key_id`` + ``aws_secret_access_key`` (+ optional ``aws_session_token``)
- ``aws_profile`` for SSO / named AWS profile
- Neither: falls back to the default credential chain (instance role, env vars, etc.)

ETag-based idempotent updates: each :class:`DocumentRef` uses the object's
ETag as its ``external_id``.  Downstream pipelines can skip re-processing when
the ETag is unchanged.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import mimetypes
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar

import boto3
import botocore.exceptions
from pydantic import BaseModel, Field

from omniscience_connectors.base import Connector, DocumentRef, FetchedDocument, WebhookHandler
from omniscience_connectors.s3.webhook import S3WebhookHandler

__all__ = ["S3Config", "S3Connector"]

logger = logging.getLogger(__name__)

# Max keys per list_objects_v2 page (AWS hard limit is 1000)
_LIST_PAGE_SIZE = 1_000


class S3Config(BaseModel):
    """Public configuration for the S3 connector (no secrets)."""

    bucket: str
    """S3 bucket name."""

    prefix: str = ""
    """Key prefix to scope discovery (e.g. ``"data/docs/"``).  Empty = whole bucket."""

    region: str = "us-east-1"
    """AWS region where the bucket lives."""

    path_include: list[str] = Field(default_factory=list)
    """Glob patterns for object keys to include.  Empty = include everything."""

    path_exclude: list[str] = Field(default_factory=list)
    """Glob patterns for object keys to exclude."""

    max_file_size_bytes: int = 10_000_000
    """Objects larger than this are skipped during fetch (bytes, default 10 MiB)."""


def _build_s3_client(region: str, secrets: dict[str, str]) -> Any:
    """Build a boto3 S3 client from runtime secrets.

    Credential resolution order:
    1. Explicit key/secret (``aws_access_key_id`` + ``aws_secret_access_key``).
    2. Named profile (``aws_profile``) — for SSO and shared config.
    3. Default credential chain (instance role, env vars, ~/.aws/credentials).
    """
    access_key = secrets.get("aws_access_key_id", "")
    secret_key = secrets.get("aws_secret_access_key", "")
    session_token = secrets.get("aws_session_token", "")
    profile = secrets.get("aws_profile", "")

    if access_key and secret_key:
        session = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token or None,
            region_name=region,
        )
    elif profile:
        session = boto3.Session(
            profile_name=profile,
            region_name=region,
        )
    else:
        # Fall through to default credential chain
        session = boto3.Session(region_name=region)

    return session.client("s3")


def _matches_patterns(key: str, patterns: list[str]) -> bool:
    """Return True if *key* matches any of the given glob patterns."""
    return any(fnmatch.fnmatch(key, p) for p in patterns)


def _guess_content_type(key: str) -> str:
    """Guess MIME type from S3 key extension; fall back to application/octet-stream."""
    mime, _ = mimetypes.guess_type(key)
    return mime or "application/octet-stream"


def _parse_s3_datetime(dt: Any) -> datetime | None:
    """Coerce an S3 API datetime value to a timezone-aware datetime."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt
    return None


class S3Connector(Connector):
    """Connector for Amazon S3 buckets.

    Stateless: all state derives from config + secrets at call time so a
    single instance can serve multiple source records simultaneously.

    ETag-based idempotent updates
    ------------------------------
    Each ``DocumentRef.external_id`` is set to the object's ETag (stripped of
    surrounding quotes).  Downstream pipelines track this value and can skip
    re-processing when the ETag is unchanged between syncs.
    """

    connector_type: ClassVar[str] = "s3"
    config_schema: ClassVar[type[BaseModel]] = S3Config

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Verify bucket access with a lightweight ``head_bucket`` call.

        Raises ``RuntimeError`` when the bucket is inaccessible or credentials
        are invalid; raises ``PermissionError`` on access denied.
        """
        cfg: S3Config = config  # type: ignore[assignment]
        client = _build_s3_client(cfg.region, secrets)

        def _check() -> None:
            try:
                client.head_bucket(Bucket=cfg.bucket)
            except botocore.exceptions.ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("403", "AccessDenied"):
                    raise PermissionError(
                        f"Access denied to S3 bucket {cfg.bucket!r}: {exc}"
                    ) from exc
                if code in ("404", "NoSuchBucket"):
                    raise RuntimeError(f"S3 bucket {cfg.bucket!r} does not exist") from exc
                raise RuntimeError(f"Failed to access S3 bucket {cfg.bucket!r}: {exc}") from exc
            except botocore.exceptions.BotoCoreError as exc:
                raise RuntimeError(f"AWS error accessing S3 bucket {cfg.bucket!r}: {exc}") from exc

        await asyncio.to_thread(_check)

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Yield a DocumentRef for every object matching the include/exclude config.

        Uses paginated ``list_objects_v2`` to avoid loading the full listing
        into memory.  Yields are interleaved with pagination so back-pressure
        from the caller naturally throttles the S3 API calls.
        """
        cfg: S3Config = config  # type: ignore[assignment]
        client = _build_s3_client(cfg.region, secrets)

        def _list_page(continuation_token: str | None) -> dict[str, Any]:
            """Fetch one page of results synchronously."""
            kwargs: dict[str, Any] = {
                "Bucket": cfg.bucket,
                "MaxKeys": _LIST_PAGE_SIZE,
            }
            if cfg.prefix:
                kwargs["Prefix"] = cfg.prefix
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            result: dict[str, Any] = dict(client.list_objects_v2(**kwargs))
            return result

        continuation_token: str | None = None
        while True:
            page = await asyncio.to_thread(_list_page, continuation_token)
            contents = page.get("Contents", [])

            for obj in contents:
                key: str = obj.get("Key", "")
                if not key:
                    continue

                # Skip directory-like keys (trailing slash)
                if key.endswith("/"):
                    continue

                if cfg.path_include and not _matches_patterns(key, cfg.path_include):
                    continue
                if cfg.path_exclude and _matches_patterns(key, cfg.path_exclude):
                    continue

                etag: str = obj.get("ETag", "").strip('"')
                size: int = obj.get("Size", 0)
                last_modified = _parse_s3_datetime(obj.get("LastModified"))
                storage_class: str = obj.get("StorageClass", "")

                # ETag as external_id enables idempotent update detection
                external_id = etag if etag else key
                uri = f"s3://{cfg.bucket}/{key}"

                yield DocumentRef(
                    external_id=external_id,
                    uri=uri,
                    updated_at=last_modified,
                    metadata={
                        "bucket": cfg.bucket,
                        "key": key,
                        "etag": etag,
                        "size": size,
                        "storage_class": storage_class,
                        "region": cfg.region,
                    },
                )

            # Check for more pages
            if not page.get("IsTruncated"):
                break
            continuation_token = page.get("NextContinuationToken")
            if not continuation_token:
                break

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Download the S3 object referenced by *ref*.

        Raises:
            ValueError: If the object exceeds ``max_file_size_bytes``.
            ValueError: If the object is empty (zero bytes).
            RuntimeError: On S3 API errors.
        """
        cfg: S3Config = config  # type: ignore[assignment]
        client = _build_s3_client(cfg.region, secrets)

        bucket = ref.metadata.get("bucket", cfg.bucket)
        key = ref.metadata.get("key", "")

        if not key:
            # Fallback: derive key from URI (s3://bucket/key)
            uri = ref.uri
            prefix_str = f"s3://{bucket}/"
            if uri.startswith(prefix_str):
                key = uri[len(prefix_str) :]
            else:
                raise ValueError(f"Cannot derive S3 key from ref uri={uri!r}")

        def _get() -> tuple[bytes, str, str]:
            """Fetch the object; returns (content, content_type, etag)."""
            try:
                head = client.head_object(Bucket=bucket, Key=key)
            except botocore.exceptions.ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                raise RuntimeError(
                    f"S3 head_object failed for s3://{bucket}/{key} (code={code}): {exc}"
                ) from exc

            size: int = head.get("ContentLength", 0)
            if size > cfg.max_file_size_bytes:
                raise ValueError(
                    f"S3 object s3://{bucket}/{key} ({size} bytes) exceeds "
                    f"max_file_size_bytes={cfg.max_file_size_bytes}"
                )

            try:
                response = client.get_object(Bucket=bucket, Key=key)
            except botocore.exceptions.ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                raise RuntimeError(
                    f"S3 get_object failed for s3://{bucket}/{key} (code={code}): {exc}"
                ) from exc

            body: bytes = response["Body"].read()
            etag: str = response.get("ETag", "").strip('"')
            s3_content_type: str = response.get("ContentType", "")
            return body, s3_content_type, etag

        content, s3_ct, etag = await asyncio.to_thread(_get)

        if not content:
            raise ValueError(f"S3 object s3://{bucket}/{key} has empty content")

        # Prefer MIME type from S3 metadata; fall back to extension-based guess
        content_type = (
            s3_ct if s3_ct and s3_ct != "binary/octet-stream" else _guess_content_type(key)
        )

        # Update the ref's external_id with the freshly-fetched ETag
        updated_ref = DocumentRef(
            external_id=etag if etag else ref.external_id,
            uri=ref.uri,
            updated_at=ref.updated_at,
            metadata={**ref.metadata, "etag": etag},
        )

        return FetchedDocument(
            ref=updated_ref,
            content_bytes=content,
            content_type=content_type,
        )

    def webhook_handler(self) -> WebhookHandler | None:
        return S3WebhookHandler()
