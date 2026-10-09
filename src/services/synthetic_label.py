"""The render-time SYNTHETIC label for the reserved demo protocol (AD-28,
Spec Kit T077, DEMO-02).

One constant decides: `vocabulary.RESERVED_DEMO_IDS`. The label is derived at
render time from it and never stored -- no field on a record, scorecard or MCP
output carries the marker -- so a reader cannot mistake the demo protocol for a
real trial on any surface that names its ID. Membership is read through the
module attribute on every call (nothing cached), so the constant stays the
single home.
"""

from __future__ import annotations

from src.models import vocabulary

SYNTHETIC_LABEL = "SYNTHETIC"


def is_synthetic(nct_id: str) -> bool:
    """True exactly when `nct_id` is a reserved demo protocol ID. Never looks
    at a title: a real trial titled "SYNTHETIC ..." stays untagged."""
    return nct_id in vocabulary.RESERVED_DEMO_IDS
