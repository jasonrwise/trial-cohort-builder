"""NLM Clinical Table Search Service client and the deterministic
accept/reject verification rule (tasks.md T021).

A fresh `httpx.AsyncClient` is constructed inside `verify_criterion` and never
held at module scope — nothing here is cached, memoised, or reused across
calls. `TERMINOLOGY_MATCH_THRESHOLD` and `TERMINOLOGY_CONCURRENCY_LIMIT` are
the two legitimate module-level constants (a bare float and a bare int, not a
dict/list, so neither trips `tests/unit/test_no_caching_layer.py`'s AST
guard); the qualifier/unit-word tuples below are `tuple[str, ...]` for the
same reason. `TERMINOLOGY_MATCH_THRESHOLD` is not configurable, not read from
the environment, and not passed as a defaultable parameter — lowering it to
reduce the UNMAPPED count is exactly what PRD SM-2/SM-C1 forbids.

`verify_criterion` never raises to its caller. A transport error or non-200 status
that persists through all 4 attempts is the distinct `TerminologyFetchError.UNAVAILABLE`
outcome (AD-34, amending AD-12 for the outage case): the terminology service was not
reached, so nothing is known about the criterion and it must never be presented as
`UNMAPPED`. A zero-result search, a below-threshold ratio, and a response that fails
strict parsing (a malformed 200 — AD-12's accepted gap) remain legitimate `UNMAPPED`
outcomes, checked in that order before any indexing happens (02-RESEARCH.md Pitfall 4).
The retry helper is shared by this module's two lookups (`verify_criterion`,
`verify_code`) only. `mapping_status=VERIFIED` is set only from a real HTTP response,
through two selection paths:

- the top-ranked similarity hit that clears `TERMINOLOGY_MATCH_THRESHOLD`, or
- a reviewed `lab_code_table` row whose code `verify_code` confirms on this call (AD-38).

The table holds reviewed LOINC observation rows only. No fallback code and no default
exists anywhere in this module.
"""

from __future__ import annotations

import asyncio
import re
from enum import Enum
from itertools import pairwise

import httpx
from rapidfuzz import fuzz

from src.models.trial import (
    CodeSystem,
    CriterionKind,
    EligibilityCriterion,
    MappingStatus,
    Operator,
)
from src.services import lab_code_table

# Locked threshold (research.md §4, D-08) — a bare module-level float, never
# configurable and never read from the environment.
TERMINOLOGY_MATCH_THRESHOLD = 0.80

# Bounds the concurrent fan-out `get_protocol_criteria` issues against NLM's
# free public endpoint — an unbounded `asyncio.gather` over a compound-heavy
# trial (NCT01779336 runs 15+ criteria) would fire that many simultaneous
# requests with no cap, a real throttling risk (per /plan-eng-review,
# 2026-09-13). A bare module-level int constant, not environment-read.
TERMINOLOGY_CONCURRENCY_LIMIT = 8

_TERMINOLOGY_SEMAPHORE = asyncio.Semaphore(TERMINOLOGY_CONCURRENCY_LIMIT)

_TERMINOLOGY_BASE_URL = "https://clinicaltables.nlm.nih.gov/api"
_REQUEST_TIMEOUT = httpx.Timeout(10.0)

# DEGR-05/AD-12: exactly 4 total attempts (1 initial + 3 retries) on a
# transport error (httpx.HTTPError) or a non-200 status, waiting 1s then 2s
# then 4s between successive attempts — mirrors AD-10's shape (trials.py's
# fetch_trial), implemented separately here per the per-adapter decision; the
# one private helper `_get_with_retry` below is shared by this module's two
# lookups only. A zero-result search or a below-threshold ratio is a real
# HTTP 200 verdict and is never retried; a malformed-200 response is likewise
# not retried (AD-12's accepted residual gap — not a transient condition a
# retry would fix). A tuple, not a list — immutable, so it does not trip
# tests/unit/test_no_caching_layer.py's module-level-mutable-binding guard.
_TERMINOLOGY_RETRY_MAX_ATTEMPTS = 4
_TERMINOLOGY_RETRY_BACKOFF_SECONDS = (1, 2, 4)

# AD-34: the one reason every adapter's DegradedEnvelope carries when the
# terminology service stayed unreachable through all attempts. A bare str
# constant derived from the attempt count — no exception text, URL or response
# body ever reaches it.
TERMINOLOGY_UNAVAILABLE_REASON = (
    f"terminology service unavailable after {_TERMINOLOGY_RETRY_MAX_ATTEMPTS} attempts."
)


class TerminologyFetchError(str, Enum):
    """AD-34: the typed outcome of a terminology lookup whose transport or status
    outage persisted through every retry. Returned as a value, never raised; callers
    test it by identity (`is`), never by truthiness — a `str` Enum member is truthy."""

    UNAVAILABLE = "UNAVAILABLE"


# D-12: numeric-comparison + lab-value phrasing routes to LOINC first;
# everything else routes to the conditions/ICD10CM table first.
_LAB_UNIT_PATTERN = re.compile(
    r"(mg/dl|mmol/l|mmhg|%|/[uµ]l|/mm3|/cmm|g/dl|ng/ml|meq/l|meq/dl)",
    re.IGNORECASE,
)
_LAB_WORD_PATTERN = re.compile(r"\b(level|count)\b", re.IGNORECASE)

# D-07: leading clinical qualifiers stripped before the noun phrase is sent
# as the search query. Claude's Discretion per D-07 — kept pure string/regex
# work, no new dependency. The generic-subject/connector words are stripped
# the same way: they name who the criterion is about or how two clauses
# join, never the clinical concept itself.
_LEADING_QUALIFIERS: tuple[str, ...] = (
    "history of",
    "diagnosis of",
    "known or suspected",
    "evidence of",
    "presence of",
    "patients with",
    "subjects with",
    "patients",
    "subjects",
    "with",
)

