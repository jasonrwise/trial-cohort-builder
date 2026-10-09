"""The data revision and the deterministic resource ids (Spec Kit T036, SEED-03,
SEED-06, AD-32; research R-5 and R-6).

This module is the single home of the generator version, the uuid5 namespace, the
default seed, the data revision and the resource id. Nothing else under ``src/``
declares them, so a seeded store and the gateway that reads its tags can never
disagree about what a revision means.

The data revision is ``<GENERATOR_VERSION>.<sha12>``: the 12 hex digits are the start
of the SHA-256 of the canonical (sorted-key, compact) JSON of the snapshot content
hash, the seed and the sorted group sizes. A changed generator version, seed, size or
snapshot therefore yields a different revision, which lets the seeder CLI refuse to mix
data from two different runs. The revision contains no ``|``, ``,``, ``$`` or
whitespace, so it is safe as a ``_tag=system|code`` search token.

Resource ids are uuid5 values of ``<nct>|<group>|<index>|<GENERATOR_VERSION>``, with a
``|<ResourceType>`` suffix for every type except Patient. ``UUID_NAMESPACE`` is a plain
string, and the ``uuid.UUID`` object is built inside ``resource_id``, because a
module-level instance of a stateful type is exactly what the no-caching guard bans.

Changing ``UUID_NAMESPACE`` or the id-name format changes every resource id written into
operators' stores. It therefore requires bumping ``GENERATOR_VERSION`` and a
reset-then-reseed everywhere.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

GENERATOR_VERSION: str = "g1"
# Generated once at authoring time. Never change without bumping GENERATOR_VERSION.
UUID_NAMESPACE: str = "a566df24-4714-44aa-bc58-3aa2cd660b3d"
DEFAULT_SEED: int = 20260930

_PATIENT: str = "Patient"
_REVISION_HEX_DIGITS: int = 12


def data_revision(content_sha256: str, seed: int, group_sizes: Sequence[tuple[str, int]]) -> str:
    """``<GENERATOR_VERSION>.<sha12>`` over the content hash, the seed and the group sizes.

    The sizes are sorted by group name before hashing, so the order a caller passes them in
    never changes the revision.
    """
    payload = json.dumps(
        {
            "content_sha256": content_sha256,
            "group_sizes": [[group, size] for group, size in sorted(group_sizes)],
            "seed": seed,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{GENERATOR_VERSION}.{digest[:_REVISION_HEX_DIGITS]}"


def resource_id(nct_id: str, group: str, index: int, resource_type: str = _PATIENT) -> str:
    """The deterministic uuid5 id of one generated resource."""
    name = f"{nct_id}|{group}|{index}|{GENERATOR_VERSION}"
    if resource_type != _PATIENT:
        name = f"{name}|{resource_type}"
    return str(uuid.uuid5(uuid.UUID(UUID_NAMESPACE), name))
