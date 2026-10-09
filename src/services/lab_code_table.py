"""The reviewed lab-code table (AD-38).

`LAB_CODE_TABLE` maps reviewed lab-test wording to one LOINC observation code. It is
a tuple of tuples of `str`, never a dict, a list or a stateful object, so the AD-1
caching guard passes. No pattern is compiled at import. `match_entry` searches the
text with `re.search` on every call.

Each row has five fields, in this order: `entry_id`, `phrase_pattern`, `loinc_code`,
`clinical_choice` and `reviewer`. The table holds LOINC observation rows only. A
condition, drug or procedure row needs a new AD.

This module trusts no code. `terminology.verify_criterion` confirms the code of every
matched row with `verify_code` on every use, and keeps no result between calls. A row
whose code the terminology service does not return gives `UNMAPPED`.
"""

from __future__ import annotations

import re

LAB_CODE_TABLE: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "hba1c",
        r"\b(?:hba1c|hb[\s-]?a1c|a1c|glyc(?:at|osylat)ed\s+ha?emoglobin)\b",
        "4548-4",
        (
            "The code names the HbA1c measurement: hemoglobin A1c as a percent of total "
            "hemoglobin in blood. It is not the HbA1c panel 112870-1 that the name search "
            "scores highest."
        ),
        "maintainer, AD-38 user decision 2026-10-05",
    ),
)


def match_entry(raw_text: str) -> tuple[str, str, str, str, str] | None:
    """Return the first row whose phrase pattern matches `raw_text`, or `None`.

    The table is read at call time, so a test can replace `LAB_CODE_TABLE`. The
    search is case-insensitive and applies no Unicode normalization."""
    for row in LAB_CODE_TABLE:
        if re.search(row[1], raw_text, re.IGNORECASE):
            return row
    return None