_TRAILING_FILLER_WORDS: tuple[str, ...] = ("and", "of", "with", "the", "a", "an")

# Addendum (02-04-PLAN.md, added post-02-02 checkpoint 2026-09-14): a long
# criterion often tacks a temporal/procedural aside onto the end of its real
# clinical concept ("...glucose level >240 mg/dl (>13.3 mmol/L) after an
# overnight fast before randomization"). Neither the leading-qualifier list
# nor the trailing-filler-word list (both of which only touch the very start
# or very end of the string) can remove an aside embedded mid-sentence, so it
# survives into the search query and returns zero live NLM results — not
# merely a low-scoring one. Once threshold/parenthetical noise is stripped,
# truncating the remaining text at the first clause-boundary connector removes
# the aside without touching the clinical noun phrase that precedes it. This
# is scoped narrowly to these four connectors, not a general clause parser.
_CLAUSE_BOUNDARY_CONNECTORS: tuple[str, ...] = ("after", "before", "during", "within")
_CLAUSE_BOUNDARY_PATTERN = re.compile(
    r"\b(?:" + "|".join(_CLAUSE_BOUNDARY_CONNECTORS) + r")\b",
    re.IGNORECASE,
)

# Addendum, part 2: live-verified against the real NLM service (not assumed)
# that clause-boundary truncation alone was insufficient for the glucose
# criterion — "Uncontrolled hyperglycaemia with a glucose level" (the
# post-truncation phrase) still returns zero loinc_items results, because the
# service's multi-word search appears to require every word to partially
# match some indexed field, and "hyperglycaemia" (a diagnosis, not a lab-test
# name) never co-occurs with any loinc_items entry — no spelling fix changes
# this, confirmed live. `_LEADING_QUALIFIERS`/the trailing-filler-word pop
# already discard a *leading* "with"/"Patients with" as pure framing,
# keeping what follows (e.g. "with type 2 diabetes" -> "type 2 diabetes");
# this generalises the exact same policy to a *mid-sentence* "with (a|an)?
# ... level/count" span specifically — the diagnosis label introducing a lab
# value ("<diagnosis> with a <measurement> level") is framing the same way a
# leading qualifier is, and the measurement clause is the real, independently
# LOINC-searchable concept. Scoped narrowly to the "level"/"count" nouns
# `_LAB_WORD_PATTERN` already treats as the lab-value marker (D-12) — it does
# not fire on criteria with no such word (confirmed: none of the other real
# criteria in either demo trial contain a mid-sentence "with" immediately
# introducing a level/count clause, so this cannot regress them).
_MID_SENTENCE_LAB_VALUE_PATTERN = re.compile(
    r"\bwith\s+(?:an?\s+)?([a-zA-Z][a-zA-Z\s]*?\b(?:level|count)s?)\b",
    re.IGNORECASE,
)

# Markdown-escaped operator variants (02-RESEARCH.md Pitfall 1: ClinicalTrials.gov
# sometimes escapes ">" as "\>") are tolerated alongside the unescaped forms.
_PARENTHETICAL_PATTERN = re.compile(r"\([^)]*\)")

# A single digit-run pattern shared by `_THRESHOLD_PATTERN` (noun-phrase
# stripping) and `_OPERATOR_PATTERNS` (threshold parsing) below, so both
# treat a number the same way. Real registry text commonly writes lab-value
# counts with comma thousands separators (e.g. "Platelets ≥ 100,000/cmm",
# NCT01779336) — the comma-grouped alternative is tried first and requires
# at least one full ",ddd" group, so it only fires on an actual
# comma-grouped number; a plain run of digits with no comma (including one
# longer than three digits, e.g. "1234") falls through to the second
# alternative and is matched in full rather than truncated at the first
# three digits.
_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"

# A number is only threshold/measurement noise — safe to strip — when it is
# either preceded by a comparison operator or directly followed by a unit
# word; a bare number with neither (e.g. the "2" in "type 2 diabetes") is
# part of the clinical concept itself and must survive.
_UNIT_ALTERNATION = (
    r"(?:mg/dl|mmol/l|mmhg|%|years?|months?|weeks?|days?|"
    r"/[uµ]l|/mm3|/cmm|g/dl|ng/ml|meq/l|meq/dl)"
)
_THRESHOLD_PATTERN = re.compile(
    rf"(?:\\?[<>≤≥=])+\s*(?:{_NUMBER})\s*{_UNIT_ALTERNATION}?"
    rf"|(?:{_NUMBER})(?:\s*-\s*(?:{_NUMBER}))?\s*{_UNIT_ALTERNATION}\b",
    re.IGNORECASE,
)

# MAP-06: a duration clause ("for at least 6 months") says how long a condition has
# lasted, not what it is. The pattern starts at the word "for", never at a whitespace
# or comma run, so a long run of spaces cannot make it quadratic. The cut removes
# everything after the hit, so "for 4 weeks prior to screening" loses "prior to
# screening" too. That is intended. It must run before `_THRESHOLD_PATTERN`, which
# would remove "6 months" and leave "for at least" behind.
_DURATION_CLAUSE_PATTERN = re.compile(
    rf"\bfor\s+(?:(?:at least|more than|over|a minimum of)\s+)?(?:{_NUMBER})\s*"
    r"(?:months?|years?|weeks?|days?)\b",
    re.IGNORECASE,
)

