"""PHASE G (#35-#39) — plan-gated feature ceilings.

Maps a tenant `plan` to concrete capability ceilings so paid-only features are enforced at the API
boundary with a clear 402. Centralizes the plan -> {max_top_k, allow_multi_hop, allow_rewrite,
retention_days} contract that the rest of the app consults, replacing the ad-hoc plan checks
scattered in the query path (e.g. the multi-hop gate from Phase C).

Deterministic and offline: no external calls. The default (unknown/empty) plan is treated as the
free/standard tier so a misconfigured tenant is never silently granted paid capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlanCapabilities:
    max_top_k: int
    allow_multi_hop: bool
    allow_rewrite: bool
    retention_days: int
    label: str


# Plan tiers. Free/standard = baseline RAG only; paid tiers unlock multi-hop, higher top_k,
# rewrite, and longer retention. Add tiers here; the route enforces them uniformly.
PLAN_CAPABILITIES: dict[str, PlanCapabilities] = {
    "standard": PlanCapabilities(
        max_top_k=10, allow_multi_hop=False, allow_rewrite=True, retention_days=30, label="Standard",
    ),
    "pro": PlanCapabilities(
        max_top_k=30, allow_multi_hop=True, allow_rewrite=True, retention_days=180, label="Pro",
    ),
    "enterprise": PlanCapabilities(
        max_top_k=50, allow_multi_hop=True, allow_rewrite=True, retention_days=365, label="Enterprise",
    ),
}

# Aliases accepted from the tenant model / API.
_PLAN_ALIASES = {
    "free": "standard",
    "basic": "standard",
    "enterprise-paid": "enterprise",
}


def capabilities_for(plan: str | None) -> PlanCapabilities:
    """Resolve capabilities for a plan name; unknown/empty falls back to the free/standard tier."""
    key = (plan or "standard").lower()
    key = _PLAN_ALIASES.get(key, key)
    return PLAN_CAPABILITIES.get(key, PLAN_CAPABILITIES["standard"])


def max_top_k_for(plan: str | None) -> int:
    return capabilities_for(plan).max_top_k


def plan_allows_multihop(plan: str | None) -> bool:
    return capabilities_for(plan).allow_multi_hop
