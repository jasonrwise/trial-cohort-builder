"""The seeder's FHIR writer and breaker-neutral store reads (12-04-PLAN.md, AD-27).

A plain, fail-fast writer: one `PUT` transaction bundle per load with
deterministic resource ids, a single attempt with no retry, nothing cached, and
a fresh `httpx.AsyncClient` per call (never held at module scope). Every
function returns `None`, a value or a `FhirWriteFailure` — it never raises
(WD-1: the upstream-adapter rule of returning a distinct error value; the CLI
turns a failure into a non-zero exit).

The bounded readiness poll (`wait_until_ready`) is the single retry-shaped
exception: it repeats only the capability read, never a write.

Callers pass the `fhir.FhirAccess` obtained from `fhir.get_fhir_access()`; this
module never reads environment variables itself and never reads or changes the
gateway's circuit breaker (AD-11 is read-side only).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

from src.services import fhir

READY_ATTEMPTS: int = 30
READY_INTERVAL_SECONDS: float = 2.0

_WRITE_TIMEOUT = httpx.Timeout(30.0)
_READ_TIMEOUT = httpx.Timeout(10.0)
_FHIR_JSON = "application/fhir+json"
_MAX_SUMMARY_ISSUES = 3
_MAX_SUMMARY_CHARS = 500


class FhirWriteError(str, Enum):
    NOT_READY = "NOT_READY"
    REJECTED = "REJECTED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    INVALID_BUNDLE = "INVALID_BUNDLE"


@dataclass(frozen=True)
class FhirWriteFailure:
    error: FhirWriteError
    detail: str


def _base(access: fhir.FhirAccess) -> str:
    return access.base_url.rstrip("/")


def _auth_headers(access: fhir.FhirAccess) -> dict[str, str]:
    return {"Authorization": f"Bearer {access.bearer_token}"}


def _summarize_rejection(response: httpx.Response) -> str:
    """A summary taken only from the server's OperationOutcome issues (severity
    and diagnostics), or `HTTP <status>` when the body offers none. Never built
    from headers, so the bearer token cannot reach it."""
    fallback = f"HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return fallback
    if not isinstance(body, dict) or body.get("resourceType") != "OperationOutcome":
        return fallback
    issues = body.get("issue")
    if not isinstance(issues, list):
        return fallback
    parts: list[str] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        diagnostics = issue.get("diagnostics")
        if not isinstance(diagnostics, str) or not diagnostics:
            continue
        severity = issue.get("severity")
        parts.append(f"{severity if isinstance(severity, str) else 'unknown'}: {diagnostics}")
        if len(parts) == _MAX_SUMMARY_ISSUES:
            break
    if not parts:
        return fallback
    return "; ".join(parts)[:_MAX_SUMMARY_CHARS]


def _bundle_problem(bundle: object) -> str | None:
    """Why `bundle` is not an all-PUT transaction of own-id entries, or `None`."""
    if not isinstance(bundle, dict):
        return "bundle is not an object"
    if bundle.get("resourceType") != "Bundle" or bundle.get("type") != "transaction":
        return "bundle is not a transaction Bundle"
    entries = bundle.get("entry")
    if not isinstance(entries, list):
        return "bundle has no entry list"
    for index, entry in enumerate(entries):
        resource = entry.get("resource") if isinstance(entry, dict) else None
        request = entry.get("request") if isinstance(entry, dict) else None
        if not isinstance(resource, dict) or not isinstance(request, dict):
            return f"entry {index} has no resource and request"
        resource_type, resource_id = resource.get("resourceType"), resource.get("id")
        if not isinstance(resource_type, str) or not isinstance(resource_id, str):
            return f"entry {index} resource has no type and id"
        if request.get("method") != "PUT":
            return f"entry {index} is not a PUT"
        if request.get("url") != f"{resource_type}/{resource_id}":
            return f"entry {index} does not PUT its own {resource_type}/{resource_id}"
    return None


async def wait_until_ready(
    access: fhir.FhirAccess,
    *,
    attempts: int | None = None,
    interval_seconds: float | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> FhirWriteFailure | None:
    """Poll `GET <base>/metadata` a bounded number of times. Returns `None` on the
    first 200; otherwise a `NOT_READY` failure naming the server URL. Sleeps
    between attempts, never after the last. The defaults are read at call time."""
    limit = READY_ATTEMPTS if attempts is None else attempts
    interval = READY_INTERVAL_SECONDS if interval_seconds is None else interval_seconds
    pause = asyncio.sleep if sleep is None else sleep
    base = _base(access)
    for attempt in range(limit):
        try:
            async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
                response = await client.get(f"{base}/metadata", headers=_auth_headers(access))
            if response.status_code == 200:
                return None
        except (httpx.HTTPError, httpx.InvalidURL):
            pass
        if attempt < limit - 1:
            await pause(interval)
    return FhirWriteFailure(
        FhirWriteError.NOT_READY,
        f"FHIR server at {base} was not ready after {limit} attempts; nothing was written.",
    )


async def put_transaction(
    access: fhir.FhirAccess, bundle: dict[str, Any]
) -> FhirWriteFailure | None:
    """POST `bundle` once to the FHIR base URL, after checking locally that every
    entry is a PUT to its own `<Type>/<id>` (`INVALID_BUNDLE` otherwise, with no
    request). Returns `None` when the server answers 200 with a
    `transaction-response` Bundle; a refusal is `REJECTED` with the server's own
    summary. Exactly one attempt, never retried."""
    problem = _bundle_problem(bundle)
    if problem is not None:
        return FhirWriteFailure(FhirWriteError.INVALID_BUNDLE, problem)
    base = _base(access)
    try:
        async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as client:
            response = await client.post(
                base,
                json=bundle,
                headers={**_auth_headers(access), "Content-Type": _FHIR_JSON},
            )
    except (httpx.HTTPError, httpx.InvalidURL):
        return FhirWriteFailure(FhirWriteError.UPSTREAM_ERROR, f"could not reach {base}")

    if response.status_code != 200:
        return FhirWriteFailure(FhirWriteError.REJECTED, _summarize_rejection(response))

    try:
        body = response.json()
    except ValueError:
        return FhirWriteFailure(FhirWriteError.INVALID_RESPONSE, "response body is not JSON")
    if (
        not isinstance(body, dict)
        or body.get("resourceType") != "Bundle"
        or body.get("type") != "transaction-response"
    ):
        return FhirWriteFailure(
            FhirWriteError.INVALID_RESPONSE, "response is not a transaction-response Bundle"
        )
    return None


async def count_patients(
    access: fhir.FhirAccess, tags: Sequence[tuple[str, str]]
) -> int | FhirWriteFailure:
    """The exact Patient count for an AND of `_tag=system|code` filters, read with
    `_summary=count` so no resources are transferred.

    HAPI caches search results for about a minute. `_total=accurate` keeps a count read right
    after a seed or reset from returning a stale value (Phase 13 plan 13-05, WD-15)."""
    base = _base(access)
    params = [("_tag", f"{system}|{code}") for system, code in tags]
    params.append(("_summary", "count"))
    params.append(("_total", "accurate"))
    try:
        async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
            response = await client.get(
                f"{base}/Patient", params=params, headers=_auth_headers(access)
            )
    except (httpx.HTTPError, httpx.InvalidURL):
        return FhirWriteFailure(FhirWriteError.UPSTREAM_ERROR, f"could not reach {base}")

    if response.status_code != 200:
        return FhirWriteFailure(FhirWriteError.UPSTREAM_ERROR, f"HTTP {response.status_code}")

    try:
        total = response.json()["total"]
    except (ValueError, KeyError, TypeError):
        return FhirWriteFailure(FhirWriteError.INVALID_RESPONSE, "count response has no total")
    if not isinstance(total, int) or isinstance(total, bool):
        return FhirWriteFailure(
            FhirWriteError.INVALID_RESPONSE, "count response total is not an integer"
        )
    return total
