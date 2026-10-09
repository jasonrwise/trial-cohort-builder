"""Self-hosted HAPI FHIR R4 JPA server client (03-01-PLAN.md Task 1,
research.md #1/#8).

Mirrors `src/services/trials.py`'s shape exactly: a fresh `httpx.AsyncClient`
is constructed per call and never held at module scope (nothing here is
cached, memoised, or reused across calls — Constitution V); exactly one
plain, single-attempt request per `fetch_cohort` call, with no second
attempt scheduled on failure and no in-process failure-threshold trip
mechanism of any kind (that class of resilience mechanism is Phase 5's
DEGR-02 job — a second, competing implementation here would diverge from it,
exactly `trials.py`'s own precedent); and every distinguishable failure mode
(transport error, non-200 status, a body that fails strict `Bundle` parsing)
returns a distinct `FhirFetchError` member, never an unhandled exception.

The OAuth2 client-credentials token is fetched fresh on every call (never
cached across calls, never logged) from `FHIR_TOKEN_URL` using
`FHIR_CLIENT_ID`/`FHIR_CLIENT_SECRET`, with the base URL from `FHIR_BASE_URL`
(AD-31: no hardcoded URL). That environment reading and token fetch is the
public, breaker-neutral helper trio `read_fhir_env`, `fetch_access_token` and
`get_fhir_access` (AD-27): `fetch_cohort` and the Phase 12 seeder both call it,
and it never reads or changes `_breaker` — the breaker bookkeeping stays at the
`fetch_cohort` call site (AD-11 is read-side only).

`Bundle`/`BundleEntry` model only the FHIR search-result envelope strictly
(`resourceType`, `entry`) — each `entry.resource` is kept as an opaque,
generically-walkable `dict`, not a fully-typed per-resource-type model. This
mirrors `src/services/trials.py`'s `_IdentificationModule`'s own
`extra="ignore"` rationale (a large, evolving third-party document this
gateway doesn't own the shape of), extended one step further here because
`src/services/deidentify.py`'s Safe Harbor stripping pass must walk *any*
FHIR resource type's fields generically (Patient, Condition, Observation
alike) rather than through a fixed set of modelled attributes.

**Circuit breaker (DEGR-02, ARCHITECTURE-SPINE.md AD-11):** `fetch_cohort`
is now guarded by `_breaker`, one module-level `_FhirBreakerState`
singleton. It trips `closed -> open` after 3 consecutive failures of any
kind and short-circuits every subsequent call to `FhirFetchError.CIRCUIT_OPEN`
with zero live requests until `_BREAKER_COOLDOWN_SECONDS` has elapsed since
the trip, at which point the next call is let through as a single live
half-open probe: success closes the breaker (resets the counter to 0), a
failed probe re-trips immediately and restarts the cooldown from zero.
`_breaker` is the ONE named, scoped exception to this codebase's
"no module-level mutable binding" convention (`test_no_caching_layer.py`'s
AST guard only flags a module-level `dict`/`list` literal, not a class
instance) — it stores only a trip/failure count and a timestamp, never a
dependency response, so it is not a cache in the sense Constitution V
prohibits (research.md D-3).

**Data revision read (AD-29):** `fetch_data_revision` is a separate, breaker-free
lookup of the seeded revision tag on the protocol's Patients. It never reads or
changes `_breaker`, returns `None` on any failure instead of raising and never
produces a degraded envelope, and the screening pipeline calls it only after a
cohort fetch succeeded, so a broken revision lookup can neither fail a screening
nor move the breaker. The tag system literals live in `src/models/vocabulary.py`.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.models import vocabulary

_REQUEST_TIMEOUT = httpx.Timeout(10.0)

_BREAKER_COOLDOWN_SECONDS = 30

_MAX_REVISION_LENGTH = 64


class FhirFetchError(str, Enum):
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"


class FhirAccessError(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    TOKEN_UNAVAILABLE = "TOKEN_UNAVAILABLE"


@dataclass(frozen=True)
class FhirEnv:
    base_url: str
    token_url: str
    client_id: str
    client_secret: str = field(repr=False)


@dataclass(frozen=True)
class FhirAccess:
    base_url: str
    bearer_token: str = field(repr=False)


class _FhirBreakerState:
    """The one Constitution-V-exempt piece of in-process mutable state this
    codebase has (AD-11) — plain state, not a Pydantic model, since it is
    transport-layer breaker state never serialized to a caller. Exactly one
    instance exists, at module scope (`_breaker` below); never
    per-call-constructed (it would lose state) and never held anywhere
    else."""

    def __init__(self) -> None:
        self.consecutive_failures: int = 0
        self.state: Literal["closed", "open", "half_open"] = "closed"
        self.tripped_at: float | None = None


_breaker = _FhirBreakerState()


def _cooldown_elapsed() -> bool:
    if _breaker.tripped_at is None:
        return True
    return (time.monotonic() - _breaker.tripped_at) >= _BREAKER_COOLDOWN_SECONDS


def _record_breaker_success() -> None:
    _breaker.consecutive_failures = 0
    _breaker.state = "closed"
    _breaker.tripped_at = None


def _record_breaker_failure() -> None:
    if _breaker.state == "open":
        # A half-open probe just failed: re-trip immediately (no second
        # chance within the same cooldown cycle), restarting the cooldown
        # from zero.
        _breaker.tripped_at = time.monotonic()
        return
    _breaker.consecutive_failures += 1
    if _breaker.consecutive_failures >= 3:
        _breaker.state = "open"
        _breaker.tripped_at = time.monotonic()


class BundleEntry(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    resource: dict[str, Any]


class Bundle(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    resourceType: Literal["Bundle"]
    entry: list[BundleEntry] = Field(default_factory=list)


def read_fhir_env() -> FhirEnv | FhirAccessError:
    """Read the four `FHIR_*` variables from the process environment. No I/O
    beyond `os.environ`; never touches `_breaker` (AD-27). Returns
    `FhirAccessError.NOT_CONFIGURED` when `FHIR_BASE_URL` or `FHIR_TOKEN_URL`
    is unset or empty; the client id and secret keep their empty-string
    fallback (the token endpoint decides whether empty credentials are
    acceptable)."""
    base_url = os.environ.get("FHIR_BASE_URL", "")
    token_url = os.environ.get("FHIR_TOKEN_URL", "")
    if not base_url or not token_url:
        return FhirAccessError.NOT_CONFIGURED
    return FhirEnv(
        base_url=base_url,
        token_url=token_url,
        client_id=os.environ.get("FHIR_CLIENT_ID", ""),
        client_secret=os.environ.get("FHIR_CLIENT_SECRET", ""),
    )


async def fetch_access_token(env: FhirEnv) -> str | FhirAccessError:
    """POST the client-credentials grant to `env.token_url`. Never raises,
    never logs, never cached across calls — a fresh token is fetched on every
    call. Returns `FhirAccessError.TOKEN_UNAVAILABLE` on any transport,
    non-200 or malformed-response failure. Never touches `_breaker` (AD-27)."""
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            response = await client.post(
                env.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": env.client_id,
                    "client_secret": env.client_secret,
                },
            )
    except (httpx.HTTPError, httpx.InvalidURL):
        return FhirAccessError.TOKEN_UNAVAILABLE

    if response.status_code != 200:
        return FhirAccessError.TOKEN_UNAVAILABLE

    try:
        body = response.json()
        return str(body["access_token"])
    except (ValueError, KeyError, TypeError):
        return FhirAccessError.TOKEN_UNAVAILABLE


async def get_fhir_access() -> FhirAccess | FhirAccessError:
    """The public reader (AD-27): the FHIR base URL plus a fresh bearer token,
    or a distinct `FhirAccessError` value — never an exception. Breaker-neutral:
    callers (`fetch_cohort`, the seeder) own any breaker bookkeeping."""
    env = read_fhir_env()
    if isinstance(env, FhirAccessError):
        return env
    token = await fetch_access_token(env)
    if isinstance(token, FhirAccessError):
        return token
    return FhirAccess(base_url=env.base_url, bearer_token=token)


def _clean_revision(value: object) -> str | None:
    """Sanitize a revision value at the read boundary (AD-29, WD-1): stripped,
    and `None` when it is not a string, is empty, is longer than
    `_MAX_REVISION_LENGTH` or holds a non-printable character. The stored model
    field stays unvalidated, so this is the only place a hostile tag or
    environment value is rejected."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > _MAX_REVISION_LENGTH or not cleaned.isprintable():
        return None
    return cleaned