# MAP-06: words that name who a criterion is about ("Female and male individuals,
# ..."). "female" comes before "male" and "women" before "men" so the longer word is
# tried first.
_SUBJECT_WORDS: tuple[str, ...] = (
    "female",
    "male",
    "women",
    "men",
    "adults",
    "individuals",
    "participants",
    "patients",
    "subjects",
    "volunteers",
    "and",
    "or",
)

# A subject preamble: one or more subject words, then an optional "with" (and "a"),
# an optional "clinical/confirmed/documented/established", and an optional
# "diagnosis of" or "diagnosed with". Anchored at the start; each part ends at a word
# boundary and then spaces or commas, so the match is linear. `extract_noun_phrase`
# strips the match only when it ends in "with" or "of". The pattern is greedy and
# never retries a shorter match: "Subjects with a " is matched, then rejected because
# it ends in "a", and the line keeps its words. That is intended. A shorter strip would
# move "Subjects with a history of ...".
_SUBJECT_PREAMBLE_PATTERN = re.compile(
    r"(?:(?:" + "|".join(_SUBJECT_WORDS) + r")\b[\s,]*)+"
    r"(?:with\b[\s,]*(?:a\b[\s,]*)?)?"
    r"(?:(?:clinical|confirmed|documented|established)\b[\s,]*)?"
    r"(?:(?:diagnosis of|diagnosed with)\b[\s,]*)?",
    re.IGNORECASE,
)

# MAP-06: abbreviations that registry text uses for a diagnosis. A rewrite changes the
# search phrase only; the code still comes from the terminology response (FR-016).
# Applied in order to the finished phrase. Plain "type 2 diabetes" is not on this list.
_DIAGNOSIS_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:dm2|t2dm|niddm)\b", re.IGNORECASE), "type 2 diabetes mellitus"),
)


def route_table(raw_text: str) -> str:
    """D-12: returns `"loinc_items"` when the criterion carries lab-value
    phrasing (a unit such as mg/dL, a percent sign, a per-microlitre unit, or
    a word such as "level"/"count"), and `"conditions"` otherwise. One table
    per criterion — the common case is one terminology call per criterion."""
    if _LAB_UNIT_PATTERN.search(raw_text) or _LAB_WORD_PATTERN.search(raw_text):
        return "loinc_items"
    return "conditions"


def _cf_for_table(table: str) -> str:
    """The NLM Clinical Table Search Service `cf` (code field) parameter for
    `table` — shared by `verify_criterion` and `verify_code` so the
    table->cf mapping has one source of truth."""
    return "icd10cm_codes" if table == "conditions" else "LOINC_NUM"


def extract_noun_phrase(raw_text: str) -> str:
    """D-07, MAP-06: returns the clinical noun phrase that `verify_criterion` sends
    to the terminology service.

    Steps, in order: cut a trailing duration clause ("for at least 6 months") and
    everything after it; strip a subject preamble ("Female and male individuals,
    with clinical diagnosis of") only when the preamble ends in "with" or "of";
    strip a leading clinical qualifier or generic-subject prefix; remove
    parenthetical asides and embedded comparison-operator/threshold/unit phrases
    (including the markdown-escaped operator forms); cut at a clause-boundary
    connector; drop filler words at both ends; then rewrite the diagnosis
    abbreviations DM2, T2DM and NIDDM to "type 2 diabetes mellitus". A rewrite
    changes the search phrase only. The code still comes from the terminology
    response (FR-016). A bare number with no adjacent operator or unit (e.g. the
    "2" in "type 2 diabetes") is part of the clinical concept, not a threshold,
    and is left untouched."""
    text = raw_text.strip()

    duration = _DURATION_CLAUSE_PATTERN.search(text)
    if duration:
        text = text[: duration.start()].rstrip(" ,")

    preamble = _SUBJECT_PREAMBLE_PATTERN.match(text)
    if preamble and preamble.group(0).rstrip(" ,").lower().endswith(("with", "of")):
        text = text[preamble.end() :]

    # Leading qualifiers/subject words may stack ("Patients with type 2
    # diabetes" -> strip "Patients" then "with"), so loop until none match.
    while True:
        lowered = text.lower()
        for qualifier in _LEADING_QUALIFIERS:
            if lowered.startswith(qualifier):
                text = text[len(qualifier) :].strip()
                break
        else:
            break

    text = _PARENTHETICAL_PATTERN.sub(" ", text)
    text = _THRESHOLD_PATTERN.sub(" ", text)

    # Addendum, part 2: when a mid-sentence "with (a|an)? ... level/count"
    # span exists and it is not already at the very start of the text (a
    # leading occurrence is already handled by the qualifier loop above and
    # the filler-word pop below), isolate that span as the noun phrase,
    # discarding the diagnosis-label framing before it.
    lab_value_match = _MID_SENTENCE_LAB_VALUE_PATTERN.search(text)
    if lab_value_match and lab_value_match.start() > 0:
        text = lab_value_match.group(1)

    # Addendum, part 1: truncate at the first temporal/procedural clause-
    # boundary connector, once threshold/parenthetical noise is already
    # gone, so a trailing aside ("after an overnight fast before
    # randomization") never reaches the search query.
    boundary = _CLAUSE_BOUNDARY_PATTERN.search(text)
    if boundary:
        text = text[: boundary.start()]

    text = re.sub(r"\s+", " ", text).strip()

    words = text.split(" ")
    while words and words[-1].lower() in _TRAILING_FILLER_WORDS:
        words.pop()
    while words and words[0].lower() in _TRAILING_FILLER_WORDS:
        words.pop(0)
    phrase = " ".join(words)
    for pattern, replacement in _DIAGNOSIS_REWRITES:
        phrase = pattern.sub(replacement, phrase)
    return phrase


