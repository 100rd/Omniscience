"""Card builders for the MCP Apps wrapper (issue #238).

This module contains the **pure** transformation from a legacy
``resolve_incident`` response dict to a
:class:`~omniscience_server.mcp.apps.card_schemas.ResolveIncidentCard`,
plus the wire-format wrapper that decides — based on the client's
advertised capabilities — whether to attach the card to the outgoing
MCP tool response.

Critical invariants
-------------------

1. **The legacy response is never mutated.**  The builder reads the
   inner dict but never writes to it; the wrapper builds a new
   envelope and copies the legacy bytes into a ``legacy`` slot.
2. **The inner schema is never modified.**  Any new optional field
   (e.g. ``similar_past`` from #233) is rendered when present and
   silently skipped when absent.  We use ``.get()`` everywhere; no
   ``KeyError`` can escape this module on a well-typed legacy payload.
3. **Backwards compat is the default.**  :func:`wrap_resolve_incident_response`
   returns the legacy payload unchanged when the client does not
   advertise ``omniscience/apps``.  This is the wire-format escape
   hatch the issue spec mandates.
4. **Rendering is deterministic.**  Two identical inner responses
   produce byte-identical cards; rank order is stable; no datetime
   read goes through ``utcnow``.
"""

from __future__ import annotations

from typing import Any

from omniscience_server.mcp.apps.capability import (
    APPS_SCHEMA_VERSION,
    should_emit_card,
)
from omniscience_server.mcp.apps.card_schemas import (
    CitationLink,
    ConfidenceBar,
    IncidentSummary,
    RankedCandidate,
    ResolveIncidentCard,
    SimilarPastIncident,
    bucket_for_score,
)

#: Maximum number of ranked candidate rows the card carries.  Pinned
#: per the issue spec ("top-3 ranked candidates").
MAX_CANDIDATES: int = 3


# ---------------------------------------------------------------------------
# Pure builder — easy to unit test, no I/O
# ---------------------------------------------------------------------------


def build_resolve_incident_card(legacy: dict[str, Any]) -> ResolveIncidentCard:
    """Build a card from a legacy ``resolve_incident`` response dict.

    The shape of ``legacy`` is the JSON dump of
    :class:`omniscience_server.incidents.ResolveIncidentResponse`.  We
    read it via ``.get()`` so missing optional fields (target_resource,
    responsible_pr, similar_past) are gracefully treated as absent.

    Parameters
    ----------
    legacy:
        The bound legacy response.  Required keys: ``alert``,
        ``confidence_score``.  All other keys are optional.

    Returns
    -------
    ResolveIncidentCard
        Validated card payload ready to ship over the wire.

    Raises
    ------
    KeyError, TypeError
        If ``legacy`` is missing the structurally-required ``alert``
        block or has a malformed type.  These should never happen on a
        well-formed legacy response and indicate a bug upstream; we
        surface them rather than silently producing a degraded card.
    """
    alert_block = legacy["alert"]
    if not isinstance(alert_block, dict):
        raise TypeError(
            "build_resolve_incident_card: legacy['alert'] must be a dict, "
            f"got {type(alert_block).__name__}"
        )

    confidence_value = float(legacy.get("confidence_score", 0.0))
    meta_block = legacy.get("meta") or {}
    below_threshold = bool(meta_block.get("below_trust_threshold", False))

    summary = _build_summary(
        alert=alert_block,
        confidence=confidence_value,
        below_threshold=below_threshold,
    )
    candidates = _build_candidates(legacy=legacy, base_confidence=confidence_value)
    similar_past = _build_similar_past(legacy.get("similar_past"))

    return ResolveIncidentCard(
        apps_version=APPS_SCHEMA_VERSION,  # type: ignore[arg-type]
        card_type="resolve_incident",
        summary=summary,
        candidates=candidates[:MAX_CANDIDATES],
        similar_past=similar_past,
    )


def _build_summary(
    *,
    alert: dict[str, Any],
    confidence: float,
    below_threshold: bool,
) -> IncidentSummary:
    """Construct the card header from the alert block + top-level score."""
    name = str(alert.get("name", "unknown"))
    chunk_text = alert.get("chunk_text")
    fired_at_raw = alert.get("valid_from")

    headline = _alert_headline(name=name, chunk_text=chunk_text)
    summary_text = _alert_summary(name=name, chunk_text=chunk_text)

    return IncidentSummary(
        headline=headline,
        summary=summary_text,
        alert_uri=name,
        fired_at=_coerce_iso(fired_at_raw),
        below_trust_threshold=below_threshold,
    )