async def fetch_data_revision(protocol_id: str) -> str | None:
    """The seeded data revision for `protocol_id` (AD-29), or `None` when it
    cannot be read. Exactly one `GET {FHIR_BASE_URL}/Patient` with
    `_tag=<protocol tag system>|<protocol_id>`, `_elements=meta`, `_sort=_id`
    and `_count=1`; the data-revision tag's code on the first Patient is the
    answer. HAPI's own `SUBSETTED` tag (added by `_elements`) is ignored because
    only the data-revision tag system is matched. Every failure (no access, a
    transport error, a non-200 status, a malformed body, no matching tag)
    returns `None`; nothing is raised. Never touches `_breaker`.

    A non-blank `DATA_REVISION` environment variable wins: it is the answer and
    no request is made, even when it sanitizes to `None` (the operator's
    override is in force but unusable, so the honest answer is "unknown"). An
    unset or blank value falls through to the FHIR read. Whichever source
    supplies it, the value is trimmed and read as `None` when longer than 64
    characters or non-printable (`_clean_revision`)."""
    override = os.environ.get("DATA_REVISION", "")
    if override.strip():
        return _clean_revision(override)

    access = await get_fhir_access()
    if isinstance(access, FhirAccessError):
        return None

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            response = await client.get(
                f"{access.base_url}/Patient",
                params={
                    "_tag": f"{vocabulary.PROTOCOL_TAG_SYSTEM}|{protocol_id}",
                    "_elements": "meta",
                    "_sort": "_id",
                    "_count": "1",
                },
                headers={"Authorization": f"Bearer {access.bearer_token}"},
            )
    except (httpx.HTTPError, httpx.InvalidURL):
        return None

    if response.status_code != 200:
        return None

    try:
        bundle = Bundle.model_validate(response.json())
    except ValueError:
        return None

    for entry in bundle.entry:
        meta = entry.resource.get("meta")
        if not isinstance(meta, dict):
            continue
        tags = meta.get("tag")
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            if tag.get("system") != vocabulary.DATA_REVISION_TAG_SYSTEM:
                continue
            code = tag.get("code")
            return _clean_revision(code)
    return None


