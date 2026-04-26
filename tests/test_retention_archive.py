"""Unit tests for the retention archive writer (issue #135, ADR-0009 §1, §7).

Covers:

1. ``build_archive_key`` produces ``{workspace_id}/{year}/{month}/{day}/snapshot.parquet``.
2. ``build_parquet_bytes`` serialises entity + edge rows round-trippably.
3. S3 write path uses KMS encryption when key is configured (non-dev).
4. S3 write path falls back to AES256 when no KMS key is set (dev only).
5. boto3 client uses path-style addressing for MinIO/Ceph compatibility.
6. moto mock S3: full PUT round-trip with body verification.
"""

from __future__ import annotations

import io
import uuid
from datetime import date

import boto3
import pyarrow.parquet as pq
import pytest
from moto import mock_aws
from omniscience_server.retention_archive import (
    build_archive_key,
    build_parquet_bytes,
    build_s3_client,
    split_parquet_bytes,
    write_archive_object,
)

# ---------------------------------------------------------------------------
# Test 1: archive key shape
# ---------------------------------------------------------------------------


def test_archive_key_matches_adr_0009_layout() -> None:
    """ADR-0009 §7: ``s3://{bucket}/{workspace_id}/{year}/{month}/{day}/snapshot.parquet``."""
    ws = uuid.UUID("11111111-2222-3333-4444-555555555555")
    key = build_archive_key(workspace_id=ws, snapshot_date=date(2025, 4, 9))
    assert key == "11111111-2222-3333-4444-555555555555/2025/04/09/snapshot.parquet"


def test_archive_key_zero_pads_month_and_day() -> None:
    """Month and day are zero-padded to 2 digits — sorts lexicographically."""
    ws = uuid.UUID("11111111-2222-3333-4444-555555555555")
    key = build_archive_key(workspace_id=ws, snapshot_date=date(2025, 1, 1))
    assert "/2025/01/01/" in key


# ---------------------------------------------------------------------------
# Test 2: parquet round-trip
# ---------------------------------------------------------------------------


def test_parquet_round_trip_entities_and_edges() -> None:
    """Parquet serialisation preserves entity + edge rows verbatim."""
    entity_rows = [
        {
            "id": "ent-a",
            "kind": "deployment",
            "name": "frontend",
            "display_name": "Frontend Deployment",
            "source_id": "src-1",
            "chunk_id": None,
            "valid_from": "2025-01-01T00:00:00Z",
            "valid_to": "2025-01-02T00:00:00Z",
            "recorded_at": "2025-01-01T00:00:00Z",
            "metadata": {"replicas": 3},
        }
    ]
    edge_rows = [
        {
            "source_entity_id": "ent-a",
            "target_entity_id": "ent-b",
            "edge_type": "DEPLOYS_TO",
            "source_id": "src-1",
            "valid_from": "2025-01-01T00:00:00Z",
            "valid_to": None,
            "recorded_at": "2025-01-01T00:00:00Z",
            "metadata": {"region": "us-east-1"},
        }
    ]
    blob = build_parquet_bytes(entity_rows=entity_rows, edge_rows=edge_rows)
    entities_pq, edges_pq = split_parquet_bytes(blob)

    entities_table = pq.read_table(io.BytesIO(entities_pq))
    edges_table = pq.read_table(io.BytesIO(edges_pq))

    assert entities_table.num_rows == 1
    assert edges_table.num_rows == 1
    # The metadata field is JSON-encoded so it survives the columnar shape.
    metadata_json = entities_table.column("metadata_json")[0].as_py()
    assert '"replicas":' in metadata_json or '"replicas": ' in metadata_json
    # Schema is documented (ADR-0009 §7) and lookups work by name.
    assert "id" in entities_table.column_names
    assert "edge_type" in edges_table.column_names


def test_parquet_round_trip_empty_inputs() -> None:
    """Empty entity + edge lists still produce a valid parquet object.

    ADR-0009 §1 archive: the snapshot exists for audit even when empty
    (operators query it to confirm "this day was empty for this
    workspace" rather than "the archive is missing").
    """
    blob = build_parquet_bytes(entity_rows=[], edge_rows=[])
    entities_pq, edges_pq = split_parquet_bytes(blob)
    entities_table = pq.read_table(io.BytesIO(entities_pq))
    edges_table = pq.read_table(io.BytesIO(edges_pq))
    assert entities_table.num_rows == 0
    assert edges_table.num_rows == 0


