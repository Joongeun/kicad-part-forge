"""The extraction instructions handed to whichever provider is configured.

Kept as data, in one place, for two reasons. It is the thing most likely to
need tuning against the datasheet corpus, and a prompt that is scattered
through call sites cannot be diffed when extraction quality moves.

What it deliberately does **not** do is ask the model to behave. "Do not look
at existing footprints" is unenforceable; the sandbox and the tool allowlist
enforce it. The prompt's job is only to describe the output shape and to make
the honest answer ("this page does not state that dimension") easy to give.
"""

from __future__ import annotations

TEMPLATE = """\
# Task

Produce a **kifab Part IR** YAML document for the part `{mpn}` from the
attached datasheet pages. Answer with the YAML document and nothing else.

You were given pages {pages} of a {total}-page datasheet: the pin table and the
mechanical drawing, selected automatically from the text layer. If something
you need is not on these pages, write a `# NOTE:` comment saying so — **never
supply a dimension from memory**. A remembered number that looks right is the
one failure this pipeline exists to prevent.

# Shape

```yaml
mpn: {mpn}
manufacturer: <from the front page>
library: kifab
reference: U            # the designator prefix: U, R, D, Q, J...
datasheet: <URL if the front page states one>
description: <one line, as the datasheet describes the part>

symbol:
  keywords: <space separated>
  pins:
    # One row per pin. `number` is the pad number; `name` is the datasheet's
    # pin name. `type` drives ERC, so get it right:
    #   power_in     supply and ground pins
    #   input        inputs, enables, resets
    #   output       outputs
    #   bidirectional  I/O
    #   passive      RF ports, matching pins, unconnected mechanical pads
    #   no_connect   pins the datasheet says must be left open
    - {{ number: 1, name: <NAME>, type: <type>, side: left }}

footprint:
  name: <PACKAGE-N[-1EP]_WxHmm_P<pitch>mm[_EP<a>x<b>mm]>
  description: <package, pin count, body size, pitch, and the drawing number>
  tags: <space separated>
  package:
    family: <one of: quad_no_lead | dual_no_lead | quad_gullwing |
             dual_gullwing | custom>
    ...
```

# Package families

State datasheet dimensions; kifab computes the lands with IPC-7351B. Write a
toleranced dimension as `"2.90 .. 3.10"`, or as a bare number **only** when the
drawing marks it BSC/basic (i.e. exact).

* `quad_no_lead` — QFN/VQFN/UQFN. Terminals flush with the body edge.
  `body: {{x: <mm>, y: <mm>}}` (nominal), `body_tolerance: <+/- mm>`,
  `pins_x:` lands on **each** of the top and bottom rows, `pins_y:` lands on
  **each** of the left and right columns, `pitch:`, `lead_width:` (drawing
  `b`), `lead_length:` (drawing `L`), and, when the drawing shows one,
  `exposed_pad: {{size_x: <mm>, size_y: <mm>, paste_pads: [<nx>, <ny>]}}`.
* `dual_no_lead` — DFN/SON. As above but `pin_count:` instead of pins_x/pins_y.
* `quad_gullwing` / `dual_gullwing` — QFP / SOIC / SOP / SOT. These have leads
  that extend past the body, so they need `lead_span` (the outside-to-outside
  dimension, drawing `E`/`D`), not the body size.
* `custom` — lands stated directly as `pads: [{{number, at: [x, y], size: [w, h]}}]`.
  Use this only when the drawing gives a recommended land pattern and no lead
  dimensions.

# Rules

1. Pin count in the symbol must equal the pad count in the footprint, exposed
   pad included. The exposed pad gets a pin too — usually `name: EP`,
   `type: passive`.
2. Do not invent a tolerance. If the drawing gives one number, give one number.
3. If the drawing states a *recommended land pattern* as well as the package
   dimensions, still state the package dimensions — kifab computes the lands
   and the recommended pattern is how a human checks the result.
4. `# NOTE:` any dimension you could not find, any pin whose type you had to
   infer, and any place the drawing was ambiguous. Those notes are read.
"""


def build_instructions(*, mpn: str, pages: list[int], total_pages: int) -> str:
    return TEMPLATE.format(
        mpn=mpn,
        pages=", ".join(str(p) for p in pages) or "(all)",
        total=total_pages,
    )
