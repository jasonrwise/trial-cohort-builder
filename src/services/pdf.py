"""AD-19 scorecard PDF renderer (06-05-PLAN.md Task 3, T013; 09-03).

Renders a stored `ScreenedRecord`'s content directly, never a live
re-query — the caller passes in whatever `audit.get_screened_record`
already returned. Pure function: no file or network I/O beyond `fpdf2`'s
own in-memory rendering and reading the vendored font files. A render
failure raises rather than returning a partial document, mirroring
`append_screened_record`'s "a lost artifact must fail loudly" discipline,
inverted for the read side (a partial PDF handed to a caller is worse than
a 500). The export endpoint, `GET /web/scorecards/{ref}/pdf`
(src/web/export_pdf.py, contracts/export_pdf.json), calls this function;
this module only renders.

Layout, top to bottom: the title, the trial heading with its SCREENED or
FINALIZED badge (and the SYNTHETIC tag after a reserved demo ID, AD-28), the
FINALIZED stamp when an approval is given (built from the
ApprovalRecord's own fields), the ref / screened-by / status-counts line, the data revision line, then, only
when it applies, the partial label and its sentence (VERIFIED exclusions the scorer
cannot evaluate, WEBAPP-12, AD-37) and the coverage warning (every candidate INELIGIBLE
while VERIFIED criteria go unhonored, WEBAPP-11), the Criteria section (headed by the count of VERIFIED criteria the scorer cannot honor, WEBAPP-11;
Inclusion then Exclusion, in stored order, with the view's
labels, codes, operators and thresholds, and the query key marker on the two
rows that drove the cohort query, DEMO-03), the Candidates section, and the
synthetic-data disclaimer on every page's footer. The wording mirrors the
scorecard view; tests/integration/test_web_pdf_export.py compares the two.

Text path: every string is drawn exactly as stored, in the vendored DejaVu
Sans 2.37 (Regular and Bold, in src/services/fonts/, license in
src/services/fonts/LICENSE, bytes pinned in tests/unit/test_web_toolchain.py).
Clinical characters outside Latin-1 (β, α, ≥, →, dashes, curly quotes, …)
therefore reach the document unaltered rather than becoming "?" (research
Pitfall 1). fpdf2 reads the font files at each render; nothing is memoized
(the no-caching guard). Stored text is never passed through fpdf2's markdown
or HTML modes.
"""

from __future__ import annotations

from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from src.models.scorecard import ApprovalRecord, CandidateResult, ScreenedRecord
from src.models.trial import CodeSystem, CriterionKind, EligibilityCriterion, MappingStatus
from src.services import anchor, synthetic_label

# Vendored DejaVu Sans 2.37 (Regular and Bold). The bytes are read by fpdf2 at
# each render; nothing here is memoized (no-caching guard), so the font table
# is a tuple of pairs.
_FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT_FAMILY = "DejaVu"
_FONT_FILES: tuple[tuple[str, str], ...] = (
    ("", "DejaVuSans.ttf"),
    ("B", "DejaVuSans-Bold.ttf"),
)


# Display labels for the stored code-system enum, as the scorecard view prints
# them (a tuple of pairs, not a dict: no module-level mutable state).
_CODE_SYSTEM_LABELS: tuple[tuple[CodeSystem, str], ...] = (
    (CodeSystem.ICD10CM, "ICD-10-CM"),
    (CodeSystem.LOINC, "LOINC"),
)

_UNMAPPED_RATIONALE = "UNMAPPED — could not verify a single ICD-10-CM/LOINC code for this criterion. Requires manual coding."


def _register_fonts(pdf: FPDF) -> None:
    """Registers the vendored Unicode font on `pdf`. Must run before the first
    page is added, because the footer draws with the family."""
    for style, filename in _FONT_FILES:
        pdf.add_font(FONT_FAMILY, style=style, fname=str(_FONT_DIR / filename))


class _ScorecardPDF(FPDF):
    """Prints the Constitution Principle VII synthetic-data disclaimer on
    every page's footer."""

    def footer(self) -> None:
        self.set_y(-15)
        self.set_font(FONT_FAMILY, size=8)
        self.cell(
            0,
            10,
            "All patient records are synthetic data. Not for clinical use.",
            align="C",
        )


def _code_system_label(code_system: CodeSystem) -> str:
    return next(label for system, label in _CODE_SYSTEM_LABELS if system is code_system)