# D-10: comparison-phrasing regex table (Claude's Discretion for the exact
# covered set). Held as a module-level `tuple`, never a `dict`/`list`, so it
# does not trip the no-caching AST guard. Covers, in both symbol and
# spelled-out form: at-least (>=, ≥, "at least", "no less than", "N years/yrs
# and/or older/above"), at-most (<=, =<, ≤, "at most", "no more than", "no
# greater than", "N years/yrs and/or younger/below"), greater-than (>,
# "greater than", "more than"), and less-than (<, "less than", "fewer than").
# Two inclusive wordings come first (MAP-07): "equal to or above/greater than/
# higher than/more than N" is at-least, and "equal to or below/less than/lower
# than/fewer than N" is at-most. Plain "above" and plain "below" are not covered.
# A number is only captured as a threshold when it is directly adjacent to
# one of these forms; a bare number elsewhere in the text is not touched.
# `_NUMBER` (comma-grouped-thousands aware) is defined once, above, and
# shared with `_THRESHOLD_PATTERN`.

# The two age rows of `_OPERATOR_PATTERNS`, named so `extract_operator_threshold(lab=True)`
# can find and drop them. The regex text is unchanged.
_AGE_AND_OLDER_PATTERN = re.compile(
    rf"({_NUMBER})\s*(?:years?|yrs?)?\s*(?:of age\s*)?(?:and|or)\s+(?:older|above)",
    re.IGNORECASE,
)
_AGE_AND_YOUNGER_PATTERN = re.compile(
    rf"({_NUMBER})\s*(?:years?|yrs?)?\s*(?:and|or)\s+(?:younger|below)",
    re.IGNORECASE,
)

# MAP-09: checks of the text right after a matched number, used with
# `.match(raw_text, match.end(1))`. Each tests one position and holds no open-ended gap,
# so each stays linear on long registry text. Spaces are followed by a letter, never by
# more spaces, so a long run of spaces cannot cause backtracking.
_AGE_UNIT_AFTER_PATTERN = re.compile(r"\s*(?:years?|yrs?|y/o|of\s+age)\b", re.IGNORECASE)
_PERCENTILE_AFTER_PATTERN = re.compile(r"\s*(?:(?:st|nd|rd|th)\s*)?percentile", re.IGNORECASE)
_DURATION_UNIT_AFTER_PATTERN = re.compile(r"\s*(?:months?|weeks?|days?)\b", re.IGNORECASE)

_OPERATOR_PATTERNS: tuple[tuple[re.Pattern[str], Operator], ...] = (
    # MAP-07: "equal to or above/greater than/higher than/more than N" is inclusive.
    # These two rows come first so the overlap rule keeps the whole phrase, and the
    # plain "greater than" and "less than" rows below cannot claim the number inside it.
    (
        re.compile(
            rf"equal\s+to\s+or\s+(?:above|greater\s+than|higher\s+than|more\s+than)\s*({_NUMBER})",
            re.IGNORECASE,
        ),
        Operator.GTE,
    ),
    (
        re.compile(
            rf"equal\s+to\s+or\s+(?:below|less\s+than|lower\s+than|fewer\s+than)\s*({_NUMBER})",
            re.IGNORECASE,
        ),
        Operator.LTE,
    ),
    (
        re.compile(rf"(?:>=|≥|at least|no less than)\s*({_NUMBER})", re.IGNORECASE),
        Operator.GTE,
    ),
    (_AGE_AND_OLDER_PATTERN, Operator.GTE),
    (
        re.compile(
            rf"(?:<=|=<|≤|at most|no more than|no greater than)\s*({_NUMBER})",
            re.IGNORECASE,
        ),
        Operator.LTE,
    ),
    (_AGE_AND_YOUNGER_PATTERN, Operator.LTE),
    (re.compile(rf"(?:>|greater than|more than)\s*({_NUMBER})", re.IGNORECASE), Operator.GT),
    (re.compile(rf"(?:<|less than|fewer than)\s*({_NUMBER})", re.IGNORECASE), Operator.LT),
)

# D-11: phrases naming a diagnosis/history/positive-test claim rather than a
# numeric comparison. When no comparison pattern above matches but one of
# these appears, the criterion is a presence-or-absence claim (Operator.
# PRESENT), distinct from "nothing recognisable at all" (None).
_PRESENCE_MARKERS: tuple[str, ...] = (
    "history of",
    "diagnosis of",
    "known or suspected",
    "known history of",
    "documented history of",
    "evidence of",
    "presence of",
    "positive for",
    "seropositivity to",
)


# MAP-08 / FR-014: a two-sided range keeps neither bound. The between form is the
# word "between", a number, at most 20 characters with no digit, "and", "to" or
# "-", then a second number. The chained form is "7.0% <= HbA1c < 10.0%": a number,
# an optional "%" or short unit word, one comparison operator, a gap of 1 to 60
# characters that holds no comparison character, a second operator of the same
# direction, then a number. The gap bound and the lookbehind (the leading number
# cannot start inside a digit run or inside a word such as "HbA1c") keep both
# patterns linear on long registry text. The alternations are flat: one for each direction.
_RANGE_BETWEEN_PATTERN = re.compile(
    rf"\bbetween\s+(?:{_NUMBER})\D{{0,20}}?(?:and|to|-)\s*(?:{_NUMBER})",
    re.IGNORECASE,
)
_RANGE_CHAINED_PATTERN = re.compile(
    rf"(?<![\w.,])(?:{_NUMBER})\s*(?:%|[a-z]{{1,6}})?\s*"
    rf"(?:(?:<=|=<|<|≤)[^<>=≤≥]{{1,60}}(?:<=|=<|<|≤)"
    rf"|(?:>=|=>|>|≥)[^<>=≤≥]{{1,60}}(?:>=|=>|>|≥))"
    rf"\s*(?:{_NUMBER})",
    re.IGNORECASE,
)
_LOWER_BOUND_OPERATORS: tuple[Operator, ...] = (Operator.GT, Operator.GTE)
_UPPER_BOUND_OPERATORS: tuple[Operator, ...] = (Operator.LT, Operator.LTE)

