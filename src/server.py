"""FastMCP stdio app and the single RBAC gate middleware in front of every
tool call (tasks.md T009).

`-32003` mechanism (RESEARCH.md Assumption A2, now settled): raising
`mcp.MCPError(code=..., message=...)` is how FastMCP's own internals surface a
specific JSON-RPC error code (confirmed by reading the installed fastmcp==4.0.3
and mcp==2.2.0 packages — `fastmcp.server.mixins.mcp_operations` and
`fastmcp.server.extensions` both raise `MCPError(code=..., message=...)`
directly for spec-specific codes). For a *tool* call specifically,
`_on_call_tool` only intercepts `FastMCPError` subclasses (returning them as an
in-band `CallToolResult(is_error=True)`, per that module's own comment: "the
SDK v2 runner turns a raise into a -32603 wire error"); `mcp.MCPError` is not a
`FastMCPError` subclass, so it is NOT caught there and propagates up to
`mcp.shared.jsonrpc_dispatcher.handler_exception_to_error_data`, which special-
cases `isinstance(exc, MCPError)` and returns `exc.error` verbatim — the
raised code reaches the wire unchanged. See 01-02-SUMMARY.md for the read-path
evidence trail.
"""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp import MCPError
from mcp_types import CallToolRequestParams

from src import config
from src.models.access import EventType
from src.models.errors import AccessDeniedError
from src.services import access_control, audit
from src.tools.dispatch_screening_alert import dispatch_screening_alert
from src.tools.get_protocol_criteria import get_protocol_criteria
from src.tools.query_patient_cohort import query_patient_cohort


class RBACGateMiddleware(Middleware):
    """The single cross-cutting RBAC gate, registered once via
    `add_middleware` — not a decorator repeated per tool, which would drift
    the moment a fourth tool is added. Enforces an unconditional allow-list
    for every tool identically, regardless of whether that tool's contract
    happens to also carry a `deniedRoles` block."""

    def __init__(self, api_key: str, keys_file_path: str) -> None:
        self._api_key = api_key
        self._keys_file_path = keys_file_path

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext,
    ):
        tool_name = context.message.name
        role = access_control.resolve_role(self._api_key, self._keys_file_path)

        if not access_control.is_allowed(tool_name, role):
            # Audited FIRST, synchronously, before the error response is
            # constructed.
            audit.write_entry(
                role=role,
                nct_id=None,
                event_type=EventType.ACCESS_DENIED,
                detail=f"denied on {tool_name}",
            )
            denied = AccessDeniedError(code=-32003, message=f"Access denied for tool '{tool_name}'")
            raise MCPError(code=denied.code, message=denied.message)

        # Makes the server-resolved role available to the tool body via
        # FastMCP's request-scoped Context state (04-01-PLAN.md Task 2) — a
        # tool that needs its caller's role (e.g. dispatch_screening_alert's
        # dispatching_role) reads it back with `await ctx.get_state(
        # "resolved_role")` from its own injected `ctx: Context` parameter,
        # never re-resolving it itself. `serializable=False` keeps this
        # request-scoped only (never session-persisted), matching AD-2's
        # never-memoised re-read-per-call guarantee — this is a per-call
        # handoff of the same call's already-resolved role, not a cache.
        if context.fastmcp_context is not None:
            await context.fastmcp_context.set_state("resolved_role", role, serializable=False)

        return await call_next(context)


def create_server() -> FastMCP:
    """Loads config first (so a misconfigured deployment dies at boot, before
    FastMCP is even constructed), builds the audit logger, constructs the
    FastMCP app, adds the RBAC gate, and registers the tracer tool."""
    settings = config.load_config()
    audit.build_audit_logger(settings.audit_log_path)

    # strict_input_validation=True carries SC-4's "no silent coercion"
    # guarantee from the model layer (plan 01-01) out to the transport
    # boundary: a wrong-typed tool argument is rejected as a handled
    # fastmcp.exceptions.ValidationError (itself converted from the
    # underlying pydantic.ValidationError — see fastmcp.tools.function_tool),
    # returned as an in-band CallToolResult(is_error=True), never coerced and
    # never an unhandled crash.
    # description= is passed explicitly rather than relying on a function
    # docstring: none of the three tool functions below has one (each module
    # carries its docstring at module level instead), so FastMCP's docstring
    # inference would otherwise leave every listed tool's description empty
    # over the wire (SC-1 requires a connected client to see the real tool
    # surface, not merely the real names). Text is copied verbatim from each
    # tool's own contracts/*.json "description" field (D-06).
    mcp = FastMCP(name="trialbridge-mcp", strict_input_validation=True)
    mcp.add_middleware(RBACGateMiddleware(settings.api_key, settings.keys_file))
    mcp.tool(
        get_protocol_criteria,
        description=(
            "Fetches and structures inclusion/exclusion criteria for a specific "
            "clinical study from ClinicalTrials.gov."
        ),
    )
    mcp.tool(
        query_patient_cohort,
        description=(
            "Queries the self-hosted synthetic FHIR store across conditions and "
            "observations using standard ontologies (ICD-10-CM / LOINC), and returns "
            "each matching candidate already de-identified and scored against the "
            "supplied verified criteria."
        ),
    )
    mcp.tool(
        dispatch_screening_alert,
        description=(
            "Returns a draft scorecard (CRC role) or dispatches it to Slack (PI role) "
            "depending on the caller's server-resolved role. Role is never taken from "
            "the request — see FR-016."
        ),
    )

    return mcp


def main() -> None:
    server = create_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
