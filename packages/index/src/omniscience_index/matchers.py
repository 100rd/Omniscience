"""Name matching utilities for cross-source entity linking.

Three public match functions are provided, each returning a confidence
score in the range ``[0.0, 1.0]``:

``exact_name_match``
    Normalises both names and returns 1.0 on equality, 0.0 otherwise.

``resource_name_match``
    Fuzzy matching between a Terraform resource name and a Kubernetes
    resource name.  Both names are normalised; the score reflects the
    proportion of tokens shared between them.

``normalize_entity_name``
    Canonical normalisation: lowercase, strip common infrastructure
    prefixes (``aws_``, ``k8s_``, ``tf_``, ``gke_``, ``gcp_``,
    ``azure_``), and replace non-alphanumeric separators with ``_``.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Common cloud / infrastructure prefixes to strip during normalisation.
_STRIP_PREFIXES: tuple[str, ...] = (
    "aws_",
    "azurerm_",
    "azure_",
    "google_",
    "gcp_",
    "gke_",
    "k8s_",
    "tf_",
    "var_",
    "data_",
)

# Separators that should be treated as equivalent token boundaries.
_RE_SEPARATORS = re.compile(r"[-./: ]+")

# Kubernetes-style suffixes that carry no semantic weight.
_K8S_SUFFIXES: frozenset[str] = frozenset(
    {"deployment", "service", "configmap", "secret", "pod", "replicaset", "statefulset"}
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_entity_name(name: str) -> str:
    """Return a canonical form of *name* for comparison.

    Steps applied in order:

    1. Lowercase.
    2. Strip leading common infrastructure prefixes (iterative — handles
       stacked prefixes like ``aws_k8s_foo``).
    3. Replace separator characters (``-``, ``.``, ``/``, ``:``, space)
       with ``_``.
    4. Strip leading / trailing underscores that may remain after the
       previous steps.

    Examples::

        normalize_entity_name("aws_s3_bucket")  →  "s3_bucket"
        normalize_entity_name("MyService")       →  "myservice"
        normalize_entity_name("k8s-nginx-pod")   →  "nginx_pod"
    """
    result = name.lower()

    # Iteratively strip known prefixes (handles stacked prefixes)
    changed = True
    while changed:
        changed = False
        for prefix in _STRIP_PREFIXES:
            if result.startswith(prefix):
                result = result[len(prefix):]
                changed = True
                break

    # Normalise separators → underscore
    result = _RE_SEPARATORS.sub("_", result)

    # Strip edge underscores
    result = result.strip("_")

    return result


def exact_name_match(name_a: str, name_b: str) -> float:
    """Return 1.0 if the normalised names are identical, else 0.0.

    Normalisation is applied to both names before comparison so that
    minor casing or separator differences do not prevent a match.

    Args:
        name_a: First entity name.
        name_b: Second entity name.

    Returns:
        ``1.0`` for an exact normalised match, ``0.0`` otherwise.
    """
    if not name_a or not name_b:
        return 0.0
    return 1.0 if normalize_entity_name(name_a) == normalize_entity_name(name_b) else 0.0


def resource_name_match(tf_resource: str, k8s_resource: str) -> float:
    """Return a fuzzy match score between a Terraform and a K8s resource name.

    Strategy:

    1. Normalise both names.
    2. Exact match → 1.0 immediately.
    3. Split into tokens (underscore-separated).
    4. Remove semantic-noise tokens (k8s kind suffixes, single-char tokens).
    5. Score = 2 * |intersection| / (|tokens_a| + |tokens_b|) —
       Sorensen-Dice coefficient on the token sets.  Returns 0.0 when
       both token sets are empty.

    Args:
        tf_resource: Terraform resource name or identifier segment
                     (e.g. ``"aws_s3_bucket.my_bucket"``).
        k8s_resource: Kubernetes resource name (e.g. ``"Deployment/my-bucket"``).

    Returns:
        Score in ``[0.0, 1.0]``.  Higher is more similar.
    """
    if not tf_resource or not k8s_resource:
        return 0.0

    norm_a = normalize_entity_name(tf_resource)
    norm_b = normalize_entity_name(k8s_resource)

    if norm_a == norm_b:
        return 1.0

    tokens_a = _meaningful_tokens(norm_a)
    tokens_b = _meaningful_tokens(norm_b)

    if not tokens_a and not tokens_b:
        return 0.0

    intersection = tokens_a & tokens_b
    denominator = len(tokens_a) + len(tokens_b)
    if denominator == 0:
        return 0.0

    return 2 * len(intersection) / denominator


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _meaningful_tokens(normalised: str) -> set[str]:
    """Split a normalised name into meaningful tokens, filtering noise."""
    raw_tokens = normalised.split("_")
    return {
        t
        for t in raw_tokens
        if len(t) > 1 and t not in _K8S_SUFFIXES
    }


__all__ = [
    "exact_name_match",
    "normalize_entity_name",
    "resource_name_match",
]
