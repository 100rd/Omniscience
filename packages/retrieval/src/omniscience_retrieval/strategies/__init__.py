"""Adaptive retrieval strategies for Omniscience.

Provides:
  - ``StrategyRouter``  — unified dispatch entry point
  - ``KeywordStrategy`` — BM25-only retrieval
  - ``StructuralStrategy`` — graph-first retrieval
  - ``classify_query`` — heuristic query classifier
"""

from .classifier import classify_query
from .keyword import KeywordStrategy
from .router import StrategyRouter
from .structural import StructuralStrategy

__all__ = [
    "KeywordStrategy",
    "StrategyRouter",
    "StructuralStrategy",
    "classify_query",
]
