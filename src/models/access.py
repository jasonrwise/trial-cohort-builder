"""File-backed access-control and audit-trail models.

Field-for-field mirror of specs/001-trial-eligibility-screening/data-model.md lines 77-100.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict


class Role(str, Enum):
    CRC = "CRC"
    PI = "PI"
    SITE_ADMIN = "SITE_ADMIN"


class EventType(str, Enum):
    DISPATCH_ALLOWED = "DISPATCH_ALLOWED"
    DISPATCH_DENIED = "DISPATCH_DENIED"
    ACCESS_DENIED = "ACCESS_DENIED"
    # 04-02-PLAN.md: distinguishes an authorized-but-failed Slack post (PI
    # caller, valid channel_id, chat.postMessage itself fails) from
    # DISPATCH_DENIED's pre-post rejection (research.md #12 AD-4).
    DISPATCH_FAILED = "DISPATCH_FAILED"
    # 06-02-PLAN.md Task 1 (AD-17): written only to the existing *rotating*
    # audit log by Phase 7's approve_finalize rejections (missing/fabricated
    # reference, NCT-ID mismatch, or already-finalized) — never to the new
    # scorecard store. Same attempt/denial family as ACCESS_DENIED/
    # DISPATCH_DENIED above.
    FINALIZE_REJECTED = "FINALIZE_REJECTED"


class AccessCredential(BaseModel):
    """The server-side key -> role mapping, one entry per line of the key file.

    Not a request/response model. Exactly one role per credential (spec Assumptions —
    no multi-role credentials). Re-read from disk on every request by the caller of this
    model — never cached in-process — so a revoked/reassigned key's new mapping applies
    on its very next use (FR-017).
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    key: str
    role: Role


class AuditTrailEntry(BaseModel):
    """Append-only audit record; never updated or deleted by the gateway itself.

    Deliberately carries no `key` field — data-model.md:83 requires the raw credential
    never be logged in full, and the absence of the field makes that structurally
    impossible rather than merely intended.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    timestamp: datetime
    role: Role | None
    nct_id: str | None = None
    event_type: EventType
    detail: str
