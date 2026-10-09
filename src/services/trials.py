"""ClinicalTrials.gov API v2 client and free-text eligibility segmentation
(tasks.md T020).

A fresh `httpx.AsyncClient` is constructed inside `fetch_trial` and never held
at module scope — no response is ever cached, memoised, or reused across
calls. `NCT_ID_PATTERN` is the one legitimate module-level constant here (a
compiled `re.Pattern`, not a dict/list, so it does not trip
`tests/unit/test_no_caching_layer.py`'s AST guard).

`fetch_trial` retries **only** on HTTP 429, up to 4 total attempts (1 initial
+ 3 retries) with 1s/2s/4s backoff between them, behind exactly one
`httpx.AsyncClient` held across the whole call (DEGR-01, AD-10). Every other
failure mode (not-found, any other non-200 status, transport error,
strict-parse failure) stays single-attempt — retry is scoped to 429 only.
`fetch_trial` never raises to its caller: every distinguishable failure mode
returns a distinct `TrialFetchError` member, following
`src/services/access_control.py`'s `resolve_role` never-raise shape.

`fetch_trial` re-validates `nct_id` against `NCT_ID_PATTERN` itself before
building the request URL, returning `TrialFetchError.UPSTREAM_ERROR` on a
mismatch — it does not merely trust a caller's own pre-check. This is
`fetch_trial`'s own defense-in-depth: `get_protocol_criteria`
(`src/tools/get_protocol_criteria.py`) already short-circuits on the same
pattern before ever calling this function, but `fetch_trial` is a public,
non-underscore-prefixed function with no guarantee a future caller (a script,
a test helper, a later phase) will replicate that guard, and unvalidated
input reaching the URL construction below would inject arbitrary path
segments into the `clinicaltrials.gov` request.
"""

from __future__ import annotations

import asyncio
import json
import re
from enum import Enum

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from src.models import vocabulary
from src.models.errors import ClientError

# Anchored, confirmed against three real live-fetched IDs in 02-RESEARCH.md:
# NCT04280783, NCT01779336, NCT01370005 — all "NCT" + exactly 8 digits. `\Z`
# (not `$`) is deliberate: `$` matches immediately before a trailing `\n`, so
# a caller-supplied "NCT01234567\n" would otherwise slip past this
# short-circuit and reach `fetch_trial`'s URL construction as a malformed
# argument (plan 02-03, D-06 malformed-boundary coverage) — `\Z` matches only
# the true end of the string. The digits are an explicit ASCII class because, in
# a str pattern, the backslash-d class matches any Unicode decimal digit, so an
# Arabic-Indic or fullwidth ID would reach a filename, a URL and FHIR tag tokens.
# This matches the pattern in the seed manifest contract schema (12-REVIEW WR-03).
NCT_ID_PATTERN = re.compile(r"^NCT[0-9]{8}\Z")

_TRIALS_BASE_URL = "https://clinicaltrials.gov/api/v2/studies"
_REQUEST_TIMEOUT = httpx.Timeout(10.0)


class TrialFetchError(str, Enum):
    NOT_FOUND = "NOT_FOUND"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    RATE_LIMIT_EXHAUSTED = "RATE_LIMIT_EXHAUSTED"


# DEGR-01/AD-10: exactly 4 total attempts (1 initial + 3 retries) at a 429,
# waiting 1s then 2s then 4s between successive attempts — fixed by
# ARCHITECTURE-SPINE.md AD-10, not open to re-derivation (Constitution
# Principle IX). A tuple, not a list — immutable, so it does not trip
# tests/unit/test_no_caching_layer.py's module-level-mutable-binding guard.
_RATE_LIMIT_MAX_ATTEMPTS = 4
_RATE_LIMIT_BACKOFF_SECONDS = (1, 2, 4)


class _IdentificationModule(BaseModel):
    """Only the two leaf fields this phase needs are modelled. `extra="ignore"`
    (not "forbid") is deliberate here and departs from this codebase's usual
    local-contract convention: this is the first upstream payload modelling a
    large, evolving third-party document (`protocolSection` alone has a dozen
    sibling modules we never touch) rather than a small closed shape we own —
    forbidding every field ClinicalTrials.gov might add or already sends would
    make every real fetch brittle for no correctness benefit. `strict=True`
    still refuses silent type coercion on the fields we do model, which is the
    actual DEGR-04 guarantee."""

    model_config = ConfigDict(strict=True, extra="ignore")

    briefTitle: str