# 17-REVIEW WR-03: two comparisons in opposite directions name one analyte's two bounds
# only when units, numbers, parenthetical asides and a joiner word sit between them.
# `_RANGE_PAIR_MAX_GAP` is the longest gap checked, the same bound as the chained range
# gap. `_RANGE_JOINER_PATTERN` is one flat alternation, removed with a single `.sub` pass.
_RANGE_PAIR_MAX_GAP = 60
_RANGE_JOINER_PATTERN = re.compile(
    rf"{_UNIT_ALTERNATION}|mmol/mol|kg/m2|\b(?:and|or|to)\b",
    re.IGNORECASE,
)


def _has_range_form(raw_text: str) -> bool:
    """True when the text holds a between form or a chained form of a range."""
    return bool(_RANGE_BETWEEN_PATTERN.search(raw_text) or _RANGE_CHAINED_PATTERN.search(raw_text))


def _comparison_matches(raw_text: str) -> list[tuple[re.Match[str], Operator]]:
    """The non-overlapping comparison matches in the text, in `_OPERATOR_PATTERNS` order.
    An earlier (higher-priority) pattern keeps a span that a later pattern also matches."""
    matches: list[tuple[re.Match[str], Operator]] = []
    consumed_spans: list[tuple[int, int]] = []

    for pattern, operator in _OPERATOR_PATTERNS:
        for match in pattern.finditer(raw_text):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in consumed_spans):
                continue  # overlaps a match already claimed by an earlier (higher-priority) pattern
            matches.append((match, operator))
            consumed_spans.append(span)

    return matches


def is_threshold_range(raw_text: str) -> bool:
    """MAP-08: True when the text names a two-sided range: a between form, a chained
    form, or two comparisons that point in opposite directions ("HbA1c >= 7% and
    <= 10%"). The seeder uses this to say "two-sided range" in its refusal. It never
    changes `mapping_status` and it never supplies a threshold: a range keeps no
    bound. Two comparisons in opposite directions name a range only when units,
    numbers, parenthetical asides and "and", "or" or "to" are all that sit between
    them. When another word sits between them (another analyte), the line still gives
    no threshold, and the seeder gives the generic "is not numeric" reason (17-REVIEW
    WR-03)."""
    if _has_range_form(raw_text):
        return True
    matches = sorted(_comparison_matches(raw_text), key=lambda item: item[0].start())
    for (first, first_op), (second, second_op) in pairwise(matches):
        opposite = (first_op in _LOWER_BOUND_OPERATORS and second_op in _UPPER_BOUND_OPERATORS) or (
            first_op in _UPPER_BOUND_OPERATORS and second_op in _LOWER_BOUND_OPERATORS
        )
        if not opposite:
            continue
        between = raw_text[first.end() : second.start()]
        if len(between) > _RANGE_PAIR_MAX_GAP:
            continue
        between = _PARENTHETICAL_PATTERN.sub(" ", between)
        between = _RANGE_JOINER_PATTERN.sub(" ", between)
        if not re.search(r"[^\W\d_]", between):
            return True
    return False


def _is_age_match(raw_text: str, match: re.Match[str]) -> bool:
    """True when the comparison match is an age: one of the two age rows, or a number
    followed by an age unit ("> 18 years")."""
    return (
        match.re is _AGE_AND_OLDER_PATTERN
        or match.re is _AGE_AND_YOUNGER_PATTERN
        or _AGE_UNIT_AFTER_PATTERN.match(raw_text, match.end(1)) is not None
    )


def _kept_comparisons(
    raw_text: str, *, lab: bool, diagnosis: bool
) -> list[tuple[re.Match[str], Operator]] | None:
    """The comparison matches that can be a threshold. Returns None when the line is
    refused: a range form, or a number followed by "percentile". With `lab` or
    `diagnosis`, a match that is an age ("> 18 years") or a duration ("at least 3
    months") is dropped. `extract_operator_threshold` and `_table_hit_threshold` share
    this one step (17-REVIEW CR-01)."""
    if _has_range_form(raw_text):
        return None

    matches = _comparison_matches(raw_text)

    if any(_PERCENTILE_AFTER_PATTERN.match(raw_text, match.end(1)) for match, _ in matches):
        return None

    if lab or diagnosis:
        matches = [
            (match, op)
            for match, op in matches
            if not _is_age_match(raw_text, match)
            and _DURATION_UNIT_AFTER_PATTERN.match(raw_text, match.end(1)) is None
        ]
    return matches


