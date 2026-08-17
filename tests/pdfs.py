"""Build a minimal, real PDF with a text layer, for tests.

Written by hand rather than pulled in as a dependency: the page-selection tests
need to state *exactly* what text is on each page, and a test fixture that
needs `reportlab` installed to run is a test that will be skipped one day.

The output is a genuine PDF — `pypdf` parses it and extracts the text, which is
the only property these tests need from it.
"""

from __future__ import annotations


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _content_stream(lines: list[str]) -> bytes:
    body = ["BT", "/F1 10 Tf", "12 TL", "40 750 Td"]
    for line in lines:
        body.append(f"({_escape(line)}) Tj")
        body.append("T*")
    body.append("ET")
    return "\n".join(body).encode("latin-1", "replace")


def make_pdf(pages: list[str]) -> bytes:
    """A PDF whose page N carries `pages[N-1]`, one line of text per line."""
    if not pages:
        raise ValueError("a PDF needs at least one page")

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)  # 1-based object number

    catalog_num = add(b"")  # 1: placeholder, filled below
    pages_num = add(b"")  # 2: placeholder
    font_num = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding "
        b"/WinAnsiEncoding >>"
    )

    page_nums: list[int] = []
    for text in pages:
        stream = _content_stream(text.splitlines())
        content_num = add(
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
        page_num = add(
            b"<< /Type /Page /Parent "
            + str(pages_num).encode()
            + b" 0 R /MediaBox [0 0 612 792] /Contents "
            + str(content_num).encode()
            + b" 0 R /Resources << /Font << /F1 "
            + str(font_num).encode()
            + b" 0 R >> >> >>"
        )
        page_nums.append(page_num)

    kids = b" ".join(f"{n} 0 R".encode() for n in page_nums)
    objects[pages_num - 1] = (
        b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(len(page_nums)).encode() + b" >>"
    )
    objects[catalog_num - 1] = (
        b"<< /Type /Catalog /Pages " + str(pages_num).encode() + b" 0 R >>"
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"

    xref_at = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root "
        + str(catalog_num).encode()
        + b" 0 R >>\nstartxref\n"
        + str(xref_at).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


# -- realistic page bodies, used by several tests -------------------------

FRONT_PAGE = """\
ACME SEMICONDUCTOR
XYZ1234 Quad Widget Driver
FEATURES
 * Wide supply range 2.7 V to 5.5 V
 * Four independent channels
DESCRIPTION
The XYZ1234 is a quad widget driver in a small package.
"""

PIN_TABLE_PAGE = """\
PIN FUNCTIONS
Pin No  Pin Name  Type  Description
1  VDD  Power  Positive supply
2  IN1  Input  Channel 1 input
3  IN2  Input  Channel 2 input
4  GND  Power  Ground
5  OUT2  Output  Channel 2 output
6  OUT1  Output  Channel 1 output
7  EN  Input  Enable, active high
8  NC  -  No internal connection
9  EP  -  Exposed pad, connect to GND
"""

MECHANICAL_PAGE = """\
PACKAGE OUTLINE
8-Lead Plastic QFN (3mm x 3mm)
DIMENSIONS ARE IN MILLIMETERS
JEDEC MO-229 VARIATION WEED
Symbol  MIN  NOM  MAX
A  0.80  0.85  0.90
A1  0.00  0.02  0.05
b  0.20  0.25  0.30
D  2.90  3.00  3.10
E  2.90  3.00  3.10
e  0.65 BSC
L  0.30  0.40  0.50
"""

FILLER_PAGE = """\
ELECTRICAL CHARACTERISTICS
Supply current versus temperature is shown in Figure 3.
Typical performance curves follow on the next several pages.
"""


def datasheet(pin_page: int = 3, mech_page: int = 6, total: int = 8) -> bytes:
    """A plausible datasheet: a front page, filler, and the two pages we want."""
    pages = [FRONT_PAGE] + [FILLER_PAGE] * (total - 1)
    pages[pin_page - 1] = PIN_TABLE_PAGE
    pages[mech_page - 1] = MECHANICAL_PAGE
    return make_pdf(pages)