class _EligibilityModule(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    eligibilityCriteria: str


class _ProtocolSection(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    identificationModule: _IdentificationModule
    eligibilityModule: _EligibilityModule


class TrialPayload(BaseModel):
    """Strict-parsed ClinicalTrials.gov v2 study response — the DEGR-04
    mechanism applied to this upstream (Pattern 1). Only
    `protocolSection.identificationModule.briefTitle` and
    `protocolSection.eligibilityModule.eligibilityCriteria` are consumed this
    phase; nothing else in the payload is reached into as a raw dict."""

    model_config = ConfigDict(strict=True, extra="ignore")

    protocolSection: _ProtocolSection


def _read_reserved_payload(nct_id: str) -> TrialPayload | TrialFetchError:
    """Read and strictly validate the committed synthetic payload for a reserved
    demo ID (AD-28). The path is built only after `fetch_trial` has matched
    `nct_id` against the frozen `vocabulary.RESERVED_DEMO_IDS`, so no caller
    string reaches the filesystem. Synchronous file I/O inside the async
    `fetch_trial` is acceptable here: the payload is one tiny local file. The
    directory is read from the `vocabulary` module at call time, never cached,
    so the file is re-read on every call. A missing or unreadable file (`OSError`)
    and malformed, non-UTF-8 or schema-invalid content (`ValueError`, which
    covers `json.JSONDecodeError`, `UnicodeDecodeError` and pydantic's
    `ValidationError`) map to `INVALID_RESPONSE` — nothing else is caught."""
    path = vocabulary.DEMO_DATA_DIR / "synthetic" / f"{nct_id}.json"
    try:
        return TrialPayload.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return TrialFetchError.INVALID_RESPONSE


async def fetch_trial(nct_id: str) -> TrialPayload | TrialFetchError:
    """GET `https://clinicaltrials.gov/api/v2/studies/{nct_id}`, retrying
    **only** on HTTP 429 (DEGR-01, AD-10): up to `_RATE_LIMIT_MAX_ATTEMPTS`
    total attempts, waiting `_RATE_LIMIT_BACKOFF_SECONDS` between successive
    attempts, behind exactly one `httpx.AsyncClient` constructed for the
    whole call — never re-constructed per attempt, never held at module
    scope. Exhausting all attempts on 429 returns `RATE_LIMIT_EXHAUSTED`.
    Never raises. `nct_id` is validated against `NCT_ID_PATTERN` before it is
    interpolated into the request URL, mapping a malformed ID to
    `UPSTREAM_ERROR` — this function is safe to call standalone and does not
    merely trust a caller's own pre-check. A 404 maps to `NOT_FOUND`; a
    transport error or any other non-200 status maps to `UPSTREAM_ERROR`
    (single-attempt, not retried); a body that fails strict parsing maps to
    `INVALID_RESPONSE`. A reserved demo ID (`vocabulary.RESERVED_DEMO_IDS`) is
    resolved first, from the local synthetic payload read per call, with no
    request, no retry and no cache (AD-28)."""
    if nct_id in vocabulary.RESERVED_DEMO_IDS:
        return _read_reserved_payload(nct_id)

    if not NCT_ID_PATTERN.match(nct_id):
        return TrialFetchError.UPSTREAM_ERROR

    url = f"{_TRIALS_BASE_URL}/{nct_id}"

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            for attempt in range(_RATE_LIMIT_MAX_ATTEMPTS):
                response = await client.get(url)
                if response.status_code != 429:
                    break
                if attempt == _RATE_LIMIT_MAX_ATTEMPTS - 1:
                    return TrialFetchError.RATE_LIMIT_EXHAUSTED
                await asyncio.sleep(_RATE_LIMIT_BACKOFF_SECONDS[attempt])
    except httpx.HTTPError:
        return TrialFetchError.UPSTREAM_ERROR

    if response.status_code == 404:
        return TrialFetchError.NOT_FOUND
    if response.status_code != 200:
        return TrialFetchError.UPSTREAM_ERROR

    try:
        body = response.json()
    except ValueError:
        return TrialFetchError.INVALID_RESPONSE

    try:
        return TrialPayload.model_validate(body)
    except ValidationError:
        return TrialFetchError.INVALID_RESPONSE


# D-03: lines the registry already exposes as structured `eligibilityModule`
# fields elsewhere, so they never become their own near-guaranteed-UNMAPPED
# criterion if a differently-formatted trial embeds them inline.
_STRUCTURED_FIELD_LABELS = (
    "sex",
    "ages eligible for study",
    "accepts healthy volunteers",
    "minimum age",
    "maximum age",
)
_STRUCTURED_LINE_PATTERN = re.compile(
    r"^\s*(" + "|".join(re.escape(label) for label in _STRUCTURED_FIELD_LABELS) + r")\s*:",
    re.IGNORECASE,
)

# D-05: case-insensitive, tolerates an optional "Key " prefix and an optional
# trailing colon — not an exact literal match.
_SECTION_MARKER_PATTERN = re.compile(
    r"^\s*(?:key\s+)?(inclusion|exclusion)\s+criteria\s*:?\s*$",
    re.IGNORECASE,
)

# D-04: the registry's own list markers — newlines (handled by splitlines),
# bullet characters, numbered items ("1.", "1)", "-", "*", "•").
_LIST_MARKER_PATTERN = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")

# 02-RESEARCH.md Pitfall 1: the registry is community-reported (not an
# NLM/ClinicalTrials.gov-confirmed behavior) to sometimes markdown-escape
# comparison operators and hyphens in the free-text `eligibilityCriteria`
# field. Defensive normalisation, not a confirmed universal behavior — if the
# assumption is wrong this substitution is a harmless no-op. Empirically
# confirmed PRESENT in this phase's own real, live-captured fixture data:
# both NCT01370005 (`\>=18`, `\>= 7.0%`, `=\< 10%`, `\>240 mg/dl`, `\>13.3
# mmol/L`) and NCT01779336 (`\<`  x3, `\>470`) carry it — see 02-03-SUMMARY.md
# for the full accounting, which closes 02-RESEARCH.md Assumption A1 as
# CONFIRMED for both demo trials, not the "neither trial exhibited it" the
# research pass predicted from a different (unselected) sample trial.
_MARKDOWN_ESCAPE_PATTERN = re.compile(r"\\([<>=-])")


def _unescape_markdown_operators(text: str) -> str:
    """Strip a backslash immediately preceding a comparison operator or
    hyphen (`\\>` -> `>`, `\\<` -> `<`, `\\=` -> `=`, `\\-` -> `-`), applied
    before any `raw_text` is emitted so neither the response nor a downstream
    consumer (plan 02-04's operator/threshold regex) ever sees the escape
    sequence survive."""
    return _MARKDOWN_ESCAPE_PATTERN.sub(r"\1", text)


def segment_criteria(eligibility_text: str) -> tuple[list[str], list[str]] | ClientError:
    """Turns the registry's free-text eligibility block into two ordered
    lists of raw criterion strings: `(inclusion, exclusion)`.

    Four steps, in order: (1) drop D-03 structured-field lines; (2) locate
    the Inclusion/Exclusion section markers (D-05); (3) reject the whole
    trial with a `ClientError` if no marker was found anywhere — including
    when the text is empty or whitespace-only — never guessing which lines
    belong to which section and never dumping everything into one bucket
    (D-02); (4) split each found section on the registry's own list markers,
    one criterion per bullet/line, never per sentence (D-04), unescaping
    markdown-escaped operators (Pitfall 1) before the text is collected.
    Order is preserved exactly — no sort, no dedupe, no re-rank (ROADMAP
    SC-4).

    D-01, one-sided sections: when exactly one marker is found, the *other*
    side is simply `[]` — not an error. This falls out of `_section_span`
    unchanged: it already returns `None` (and therefore `[]` from
    `_collect`) for a kind whose marker is absent, independent of whether the
    other kind's marker was found. Only the neither-marker-found case (empty
    `markers`) is the D-02 rejection branch below.

    PD-01, nested sub-lists (02-RESEARCH.md Pitfall 2 / Open Question 2,
    locked by this plan): a parent bullet followed by a nested numbered
    sub-list (e.g. NCT01779336's "normal organ and marrow function as
    defined below:" followed by five numbered lab-value sub-items) becomes
    one independent criterion per sub-item *plus* the parent header line as
    its own criterion — this also falls out of the existing per-line
    collection unchanged, since every non-empty line in a section's span
    (parent header and each numbered sub-item alike) already becomes its own
    entry; no line-fusing or line-dropping logic exists to undo. Splitting
    maximises the cleanly-verifiable surface (each lab value is
    independently LOINC-checkable); keeping the parent line honours the
    no-silent-drop prohibition (a header with no lab value of its own lands
    on UNMAPPED, which is the correct, visible outcome). The exact resulting
    criterion count for NCT01779336 is pinned by a test in
    `tests/unit/test_trials.py` so it cannot drift silently.
    """
    lines = eligibility_text.splitlines()
    kept_lines = [line for line in lines if not _STRUCTURED_LINE_PATTERN.match(line.strip())]

    markers: list[tuple[int, str]] = []
    for index, line in enumerate(kept_lines):
        match = _SECTION_MARKER_PATTERN.match(line)
        if match:
            markers.append((index, match.group(1).lower()))

    if not markers:
        return ClientError(
            status="ERROR",
            reason=(
                "Eligibility text could not be segmented: no 'Inclusion Criteria' or "
                "'Exclusion Criteria' section marker was found"
            ),
        )

    def _section_span(kind: str) -> tuple[int, int] | None:
        for marker_index, (line_no, found_kind) in enumerate(markers):
            if found_kind == kind:
                end = (
                    markers[marker_index + 1][0]
                    if marker_index + 1 < len(markers)
                    else len(kept_lines)
                )
                return (line_no + 1, end)
        return None

    def _collect(span: tuple[int, int] | None) -> list[str]:
        if span is None:
            return []
        start, end = span
        criteria: list[str] = []
        for line in kept_lines[start:end]:
            stripped = _LIST_MARKER_PATTERN.sub("", line).strip()
            if stripped:
                criteria.append(_unescape_markdown_operators(stripped))
        return criteria

    inclusion = _collect(_section_span("inclusion"))
    exclusion = _collect(_section_span("exclusion"))
    return inclusion, exclusion
