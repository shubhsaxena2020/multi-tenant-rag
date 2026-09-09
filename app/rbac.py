"""Document-level RBAC for multi-tenant retrieval.

A tenant may have many internal sub-users with different access. The tenant passes an
`acl` scope (e.g. a list of group/role ids the calling sub-user belongs to) and the
service restricts retrieval to chunks whose `acl` intersects those groups OR is public.

SECURITY MODEL (fixes audit finding #2 — caller-supplied acl must NOT be trusted blindly):
The set of groups a tenant is *authorized* for is provisioned server-side on the tenant
record (`allowed_groups`, default `["__public__"]` = public-only). A request may only *narrow* to
groups the tenant is actually provisioned for; any requested group outside `allowed_groups`
is dropped (logged as a self-escalation attempt). This prevents a tenant from widening its
own access by simply claiming extra groups in the API body.

`"__public__"` is the safe default; `"*"` remains a wildcard only when explicitly
provisioned (the tenant sees every group it is entitled to).

The tenant KEY still derives collection isolation (forged tenant IDs are rejected at the
routing layer) — that mechanism is unchanged and correct; this layer is *document-level*
scoping *within* an authorized tenant.
"""
from __future__ import annotations

from qdrant_client.models import FieldCondition, Filter, MatchAny

PUBLIC_GROUP = "__public__"


def resolve_acl(
    requested: list[str] | None,
    allowed_groups: list[str] | None,
    *,
    default_to_public: bool,
) -> list[str]:
    """Compute the concrete acl list to APPLY (never None).

    - requested is None:
        * query  (default_to_public=False): caller wants everything the tenant is
          entitled to -> return the tenant's allowed_groups (public-only by default;
          wildcard only when explicitly provisioned).
        * ingest (default_to_public=True): no group specified -> the doc is PUBLIC.
    - requested is a list: keep only groups the tenant is authorized for (drop the rest,
      which would otherwise be a self-escalation). If nothing remains, fall back to
      [PUBLIC_GROUP] so the result is "visible to none-but-public" rather than "all".
    """
    allowed = allowed_groups or []
    if requested is None:
        # Query (default_to_public=False): show everything the tenant is entitled to.
        # Ingest (default_to_public=True): unspecified group -> the doc is PUBLIC.
        if default_to_public:
            return [PUBLIC_GROUP]
        return list(allowed) if allowed else [PUBLIC_GROUP]
    eff = [
        g for g in requested
        if g == PUBLIC_GROUP or allowed == ["*"] or g in allowed
    ]
    return eff or [PUBLIC_GROUP]


def build_acl_filter(acl: list[str] | None):
    """Build a Qdrant filter from an acl group list, or None for 'no restriction'.

    - None -> no restriction (caller already decided the tenant may see everything).
    - "*" present -> wildcard; no restriction.
    - otherwise -> chunk.acl must intersect (groups + PUBLIC_GROUP).
    """
    if acl is None:
        return None
    groups = list(acl)
    if "*" in groups:
        return None
    groups = groups + [PUBLIC_GROUP]
    return Filter(
        must=[FieldCondition(key="acl", match=MatchAny(any=groups))]
    )


def apply_acl_to_chunk_metadata(metadata: dict, acl: list[str] | None) -> dict:
    """Embed the document-level acl into a chunk's metadata so the Qdrant payload filter
    (see build_acl_filter) can scope retrieval. An empty/None acl means public-only."""
    md = dict(metadata or {})
    if acl:
        md["acl"] = list(acl)
    else:
        md["acl"] = [PUBLIC_GROUP]
    return md
