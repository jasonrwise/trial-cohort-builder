"""The one wall-clock read the seeder path uses (D-02).

A criteria snapshot records when it was captured, but that timestamp is metadata
only and never feeds the content hash. The seeder CLI (an adapter) calls
`utc_now_iso` and passes the string down; nothing under `src/seeder` reads the
clock itself, so the determinism guard (AD-33) holds with no exemption.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now_iso() -> str:
    """Current UTC time as ISO 8601 with seconds precision, for example
    `2026-10-02T15:50:43+00:00`."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()
