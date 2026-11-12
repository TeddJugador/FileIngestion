from __future__ import annotations

from typing import Any

import pytest

from knowledge.extraction import BlockKind, ExtractionError, ExtractionFailure, extract
from knowledge.extraction.normalise import fix_hyphenation, remove_repeated_lines, split_paragraphs
from tests.unit.knowledge.documents import docx_bytes, pdf, scanned_pdf

BODY = "Learners must arrive by 07:30 and sign the register at the front office."


def _texts(document: Any) -> list[str]:
    return [b.text for b in document.blocks]


# --- PDF ---------------------------------------------------------------------------------


def test_pdf_keeps_page_numbers() -> None:
    document = extract(
        pdf(
            [
                [f"Page one rules. {BODY}"],
                [f"Page two rules. {BODY}"],
                [f"Page three rules. {BODY}"],
            ]
        ),
        "pdf",
    )
    assert document.page_count == 3
    assert [b.page for b in document.blocks] == [1, 2, 3]
    assert document.blocks[1].text.startswith("Page two rules.")


def test_pdf_removes_running_headers_and_footers() -> None:
    document = extract(
        pdf(
            [[f"Section {i}. {BODY}"] for i in range(1, 5)],
            header="Riverside Primary School - Code of Conduct 2027",
            footer="Page {page} of {pages}",
        ),
        "pdf",
    )
    text = "\n".join(_texts(document))
    assert "Riverside Primary School - Code of Conduct 2027" not in text
    assert "Page 2 of 4" not in text  # differs only by number on every page
    for i in range(1, 5):
        assert f"Section {i}." in text


def test_pdf_with_two_pages_keeps_shared_lines() -> None:
    # With 2 pages "more than half" would mean any shared line: nothing is removed.
    document = extract(
        pdf([[f"Intro. {BODY}"], [f"More. {BODY}"]], header="School Handbook"), "pdf"
    )
    assert sum("School Handbook" in t for t in _texts(document)) == 2


def test_pdf_fixes_hyphenated_line_breaks() -> None:
    document = extract(
        pdf(
            [
                [
                    "Fees are payable in advance. Late pay-",
                    "ments attract a penalty of two percent each month.",
                ]
            ]
        ),
        "pdf",
    )
    assert "Late payments attract" in document.blocks[0].text


def test_scanned_pdf_has_no_extractable_text() -> None:
    with pytest.raises(ExtractionError) as excinfo:
        extract(scanned_pdf(), "pdf")
    assert excinfo.value.reason is ExtractionFailure.NO_EXTRACTABLE_TEXT


def test_pdf_with_only_page_numbers_counts_as_scanned() -> None:
    with pytest.raises(ExtractionError) as excinfo:
        extract(pdf([["1"], ["2"], ["3"]]), "pdf")
    assert excinfo.value.reason is ExtractionFailure.NO_EXTRACTABLE_TEXT


def test_encrypted_pdf_fails_extraction() -> None:
    with pytest.raises(ExtractionError) as excinfo:
        extract(pdf([[BODY]], encrypt="secret"), "pdf")
    assert excinfo.value.reason is ExtractionFailure.EXTRACTION_FAILED


@pytest.mark.parametrize("data", [b"%PDF-1.7 truncated", b"", b"not a pdf at all"])
def test_corrupt_pdf_fails_extraction(data: bytes) -> None:
    with pytest.raises(ExtractionError) as excinfo:
        extract(data, "pdf")
    assert excinfo.value.reason is ExtractionFailure.EXTRACTION_FAILED


# --- DOCX --------------------------------------------------------------------------------


def _policy(document: Any) -> None:
    document.add_heading("Fee Policy 2027", level=0)  # Title style
    document.add_heading("Payment", level=1)
    document.add_paragraph("Fees are due on the 7th of each month.")
    document.add_heading("Late payment", level=2)
    document.add_paragraph("A penalty of two percent applies after 30 days.")
    document.add_heading("Discounts", level=1)
    document.add_paragraph("Siblings receive a ten percent discount.")
    document.add_paragraph("Third child", style="List Bullet")
    document.add_paragraph("Fourth child", style="List Bullet")
    table = document.add_table(rows=3, cols=3)
    for r, row in enumerate(
        [
            ["Grade", "Term fee", "Annual fee"],
            ["R", "R8 500", "R34 000"],
            ["7", "R12 500", "R50 000"],
        ]
    ):
        for c, value in enumerate(row):
            table.cell(r, c).text = value


def test_docx_heading_paths() -> None:
    document = extract(docx_bytes(_policy), "docx")
    by_text = {b.text.split("\n")[0]: b.heading_path for b in document.blocks}
    # The Title style isn't part of heading paths: the metadata title leads the context.
    assert by_text["Fees are due on the 7th of each month."] == ("Payment",)
    assert by_text["A penalty of two percent applies after 30 days."] == ("Payment", "Late payment")
    # A new level-1 heading replaces "Payment" and drops "Late payment".
    assert by_text["Siblings receive a ten percent discount."] == ("Discounts",)
    assert all("Fee Policy 2027" not in b.text for b in extract(docx_bytes(_policy), "docx").blocks)


def test_docx_tables_are_flattened_row_by_row() -> None:
    (table,) = [b for b in extract(docx_bytes(_policy), "docx").blocks if b.kind is BlockKind.TABLE]
    assert (
        table.text == "Grade | Term fee | Annual fee\nR | R8 500 | R34 000\n7 | R12 500 | R50 000"
    )


