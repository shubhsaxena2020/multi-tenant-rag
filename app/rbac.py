"""Document-level RBAC for multi-tenant retrieval (Truto 2026 enhancement).

A tenant may have many internal sub-users with different access. The tenant passes an
`acl` scope (e.g. a list of group/role ids the calling user belongs to) at query time.
Each chunk carries an `acl` field (the source-system groups allowed to see it, mirrored
from the origin ACL at ingestion). Public chunks carry the sentinel group PUBLIC_GROUP.
We build a Qdrant `MatchAny` filter (which matches array payloads) that ONLY returns
chunks whose `acl` intersects the caller's groups OR is public. This filter is applied
at the DATABASE layer — it can never be bypassed by app logic or prompt injection,
matching the isolation-at-the-DB principle.

If the tenant doesn't use sub-user RBAC, every chunk is public (`["__public__"]`) and
all are returned (standard silo behavior). RBAC is strictly additive to tenant isolation.
"""
from __future__ import annotations

from typing import Any

from qdrant_client.models import FieldCondition, Filter, MatchAny

PUBLIC_GROUP = "__public__"


def build_acl_filter(groups: list[str] | None) -> Filter | None:
    """Return a Qdrant filter enforcing sub-user group access.

    - groups is None  -> no RBAC (tenant sees all its own data).
    - groups == []     -> only PUBLIC chunks.
    - groups provided  -> chunks whose acl intersects groups, OR public chunks.
    """
    if groups is None:
        return None
    allowed = list(groups) + [PUBLIC_GROUP] if groups else [PUBLIC_GROUP]
    return Filter(must=[FieldCondition(key="acl", match=MatchAny(any=allowed))])


def apply_acl_to_chunk_metadata(metadata: dict[str, Any], acl: list[str] | None) -> dict[str, Any]:
    """Attach an `acl` field to chunk metadata at ingestion (mirror source ACL).

    Public chunks carry the sentinel PUBLIC_GROUP; access-controlled chunks carry the
    supplied groups. The filter always resolves via MatchAny (array-aware).
    """
    groups = list(acl) if acl else [PUBLIC_GROUP]
    return {**metadata, "acl": groups}
