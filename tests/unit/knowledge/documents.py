"""Build small, readable test documents in code (no binary fixtures)."""

from __future__ import annotations

from io import BytesIO

from docx import Document
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def pdf(
    pages: list[list[str]],
    *,
    header: str | None = None,
    footer: str | None = None,
    encrypt: str | None = None,
) -> bytes:
    """One line of text per list item. ``footer`` may contain {page} and {pages}."""
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4, encrypt=encrypt)
    _, height = A4
    for number, lines in enumerate(pages, start=1):
        y = height - 50
        if header:
            c.drawString(50, y, header)
            y -= 30
        for line in lines:
            c.drawString(50, y, line)
            y -= 16
        if footer:
            c.drawString(50, 40, footer.format(page=number, pages=len(pages)))
        c.showPage()
    c.save()
    return buffer.getvalue()


def scanned_pdf(pages: int = 3) -> bytes:
    """Pages with drawings and no text layer, like a scan."""
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    for _ in range(pages):
        c.rect(50, 50, 400, 600, fill=1)
        c.showPage()
    c.save()
    return buffer.getvalue()


def docx_bytes(build: object) -> bytes:
    """``build(document)`` adds content to a fresh python-docx Document."""
    document = Document()
    build(document)  # type: ignore[operator]
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()
