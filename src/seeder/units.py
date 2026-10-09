"""The committed LOINC-to-UCUM unit mapping for generated Observations (Spec Kit T036,
FR-023, research R-7).

A small table of the codes the demo and the noise pools use. A LOINC code outside it
gets the generic UCUM unit ``1`` and a ``True`` flag, so the run report can list every
code whose unit is a placeholder instead of silently inventing one.

The table is a tuple of pairs, not a dict, so no module-level mutable container exists
(AD-1, AD-33) and a lookup scans it in a fixed order.
"""

from __future__ import annotations

UCUM_SYSTEM: str = "http://unitsofmeasure.org"

# (LOINC code, UCUM unit): 4548-4 HbA1c, 8480-6 systolic and 8462-4 diastolic blood
# pressure, 2093-3 total cholesterol, 2085-9 HDL cholesterol, 39156-5 body mass index.
LOINC_UNITS: tuple[tuple[str, str], ...] = (
    ("2085-9", "mg/dL"),
    ("2093-3", "mg/dL"),
    ("39156-5", "kg/m2"),
    ("4548-4", "%"),
    ("8462-4", "mm[Hg]"),
    ("8480-6", "mm[Hg]"),
)

# The dimensionless UCUM unit, used (and flagged) when a code has no committed mapping.
FALLBACK_UNIT: str = "1"


def unit_for(loinc_code: str) -> tuple[str, bool]:
    """``(unit, is_fallback)`` for a LOINC code."""
    for code, unit in LOINC_UNITS:
        if code == loinc_code:
            return unit, False
    return FALLBACK_UNIT, True