def _alert_headline(*, name: str, chunk_text: Any) -> str:
    """Produce a one-line headline for the card header.

    Prefers a non-empty ``chunk_text`` from the alert entity (the
    connectors carry the alert title there); falls back to the alert
    URI's tail segment so the headline is always non-empty.
    """
    if isinstance(chunk_text, str) and chunk_text.strip():
        first_line = chunk_text.strip().splitlines()[0]
        return first_line[:140]
    # Tail segment of ``alert://provider/id`` -> ``id``.
    if name.startswith("alert://"):
        tail = name[len("alert://") :]
        return tail.rsplit("/", maxsplit=1)[-1] or name
    return name


def _alert_summary(*, name: str, chunk_text: Any) -> str:
    """Produce a paragraph-length summary for the card header."""
    if isinstance(chunk_text, str) and chunk_text.strip():
        return chunk_text.strip()
    return f"Incident bundle for {name}"


# ---------------------------------------------------------------------------
# Ranked candidates: alert -> resource -> PR -> slack threads
# ---------------------------------------------------------------------------


def _build_candidates(
    *,
    legacy: dict[str, Any],
    base_confidence: float,
) -> list[RankedCandidate]:
    """Build up to three ranked candidate rows.

    Ordering: the **responsible PR is the strongest single signal**
    in the v0.1 heuristic (#153 §C) so it ranks first when present;
    next the target resource (the thing actually broken); then the
    most-recent Slack thread.  When PR is absent the resource still
    ranks first.  Threads beyond the first are not surfaced in the
    card (the renderer can dereference the legacy response for the
    full list).
    """
    rows: list[RankedCandidate] = []
    pr = legacy.get("responsible_pr")
    resource = legacy.get("target_resource")
    threads = legacy.get("slack_threads") or []

    if isinstance(pr, dict):
        rows.append(_pr_candidate(pr, confidence=base_confidence))

    if isinstance(resource, dict):
        rows.append(
            _resource_candidate(
                resource,
                # Resource alone (no PR) keeps the base score, otherwise
                # it ranks below the PR with a soft decay so the bars
                # render distinguishably.
                confidence=base_confidence if pr is None else _decay(base_confidence, by=0.15),
            )
        )

    if isinstance(threads, list) and threads:
        first_thread = threads[0]
        if isinstance(first_thread, dict):
            rows.append(
                _thread_candidate(
                    first_thread,
                    confidence=_decay(base_confidence, by=0.3),
                )
            )

    # Re-rank from 1 ascending so the wire shape is stable regardless
    # of which optional fields were populated upstream.
    for idx, row in enumerate(rows, start=1):
        rows[idx - 1] = row.model_copy(update={"rank": idx})
    return rows


def _pr_candidate(pr: dict[str, Any], *, confidence: float) -> RankedCandidate:
    name = str(pr.get("name", "unknown PR"))
    merged_at = _coerce_iso(pr.get("merged_at"))
    subtitle = f"merged {merged_at}" if merged_at else "no merge timestamp"
    return RankedCandidate(
        rank=1,
        title=_pr_title(name),
        subtitle=subtitle,
        kind="pull_request",
        confidence=_confidence_bar(confidence, label="PR temporal match"),
        citations=[CitationLink(title=_pr_title(name), uri=name, kind="pull_request")],
    )


def _resource_candidate(resource: dict[str, Any], *, confidence: float) -> RankedCandidate:
    name = str(resource.get("name", "unknown resource"))
    kind_hint = resource.get("kind") or "resource"
    subtitle_parts = [str(kind_hint)]
    edge_type = resource.get("edge_type")
    if isinstance(edge_type, str) and edge_type:
        subtitle_parts.append(f"via {edge_type}")
    return RankedCandidate(
        rank=1,
        title=name,
        subtitle=" • ".join(subtitle_parts),
        kind="resource",
        confidence=_confidence_bar(confidence, label="target resource"),
        citations=[CitationLink(title=name, uri=name, kind="resource")],
    )


def _thread_candidate(thread: dict[str, Any], *, confidence: float) -> RankedCandidate:
    name = str(thread.get("name", "unknown thread"))
    text = thread.get("chunk_text")
    subtitle: str | None = None
    if isinstance(text, str) and text.strip():
        subtitle = text.strip().splitlines()[0][:140]
    return RankedCandidate(
        rank=1,
        title=_thread_title(name),
        subtitle=subtitle,
        kind="slack_thread",
        confidence=_confidence_bar(confidence, label="related discussion"),
        citations=[CitationLink(title=_thread_title(name), uri=name, kind="slack_thread")],
    )


def _pr_title(uri: str) -> str:
    """Render ``https://github.com/o/r/pull/42`` -> ``o/r#42``."""
    if not uri.startswith(("http://", "https://")):
        return uri
    try:
        # ``.../<owner>/<repo>/pull/<n>`` — best-effort, fall back to uri.
        rest = uri.split("github.com/", 1)[1]
        owner, repo, _, num = rest.split("/", maxsplit=3)
        num = num.split("/")[0].split("?")[0].split("#")[0]
        return f"{owner}/{repo}#{num}"
    except (IndexError, ValueError):
        return uri


