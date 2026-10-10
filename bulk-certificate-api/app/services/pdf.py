"""Certificate rendering with ReportLab. Two built-in templates ("classic", "modern");
organization, signatory, title and date come from the job, the name from each recipient."""

from datetime import date
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from app import config
from app.errors import GenerationError

PAGE_W, PAGE_H = landscape(A4)
MAX_TEXT_WIDTH = PAGE_W - 160

THEMES: dict[str, dict[str, Any]] = {
    "classic": {
        "primary": colors.HexColor("#1f3a5f"),
        "accent": colors.HexColor("#b8923a"),
        "text": colors.HexColor("#555555"),
        "frame": "double",  # navy + gold double border
    },
    "modern": {
        "primary": colors.HexColor("#0f766e"),
        "accent": colors.HexColor("#f59e0b"),
        "text": colors.HexColor("#475569"),
        "frame": "sidebar",  # thin border + coloured bar on the left
    },
}


def _fonts() -> tuple[str, str]:
    """(regular, bold) font names. Uses CERT_FONT_PATH if configured, else built-in Helvetica."""
    if config.FONT_PATH:
        if "CertFont" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("CertFont", config.FONT_PATH))
        return "CertFont", "CertFont"
    return "Helvetica", "Helvetica-Bold"


def _check_renderable(text: str, builtin_font: bool) -> None:
    # Built-in PDF fonts only cover Latin-1/WinAnsi; anything else would render as garbage.
    if builtin_font:
        try:
            text.encode("cp1252")
        except UnicodeEncodeError:
            raise GenerationError("Text contains characters the certificate font cannot render") from None


def _fit(text: str, font: str, size: float, min_size: float = 12) -> float:
    """Shrink the font until the text fits the page width; fail if it never does."""
    while pdfmetrics.stringWidth(text, font, size) > MAX_TEXT_WIDTH:
        size -= 1
        if size < min_size:
            raise GenerationError("Text is too long to fit on the certificate")
    return size


def generate_certificate_pdf(
    dest: Path,
    *,
    recipient_name: str,
    title: str,
    issued_by: str,
    issue_date: date,
    certificate_number: str,
    remarks: str | None = None,
    template: str = "classic",
    signatory_name: str | None = None,
    signatory_title: str | None = None,
) -> None:
    """Render one certificate to ``dest``. Written atomically (temp file + rename), so a
    failure never leaves a half-written PDF behind."""
    theme = THEMES.get(template)
    if theme is None:
        raise GenerationError(f"Unknown template '{template}'")
    regular, bold = _fonts()
    builtin = not config.FONT_PATH
    for text in (recipient_name, title, issued_by, remarks, signatory_name, signatory_title):
        if text:
            _check_renderable(text, builtin)
    name_size = _fit(recipient_name, bold, 40)
    title_size = _fit(title, bold, 24)
    org_size = _fit(issued_by.upper(), bold, 15, min_size=9)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".pdf.tmp")
    cx = PAGE_W / 2
    primary, accent, grey = theme["primary"], theme["accent"], theme["text"]
    try:
        c = canvas.Canvas(str(tmp), pagesize=(PAGE_W, PAGE_H))
        c.setTitle(f"Certificate - {recipient_name}")
        c.setAuthor(issued_by)

        if theme["frame"] == "double":
            c.setStrokeColor(primary)
            c.setLineWidth(4)
            c.rect(25, 25, PAGE_W - 50, PAGE_H - 50)
            c.setStrokeColor(accent)
            c.setLineWidth(1.5)
            c.rect(36, 36, PAGE_W - 72, PAGE_H - 72)
        else:
            c.setStrokeColor(primary)
            c.setLineWidth(1.5)
            c.rect(25, 25, PAGE_W - 50, PAGE_H - 50)
            c.setFillColor(primary)
            c.rect(25, 25, 22, PAGE_H - 50, stroke=0, fill=1)
            c.setFillColor(accent)
            c.rect(47, 25, 5, PAGE_H - 50, stroke=0, fill=1)

        c.setFillColor(accent)
        c.setFont(bold, org_size)
        c.drawCentredString(cx, PAGE_H - 80, issued_by.upper())
        c.setFillColor(primary)
        c.setFont(bold, 38)
        c.drawCentredString(cx, PAGE_H - 130, "CERTIFICATE")
        c.setFillColor(accent)
        c.setFont(regular, 16)
        c.drawCentredString(cx, PAGE_H - 157, "OF COMPLETION")

        c.setFillColor(grey)
        c.setFont(regular, 15)
        c.drawCentredString(cx, PAGE_H - 215, "This is to certify that")
        c.setFillColor(primary)
        c.setFont(bold, name_size)
        c.drawCentredString(cx, PAGE_H - 275, recipient_name)
        c.setStrokeColor(accent)
        c.setLineWidth(1)
        c.line(cx - 220, PAGE_H - 288, cx + 220, PAGE_H - 288)

        c.setFillColor(grey)
        c.setFont(regular, 15)
        c.drawCentredString(cx, PAGE_H - 325, "has successfully completed")
        c.setFillColor(primary)
        c.setFont(bold, title_size)
        c.drawCentredString(cx, PAGE_H - 365, title)
        if remarks:
            c.setFillColor(grey)
            c.setFont(regular, 14)
            c.drawCentredString(cx, PAGE_H - 395, remarks)

        # footer: date (left) | signature block (right)
        c.setStrokeColor(grey)
        c.setLineWidth(0.8)
        c.line(95, 128, 295, 128)
        c.line(PAGE_W - 295, 128, PAGE_W - 95, 128)
        c.setFillColor(grey)
        c.setFont(regular, 12)
        c.drawCentredString(195, 110, issue_date.strftime("%d %B %Y"))
        sig_x = PAGE_W - 195
        if signatory_name:
            c.setFont(bold, 12)
            c.drawCentredString(sig_x, 110, signatory_name)
            if signatory_title:
                c.setFont(regular, 10)
                c.drawCentredString(sig_x, 95, signatory_title)
        else:
            c.setFont(regular, 10)
            c.drawCentredString(sig_x, 110, "Authorized signature")
        c.setFont(regular, 10)
        c.drawCentredString(195, 95, "Date of issue")
        c.setFont(regular, 9)
        c.drawCentredString(cx, 60, f"Certificate No. {certificate_number}")
        c.save()
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
