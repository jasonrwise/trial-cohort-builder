"""Safe Harbor de-identification pass (03-01-PLAN.md Task 2, research.md #7,
reused unchanged from `specs/001-trial-eligibility-screening/research.md`
§9).

**Allowlist, not blocklist (`/review phase 3`, 2026-09-15):** a related
Condition/Observation resource's top-level fields are first filtered down to
an explicit per-resourceType allowlist of known-safe fields
(`_CONDITION_SAFE_KEYS`/`_OBSERVATION_SAFE_KEYS`) before anything else runs.
Free-text FHIR fields this module doesn't know about — `note` (Annotation),
`valueString`, `component`, `interpretation`, `encounter`, `extension`,
`device`, `specimen`, `bodySite`, and any future field this list hasn't been
taught about — are dropped by default instead of forwarded verbatim. This
replaced an earlier design that only stripped a fixed *blocklist* of
identifier-shaped keys and forwarded everything else unexamined, which let
exactly those free-text fields leak through untested and undetected.

Within the allowlisted subtree, `strip_bundle` walks every remaining
structured field for the Safe-Harbor-identifier-shaped field names (`name`,
`birthDate`, `address`, `telecom`, `identifier`, `photo`, `contact`,
`generalPractitioner`, `managingOrganization`, `subject`, `performer`,
`recorder`, `asserter`, and `id` — `id` because a Condition/Observation's own
resource `id` is Safe Harbor category 18 ("any other unique identifying
number/code"), the same reasoning already applied to `Patient.id`; `subject`
because its `reference` embeds that real `Patient.id`) at any nesting depth,
and redacts the same identifying values when they are also duplicated in a
resource's narrative `text.div` HTML block (Synthea's own known behavior,
per research.md #7 and `specs/001`'s research.md §9). Every individual-linked
date field — `onsetDateTime`/`recordedDate`/`abatementDateTime`,
`effectiveDateTime`/`issued`/`effectiveInstant`, `lastUpdated`, and the
`onsetPeriod`/`abatementPeriod`/`effectivePeriod` choice-field shapes'
`start`/`end` — is coarsened to its bare year, never forwarded with
month/day/time precision (Safe Harbor category 3, 03-SECURITY.md T2, CR-01).
Age is derived from `birthDate` at strip time and capped/aggregated to the
literal `"90 or older"` once it exceeds 89, or reported as `None` ("unknown")
when `birthDate` is missing, fails to parse as a full-precision date (CR-03),
or resolves to a negative age (IN-02) — never a specific, plausible-looking
but wrong integer like `0` — `birthDate` itself is never forwarded in any
field, structured or narrative. `strip_bundle` selects the related
Condition/Observation matching the caller's own queried codes rather than
trusting FHIR `_revinclude` bundle order, since `_revinclude` has no code
filter of its own (CR-02).

`patient_pseudonym` is a server-generated, sequential, opaque identifier
(`CAND-0001`, `CAND-0002`, ...) — never the real FHIR `Patient.id` or an MRN,
which would itself be a Safe Harbor identifier (data-model.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any, Literal

from src.models.candidate import MAX_INTEGER_AGE
from src.services.fhir import Bundle

# Allowlist, not blocklist (`/review phase 3`): only these top-level fields
# of a related Condition/Observation resource are ever forwarded at all.
# Anything not on this list — `note` (Annotation, free text),
# `valueString`/`component` (Observation's free-text/multi-component value[x]
# alternates to the structured `valueQuantity` this phase actually consumes),
# `interpretation` (its own `.text` sub-field is free text), `encounter`,
# `extension`, `device`, `specimen`, `bodySite`, and any other field this
# module hasn't been taught about — fails closed (dropped) instead of
# forwarding an unanticipated field verbatim. `id`/`subject` are deliberately
# NOT here even though FHIR resources carry them: they're still removed by
# `_IDENTIFIER_KEYS` below, so listing them here would be a no-op at best and
# a foot-gun if `_IDENTIFIER_KEYS` ever changed without this list changing
# too. `meta` is allowed through so its nested `lastUpdated` can still be
# coarsened by `_DATE_COARSEN_KEYS` below — `meta`'s own field set is small
# and FHIR-spec-fixed (`versionId`/`lastUpdated`/`source`/`profile`/
# `security`/`tag`), none of which are individual-linked free text.
_CONDITION_SAFE_KEYS = frozenset(
    {
        "resourcetype",
        "code",
        "clinicalstatus",
        "verificationstatus",
        "category",
        "severity",
        "onsetdatetime",
        "onsetperiod",
        "onsetage",
        "onsetrange",
        "abatementdatetime",
        "abatementperiod",
        "abatementage",
        # "abatementstring" deliberately NOT here (CR-01, gsd-code-review
        # 03-REVIEW.md): `Condition.abatement[x]`'s `abatementString` shape
        # is free text ("resolved after treatment", "in remission since
        # discharge", ...), not a structured/coded value and not a
        # parseable date `_year_only` could coarsen — same rationale as
        # `note`/`valueString`/`interpretation` above, it fails closed.
        "recordeddate",
        "meta",
        "text",
    }
)

_OBSERVATION_SAFE_KEYS = frozenset(
    {
        "resourcetype",
        "code",
        "status",
        "category",
        "valuequantity",
        "effectivedatetime",
        "effectiveperiod",
        "effectiveinstant",
        "issued",
        "meta",
        "text",
    }
)


def _allowlist_resource_keys(resource: dict[str, Any]) -> dict[str, Any]:
    """Filters `resource` down to only the fields known-safe for its own
    `resourceType` — the allowlist half of Safe Harbor stripping. A
    `resourceType` this module doesn't have an allowlist for is returned
    unfiltered (defensive fallback; `strip_bundle` only ever calls this on
    genuine Condition/Observation resources)."""
    resource_type = resource.get("resourceType")
    if resource_type == "Condition":
        safe_keys = _CONDITION_SAFE_KEYS
    elif resource_type == "Observation":
        safe_keys = _OBSERVATION_SAFE_KEYS
    else:
        return resource
    return {key: value for key, value in resource.items() if key.lower() in safe_keys}


_IDENTIFIER_KEYS = frozenset(
    {
        "name",
        "birthdate",
        "address",
        "telecom",
        "identifier",
        "photo",
        "contact",
        "generalpractitioner",
        "managingorganization",
        "subject",
        "performer",
        "recorder",
        "asserter",
        # Safe Harbor category 18 ("any other unique identifying number,
        # characteristic, or code") — a Condition/Observation's own `id` is
        # exactly this, the same reasoning already applied to `Patient.id`
        # (03-SECURITY.md T2).
        "id",
    }
)

# Safe Harbor category 3 ("all elements of dates ... directly related to an
# individual") permits retaining the YEAR only. These are the flat-string
# individual-linked date fields this phase's Condition/Observation resources
# can carry; unlike `_IDENTIFIER_KEYS` they are coarsened, not removed
# outright (03-SECURITY.md T2). `Condition.onset[x]`/`abatement[x]` and
# `Observation.effective[x]` are FHIR R4 *polymorphic* choice fields — a
# real store is free to populate any of several `*[x]`-suffixed keys instead
# of the plain `*DateTime` variant below, so `abatementDateTime` joins this
# set (CR-01, gsd-code-review 03-REVIEW.md) alongside the original five.
_DATE_COARSEN_KEYS = frozenset(
    {
        "onsetdatetime",
        "recordeddate",
        "effectivedatetime",
        "issued",
        "lastupdated",
        "abatementdatetime",
    }
)

# The `*Period`/`*Instant` choice-field shapes of the same polymorphic
# fields above are not flat strings — `_year_only` cannot apply directly to
# a `{"start": ..., "end": ...}` object or handle `effectiveInstant`'s own
# key — so they get dedicated handling in `_strip_identifier_keys` (CR-01).
# `onsetAge`/`onsetRange`/`abatementAge` are deliberately NOT included here:
# they are not literal dates, so Safe Harbor's date-precision rule does not
# directly apply to them.
_DATE_COARSEN_PERIOD_KEYS = frozenset({"onsetperiod", "abatementperiod", "effectiveperiod"})
_DATE_COARSEN_INSTANT_KEYS = frozenset({"effectiveinstant"})

_REDACTED = "[REDACTED]"


def _year_only(value: Any) -> Any:
    """Coarsens an ISO-8601 date/datetime string to its year component only.
    `None` passes through unchanged (nothing to coarsen); any other non-
    conforming value is redacted outright rather than forwarded unexamined —
    fail closed, the same discipline `terminology.verify_code` applies to
    verification failures."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) < 4 or not value[:4].isdigit():
        return _REDACTED
    return value[:4]