def _thread_title(uri: str) -> str:
    """Render ``slack://channel/C123/thread/1700000000.123`` -> ``#C123 / 1700000000.123``."""
    prefix = "slack://channel/"
    if not uri.startswith(prefix):
        return uri
    rest = uri[len(prefix) :]
    if "/thread/" not in rest:
        return f"#{rest}"
    channel, _, ts = rest.partition("/thread/")
    return f"#{channel} / {ts}"


# ---------------------------------------------------------------------------
# similar_past — additive field from #233 (optional, must not break absence)
# ---------------------------------------------------------------------------


def _build_similar_past(raw: Any) -> list[SimilarPastIncident]:
    """Render the optional ``similar_past`` field from issue #233.

    The field is added by a parallel agent and may not exist on the
    inner response when #233 ships after #238.  We never raise on
    absence; we never raise on a malformed entry; we silently skip
    anything we cannot parse so a buggy clusterer cannot break the
    card.
    """
    if not isinstance(raw, list):
        return []
    rows: list[SimilarPastIncident] = []
    for entry in raw:
        row = _try_build_similar_past_entry(entry)
        if row is not None:
            rows.append(row)
    return rows


def _try_build_similar_past_entry(entry: Any) -> SimilarPastIncident | None:
    """Best-effort builder for one ``similar_past`` row; returns ``None`` on bad input."""
    if not isinstance(entry, dict):
        return None
    alert_id = entry.get("alert_id") or entry.get("name")
    if not isinstance(alert_id, str) or not alert_id:
        return None
    title_value = entry.get("title") or entry.get("summary") or alert_id
    if not isinstance(title_value, str):
        return None
    similarity = entry.get("similarity_score")
    similarity_value: float | None
    if isinstance(similarity, (int, float)) and 0.0 <= float(similarity) <= 1.0:
        similarity_value = float(similarity)
    else:
        similarity_value = None
    return SimilarPastIncident(
        alert_id=alert_id,
        title=title_value,
        occurred_at=_coerce_iso(entry.get("occurred_at") or entry.get("valid_from")),
        similarity_score=similarity_value,
        resolution_hint=_coerce_optional_str(entry.get("resolution_hint")),
    )


# ---------------------------------------------------------------------------
# Wire wrapper — invoked from mcp/tools.py at the very end of the tool
# ---------------------------------------------------------------------------


def wrap_resolve_incident_response(
    legacy: dict[str, Any],
    *,
    ctx: Any,
) -> dict[str, Any]:
    """Attach a card to the response iff the client supports MCP Apps.

    When the client did **not** advertise ``omniscience/apps``, returns
    ``legacy`` unchanged — the legacy contract holds byte-for-byte.

    When the client **did** advertise the capability, returns a new
    envelope of the shape::

        {
            "_meta": {"omniscience/apps": { ...ResolveIncidentCard... }},
            ... all legacy keys, copied through verbatim ...
        }

    The ``_meta`` key is the MCP-standard slot for transport-level
    metadata; nesting the card under it means legacy fields stay at
    the top level and clients that only know the legacy shape see no
    change (other than the extra ``_meta`` key which they MUST ignore
    per the MCP base spec).

    Building the card is deliberately wrapped in ``try``/``except``:
    if the renderer trips on a malformed legacy payload we surface
    the legacy response unchanged rather than 500ing the tool.  The
    failure is recorded by the caller's existing telemetry; the user
    experience is "no card", not "broken tool".
    """
    if not should_emit_card(ctx):
        return legacy
    try:
        card = build_resolve_incident_card(legacy).model_dump(mode="json")
    except Exception:
        # Never let card-rendering errors break the tool.  The legacy
        # response remains byte-identical.
        return legacy

    enveloped: dict[str, Any] = dict(legacy)
    existing_meta = enveloped.get("_meta")
    new_meta: dict[str, Any] = dict(existing_meta) if isinstance(existing_meta, dict) else {}
    new_meta["omniscience/apps"] = card
    enveloped["_meta"] = new_meta
    return enveloped


# ---------------------------------------------------------------------------
# Small coercion helpers — kept private to this module
# ---------------------------------------------------------------------------


def _coerce_iso(value: Any) -> str | None:
    """Coerce a datetime-ish value into an ISO-8601 string or ``None``."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        result = iso()
        if isinstance(result, str):
            return result
    return None


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return None


def _confidence_bar(value: float, *, label: str) -> ConfidenceBar:
    """Build a :class:`ConfidenceBar` for a row given a base score."""
    clamped = max(0.0, min(1.0, value))
    return ConfidenceBar(
        value=clamped,
        bucket=bucket_for_score(clamped),
        label=f"{label} ({clamped:.2f})",
    )


def _decay(score: float, *, by: float) -> float:
    """Soft-decay a confidence by an additive amount, clamped to [0, 1]."""
    return max(0.0, min(1.0, score - by))
