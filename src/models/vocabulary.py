"""Shared FHIR vocabulary for the TrialBridge gateway and the demo seeder (AD-32).

This module is the single home of every vocabulary literal below. No other module
under ``src/`` may declare them: the single-home guard in
``tests/unit/test_vocabulary.py`` fails if one does, so the seeder and the gateway
can never disagree on a system URI, a tag system, a reserved ID or the payload
directory.

Later modules must import ``DEMO_DATA_DIR`` from here rather than write the
``demo-data`` directory name themselves.

``DEMO_DATA_DIR`` is anchored on this file's own location (AD-28, AD-31) -- never
on the working directory or a container path -- so it resolves to the repository's
``demo-data`` directory no matter where the process is launched from.
"""

from __future__ import annotations

from pathlib import Path

# Code systems used in the FHIR resources the seeder writes and the gateway reads.
ICD10CM_SYSTEM: str = "http://hl7.org/fhir/sid/icd-10-cm"
LOINC_SYSTEM: str = "http://loinc.org"

# Tag systems stamped on seeded resources. Tag codes contain no '|', ',', '$' or whitespace.
PROTOCOL_TAG_SYSTEM: str = "urn:trialbridge:protocol"
GROUP_TAG_SYSTEM: str = "urn:trialbridge:group"
DATA_REVISION_TAG_SYSTEM: str = "urn:trialbridge:data-revision"

# Patient identity constants the seeder writes: the MRN identifier system (WD-3) and the explicit
# synthetic marker, the HL7 v3 ActReason code HTEST ("test health data") in meta.security (research A8).
MRN_IDENTIFIER_SYSTEM: str = "urn:trialbridge:mrn"
SYNTHETIC_MARKER_SYSTEM: str = "http://terminology.hl7.org/CodeSystem/v3-ActReason"
SYNTHETIC_MARKER_CODE: str = "HTEST"

# Reserved demo trial IDs: the seeder owns these; the live registry path never serves them.
RESERVED_DEMO_IDS: frozenset[str] = frozenset({"NCT99999999"})

# parents[0] is src/models, parents[1] is src, parents[2] is the repository root.
DEMO_DATA_DIR: Path = Path(__file__).resolve().parents[2] / "demo-data"
