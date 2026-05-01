"""Deprecation announcement tests for the legacy ``k8s-agentic`` connector.

Issue #168 — deprecation plan + migration guide + removal schedule for
``packages/connectors/src/omniscience_connectors/agentic/k8s.py``.

These tests guard the *contract* of the deprecation announcement:

1. Importing :mod:`omniscience_connectors.agentic.k8s` raises exactly one
   :class:`DeprecationWarning`.
2. The warning's message contains the removal version string ``"0.5"``,
   the migration target ``"omniscience-operator"``, and a URL to the
   migration guide.
3. The connector remains *functionally* importable — the warning is
   informational, not an error.

These tests deliberately do NOT touch any other behaviour of the
connector (factory registration, discovery, fetch).  Regression of the
existing connector behaviour is covered by ``tests/test_agentic_connector.py``.

Implementation note on warning isolation
----------------------------------------

The ``warnings.warn`` call lives at module-import time, so a previous
test in the same process may have already imported the module and
silenced the warning under the default ``"default"`` filter (which fires
once per location).  Each test below uses
:func:`importlib.reload` inside a ``warnings.catch_warnings`` block with
``simplefilter("always")`` to guarantee the warning is observable
regardless of import ordering.
"""

from __future__ import annotations

import importlib
import warnings


def test_importing_k8s_agentic_emits_deprecation_warning() -> None:
    """Importing the module raises exactly one DeprecationWarning.

    The warning's message must be the load-bearing payload — the rest of
    this test asserts the contents.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # Reload to force re-execution of module-level code regardless of
        # whether a prior test (or pytest's collection) already imported it.
        module = importlib.import_module("omniscience_connectors.agentic.k8s")
        importlib.reload(module)

    deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecation_warnings) == 1, (
        f"Expected exactly one DeprecationWarning on import; got {len(deprecation_warnings)}: "
        f"{[str(w.message) for w in deprecation_warnings]}"
    )

    message = str(deprecation_warnings[0].message)
    # Removal version is the load-bearing schedule pin (ADR-0011).
    assert "0.5" in message, f"DeprecationWarning must mention removal version 0.5; got: {message}"
    # Migration target name is what customers grep their docs for.
    assert "omniscience-operator" in message, (
        f"DeprecationWarning must name the migration target 'omniscience-operator'; got: {message}"
    )
    # Migration-guide URL is what the warning text steers them to.
    # The URL points at docs/connectors/k8s-agentic-deprecation.md so it
    # round-trips through GitHub Markdown rendering.
    assert "docs/connectors/k8s-agentic-deprecation.md" in message, (
        "DeprecationWarning must include the migration-guide URL "
        f"(docs/connectors/k8s-agentic-deprecation.md); got: {message}"
    )


def test_connector_class_is_still_importable_after_deprecation() -> None:
    """The connector class continues to import successfully in v0.3.

    Deprecation is an *announcement*, not an enforcement.  Customers'
    existing pipelines must continue to work without code changes through
    the v0.3 line.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from omniscience_connectors.agentic.k8s import (
            K8sAgenticConfig,
            K8sAgenticConnector,
        )

    # The class object resolves and is the expected type.
    assert K8sAgenticConnector.connector_type == "k8s-agentic"
    # The Pydantic config schema is intact.
    cfg = K8sAgenticConfig()
    assert cfg.cluster_name == "default"
    assert cfg.api_server == "https://kubernetes.default.svc"


def test_deprecation_message_constant_exposes_removal_version() -> None:
    """The exported ``DEPRECATION_MESSAGE`` constant is asserted as the
    canonical wording.

    The migration guide and the ADR both quote this constant by reference;
    a wording change must be a deliberate edit visible in code review.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from omniscience_connectors.agentic.k8s import DEPRECATION_MESSAGE

    assert "v0.5" in DEPRECATION_MESSAGE
    assert "omniscience-operator" in DEPRECATION_MESSAGE
    assert "docs/connectors/k8s-agentic-deprecation.md" in DEPRECATION_MESSAGE
