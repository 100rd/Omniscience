"""Typed producer outcomes (SPEC-MCTX REQ-MCTX-1/7/8).

``ManagementContextResult`` is exactly one of: a success bundle, a scope denial
(REQ-MCTX-1 -- rejected before retrieval), or a fallback-required signal
(REQ-MCTX-7/8 -- contract skew or severance). There is no partially-favorable
fourth state; a caller that receives anything other than ``.bundle`` must treat the
response as unusable, never stale-but-current.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from omniscience_core.management.contract_schemas import validate_against_schema
from omniscience_core.management.taxonomy import DENIAL_REASONS, FALLBACK_REASONS

if TYPE_CHECKING:
    from omniscience_core.management.bundle import ManagementContextBundle


@dataclass(frozen=True)
class DenialResult:
    reason: str
    detail: tuple[str, ...]


@dataclass(frozen=True)
class FallbackResult:
    reason: str
    detail: tuple[str, ...]

    def to_schema_dict(self) -> dict[str, Any]:
        return {"fallback_required": True, "reason": self.reason, "detail": list(self.detail)}


def validate_fallback_result(result: FallbackResult) -> list[str]:
    return validate_against_schema(result.to_schema_dict(), "fallback-result.schema.json")


@dataclass(frozen=True)
class ManagementContextResult:
    bundle: ManagementContextBundle | None = None
    denial: DenialResult | None = None
    fallback: FallbackResult | None = None

    def __post_init__(self) -> None:
        populated = [value is not None for value in (self.bundle, self.denial, self.fallback)]
        if sum(populated) != 1:
            raise ValueError(
                "ManagementContextResult must set exactly one of bundle/denial/fallback"
            )
        if self.denial is not None and self.denial.reason not in DENIAL_REASONS:
            raise ValueError(f"unknown denial reason: {self.denial.reason!r}")
        if self.fallback is not None and self.fallback.reason not in FALLBACK_REASONS:
            raise ValueError(f"unknown fallback reason: {self.fallback.reason!r}")

    @classmethod
    def ok(cls, bundle: ManagementContextBundle) -> ManagementContextResult:
        return cls(bundle=bundle)

    @classmethod
    def deny(cls, reason: str, detail: tuple[str, ...] = ()) -> ManagementContextResult:
        return cls(denial=DenialResult(reason=reason, detail=detail))

    @classmethod
    def require_fallback(
        cls, reason: str, detail: tuple[str, ...] = ()
    ) -> ManagementContextResult:
        return cls(fallback=FallbackResult(reason=reason, detail=detail))

    @property
    def is_success(self) -> bool:
        return self.bundle is not None