def extract_operator_threshold(
    raw_text: str, *, lab: bool = False, diagnosis: bool = False
) -> tuple[Operator | None, float | str | None]:
    """D-10/D-11: parses a comparison operator and numeric threshold out of a
    criterion's raw text, without ever touching `mapping_status` — this
    function is called only on `verify_criterion`'s accepting branch, and its
    result never changes whether a criterion is VERIFIED or UNMAPPED.

    Covered phrasings (see `_OPERATOR_PATTERNS`): the symbol and spelled-out
    forms of at-least, at-most, greater-than and less-than. Declines (returns
    `(None, None)`) in three cases: no pattern matches confidently, a between
    or chained range form is present ("HbA1c between 7.0% and 10%", "7.0% <=
    HbA1c < 10.0%"), or more than one non-overlapping comparison matches in the
    same text — a single bullet naming two bounds (e.g. "HbA1c of >= 7.0% and
    =< 10%") is a range, and picking one bound arbitrarily would silently
    narrow it into a different clinical claim; returning null is the honest
    answer (D-11 makes it a legal VERIFIED shape).

    When no numeric comparison is found at all, a diagnosis/history/positive-
    test phrase (`_PRESENCE_MARKERS`) maps to `Operator.PRESENT` with a null
    threshold — a presence-or-absence claim, not "nothing recognised". Every
    enum value returned here is constructed explicitly (`Operator(...)` /
    `Operator.X`), never coerced from a plain string, per 02-RESEARCH.md
    Pitfall 3.

    A number followed by "percentile" is not a threshold (MAP-09, FR-015): when any
    comparison names a percentile, the whole line returns `(None, None)`, so no bound
    is left over from an either-or line. With `lab=True`, a comparison that is an age
    ("50 years or older", "> 18 years") is dropped before the checks above, so an age
    number never becomes a lab threshold. A number followed by a duration unit ("at
    least 3 months") is dropped too, so a duration never becomes a lab threshold
    (17-REVIEW CR-01). The default is unchanged: with no keyword, an age criterion
    ("Patients aged >=18 years") keeps its parse. With `diagnosis=True`, the same age
    and duration rules apply. Scoring maps a matched condition to `True`, so a numeric
    comparison on a diagnosis can only fail every candidate; the presence marker or
    null remains.

    This function does not re-normalise markdown escape sequences (`\\>` for
    `>`) — plan 02-03's segmentation is expected to normalise those upstream,
    so the text reaching this function is already clean.
    """
    matches = _kept_comparisons(raw_text, lab=lab, diagnosis=diagnosis)
    if matches is None:
        return (None, None)

    if len(matches) > 1:
        return (None, None)

    if len(matches) == 1:
        match, operator = matches[0]
        try:
            threshold = float(match.group(1).replace(",", ""))
        except (TypeError, ValueError, IndexError):
            return (None, None)
        return (operator, threshold)

    lowered = raw_text.lower()
    if any(marker in lowered for marker in _PRESENCE_MARKERS):
        return (Operator.PRESENT, None)

    return (None, None)


# 17-REVIEW CR-02: words that name another analyte. A table hit counts a comparison only
# when none of these sits between the matched phrase and the operator. The list comes
# from three sources: the lab analytes of the two recorded trials (NCT01370005 and
# NCT01779336), the analytes of `src/seeder/units.py`, and the analytes in the review
# examples. It is not complete (see the limit stated in `verify_criterion`).
_OTHER_ANALYTE_WORDS: tuple[str, ...] = (
    # Recorded trials.
    "glucose",
    "blood pressure",
    "systolic",
    "diastolic",
    "neutrophil",
    "neutrophils",
    "platelet",
    "platelets",
    "bilirubin",
    "AST",
    "ALT",
    "creatinine",
    # Seeder unit table.
    "cholesterol",
    "HDL",
    "body mass index",
    "BMI",
    # Review examples.
    "eGFR",
)

# 17-REVIEW CR-02: a mark or word that ends the clause of the matched phrase. One flat
# alternation: a semicolon, a comma or a line break; a period followed by whitespace or
# the end of the text; the whole words "and" and "or"; or a whole analyte word. A space
# inside an analyte word matches one or more whitespace characters. No entry nests an
# open-ended quantifier, so a search stays linear on long registry text.
_PHRASE_GAP_BREAK_PATTERN = re.compile(
    r"[;,\r\n]|\.(?:\s|$)|\b(?:and|or)\b|\b(?:"
    + "|".join(re.escape(word).replace(r"\ ", r"\s+") for word in _OTHER_ANALYTE_WORDS)
    + r")\b",
    re.IGNORECASE,
)


def _table_hit_threshold(
    raw_text: str, hit: re.Match[str]
) -> tuple[Operator | None, float | str | None]:
    """17-REVIEW CR-01 and CR-02, MAP-09: the operator and threshold of a table hit.
    `hit` is the match of the matched row's own phrase pattern in `raw_text`.

    A range form anywhere in the line gives no threshold. Otherwise the text from the
    phrase onward is parsed as a lab line, which drops age and duration numbers. A
    numeric comparison counts only when `_PHRASE_GAP_BREAK_PATTERN` finds no break
    between the end of the phrase and the start of the comparison. A presence marker
    or a null result is returned as parsed."""
    if _has_range_form(raw_text):
        return (None, None)

    tail = raw_text[hit.start() :]
    operator, threshold = extract_operator_threshold(tail, lab=True)
    if operator not in _LOWER_BOUND_OPERATORS + _UPPER_BOUND_OPERATORS:
        return (operator, threshold)

    kept = _kept_comparisons(tail, lab=True, diagnosis=False)
    if kept is None or len(kept) != 1:
        return (None, None)

    comparison = kept[0][0]
    if _PHRASE_GAP_BREAK_PATTERN.search(raw_text[hit.end() : hit.start() + comparison.start()]):
        return (None, None)
    return (operator, threshold)


def _unmapped(raw_text: str, kind: CriterionKind) -> EligibilityCriterion:
    return EligibilityCriterion(
        raw_text=raw_text,
        kind=kind,
        mapping_status=MappingStatus.UNMAPPED,
        code_system=None,
        code=None,
        operator=None,
        threshold=None,
    )


