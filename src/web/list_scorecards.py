"""`GET /web/scorecards` (08-01-PLAN.md Task 1, tasks.md T007,
contracts/list_scorecards.json, FR-001; 08-02-PLAN.md Task 1 adds `state`/`q`
filtering).

Gated centrally in `src/web/app.py` behind `require_web_endpoint(
"list_scorecards")` (AD-18) -- no per-role or per-session filter inside this
handler. Every CRC/PI session sees every web-app `ScreenedRecord`, regardless
of who screened it. Each row's `state`/`finalized_by_role`/`finalized_at` is
derived fresh via `audit.get_finalization` (AD-17) -- the store is re-read on
every request, never cached (Constitution Principle V).

Filtering is server-side, in Python, over a fresh read every request -- never
a cache or index (research.md Sec 3). `state` narrows to SCREENED rows
("awaiting_approval" -- "Awaiting approval" always means SCREENED, never a
separate stored status) or FINALIZED rows ("finalized"), or keeps every row
("all", the default). `q` is a free-text, case-insensitive substring match
against `nct_id`/`trial_title`; `state` and `q` combine with AND.

A corrupt store (`audit.ScorecardStoreCorruptError`) or an unreadable store
file (`OSError`) never leaks the store path or the corrupt line number to the
browser -- the exception detail is printed to stderr only (08-02-PLAN.md
Task 2, T-08-12); the response renders the fixed EXPERIENCE.md load-failed
copy at 500. `results_state` (`"rows"`/`"empty"`/`"search-miss"`/
`"filter-empty"`/`"load-failed"`) tells the template which of EXPERIENCE.md's
list states to render.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import Response

from src.services import audit
from src.web import rendering

router = APIRouter(prefix="/web")

StateFilter = Literal["all", "awaiting_approval", "finalized"]


@dataclass(frozen=True)
class ScorecardListRow:
    ref: str
    nct_id: str
    trial_title: str
    screened_at: datetime
    screened_by_role: str
    state: str
    finalized_by_role: str | None
    finalized_at: datetime | None
    data_revision: str | None


def build_rows(store_path: str) -> list[ScorecardListRow]:
    """Every stored `ScreenedRecord`, newest `screened_at` first. Python's
    `sorted` is stable, so reversing append order before sorting keeps
    equal-`screened_at` rows in later-appended-first order (08-01-PLAN.md
    Task 1, tasks.md T009)."""
    rows = []
    for record in audit.list_screened_records(store_path):
        approval = audit.get_finalization(record.ref, store_path)
        if approval is not None:
            state = "FINALIZED"
            finalized_by_role: str | None = approval.approved_by_role
            finalized_at: datetime | None = approval.approved_at
        else:
            state = "SCREENED"
            finalized_by_role = None
            finalized_at = None
        rows.append(
            ScorecardListRow(
                ref=record.ref,
                nct_id=record.nct_id,
                trial_title=record.trial_title,
                screened_at=record.screened_at,
                screened_by_role=record.screened_by_role.value,
                state=state,
                finalized_by_role=finalized_by_role,
                finalized_at=finalized_at,
                data_revision=record.data_revision,
            )
        )
    # The inner reversed() is NOT redundant with reverse=True below (ruff
    # C414 is a false positive for this stable-sort tie-break): sorted()
    # with reverse=True keeps equal-key elements in their *input* relative
    # order, so reversing append order first is what makes a later-appended
    # row with an equal screened_at sort ahead of an earlier one.
    return sorted(reversed(rows), key=lambda row: row.screened_at, reverse=True)  # noqa: C414


def filter_rows(rows: list[ScorecardListRow], state: StateFilter, q: str) -> list[ScorecardListRow]:
    """Server-side state/search filter (08-02-PLAN.md Task 1, research.md
    Sec 3) over an already-sorted row list -- input order (newest-first, per
    `build_rows`) is preserved. `state` keeps only SCREENED rows for
    "awaiting_approval", only FINALIZED rows for "finalized", or every row
    for "all". `q` is stripped and case-folded; an empty needle matches
    every row, otherwise a row is kept only when the needle occurs in its
    `nct_id` or `trial_title` (case-insensitive). `state` and `q` combine
    with AND."""
    needle = q.strip().casefold()
    kept = []
    for row in rows:
        if state == "awaiting_approval" and row.state != "SCREENED":
            continue
        if state == "finalized" and row.state != "FINALIZED":
            continue
        if (
            needle
            and needle not in row.nct_id.casefold()
            and needle not in row.trial_title.casefold()
        ):
            continue
        kept.append(row)
    return kept


@router.get("/scorecards")
async def list_scorecards(request: Request, state: StateFilter = "all", q: str = "") -> Response:
    settings = request.app.state.settings
    store_path = settings.scorecard_store_path
    stripped_q = q.strip()

    try:
        all_rows = build_rows(store_path)
    except (audit.ScorecardStoreCorruptError, OSError) as exc:
        print(f"SCORECARD STORE UNREADABLE: {exc}", file=sys.stderr)
        return rendering.render(
            request,
            request.app.state.templates,
            "_scorecards_results.html",
            {
                "role": request.state.role.value,
                "rows": [],
                "state": state,
                "q": stripped_q,
                "results_state": "load-failed",
            },
            full_page_template="scorecards_list.html",
            status_code=500,
        )

    filtered = filter_rows(all_rows, state, q)
    if filtered:
        results_state = "rows"
    elif not all_rows:
        results_state = "empty"
    elif stripped_q:
        results_state = "search-miss"
    else:
        results_state = "filter-empty"

    return rendering.render(
        request,
        request.app.state.templates,
        "_scorecards_results.html",
        {
            "role": request.state.role.value,
            "rows": filtered,
            "state": state,
            "q": stripped_q,
            "results_state": results_state,
        },
        full_page_template="scorecards_list.html",
    )