async def fetch_cohort(
    condition_code: str,
    observation_loinc: str | None,
    limit: int,
) -> Bundle | FhirFetchError:
    """A single, plain GET against the FHIR store, joined on `Patient`,
    capped at `limit` via `_count`, matching research.md #1's "query
    Condition and, when supplied, Observation resources, joined on their
    shared Patient reference" decision in one request rather than two.

    When `observation_loinc` is omitted, this searches the `Condition`
    endpoint directly, `_include`-ing the referenced `Patient` and
    `_revinclude`-ing any `Observation` resources that share the same
    patient reference.

    When `observation_loinc` is supplied, the search is instead rebased
    onto the `Patient` endpoint — the one resource type both `Condition`
    and `Observation` actually reference — using two structurally-valid,
    single-hop `_has` reverse-chaining filters
    (`_has:Condition:patient:code`, `_has:Observation:patient:code`) plus a
    `_revinclude` for each, so the response still carries the matching
    Condition/Observation resources in the same bundle. (FHIR `_has`
    requires the *referencing* resource's field to point directly at the
    *base* resource in the URL path; `Observation.patient`/`.subject` only
    ever reference `Patient`, never `Condition` — attaching `_has:
    Observation:patient:code` to a `Condition`-base search is structurally
    unsatisfiable on any conformant server, independent of data. See
    .planning/debug/resolved/fetch-cohort-has-wrong-target.md.) Still one
    request total, no second round trip.

    No second attempt on failure. A failure obtaining FHIR access (the public
    `get_fhir_access` reader: an unconfigured variable or an unavailable
    token), a transport-level failure, or a non-200 status all return
    `FhirFetchError.UPSTREAM_ERROR`; a response body that fails strict
    `Bundle` parsing returns `FhirFetchError.INVALID_RESPONSE` — never an
    unhandled exception.

    Guarded by the module-level circuit breaker (`_breaker`, DEGR-02,
    AD-11): while the breaker is `open` and its cooldown has not yet
    elapsed, this returns `FhirFetchError.CIRCUIT_OPEN` immediately with no
    live request attempted. Once cooldown has elapsed, the next call is let
    through as a single live half-open probe, and its outcome updates the
    breaker per the state-transition table in `data-model.md`."""
    if _breaker.state == "open" and not _cooldown_elapsed():
        return FhirFetchError.CIRCUIT_OPEN

    access = await get_fhir_access()
    if isinstance(access, FhirAccessError):
        _record_breaker_failure()
        return FhirFetchError.UPSTREAM_ERROR

    params: dict[str, str | list[str]]
    if observation_loinc:
        resource = "Patient"
        params = {
            "_has:Condition:patient:code": condition_code,
            "_has:Observation:patient:code": observation_loinc,
            "_revinclude": ["Condition:patient", "Observation:patient"],
            "_count": str(limit),
        }
    else:
        resource = "Condition"
        params = {
            "code": condition_code,
            "_include": "Condition:patient",
            "_revinclude": "Observation:patient",
            "_count": str(limit),
        }

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            response = await client.get(
                f"{access.base_url}/{resource}",
                params=params,
                headers={"Authorization": f"Bearer {access.bearer_token}"},
            )
    except (httpx.HTTPError, httpx.InvalidURL):
        _record_breaker_failure()
        return FhirFetchError.UPSTREAM_ERROR

    if response.status_code != 200:
        _record_breaker_failure()
        return FhirFetchError.UPSTREAM_ERROR

    try:
        body = response.json()
    except ValueError:
        _record_breaker_failure()
        return FhirFetchError.INVALID_RESPONSE

    try:
        bundle = Bundle.model_validate(body)
    except ValidationError:
        _record_breaker_failure()
        return FhirFetchError.INVALID_RESPONSE

    _record_breaker_success()
    return bundle