def test_docx_merged_cells_appear_once() -> None:
    def build(document: Any) -> None:
        table = document.add_table(rows=2, cols=3)
        merged = table.cell(0, 0).merge(table.cell(0, 2))
        merged.text = "Term 1 fees"
        for c, value in enumerate(["Tuition", "Books", "Trips"]):
            table.cell(1, c).text = value

    (table,) = extract(docx_bytes(build), "docx").blocks
    assert table.text == "Term 1 fees\nTuition | Books | Trips"


def test_docx_list_items_stay_together() -> None:
    (items,) = [b for b in extract(docx_bytes(_policy), "docx").blocks if b.kind is BlockKind.LIST]
    assert items.text == "Third child\nFourth child"


def test_empty_docx_has_no_text() -> None:
    with pytest.raises(ExtractionError) as excinfo:
        extract(docx_bytes(lambda d: None), "docx")
    assert excinfo.value.reason is ExtractionFailure.NO_EXTRACTABLE_TEXT


def test_corrupt_docx_fails_extraction() -> None:
    with pytest.raises(ExtractionError) as excinfo:
        extract(b"PK\x03\x04 broken zip", "docx")
    assert excinfo.value.reason is ExtractionFailure.EXTRACTION_FAILED


# --- Markdown and text --------------------------------------------------------------------

MARKDOWN = b"""# Uniform Policy

All learners wear the school uniform.

## Shoes

Black leather school shoes.
Polished daily.

## Sports kit

- Navy shorts
- White golf shirt
- Takkies

```
# this is code, not a heading
```

### Swimming

Costumes must be navy.
"""


def test_markdown_splits_on_headings() -> None:
    blocks = extract(MARKDOWN, "md").blocks
    paths = {b.text: b.heading_path for b in blocks}
    assert paths["All learners wear the school uniform."] == ("Uniform Policy",)
    assert paths["Black leather school shoes. Polished daily."] == ("Uniform Policy", "Shoes")
    assert paths["Costumes must be navy."] == ("Uniform Policy", "Sports kit", "Swimming")


def test_markdown_lists_and_code_blocks() -> None:
    blocks = extract(MARKDOWN, "md").blocks
    (items,) = [b for b in blocks if b.kind is BlockKind.LIST]
    assert items.text == "- Navy shorts\n- White golf shirt\n- Takkies"
    (code,) = [b for b in blocks if b.kind is BlockKind.CODE]
    assert code.text == "# this is code, not a heading"
    assert code.heading_path == ("Uniform Policy", "Sports kit")  # '#' in code isn't a heading


def test_plain_text_paragraphs_and_bom() -> None:
    blocks = extract(
        b"\xef\xbb\xbfFirst   paragraph\nwraps here.\n\n\nSecond\tparagraph.\n", "txt"
    ).blocks
    assert [b.text for b in blocks] == ["First paragraph wraps here.", "Second paragraph."]
    assert all(b.heading_path == () and b.page is None for b in blocks)


def test_invalid_utf8_fails_extraction() -> None:
    with pytest.raises(ExtractionError) as excinfo:
        extract(b"caf\xe9", "txt")
    assert excinfo.value.reason is ExtractionFailure.EXTRACTION_FAILED


@pytest.mark.parametrize("data", [b"", b"   \n\n  \t"])
def test_blank_text_has_no_extractable_text(data: bytes) -> None:
    with pytest.raises(ExtractionError) as excinfo:
        extract(data, "txt")
    assert excinfo.value.reason is ExtractionFailure.NO_EXTRACTABLE_TEXT


def test_unsupported_extension() -> None:
    with pytest.raises(ExtractionError) as excinfo:
        extract(b"x", "rtf")
    assert excinfo.value.reason is ExtractionFailure.EXTRACTION_FAILED


def test_failure_details_never_quote_document_text() -> None:
    with pytest.raises(ExtractionError) as excinfo:
        extract(pdf([["Thabo Ndlovu"], ["1"], ["2"]]), "pdf")
    assert "Thabo" not in str(excinfo.value)


# --- normalisation helpers ----------------------------------------------------------------


def test_whitespace_is_normalised() -> None:
    (paragraph,) = split_paragraphs("Fees\u00a0are\u2003due\t\ton  the 7th.\u200b")
    assert paragraph == ("Fees are due on the 7th.", False)


def test_hyphenation_only_joins_lowercase_continuations() -> None:
    assert fix_hyphenation("pay-\nment") == "payment"
    assert fix_hyphenation("Grade-\n7 learners") == "Grade-\n7 learners"  # not a broken word


def test_numbered_body_lines_on_short_pages_are_kept() -> None:
    pages = [
        [f"Section {i}. Learners must arrive by 07:30.", f"Page {i} of 4"] for i in range(1, 5)
    ]
    cleaned = remove_repeated_lines(pages)
    assert [p[0] for p in cleaned] == [
        f"Section {i}. Learners must arrive by 07:30." for i in range(1, 5)
    ]
    assert all(len(p) == 1 for p in cleaned)  # only the page numbers went


def test_repeated_lines_only_considered_at_page_edges() -> None:
    body_repeat = "Learners must respect one another."
    pages = [
        ["Header", f"Body {i}", body_repeat, f"More {i}", "x", "y", "z", "Footer"] for i in range(4)
    ]
    cleaned = remove_repeated_lines(pages)
    assert all("Header" not in p and "Footer" not in p for p in cleaned)
    assert all(body_repeat in p for p in cleaned)  # mid-page repeats are content


def test_text_directly_under_the_title_has_no_heading_path() -> None:
    def build(document: Any) -> None:
        document.add_heading("Transport Rules", level=0)
        document.add_paragraph("These rules apply to every school bus route.")

    (block,) = extract(docx_bytes(build), "docx").blocks
    assert block.heading_path == ()