async def _get_with_retry(
    client: httpx.AsyncClient, url: str, params: dict[str, str]
) -> httpx.Response | None:
    """The shared retry loop (AD-12/AD-34): up to `_TERMINOLOGY_RETRY_MAX_ATTEMPTS`
    attempts, where an `httpx.HTTPError` or a non-200 status is a failed attempt;
    sleeps `_TERMINOLOGY_RETRY_BACKOFF_SECONDS[attempt]` between failed attempts
    (never after the last). Returns the first 200 response, or `None` when every
    attempt failed. Called inside the caller's semaphore acquisition."""
    for attempt in range(_TERMINOLOGY_RETRY_MAX_ATTEMPTS):
        try:
            response = await client.get(url, params=params)
        except httpx.HTTPError:
            response = None
        if response is not None and response.status_code == 200:
            return response
        if attempt < _TERMINOLOGY_RETRY_MAX_ATTEMPTS - 1:
            await asyncio.sleep(_TERMINOLOGY_RETRY_BACKOFF_SECONDS[attempt])
    return None


async def verify_criterion(
    raw_text: str, kind: CriterionKind
) -> EligibilityCriterion | TerminologyFetchError:
    """The only function that can set `mapping_status=VERIFIED`, through two selection
    paths (AD-38). First, `lab_code_table.match_entry` checks the raw text against the
    reviewed lab-code table. On a hit, `verify_code` confirms the row's code on this
    call: `True` gives VERIFIED with the row's code, `False` gives UNMAPPED, and
    `TerminologyFetchError.UNAVAILABLE` is returned as is. On a miss, this function
    extracts the noun phrase (D-07), routes the table (D-12), performs one GET against
    that table with the search text passed as an httpx `params` mapping
    (never concatenated into the URL), takes only the top-ranked result
    (D-09), and compares the noun phrase against that result's display name
    (D-08). On the accepting branch only, `extract_operator_threshold` (D-10)
    parses `operator`/`threshold` from `raw_text` — both are independent of
    whether the concept itself verified (D-11); the reject branch always
    keeps them `None`, which is the only combination the strict model allows
    for an UNMAPPED criterion. A transport error or non-200 status through all
    4 attempts returns `TerminologyFetchError.UNAVAILABLE` instead (AD-34)."""
    # AD-38: a reviewed lab-code table row is checked first. This branch sits outside
    # the semaphore block below, because `verify_code` takes the same semaphore.
    row = lab_code_table.match_entry(raw_text)
    if row is not None:
        table_code = row[2]
        verified = await verify_code(table_code, CodeSystem.LOINC)
        if verified is TerminologyFetchError.UNAVAILABLE:
            return verified
        if verified is not True:
            return _unmapped(raw_text, kind)
        # 16-REVIEW CR-01 and 17-REVIEW CR-01 and CR-02: the threshold belongs to the one
        # comparison at or after the matched phrase. A range form anywhere in the line
        # gives no threshold, because the chained form's first bound sits before the
        # phrase. A duration or age number is dropped. The comparison counts only when no
        # "and", "or", semicolon, comma, sentence break or listed analyte word sits
        # between the phrase and the operator. A comparison written before the phrase,
        # two comparisons after it, or a comparison behind such a break give no
        # threshold. Known limit: an analyte that `_OTHER_ANALYTE_WORDS` does not name,
        # written after the phrase with none of those marks between, is not detected.
        hit = re.search(row[1], raw_text, re.IGNORECASE)
        operator, threshold = (None, None) if hit is None else _table_hit_threshold(raw_text, hit)
        return EligibilityCriterion(
            raw_text=raw_text,
            kind=kind,
            mapping_status=MappingStatus.VERIFIED,
            code_system=CodeSystem.LOINC,
            code=table_code,
            operator=operator,
            threshold=threshold,
        )

    noun_phrase = extract_noun_phrase(raw_text)
    # 17-REVIEW WR-02: an empty phrase cannot match a terminology concept, so no
    # request is sent and no retry budget is spent.
    if not noun_phrase:
        return _unmapped(raw_text, kind)
    table = route_table(raw_text)
    code_system = CodeSystem.LOINC if table == "loinc_items" else CodeSystem.ICD10CM

    # Rule 1 bug, found and fixed in plan 02-05's live demo run (not
    # previously caught, since every prior test of this branch went through
    # mock_upstreams' recorded fixtures rather than a live, unmocked call):
    # without an explicit `cf=icd10cm_codes`, the `conditions` table's
    # default `codes_array` position is not reliably ICD-10-CM-shaped —
    # live-verified this session that the same query ("type 2 diabetes")
    # returns real ICD-10-CM codes (`E11.9`) from one call and bare internal
    # `key_id` sequence numbers (`10180`) from another, depending on the
    # service's own default-field selection for the top-ranked record. A
    # `key_id` mislabeled as `code_system: ICD10CM` is exactly the
    # "confidently wrong code" T-02-24 exists to catch and PROJECT.md's
    # Authenticity constraint forbids. `scripts/capture_fixtures.py` already
    # requested `cf=icd10cm_codes&df=primary_name` for `conditions` (and
    # `cf=LOINC_NUM` for `loinc_items`, whose default `codes_array` is
    # already `LOINC_NUM`-shaped, live-verified) when it recorded every
    # fixture this test suite runs against — this runtime request now
    # matches those same fields exactly, so a recorded fixture genuinely
    # represents what a live call returns instead of silently diverging from
    # it.
    params = {"terms": noun_phrase, "cf": _cf_for_table(table)}
    if table == "conditions":
        params["df"] = "primary_name"

    # DEGR-05/AD-12/AD-34: one httpx.AsyncClient held across the whole retry
    # sequence, run inside the existing _TERMINOLOGY_SEMAPHORE acquisition — never
    # around it, so a struggling NLM service self-throttles the fan-out rather than
    # adding more concurrent load. Exhausting every attempt is UNAVAILABLE.
    async with _TERMINOLOGY_SEMAPHORE, httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        response = await _get_with_retry(
            client, f"{_TERMINOLOGY_BASE_URL}/{table}/v3/search", params
        )
    if response is None:
        return TerminologyFetchError.UNAVAILABLE

    try:
        body = response.json()
    except ValueError:
        return _unmapped(raw_text, kind)

    # Canonical positional shape: [total_count, codes_array, extra_data, display_rows].
    try:
        codes = body[1]
        display_rows = body[3]
    except (IndexError, TypeError, KeyError):
        return _unmapped(raw_text, kind)

    # 02-RESEARCH.md Pitfall 4: check a top result actually exists before
    # indexing anything — a zero-result search is a clean UNMAPPED, not a
    # crash.
    if not codes or not display_rows:
        return _unmapped(raw_text, kind)

    top_code_raw = codes[0]
    top_display_row = display_rows[0]
    top_display = (
        top_display_row[0] if isinstance(top_display_row, list) and top_display_row else None
    )

    if not top_code_raw or not top_display:
        return _unmapped(raw_text, kind)

    # rapidfuzz.fuzz.token_set_ratio returns 0-100 (token-set-aware, order-
    # and-repetition-insensitive), not 0-1 like difflib.SequenceMatcher.ratio()
    # did — normalize by /100.0 before comparing against the locked 0.80
    # TERMINOLOGY_MATCH_THRESHOLD, which itself is unchanged.
    ratio = fuzz.token_set_ratio(noun_phrase.lower(), str(top_display).lower()) / 100.0
    if ratio < TERMINOLOGY_MATCH_THRESHOLD:
        return _unmapped(raw_text, kind)

    # A comma-separated multi-code field takes only the first token.
    code = str(top_code_raw).split(",")[0].strip()

    # D-10/D-11: operator-parse failure and terminology-verification failure
    # are two independent axes; only the latter drives mapping_status. This
    # call happens only on the accepting branch — the reject branch (every
    # `_unmapped(...)` return above) always keeps both fields `None`.
    operator, threshold = extract_operator_threshold(
        raw_text,
        lab=code_system is CodeSystem.LOINC,
        diagnosis=code_system is CodeSystem.ICD10CM,
    )

    return EligibilityCriterion(
        raw_text=raw_text,
        kind=kind,
        mapping_status=MappingStatus.VERIFIED,
        code_system=code_system,
        code=code,
        operator=operator,
        threshold=threshold,
    )