@dataclass(frozen=True)
class DeidentifiedCandidate:
    """One synthetic patient's Safe-Harbor-stripped values, before
    `src/tools/query_patient_cohort.py` merges in
    `src/services/scoring.py`'s eligibility determination to build the wire
    `Candidate` model."""

    patient_pseudonym: str
    age: int | Literal["90 or older"] | None
    condition_values: dict[str, Any]
    observation_values: dict[str, Any]


def _collect_identifier_strings(node: Any, under_identifier_key: bool = False) -> list[str]:
    """Collects every string value nested under a Safe-Harbor-identifier-
    shaped key anywhere in `node`, for later narrative redaction — used
    *before* the structured fields themselves are stripped.

    Also collects the *raw*, pre-coarsening value of every individual-linked
    date field this module later reduces to year-only precision
    (`_DATE_COARSEN_KEYS`/`_DATE_COARSEN_PERIOD_KEYS`/
    `_DATE_COARSEN_INSTANT_KEYS`) — a full-precision date is itself a Safe
    Harbor category-3 identifier, and a FHIR narrative generator (e.g.
    HAPI's default Thymeleaf-based one) commonly echoes the same
    full-precision value into `text.div`'s free-text HTML, a code path the
    structured-field coarsening in `_strip_identifier_keys` never touches
    (CR-02, gsd-code-review 03-REVIEW.md)."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = key.lower()
            is_identifier_key = lowered in _IDENTIFIER_KEYS
            if lowered in _DATE_COARSEN_KEYS or lowered in _DATE_COARSEN_INSTANT_KEYS:
                if isinstance(value, str) and value.strip():
                    found.append(value)
            elif lowered in _DATE_COARSEN_PERIOD_KEYS and isinstance(value, dict):
                for period_key, period_value in value.items():
                    if (
                        period_key.lower() in ("start", "end")
                        and isinstance(period_value, str)
                        and period_value.strip()
                    ):
                        found.append(period_value)
            found.extend(
                _collect_identifier_strings(value, under_identifier_key or is_identifier_key)
            )
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_identifier_strings(item, under_identifier_key))
    elif isinstance(node, str) and under_identifier_key and node.strip():
        found.append(node)
    return found


def _strip_identifier_keys(node: Any) -> Any:
    """Returns a structurally-equal copy of `node` with every Safe-Harbor-
    identifier-shaped key removed at any nesting depth, and every
    individual-linked date field coarsened to its year component only —
    flat fields via `_DATE_COARSEN_KEYS`, FHIR `Period` choice fields
    (`start`/`end`, themselves full-precision dates) via
    `_DATE_COARSEN_PERIOD_KEYS`, and `instant` choice fields via
    `_DATE_COARSEN_INSTANT_KEYS` (CR-01)."""
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for key, value in node.items():
            lowered = key.lower()
            if lowered in _IDENTIFIER_KEYS:
                continue
            if lowered in _DATE_COARSEN_KEYS or lowered in _DATE_COARSEN_INSTANT_KEYS:
                result[key] = _year_only(value)
                continue
            if lowered in _DATE_COARSEN_PERIOD_KEYS:
                if not isinstance(value, dict):
                    # Fail closed like `_year_only` does for a malformed flat
                    # date string: a non-object Period shape can't be
                    # start/end-coarsened, so it must not be forwarded as-is.
                    result[key] = _REDACTED
                    continue
                result[key] = {
                    period_key: (
                        _year_only(period_value)
                        if period_key.lower() in ("start", "end")
                        else period_value
                    )
                    for period_key, period_value in value.items()
                }
                continue
            result[key] = _strip_identifier_keys(value)
        return result
    if isinstance(node, list):
        return [_strip_identifier_keys(item) for item in node]
    return node


def _matches_code(resource: dict[str, Any], code: str) -> bool:
    """True when `resource["code"]["coding"]` contains an entry whose own
    `code` equals `code`. Used to select the specific Condition/Observation
    the caller actually queried for: FHIR's `_revinclude` (unlike the
    primary search's own `code=` filter) has no code parameter of its own,
    so it returns every Observation referencing an included Patient
    regardless of that Observation's own code (CR-02, gsd-code-review
    03-REVIEW.md)."""
    code_field = resource.get("code")
    codings = code_field.get("coding") if isinstance(code_field, dict) else None
    return any(coding.get("code") == code for coding in (codings or []) if isinstance(coding, dict))


# FHIR narrative generators (including HAPI's default Thymeleaf-based one,
# `fhir.py`'s own target server) commonly HTML-entity-escape these
# characters when embedding a structured field's raw value into
# `resource.text.div`. An exact substring match against the raw value alone
# silently misses any occurrence containing one of them (WR-01,
# gsd-code-review 03-REVIEW.md) — several equivalent entity spellings exist
# per character (named, decimal, hex), so all are matched. `MappingProxyType`
# (not a plain `dict` literal) keeps this a genuinely-immutable static
# lookup table, consistent with `tests/unit/test_no_caching_layer.py`'s
# static guard against module-level mutable bindings under `src/services/`.
_NARRATIVE_HTML_ENTITY_VARIANTS: MappingProxyType[str, tuple[str, ...]] = MappingProxyType(
    {
        "&": ("&amp;", "&#38;", "&#x26;", "&#X26;"),
        "<": ("&lt;", "&#60;", "&#x3c;", "&#X3C;"),
        ">": ("&gt;", "&#62;", "&#x3e;", "&#X3E;"),
        '"': ("&quot;", "&#34;", "&#x22;", "&#X22;"),
        "'": ("&apos;", "&#39;", "&#x27;", "&#X27;"),
    }
)


# CR-01 (iteration 2, gsd-code-review 03-REVIEW.md): the exact-string scan
# `_narrative_match_pattern` performs only ever catches a narrative date that
# echoes a *collected* raw field value byte-for-byte (`_collect_identifier_strings`).
# A FHIR narrative generator is equally free to render the same
# individual-linked date at reduced precision (date only, no time-of-day/
# offset) or in a materially different format than the raw stored value —
# that rendering never equals any collected identifier string, so its
# month/day would otherwise survive untouched (the literal-echo case this
# module already handled is the narrower, less likely rendering; a
# human-readable date-only rendering is the more likely one).
#
# CR-01 (iteration 3) then broadened iteration-2's single `YYYY-MM-DD[, time]`
# pattern to an enumerated list of specific alternatives (reduced `YYYY-MM`
# precision, `/`- and `.`-delimited, month-name forms). 03-VERIFICATION.md's
# COHT-02 gap (iteration 4) independently reproduced 3 MORE unmatched formats
# against that enumerated list (compact `20230615`, non-zero-padded
# `2023-6-15`, day-before-month-name `15-Jun-2023`) — the same failure mode
# repeating: enumerating exact formats always leaves the *next* one unmatched.
#
# The patterns below replace that enumeration with a genuinely general,
# format-agnostic scanner: any 1-2/1-2/4-digit (or 4/1-2/1-2-digit) numeric
# triplet in a common ordering (YMD/MDY/DMY), with `-`, `/`, `.`, or no
# separator at all, plus a full-or-abbreviated month name with the day before
# or after it. A regex alone cannot distinguish a genuine date from an
# incidental digit run that merely fits one of these shapes (a phone number,
# an MRN, an arbitrary ID) — so every candidate match is additionally
# validated with `datetime.date` itself (`_valid_calendar_date`/
# `_valid_year_month`, gated by `_plausible_year`) before it is redacted;
# a candidate that doesn't parse as a real, plausible calendar date is left
# untouched. This closes the false-negative class (a genuinely novel format
# is still date-*shaped* and so still matches) and the false-positive risk
# (matching non-date numeric text) with one mechanism, per 03-VERIFICATION.md's
# fix guidance. Safe Harbor category 3 permits retaining the year, so only
# the narrower month/day/time portion of a matched date is ever discarded.
_MONTH_NUMBERS: MappingProxyType[str, int] = MappingProxyType(
    {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
)

_MONTH_NAME_ALTERNATION = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?"
    r"|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
)

# Year-first, delimited: YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD, 1-2 digit
# month/day (so both zero-padded and non-padded forms match), optional
# trailing ISO time/offset suffix.
_YEAR_FIRST_DATE_PATTERN = re.compile(
    r"\b(?P<yf_year>\d{4})(?P<yf_sep>[-/.])(?P<yf_a>\d{1,2})(?P=yf_sep)(?P<yf_b>\d{1,2})"
    r"(?:T[\d:.,+Zz-]*)?\b"
)
# Year-first, reduced precision: YYYY-MM only (no day) — FHIR's `date` type
# legally permits this, the same class `_compute_capped_age` documents for
# `birthDate`.
_YEAR_MONTH_REDUCED_PATTERN = re.compile(r"\b(?P<ym_year>\d{4})-(?P<ym_month>\d{2})\b")
# Year-last, delimited, order-ambiguous: {MM,DD}/{DD,MM}/YYYY in either MDY
# (US) or DMY (international) order — whichever parses as a real date.
_YEAR_LAST_DATE_PATTERN = re.compile(
    r"\b(?P<yl_a>\d{1,2})(?P<yl_sep>[-/.])(?P<yl_b>\d{1,2})(?P=yl_sep)(?P<yl_year>\d{4})\b"
)
# Compact, no separator at all: an 8-digit run tried as YYYYMMDD, MMDDYYYY,
# and DDMMYYYY in turn.
_COMPACT_DATE_PATTERN = re.compile(r"\b(?P<compact>\d{8})\b")
# Month name before day: "June 15, 2023" / "Jun 15, 2023".
_MONTH_THEN_DAY_PATTERN = re.compile(
    rf"\b(?P<mtd_month>{_MONTH_NAME_ALTERNATION})\.?\s+(?P<mtd_day>\d{{1,2}})"
    rf"(?:st|nd|rd|th)?,?\s+(?P<mtd_year>\d{{4}})\b",
    re.IGNORECASE,
)
# Day before month name: "15-Jun-2023" / "15 June 2023".
_DAY_THEN_MONTH_PATTERN = re.compile(
    rf"\b(?P<dtm_day>\d{{1,2}})(?:st|nd|rd|th)?[-\s]+(?P<dtm_month>{_MONTH_NAME_ALTERNATION})"
    rf"\.?[-\s,]+(?P<dtm_year>\d{{4}})\b",
    re.IGNORECASE,
)

# Bounds a candidate match's "year" slot to a plausible real-world range —
# guards against, e.g., an arbitrary 4-digit numeric field being mistaken for
# the year component of a date-shaped triplet purely because it sits in the
# right position.
_MIN_PLAUSIBLE_YEAR = 1900
_MAX_PLAUSIBLE_YEAR = 2099


def _plausible_year(year: int) -> bool:
    return _MIN_PLAUSIBLE_YEAR <= year <= _MAX_PLAUSIBLE_YEAR


def _valid_calendar_date(year: int, month: int, day: int) -> bool:
    """True only when `(year, month, day)` is both a plausible year and a
    real calendar date per `datetime.date` — validating against the stdlib
    itself, not another regex, so e.g. day=32 or month=13 is rejected even
    though it matched one of the date-shaped patterns above."""
    if not _plausible_year(year):
        return False
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def _valid_year_month(year: int, month: int) -> bool:
    return _plausible_year(year) and 1 <= month <= 12


def _redact_year_first_date(match: re.Match[str]) -> str:
    year = int(match.group("yf_year"))
    month, day = int(match.group("yf_a")), int(match.group("yf_b"))
    if _valid_calendar_date(year, month, day):
        return match.group("yf_year")
    return match.group(0)


def _redact_year_month_reduced(match: re.Match[str]) -> str:
    year, month = int(match.group("ym_year")), int(match.group("ym_month"))
    if _valid_year_month(year, month):
        return match.group("ym_year")
    return match.group(0)


def _redact_year_last_date(match: re.Match[str]) -> str:
    year = int(match.group("yl_year"))
    a, b = int(match.group("yl_a")), int(match.group("yl_b"))
    # Ambiguous ordering — no locale marker survives narrative rendering —
    # so accept either month-day (MDY) or day-month (DMY) as long as ONE of
    # them is a real calendar date.
    if _valid_calendar_date(year, a, b) or _valid_calendar_date(year, b, a):
        return match.group("yl_year")
    return match.group(0)


def _redact_compact_date(match: re.Match[str]) -> str:
    digits = match.group("compact")
    for year_str, month_str, day_str in (
        (digits[0:4], digits[4:6], digits[6:8]),  # YYYYMMDD
        (digits[4:8], digits[0:2], digits[2:4]),  # MMDDYYYY
        (digits[4:8], digits[2:4], digits[0:2]),  # DDMMYYYY
    ):
        if _valid_calendar_date(int(year_str), int(month_str), int(day_str)):
            return year_str
    return match.group(0)


def _redact_month_then_day(match: re.Match[str]) -> str:
    month = _MONTH_NUMBERS[match.group("mtd_month")[:3].lower()]
    day, year = int(match.group("mtd_day")), int(match.group("mtd_year"))
    if _valid_calendar_date(year, month, day):
        return match.group("mtd_year")
    return match.group(0)


def _redact_day_then_month(match: re.Match[str]) -> str:
    month = _MONTH_NUMBERS[match.group("dtm_month")[:3].lower()]
    day, year = int(match.group("dtm_day")), int(match.group("dtm_year"))
    if _valid_calendar_date(year, month, day):
        return match.group("dtm_year")
    return match.group(0)


def _redact_narrative_dates(text: str) -> str:
    """Runs each date-shaped pattern above over `text` in turn, redacting a
    substring down to its year component only when it BOTH matches a
    date-shaped pattern AND validates as a real calendar date — the general,
    format-agnostic replacement for the prior enumerated
    `_NARRATIVE_DATE_PATTERN` (03-VERIFICATION.md gap COHT-02, iteration 4).
    Patterns are applied sequentially (each over the previous pattern's
    output) rather than as one combined alternation, since the different
    orderings/separators are structurally disjoint and this keeps each
    pattern's own validation logic independently readable and testable."""
    text = _YEAR_FIRST_DATE_PATTERN.sub(_redact_year_first_date, text)
    text = _YEAR_MONTH_REDUCED_PATTERN.sub(_redact_year_month_reduced, text)
    text = _YEAR_LAST_DATE_PATTERN.sub(_redact_year_last_date, text)
    text = _COMPACT_DATE_PATTERN.sub(_redact_compact_date, text)
    text = _MONTH_THEN_DAY_PATTERN.sub(_redact_month_then_day, text)
    text = _DAY_THEN_MONTH_PATTERN.sub(_redact_day_then_month, text)
    return text


