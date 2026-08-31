# Sentinel Staging Plan — rag-service

**Purpose:** Operator-approved scope and checklist for future Sentinel security scans. No live scan is attempted until all items below are explicitly signed off.

## Current Security Posture (Deny-By-Default — No Operator Action Required)

The following guards are always enforced and hold without further operator approval:

| Guard | Behavior | Status |
|---|---|---|
| SSRF scheme allowlist | Only `http`, `https` schemes allowed | ✅ Enforced |
| SSRF port allowlist | Only ports `80` and `443` allowed | ✅ Enforced |
| SSRF host resolution | Loopback (127.0.0.1), RFC1918, link-local, cloud metadata endpoints blocked | ✅ Enforced |
| CORS allowlist | Empty `allowed_embed_origins` → 403 disallowed-origin preflight + no `Access-Control-Allow-Origin` header on simple requests | ✅ Enforced (deny-by-default) |
| ACL parser | Wildcard `*` and empty `[]` rejected at parse time (422) | ✅ Enforced |
| Key tier | `pk_*` read-only; `rk_*` full power; expired/revoked → 401 | ✅ Enforced |
| Rate limits | Per-IP 120/min, per-tenant 600/min; 429 + `Retry-After` on exceedance | ✅ Enforced |

## Artifacts Created for Operator Sign-Off

| Artifact | Path | Status |
|---|---|---|
| Safe scope file | `safe_scope.json` | ✅ Created (skeleton with deny-by-default guidelines; operator to populate real targets) |
| Target checklist | `target_checklist.md` | ✅ Created (complete audit scope; operator sign-off required before live scan) |
| This staging plan | `staging_plan.md` | ✅ Created (operator sign-off checklist below) |

## Operator Sign-Off Checklist

The following must be verified in writing by the operator before any live Sentinel scan is permitted:

- [ ] **`allowed_embed_origins` populated** — trusted widget origin domains listed; `allow_credentials` remains `false`
- [ ] **Egress network policy confirmed** — approved outbound destinations documented and enforced at the network level
- [ ] **`safe_scope.json` populated** — explicit list of allowed scan targets (hosts/ports descriptions); `verified_by_operator: true` on each entry
- [ ] **SSRF guard rules reviewed** — current deny-by-default rules approved as-is or updated with new allowances
- [ ] **No contradictory changes** — no modifications to `app/auth.py`, `app/rbac.py`, `app/tenants.py`, or `app/validation.py` that weaken the above guards without corresponding update to this plan

## Sign-Off Record

When the operator completes the checklist above, record the sign-off here:

**Operator:** ______________________________

**Date:** ______________________________

**Verification:** Ran `pytest tests/test_security_fixes.py` — all {n}/45 passing

**Notes:** ________________________________________________________________

________________________________________________________________

**Next scan permitted:** Date ______________ (after all above items verified)

---

*This plan is fail-closed by default. No live scan is attempted until every item in the "Operator Sign-Off Checklist" is verified. The current security posture (SSRF, CORS, ACL, key-tier, rate-limit guards) is enforceable without further operator action.*