def _write_line(pdf: _ScorecardPDF, text: str, *, height: float = 6) -> None:
    pdf.multi_cell(0, height, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _noun(count: int) -> str:
    return "criterion" if count == 1 else "criteria"


def _unhonored_phrase(count: int) -> str:
    """The count phrase, as the scorecard view's criteria summary prints it
    (WEBAPP-11, WD-10)."""
    return f"{count} VERIFIED {_noun(count)} the scorer cannot honor"


def _coverage_warning_text(count: int) -> str:
    """The coverage warning sentence, as the scorecard view prints it
    (WEBAPP-11, WD-10). It calls the result a possible coverage gap, never a
    clinical result."""
    return (
        f"Coverage warning: every candidate is INELIGIBLE while the scorer cannot honor "
        f"{count} VERIFIED {_noun(count)} outside the query keys. "
        f"Treat this as a possible coverage gap, not a clinical result."
    )


# The sentence under the partial label. The scorecard view prints the same text
# (src/web/templates/_screening_step3.html); tests/integration/test_web_pdf_export.py
# pins the two together.
_PARTIAL_LABEL_SENTENCE = (
    "ELIGIBLE means no evaluated criterion failed. It does not mean no exclusion applies."
)


def _partial_label_text(count: int) -> str:
    """The partial label line, as the scorecard view prints it (WEBAPP-12). `count`
    is the number of VERIFIED exclusions the scorer cannot evaluate. The label
    prints a number and fixed text, never criterion text."""
    noun = "exclusion" if count == 1 else "exclusions"
    return f"Partial: {count} {noun} not evaluated"


def _write_criterion(
    pdf: _ScorecardPDF, criterion: EligibilityCriterion, *, query_key: bool = False
) -> None:
    """One criteria row, as the scorecard view prints it: status label, the
    stored text, then the code (with operator and threshold when stored) or the
    UNMAPPED rationale. A VERIFIED row that drove the cohort query ends with the
    "query key" marker (DEMO-03)."""
    verified = criterion.mapping_status is MappingStatus.VERIFIED
    pdf.set_font(FONT_FAMILY, style="B", size=10)
    _write_line(pdf, "VERIFIED" if verified else "UNMAPPED")
    pdf.set_font(FONT_FAMILY, size=10)
    _write_line(pdf, criterion.raw_text)
    if verified:
        code_line = f"{_code_system_label(criterion.code_system)} {criterion.code}"
        if criterion.operator is not None:
            code_line += f" {criterion.operator.value}"
            if criterion.threshold is not None:
                code_line += f" {criterion.threshold}"
        _write_line(pdf, code_line)
        if query_key:
            _write_line(pdf, "query key")
    else:
        _write_line(pdf, _UNMAPPED_RATIONALE)
    pdf.ln(1)


def _write_criteria(
    pdf: _ScorecardPDF, criteria: list[EligibilityCriterion], *, unhonored: int
) -> None:
    """The Criteria section: the unhonored-criteria count, then Inclusion and
    Exclusion, each in stored (protocol) order. Both group headings print even
    when a group is empty."""
    pdf.ln(2)
    pdf.set_font(FONT_FAMILY, style="B", size=13)
    _write_line(pdf, "Criteria", height=7)
    pdf.set_font(FONT_FAMILY, size=10)
    _write_line(pdf, _unhonored_phrase(unhonored))
    key_positions = anchor.query_key_positions(
        [c for c in criteria if c.kind is CriterionKind.INCLUSION]
    )
    for heading, kind in (
        ("Inclusion", CriterionKind.INCLUSION),
        ("Exclusion", CriterionKind.EXCLUSION),
    ):
        pdf.set_font(FONT_FAMILY, style="B", size=11)
        _write_line(pdf, heading)
        position = 0
        for criterion in criteria:
            if criterion.kind is kind:
                is_key = kind is CriterionKind.INCLUSION and position in key_positions
                _write_criterion(pdf, criterion, query_key=is_key)
                position += 1


def _write_header(
    pdf: _ScorecardPDF,
    record: ScreenedRecord,
    approval: ApprovalRecord | None,
    *,
    coverage_warning: str | None,
    partial_label: str | None = None,
) -> None:
    """Title, the trial heading with its state badge, the FINALIZED stamp (only
    when `approval` is given, built from the approval's own fields), and the
    action-bar meta line with the status counts, then the data revision line and,
    when given, the partial label with its sentence (WEBAPP-12) and the coverage
    warning (WEBAPP-11), in that order."""
    pdf.set_font(FONT_FAMILY, style="B", size=16)
    _write_line(pdf, "TrialBridge scorecard", height=8)

    state = "SCREENED" if approval is None else "FINALIZED"
    pdf.set_font(FONT_FAMILY, style="B", size=12)
    heading_id = record.nct_id
    if synthetic_label.is_synthetic(record.nct_id):
        heading_id = f"{record.nct_id} {synthetic_label.SYNTHETIC_LABEL}"
    _write_line(pdf, f"{heading_id} · {record.trial_title}  {state}", height=7)

    if approval is not None:
        approved_at = approval.approved_at.strftime("%Y-%m-%d %H:%M")
        pdf.set_font(FONT_FAMILY, style="B", size=11)
        _write_line(
            pdf,
            f"FINALIZED by {approval.approved_by_role} · {approved_at} UTC "
            f"· ref {approval.screened_ref}",
        )

    counts = {"ELIGIBLE": 0, "BORDERLINE": 0, "INELIGIBLE": 0}
    for candidate in record.candidates:
        counts[candidate.eligibility_status] += 1
    screened_at = record.screened_at.strftime("%Y-%m-%d %H:%M")
    pdf.set_font(FONT_FAMILY, size=11)
    _write_line(
        pdf,
        f"ref {record.ref} · screened {screened_at} UTC by {record.screened_by_role.value} "
        f"· {counts['ELIGIBLE']} ELIGIBLE · {counts['BORDERLINE']} BORDERLINE "
        f"· {counts['INELIGIBLE']} INELIGIBLE",
    )
    _write_line(pdf, f"data revision {record.data_revision or 'unknown'}")
    if partial_label is not None:
        pdf.set_font(FONT_FAMILY, style="B", size=11)
        _write_line(pdf, partial_label)
        pdf.set_font(FONT_FAMILY, size=11)
        _write_line(pdf, _PARTIAL_LABEL_SENTENCE)
    if coverage_warning is not None:
        pdf.set_font(FONT_FAMILY, style="B", size=11)
        _write_line(pdf, coverage_warning)


def _evidence_summary(candidate: CandidateResult) -> str:
    """The row's one-line evidence summary, by the view's rule: exclusion flags,
    else the matching criteria, else a "no match" note."""
    if candidate.exclusion_flags:
        return "Excl: " + " · ".join(candidate.exclusion_flags)
    if candidate.matching_criteria:
        return " · ".join(candidate.matching_criteria)
    return "No criteria matched"


def _write_candidate(pdf: _ScorecardPDF, candidate: CandidateResult) -> None:
    """One candidate as the view shows it expanded: the row (pseudonym, status,
    flag chips, evidence summary), then the evidence panel."""
    row = f"{candidate.patient_pseudonym}  {candidate.eligibility_status}"
    if candidate.unmapped_criteria:
        row += "  unmapped gap"
    if candidate.recomputed_mismatch:
        row += "  discrepancy"
    pdf.set_font(FONT_FAMILY, style="B", size=10)
    _write_line(pdf, row)

    pdf.set_font(FONT_FAMILY, size=10)
    _write_line(pdf, _evidence_summary(candidate))
    for label, values in (
        ("Matched:", candidate.matching_criteria),
        ("Exclusion flags:", candidate.exclusion_flags),
        ("Unmapped — not evaluated:", candidate.unmapped_criteria),
    ):
        _write_line(pdf, f"{label} {' · '.join(values) if values else 'none'}")
    if candidate.recomputed_mismatch:
        _write_line(
            pdf,
            f"LLM proposed {candidate.llm_proposed_status}; gateway computed "
            f"{candidate.eligibility_status}.",
        )
    pdf.ln(2)


def _write_candidates(pdf: _ScorecardPDF, candidates: list[CandidateResult]) -> None:
    pdf.ln(2)
    pdf.set_font(FONT_FAMILY, style="B", size=13)
    _write_line(pdf, "Candidates", height=7)
    if not candidates:
        pdf.set_font(FONT_FAMILY, size=10)
        _write_line(pdf, "No candidates matched the verified criteria.")
        return
    for candidate in candidates:
        _write_candidate(pdf, candidate)


def render_scorecard_pdf(record: ScreenedRecord, approval: ApprovalRecord | None) -> bytes:
    """Renders `record` (plus `approval`'s FINALIZED stamp, when given) to a
    self-contained PDF. Raises `ValueError` — and returns no bytes — when
    `approval.screened_ref` doesn't match `record.ref`; lets any `fpdf2`
    exception propagate untouched."""
    if approval is not None and approval.screened_ref != record.ref:
        raise ValueError(
            f"approval.screened_ref {approval.screened_ref!r} does not match "
            f"record.ref {record.ref!r}"
        )

    pdf = _ScorecardPDF(orientation="P", unit="mm", format="A4")
    _register_fonts(pdf)
    pdf.add_page()

    unhonored = anchor.unhonored_criteria_count(record.criteria_snapshot)
    warn = anchor.coverage_gap_warning(
        unhonored, [candidate.eligibility_status for candidate in record.candidates]
    )
    unevaluated = anchor.unevaluated_exclusion_count(record.criteria_snapshot)
    _write_header(
        pdf,
        record,
        approval,
        coverage_warning=_coverage_warning_text(unhonored) if warn else None,
        partial_label=_partial_label_text(unevaluated) if unevaluated > 0 else None,
    )
    _write_criteria(pdf, record.criteria_snapshot, unhonored=unhonored)
    _write_candidates(pdf, record.candidates)

    return bytes(pdf.output())