def _narrative_match_pattern(value: str) -> re.Pattern[str]:
    """Builds a regex matching `value` verbatim, OR with any of its
    HTML-entity-escapable characters (`&`/`<`/`>`/`"`/`'`) substituted for
    one of their entity-escaped spellings in any combination — so a value
    that survived narrative HTML-entity-escaping is still matched (WR-01)."""
    parts: list[str] = []
    for char in value:
        variants = _NARRATIVE_HTML_ENTITY_VARIANTS.get(char)
        if variants:
            alternatives = "|".join(re.escape(spelling) for spelling in (char, *variants))
            parts.append(f"(?:{alternatives})")
        else:
            parts.append(re.escape(char))
    return re.compile("".join(parts))


def _redact_narrative(resource: dict[str, Any], identifier_strings: list[str]) -> dict[str, Any]:
    """Replaces every occurrence of a collected identifier string inside
    `resource["text"]["div"]` with a fixed redaction marker — the narrative-
    text half of Safe Harbor stripping (Synthea duplicates structured
    identifiers there, research.md #7). Matches both the value's raw form
    and any HTML-entity-escaped form a narrative generator may have applied
    (WR-01, gsd-code-review 03-REVIEW.md)."""
    text = resource.get("text")
    if not isinstance(text, dict) or not isinstance(text.get("div"), str):
        return resource

    redacted_div = text["div"]
    for value in identifier_strings:
        if value:
            redacted_div = _narrative_match_pattern(value).sub(_REDACTED, redacted_div)

    # CR-01 (iteration 2/3), COHT-02 (iteration 4): catches a narrative date
    # rendered in a different format/precision than the raw structured
    # value, independent of the exact-match scan above — keeping only the
    # matched date's year, the same precision `_year_only` already applies
    # structurally.
    redacted_div = _redact_narrative_dates(redacted_div)

    return {**resource, "text": {**text, "div": redacted_div}}