# ---------------------------------------------------------------------------
# Test 3 + 4: S3 PUT with encryption
# ---------------------------------------------------------------------------


@mock_aws
def test_s3_put_with_kms_key_uses_aws_kms_encryption() -> None:
    """ADR-0009 §1: KMS-managed keys mandatory in non-dev."""
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-archive")
    client = build_s3_client(region_name="us-east-1", endpoint_url=None)
    write_archive_object(
        s3_client=client,
        bucket="test-archive",
        key="ws-1/2025/01/01/snapshot.parquet",
        body=b"hello-world",
        kms_key_arn="arn:aws:kms:us-east-1:123456789012:key/abcd",
    )
    head = client.head_object(Bucket="test-archive", Key="ws-1/2025/01/01/snapshot.parquet")
    assert head["ServerSideEncryption"] == "aws:kms"
    assert "abcd" in head.get("SSEKMSKeyId", "")


@mock_aws
def test_s3_put_without_kms_key_falls_back_to_aes256() -> None:
    """Dev profile: SSE-S3 fallback, with a WARN log (validated separately)."""
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-archive-dev")
    client = build_s3_client(region_name="us-east-1", endpoint_url=None)
    write_archive_object(
        s3_client=client,
        bucket="test-archive-dev",
        key="ws-2/2025/01/01/snapshot.parquet",
        body=b"hello-dev",
        kms_key_arn=None,
    )
    head = client.head_object(Bucket="test-archive-dev", Key="ws-2/2025/01/01/snapshot.parquet")
    assert head["ServerSideEncryption"] == "AES256"


# ---------------------------------------------------------------------------
# Test 5: S3 client transport
# ---------------------------------------------------------------------------


@mock_aws
def test_s3_client_uses_path_style_addressing() -> None:
    """Path-style addressing makes the client work with MinIO / Ceph.

    Wrapped in ``mock_aws`` so the boto3 credential resolution chain
    finds the moto stub credentials rather than reaching for the real
    instance metadata service / login provider during CI.
    """
    client = build_s3_client(region_name="us-west-2", endpoint_url="http://minio.local:9000")
    config = client.meta.config
    # Path-style: ``s3://endpoint/bucket/key`` instead of virtual-host
    # style ``s3://bucket.endpoint/key``.
    assert config.s3.get("addressing_style") == "path"
    assert config.signature_version == "s3v4"
    # Custom endpoint applied.
    assert client.meta.endpoint_url == "http://minio.local:9000"


# ---------------------------------------------------------------------------
# Test 6: PUT body byte-for-byte
# ---------------------------------------------------------------------------


@mock_aws
def test_s3_put_body_round_trips() -> None:
    """The bytes we PUT come back identically on GET."""
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="rt-bucket")
    client = build_s3_client(region_name="us-east-1", endpoint_url=None)
    payload = build_parquet_bytes(
        entity_rows=[{"id": "x", "kind": "k", "name": "n"}],
        edge_rows=[],
    )
    write_archive_object(
        s3_client=client,
        bucket="rt-bucket",
        key="ws-3/2025/02/02/snapshot.parquet",
        body=payload,
        kms_key_arn=None,
    )
    obj = client.get_object(Bucket="rt-bucket", Key="ws-3/2025/02/02/snapshot.parquet")
    body_back = obj["Body"].read()
    assert body_back == payload


# ---------------------------------------------------------------------------
# Test 7: split_parquet_bytes rejects malformed blobs
# ---------------------------------------------------------------------------


def test_split_parquet_bytes_rejects_short_blob() -> None:
    """A blob without the 4-byte length prefix raises a clear error."""
    with pytest.raises(ValueError, match="too_short"):
        split_parquet_bytes(b"abc")


def test_split_parquet_bytes_rejects_truncated_blob() -> None:
    """Blob with declared length > actual body raises."""
    # Header says entities body is 1000 bytes, but only 5 bytes follow.
    bad = (1000).to_bytes(4, "big") + b"short"
    with pytest.raises(ValueError, match="truncated"):
        split_parquet_bytes(bad)
