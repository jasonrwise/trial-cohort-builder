"""Per-request, no-cache key->role resolution and the unconditional per-tool
allow-list (tasks.md T007), plus a second, parallel allow-list for the web
app's own endpoint names (06-02-PLAN.md Task 2, tasks.md T010, AD-18) — never
merged with the MCP table, since the two surfaces are gated independently.

No `lru_cache`, no `functools.cache`, no module-level dict holding file
contents, and no "reload if mtime changed" optimisation anywhere below — the
no-caching constraint is a project-level constraint (PROJECT.md, FR-017), and
plan 01-04 adds a static guard that fails on any such construct under
`src/services/`.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from src.models.access import AccessCredential, Role

# Populated for all three contract tools now, even though only
# `get_protocol_criteria` is registered in this plan — plans 01-03 registers
# the other two against the same table.
ALLOWED_ROLES_BY_TOOL: dict[str, frozenset[Role]] = {
    "get_protocol_criteria": frozenset({Role.CRC, Role.PI}),
    "query_patient_cohort": frozenset({Role.CRC, Role.PI}),
    "dispatch_screening_alert": frozenset({Role.CRC, Role.PI}),
}


def resolve_role(api_key: str, keys_file_path: str) -> Role | None:
    """Re-reads the key->role JSON file from disk on EVERY call — never
    cached — and validates the matching entry through `AccessCredential`
    (D-01). Returns `None` for every failure mode (key absent, file missing,
    unparseable JSON, an entry that fails strict validation): it never raises
    and never calls `sys.exit`, and it never logs or echoes the key value."""
    try:
        with open(keys_file_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None

    raw_role = data.get(api_key)
    if raw_role is None:
        return None

    # AccessCredential is strict: an Enum field under strict=True requires an
    # actual Role instance, not merely a string that matches one of its
    # values (pydantic does not coerce str->Enum in strict mode) — so the
    # str->Role conversion happens explicitly here, once, and anything that
    # isn't an exact match (wrong case, unknown role) raises ValueError and
    # resolves to "no role" rather than being silently accepted.
    try:
        role = Role(raw_role)
    except ValueError:
        return None

    try:
        credential = AccessCredential(key=api_key, role=role)
    except ValidationError:
        return None

    return credential.role


def is_allowed(tool_name: str, role: Role | None) -> bool:
    """Unconditional allow-list check — NOT a `deniedRoles` check. Returns
    False for an unregistered tool name (deny-by-default) so a tool added
    later without a table entry is denied rather than left open, and False
    whenever `role` is None (an unresolved key has no role, never a default
    one)."""
    if role is None:
        return False
    return role in ALLOWED_ROLES_BY_TOOL.get(tool_name, frozenset())


# A second, parallel deny-by-default table keyed by src/web module/endpoint
# names (06-02-PLAN.md Task 2, AD-18) — never merged with ALLOWED_ROLES_BY_TOOL
# above, since the MCP and web surfaces are gated independently. "login" maps
# to an empty frozenset: it is the one pre-authentication endpoint, gated by
# AD-16's lockout instead of a role check.
ALLOWED_ROLES_BY_WEB_ENDPOINT: dict[str, frozenset[Role]] = {
    "login": frozenset(),
    "list_scorecards": frozenset({Role.CRC, Role.PI}),
    "get_scorecard": frozenset({Role.CRC, Role.PI}),
    "query_screening": frozenset({Role.CRC, Role.PI}),
    "rerun_scorecard": frozenset({Role.CRC, Role.PI}),
    "export_pdf": frozenset({Role.CRC, Role.PI}),
    "approve_finalize": frozenset({Role.PI}),
}


def is_allowed_web_endpoint(endpoint_name: str, role: Role | None) -> bool:
    """`is_allowed`'s exact body shape, applied to the web table above:
    False for an unregistered endpoint name (deny-by-default), False
    whenever `role` is None."""
    if role is None:
        return False
    return role in ALLOWED_ROLES_BY_WEB_ENDPOINT.get(endpoint_name, frozenset())