def _strip_and_redact(
    resources: list[dict[str, Any]], identifier_strings: list[str]
) -> dict[str, Any]:
    """Applies `_allowlist_resource_keys`, then `_strip_identifier_keys`,
    then `_redact_narrative` to the first of `resources`, or returns `{}`
    when there is none — the shared "select and de-identify" step
    `strip_bundle` runs once for the related Condition and once for the
    related Observation."""
    if not resources:
        return {}
    allowlisted = _allowlist_resource_keys(resources[0])
    return _redact_narrative(_strip_identifier_keys(allowlisted), identifier_strings)


def _references_patient(resource: dict[str, Any], patient_id: Any) -> bool:
    if patient_id is None:
        return False
    subject = resource.get("subject")
    if not isinstance(subject, dict):
        return False
    return subject.get("reference") == f"Patient/{patient_id}"


def _compute_capped_age(birth_date: str) -> int | Literal["90 or older"] | None:
    """`None` when `birth_date` cannot be honestly resolved to a real age —
    either `date.fromisoformat` cannot parse it (FHIR's `date` type legally
    permits reduced precision, `"1960"`/`"1960-03"`, and the FHIR store's
    response is untrusted, externally-sourced data this gateway does not
    control the shape of), or it parses but yields a negative age (a
    `birthDate` in the future — a plausible data-entry error upstream this
    gateway also does not control, IN-02, gsd-code-review 03-REVIEW.md).
    Never raises: an uncaught `ValueError` here would break the fail-closed,
    never-unhandled-exception discipline `fhir.py`/`terminology.py` are
    careful to uphold (CR-03, gsd-code-review 03-REVIEW.md)."""
    try:
        born = date.fromisoformat(birth_date)
    except ValueError:
        return None
    today = datetime.now(UTC).date()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    if age < 0:
        return None
    if age > MAX_INTEGER_AGE:
        return "90 or older"
    return age


