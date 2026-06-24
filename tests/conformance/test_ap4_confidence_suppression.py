"""AP4 conformance tests — confidence decimal suppression when uncalibrated.

AP4 invariant: when ``calibrated=False`` (no fitted artifact committed),
the live ``score_incident`` path must NOT return a decimal confidence
score.  Instead it must return a qualitative band (``low``/``medium``/
``high``) and a ``calibrated: bool = False`` flag IN the Pydantic schema
(not injected post-``model_dump``).

The calibrated numeric path is preserved for when a real fitted artifact
exists; the v0.1 ladder constants ``0.9/0.6/0.4/0.1`` must not be served
to users as if they were calibrated probabilities.

Implementation notes (for the implementer):
- Add ``confidence_band: Literal["low", "medium", "high"] | None`` and
  ``calibrated: bool`` to ``ResolveIncidentResponse`` (or the relevant
  Pydantic model) — NOT injected post-``model_dump``.
- When ``config=None`` (no fitted artifact): set ``confidence_band``
  based on the v0.1 ladder thresholds, set ``confidence=None`` (or
  suppress), set ``calibrated=False``.
- When a fitted artifact is loaded: set ``confidence`` (the decimal),
  ``calibrated=True``, ``confidence_band=None`` (or derived).

These tests are STUBS — they encode the intended invariant and are
marked xfail until AP4 is implemented in Batch D.  When the feature
lands: remove xfail, make tests pass.
"""

from __future__ import annotations

import pytest


@pytest.mark.xfail(reason="implemented in v9 Batch D", strict=False)
def test_uncalibrated_path_returns_band_not_decimal() -> None:
    """AP4: when calibrated=False, confidence is None/absent; band is returned instead.

    The live score_incident path with config=None (no fitted artifact)
    must NOT return a decimal confidence; it must return confidence_band
    and calibrated=False in the schema.
    """
    raise NotImplementedError("AP4 confidence suppression not yet implemented")


@pytest.mark.xfail(reason="implemented in v9 Batch D", strict=False)
def test_calibrated_field_is_in_pydantic_schema_not_injected() -> None:
    """AP4: calibrated is a first-class Pydantic field, not post-model_dump injection.

    The ``calibrated`` boolean must be declared in the response model
    definition, not patched onto the dict after ``model_dump()``.
    """
    raise NotImplementedError("AP4 schema field not yet implemented")


@pytest.mark.xfail(reason="implemented in v9 Batch D", strict=False)
def test_fitted_artifact_path_returns_decimal_confidence() -> None:
    """AP4: when a fitted artifact exists, the decimal confidence is returned.

    The isotonic-regression calibrated path must return a real decimal
    ``confidence`` value and ``calibrated=True`` in the schema.
    """
    raise NotImplementedError("AP4 calibrated path not yet verified")


@pytest.mark.xfail(reason="implemented in v9 Batch D", strict=False)
def test_confidence_band_values_are_valid() -> None:
    """AP4: confidence_band must be one of 'low', 'medium', 'high' or None.

    No other string values are permissible.
    """
    raise NotImplementedError("AP4 band validation not yet implemented")


@pytest.mark.xfail(reason="implemented in v9 Batch D", strict=False)
def test_ladder_constants_not_exposed_to_users_as_calibrated() -> None:
    """AP4: the v0.1 ladder constants (0.9/0.6/0.4/0.1) are never returned
    as calibrated=True to users.

    If the ladder is used, calibrated must be False and confidence must be
    suppressed (None or absent).
    """
    raise NotImplementedError("AP4 ladder suppression not yet implemented")
