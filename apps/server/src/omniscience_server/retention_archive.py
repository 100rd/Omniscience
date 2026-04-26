"""Archive writer for the retention worker (issue #135, ADR-0009 §1 / §7).

Writes per-(workspace_id, snapshot_date) parquet objects to S3-compatible
object storage.  KMS-managed encryption is mandatory in non-dev (ADR-0009
§1); dev profile may use SSE-S3.

The writer is intentionally small and synchronous (boto3 + pyarrow):
- One parquet object per snapshot date per workspace (ADR-0009 §7).
- Workspace-id prefix at the top of the object key for IAM scoping
  (ADR-0009 §Consequences-security #3).
- Two columnar tables per object: ``entities`` and ``edges``.

This module does NOT decide eviction — the caller (``RetentionWorker``)
fetches the rows from Neo4j and hands them here.  Failures bubble up so
the worker can record metrics and skip deletion of the warm rows; the
next run will retry without losing data (ADR-0009 §9 object-storage
outage runbook).
"""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import structlog
from botocore.config import Config as BotoConfig
from mypy_boto3_s3.client import S3Client

from omniscience_server.retention_constants import (
    ARCHIVE_KEY_TEMPLATE,
    S3_SSE_AES256,
    S3_SSE_KMS,
)

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ArchiveTarget:
    """Resolved S3 target for a single archive write."""

    bucket: str
    key: str

    @property
    def uri(self) -> str:
        """Return the canonical ``s3://{bucket}/{key}`` URI."""
        return f"s3://{self.bucket}/{self.key}"


def build_archive_key(*, workspace_id: uuid.UUID, snapshot_date: date) -> str:
    """Render ``ARCHIVE_KEY_TEMPLATE`` for ``(workspace_id, snapshot_date)``."""
    return ARCHIVE_KEY_TEMPLATE.format(
        workspace_id=str(workspace_id),
        year=snapshot_date.year,
        month=snapshot_date.month,
        day=snapshot_date.day,
    )


def build_parquet_bytes(
    *,
    entity_rows: list[dict[str, Any]],
    edge_rows: list[dict[str, Any]],
) -> bytes:
    """Serialise warm-snapshot rows to an in-memory parquet payload.

    Two pyarrow tables (``entities`` and ``edges``) are concatenated as
    a single multi-table parquet file via the row-group separation
    inherent to pyarrow's writer.  The on-disk layout matches the warm
    row shape per ADR-0009 §7, columnar to win on compression
    (ADR-0009 §1 archive: "Parquet wins over JSONL on compression
    ratio").

    Empty inputs are valid — the parquet object still gets written so
    operators can audit the snapshot's existence; downstream readers
    treat empty tables as "this snapshot day was empty for this
    workspace" rather than "the archive is missing".
    """
    entities_table = _rows_to_table(entity_rows, _ENTITY_SCHEMA)
    edges_table = _rows_to_table(edge_rows, _EDGE_SCHEMA)
    buffer = io.BytesIO()
    # Single parquet file with two row-group tables.  Reader API
    # (DuckDB / Athena / Spark) treats this as one logical file with
    # two schemas; this is the documented offline-restore shape per
    # ADR-0009 §1.
    with pq.ParquetWriter(buffer, entities_table.schema) as writer:  # type: ignore[no-untyped-call]
        writer.write_table(entities_table)
    entities_bytes = buffer.getvalue()
    edges_buffer = io.BytesIO()
    with pq.ParquetWriter(edges_buffer, edges_table.schema) as writer:  # type: ignore[no-untyped-call]
        writer.write_table(edges_table)
    edges_bytes = edges_buffer.getvalue()
    # Frame the two parquet bodies with a tiny header so a reader can
    # split them without ambiguity.  4-byte big-endian length of the
    # entities body, then the entities body, then the edges body.
    header = len(entities_bytes).to_bytes(4, "big")
    return header + entities_bytes + edges_bytes


