"""Pure Python implementation of Protobuf encoding and decoding for JetStream events."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

def encode_varint(value: int) -> bytes:
    ret = bytearray()
    while value >= 0x80:
        ret.append((value & 0x7f) | 0x80)
        value >>= 7
    ret.append(value & 0x7f)
    return bytes(ret)


def decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while True:
        b = data[pos]
        pos += 1
        val |= (b & 0x7f) << shift
        if not (b & 0x80):
            break
        shift += 7
    return val, pos


def encode_string(field_num: int, val: str | None) -> bytes:
    if not val:
        return b""
    payload = val.encode("utf-8")
    tag = (field_num << 3) | 2
    return encode_varint(tag) + encode_varint(len(payload)) + payload


def encode_int(field_num: int, val: int) -> bytes:
    tag = (field_num << 3) | 0
    return encode_varint(tag) + encode_varint(val)


def decode_message(data: bytes) -> dict[int, list[Any]]:
    pos = 0
    length = len(data)
    fields: dict[int, list[Any]] = {}
    while pos < length:
        tag, pos = decode_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 2:
            val_len, pos = decode_varint(data, pos)
            val = data[pos:pos+val_len]
            pos += val_len
            fields.setdefault(field_num, []).append(val)
        elif wire_type == 0:
            val, pos = decode_varint(data, pos)
            fields.setdefault(field_num, []).append(val)
        else:
            raise NotImplementedError(f"Unsupported wire type {wire_type}")
    return fields


def get_str_field(fields: dict[int, list[Any]], field_num: int, default: str = "") -> str:
    vals = fields.get(field_num)
    if not vals:
        return default
    return vals[0].decode("utf-8")


def get_int_field(fields: dict[int, list[Any]], field_num: int, default: int = 0) -> int:
    vals = fields.get(field_num)
    if not vals:
        return default
    return vals[0]


def serialize_document_change_event(event: Any) -> bytes:
    buf = b""
    buf += encode_string(1, str(event.source_id))
    buf += encode_string(2, event.source_type)
    buf += encode_string(3, event.external_id)
    buf += encode_string(4, event.uri)
    buf += encode_string(5, event.action)
    return buf


def deserialize_document_change_event(data: bytes) -> dict[str, Any]:
    fields = decode_message(data)
    return {
        "source_id": UUID(get_str_field(fields, 1)),
        "source_type": get_str_field(fields, 2),
        "external_id": get_str_field(fields, 3),
        "uri": get_str_field(fields, 4),
        "action": get_str_field(fields, 5),
    }


def serialize_entity_upsert_event(event: Any) -> bytes:
    buf = b""
    buf += encode_string(1, str(event.workspace_id) if event.workspace_id else None)
    buf += encode_string(2, str(event.id))
    buf += encode_string(3, str(event.source_id))
    buf += encode_string(4, event.entity_type)
    buf += encode_string(5, event.name)
    buf += encode_string(6, event.display_name)
    buf += encode_string(7, json.dumps(event.metadata))
    return buf


def deserialize_entity_upsert_event(data: bytes) -> dict[str, Any]:
    fields = decode_message(data)
    meta_str = get_str_field(fields, 7, "{}")
    try:
        metadata = json.loads(meta_str)
    except Exception:
        metadata = {}
    ws_str = get_str_field(fields, 1)
    return {
        "workspace_id": ws_str if ws_str else None,
        "id": get_str_field(fields, 2),
        "source_id": get_str_field(fields, 3),
        "entity_type": get_str_field(fields, 4),
        "name": get_str_field(fields, 5),
        "display_name": get_str_field(fields, 6),
        "metadata": metadata,
    }


def serialize_edge_upsert_event(event: Any) -> bytes:
    buf = b""
    buf += encode_string(1, str(event.workspace_id) if event.workspace_id else None)
    buf += encode_string(2, str(event.id))
    buf += encode_string(3, str(event.source_entity_id))
    buf += encode_string(4, str(event.target_entity_id))
    buf += encode_string(5, event.edge_type)
    buf += encode_string(6, json.dumps(event.metadata))
    return buf


def deserialize_edge_upsert_event(data: bytes) -> dict[str, Any]:
    fields = decode_message(data)
    meta_str = get_str_field(fields, 6, "{}")
    try:
        metadata = json.loads(meta_str)
    except Exception:
        metadata = {}
    ws_str = get_str_field(fields, 1)
    return {
        "workspace_id": ws_str if ws_str else None,
        "id": get_str_field(fields, 2),
        "source_entity_id": get_str_field(fields, 3),
        "target_entity_id": get_str_field(fields, 4),
        "edge_type": get_str_field(fields, 5),
        "metadata": metadata,
    }


def serialize_dlq_message(event: Any) -> bytes:
    buf = b""
    buf += encode_string(1, event.original_subject)
    buf += encode_string(2, event.original_payload)
    buf += encode_string(3, event.error)
    buf += encode_int(4, event.attempt_count)
    buf += encode_string(5, event.failed_at.isoformat() if hasattr(event.failed_at, "isoformat") else str(event.failed_at))
    return buf


def deserialize_dlq_message(data: bytes) -> dict[str, Any]:
    import datetime
    fields = decode_message(data)
    failed_at_str = get_str_field(fields, 5)
    try:
        failed_at = datetime.datetime.fromisoformat(failed_at_str)
    except Exception:
        failed_at = datetime.datetime.now(datetime.UTC)
    return {
        "original_subject": get_str_field(fields, 1),
        "original_payload": get_str_field(fields, 2),
        "error": get_str_field(fields, 3),
        "attempt_count": get_int_field(fields, 4),
        "failed_at": failed_at,
    }