def strip_bundle(
    bundle: Bundle, condition_code: str, observation_loinc: str | None = None
) -> list[DeidentifiedCandidate]:
    """De-identifies every `Patient`/`Condition`/`Observation` resource in
    `bundle`, joined on the FHIR `subject.reference` convention, and returns
    one `DeidentifiedCandidate` per `Patient` resource present. Only the
    first related `Condition`/`Observation` resource per patient is
    surfaced — this phase's `query_patient_cohort` queries a single
    `condition_code` and, when supplied, a single `observation_loinc` per
    call (data-model.md's `CohortQuery`), so at most one of each is
    expected per candidate.

    `condition_code`/`observation_loinc` are the SAME codes the caller
    queried `fhir.fetch_cohort` with, and are required here to select the
    right related resource among possibly several: FHIR's `_has` reverse-
    chaining filter narrows which *Patients* the primary search matches, not
    which `_revinclude`-joined Observations land in the Bundle, so a patient
    with more than one Observation returns all of them, unfiltered by code.
    Trusting bundle order (`[0]`) without matching on code would silently
    feed the wrong Observation's value into scoring (CR-02, gsd-code-review
    03-REVIEW.md). `condition_code` is always the primary search's own
    `code=` filter (`fhir.py`), so every returned Condition already matches
    it — the match here is applied defensively anyway, in case that query
    shape ever changes. `observation_loinc` is optional; when the caller
    didn't request one, every related Observation is kept unfiltered
    (pre-existing behavior — there is no code to disambiguate against)."""
    resources = [entry.resource for entry in bundle.entry]
    # `_include=Condition:patient` can legitimately return the same Patient
    # resource more than once (e.g. via more than one matching Condition) —
    # dedupe by id so one real individual never becomes two candidates. A
    # Patient resource without a usable id is skipped rather than
    # deduped-and-kept: any falsy-or-non-string `id` — missing entirely
    # (`None`), an empty string (`""`), or a non-string value a malformed
    # upstream response might send (`0`/`False`) — for two distinct,
    # unrelated malformed Patient resources would otherwise collapse them
    # into a single candidate via that shared falsy key colliding in
    # `seen_patient_ids` (WR-02, then WR-01 iteration 2, gsd-code-review
    # 03-REVIEW.md: the `id is None` guard alone missed the empty-string
    # case) — the same fail-closed discipline
    # `_references_patient`/`_matches_code`/`_year_only` already apply to
    # other malformed-upstream-data shapes, since `_revinclude`'s
    # `Bundle.entry.resource` is untyped, externally-controlled input
    # (`fhir.py`'s own docstring).
    seen_patient_ids: set[Any] = set()
    patients: list[dict[str, Any]] = []
    for resource in resources:
        if resource.get("resourceType") != "Patient":
            continue
        patient_id = resource.get("id")
        if not isinstance(patient_id, str) or not patient_id or patient_id in seen_patient_ids:
            continue
        seen_patient_ids.add(patient_id)
        patients.append(resource)
    conditions = [resource for resource in resources if resource.get("resourceType") == "Condition"]
    observations = [
        resource for resource in resources if resource.get("resourceType") == "Observation"
    ]

    candidates: list[DeidentifiedCandidate] = []
    for index, patient in enumerate(patients):
        patient_id = patient.get("id")
        pseudonym = f"CAND-{index + 1:04d}"

        birth_date = patient.get("birthDate")
        age: int | Literal["90 or older"] | None = (
            _compute_capped_age(birth_date) if isinstance(birth_date, str) else None
        )

        related_conditions = [
            resource
            for resource in conditions
            if _references_patient(resource, patient_id) and _matches_code(resource, condition_code)
        ]
        related_observations = [
            resource
            for resource in observations
            if _references_patient(resource, patient_id)
            and (observation_loinc is None or _matches_code(resource, observation_loinc))
        ]

        identifier_strings = _collect_identifier_strings(patient)
        for related_resource in related_conditions + related_observations:
            identifier_strings.extend(_collect_identifier_strings(related_resource))

        condition_values = _strip_and_redact(related_conditions, identifier_strings)
        observation_values = _strip_and_redact(related_observations, identifier_strings)

        candidates.append(
            DeidentifiedCandidate(
                patient_pseudonym=pseudonym,
                age=age,
                condition_values=condition_values,
                observation_values=observation_values,
            )
        )
    return candidates