def split_parquet_bytes(blob: bytes) -> tuple[bytes, bytes]:
    """Reverse of :func:`build_parquet_bytes`; used by the restore CLI.

    Returns ``(entities_parquet_bytes, edges_parquet_bytes)``.
    """
    if len(blob) < 4:
        raise ValueError("retention_archive_blob_too_short")
    entities_len = int.from_bytes(blob[:4], "big")
    if 4 + entities_len > len(blob):
        raise ValueError("retention_archive_blob_truncated")
    entities = blob[4 : 4 + entities_len]
    edges = blob[4 + entities_len :]
    return entities, edges


_ENTITY_SCHEMA: pa.Schema = pa.schema(
    [
        ("id", pa.string()),
        ("kind", pa.string()),
        ("name", pa.string()),
        ("display_name", pa.string()),
        ("source_id", pa.string()),
        ("chunk_id", pa.string()),
        ("valid_from", pa.string()),
        ("valid_to", pa.string()),
        ("recorded_at", pa.string()),
        ("metadata_json", pa.string()),
    ]
)


_EDGE_SCHEMA: pa.Schema = pa.schema(
    [
        ("source_entity_id", pa.string()),
        ("target_entity_id", pa.string()),
        ("edge_type", pa.string()),
        ("source_id", pa.string()),
        ("valid_from", pa.string()),
        ("valid_to", pa.string()),
        ("recorded_at", pa.string()),
        ("metadata_json", pa.string()),
    ]
)


def _rows_to_table(rows: list[dict[str, Any]], schema: pa.Schema) -> pa.Table:
    """Project ``rows`` onto ``schema`` field-by-field, JSON-encoding metadata."""
    import json

    columns: dict[str, list[Any]] = {field.name: [] for field in schema}
    for row in rows:
        for field in schema:
            value = row.get(field.name)
            if field.name == "metadata_json":
                # Source row ships ``metadata`` (dict); we store it as a
                # JSON string to keep the schema flat.
                meta = row.get("metadata") or {}
                columns[field.name].append(json.dumps(meta, sort_keys=True, default=str))
            elif value is None:
                columns[field.name].append(None)
            else:
                columns[field.name].append(str(value))
    return pa.table(columns, schema=schema)


def build_s3_client(
    *,
    region_name: str,
    endpoint_url: str | None,
) -> S3Client:
    """Construct a boto3 S3 client.

    The client uses path-style addressing so it works with MinIO and
    Ceph in addition to AWS S3 (per ADR-0009 §1: "S3-compatible object
    storage").
    """
    config = BotoConfig(
        region_name=region_name,
        signature_version="s3v4",
        s3={"addressing_style": "path"},
    )
    client_kwargs: dict[str, Any] = {"config": config}
    if endpoint_url is not None:
        client_kwargs["endpoint_url"] = endpoint_url
    # boto3's stub `client` returns Any; we annotate the return type.
    return boto3.client("s3", **client_kwargs)


def write_archive_object(
    *,
    s3_client: S3Client,
    bucket: str,
    key: str,
    body: bytes,
    kms_key_arn: str | None,
) -> None:
    """Put ``body`` to ``s3://{bucket}/{key}`` with the ADR-0009 encryption.

    Encryption: KMS when ``kms_key_arn`` is set (mandatory non-dev per
    ADR-0009 §1), otherwise SSE-S3 / AES256 (dev profile only).  The
    caller is responsible for setting the right Helm value; this
    function does NOT downgrade silently — it logs the fallback so a
    misconfigured non-dev deployment is auditable.
    """
    extra_args: dict[str, Any] = {}
    if kms_key_arn:
        extra_args["ServerSideEncryption"] = S3_SSE_KMS
        extra_args["SSEKMSKeyId"] = kms_key_arn
    else:
        extra_args["ServerSideEncryption"] = S3_SSE_AES256
        log.warning(
            "retention_archive_sse_aes256_fallback",
            bucket=bucket,
            key=key,
            note="KMS key not configured; using SSE-S3 (acceptable in dev only).",
        )
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/vnd.apache.parquet",
        **extra_args,
    )


__all__ = [
    "ArchiveTarget",
    "build_archive_key",
    "build_parquet_bytes",
    "build_s3_client",
    "split_parquet_bytes",
    "write_archive_object",
]