async def verify_code(code: str, code_system: CodeSystem) -> bool | TerminologyFetchError:
    """03-01-PLAN.md Task 1 (research.md #4): `query_patient_cohort`'s own
    re-verification of a caller-supplied `condition_code`/`observation_loinc`
    at call time — no result is cached or trusted from a prior
    `get_protocol_criteria` call, since nothing in this system persists which
    codes a prior call verified (Constitution V, no caching layer anywhere).

    This is an exact code-field membership check against the same NLM table
    `verify_criterion` queries — never the fuzzy `rapidfuzz.fuzz.token_set_ratio`
    display-name comparison that function performs on its accepting branch.
    A `codes_array` entry may itself be a comma-separated multi-code string
    (the same shape `verify_criterion`'s `code.split(",")[0]` already handles
    on the accepting side) — membership is checked against every
    comma-split token, not just the first.

    Never raises. A transport error or non-200 status that persists through all
    4 attempts (the same shared retry as `verify_criterion`, WD-1) returns
    `TerminologyFetchError.UNAVAILABLE` (AD-34) — the service was not reached, so
    the code is neither verified nor rejected, and a caller must test the result by
    identity (`is`), never by truthiness. Anything else that is not a match — a
    response that fails to parse as JSON, a missing or empty codes array, or a
    non-matching result — returns `False`: fail closed, since an unverifiable code
    must never be treated as verified."""
    table = "loinc_items" if code_system is CodeSystem.LOINC else "conditions"
    params = {"terms": code, "cf": _cf_for_table(table)}
    # verify-code-missing-sf-param (.planning/debug/resolved/): without an
    # explicit `sf` (search field), the conditions table's default search
    # fields do not include the code column, so a code-only `terms` query
    # against it can never match — live-verified (curl) that
    # `terms=I10&cf=icd10cm_codes` returns zero results while the identical
    # request plus `sf=icd10cm_codes` returns real matches. Scoped to the
    # conditions table only: loinc_items' default search fields already
    # include the code field (live-verified unaffected), so adding `sf`
    # there is unnecessary and out of this fix's scope.
    if table == "conditions":
        params["sf"] = _cf_for_table(table)

    async with _TERMINOLOGY_SEMAPHORE, httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        response = await _get_with_retry(
            client, f"{_TERMINOLOGY_BASE_URL}/{table}/v3/search", params
        )
    if response is None:
        return TerminologyFetchError.UNAVAILABLE

    try:
        body = response.json()
    except ValueError:
        return False

    try:
        codes = body[1]
    except (IndexError, TypeError, KeyError):
        return False

    if not codes:
        return False

    for raw_code_entry in codes:
        tokens = [token.strip() for token in str(raw_code_entry).split(",")]
        if code in tokens:
            return True

    return False
