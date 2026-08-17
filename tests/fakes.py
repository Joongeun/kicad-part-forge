"""Test doubles for the LLM layer.

A canned provider, so the whole T2 pipeline — page selection, slicing,
parsing, building, validating, the review gate and the audit — is exercised
end to end with no model, no key and no network. Every test in the suite runs
offline; the model is the only part that cannot be, and it is the part these
doubles replace.
"""

from __future__ import annotations

from kifab.llm import ExtractionRequest, LLMProvider

#: A valid Part IR document for the synthetic datasheet in `tests/pdfs.py`.
GOOD_YAML = """\
mpn: XYZ1234
manufacturer: ACME Semiconductor
library: kifab
reference: U
datasheet: https://example.invalid/xyz1234.pdf
description: Quad widget driver, 8-lead QFN 3x3 mm

symbol:
  keywords: widget driver quad
  pins:
    - { number: 1, name: VDD, type: power_in, side: left }
    - { number: 2, name: IN1, type: input, side: left }
    - { number: 3, name: IN2, type: input, side: left }
    - { number: 4, name: GND, type: power_in, side: left }
    - { number: 5, name: OUT2, type: output, side: right }
    - { number: 6, name: OUT1, type: output, side: right }
    - { number: 7, name: EN, type: input, side: right }
    - { number: 8, name: NC, type: no_connect, side: right }
    - { number: 9, name: EP, type: passive, side: bottom }

footprint:
  name: QFN-8-1EP_3x3mm_P0.65mm_EP1.6x1.6mm
  description: QFN, 8 pin, 3x3 mm body, 0.65 mm pitch, 1.6x1.6 mm exposed pad (JEDEC MO-229)
  tags: QFN
  package:
    family: quad_no_lead
    body: { x: 3.0, y: 3.0 }
    body_tolerance: 0.1
    pins_x: 2
    pins_y: 2
    pitch: 0.65
    lead_width: 0.20 .. 0.30
    lead_length: 0.30 .. 0.50
    exposed_pad: { size_x: 1.6, size_y: 1.6, paste_pads: [2, 2] }
"""


class CannedProvider(LLMProvider):
    """Returns a fixed reply, and records what it was asked."""

    name = "canned"

    def __init__(self, sandbox, *, reply: str = GOOD_YAML, **kwargs):
        super().__init__(sandbox, **kwargs)
        self.reply = reply
        self.requests: list[ExtractionRequest] = []

    def _run(self, request: ExtractionRequest) -> str:
        self.requests.append(request)
        return self.reply


class CheatingProvider(LLMProvider):
    """Pretends to have read an existing footprint, so the auditor can catch it.

    This exists because an auditor that has never seen a violation is an
    auditor nobody has tested.
    """

    name = "cheating"

    def __init__(self, sandbox, *, how: str = "library", **kwargs):
        super().__init__(sandbox, **kwargs)
        self.how = how

    def _run(self, request: ExtractionRequest) -> str:
        if self.how == "library":
            self.sandbox.transcript.record(
                "tool_call",
                tool="read_pdf",
                path="/Applications/KiCad/KiCad.app/Contents/SharedSupport/"
                "footprints/Package_DFN_QFN.pretty/DFN-12-1EP_2x3mm.kicad_mod",
            )
        elif self.how == "vendor":
            self.sandbox.transcript.record(
                "tool_call", tool="read_pdf", url="https://www.snapeda.com/parts/x/"
            )
        elif self.how == "escape":
            self.sandbox.transcript.record(
                "tool_call", tool="read_pdf", path="/etc/passwd"
            )
        return GOOD_YAML
