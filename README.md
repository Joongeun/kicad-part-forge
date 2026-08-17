# KiCad Part Forge

Generate KiCad 9 symbols and footprints from one validated, reviewable
description of a part.

Everything flows through a single intermediate representation (IR): a pydantic
model, written as YAML in `parts/`. Sources fill it, emitters read it,
validators check it. That is what makes house style a one-place change and
makes a wrong pin a one-line diff instead of a GUI redraw.

```
parts/<MPN>.yaml  ──▶  Part IR  ──┬──▶  build/<lib>.kicad_sym
   (hand-written,      (pydantic) │
    diffable)                     └──▶  build/<lib>.pretty/<package>.kicad_mod
```

## Use it

```sh
uv run kifab build parts/            # everything in parts/ -> build/
uv run kifab build parts/24LC256.yaml -o /tmp/out
```

Then point KiCad at `build/kifab.kicad_sym` and `build/kifab.pretty/`.

## Reuse before you generate (tier T0)

KiCad ships **22,387 symbols and 15,179 footprints**, and they are KLC-clean.
For a large fraction of parts the right answer is "this already exists", so
search comes before generation.

```sh
uv run kifab index                       # one-off, ~40 s; then ~0.3 s to refresh
uv run kifab search LTC5552 --package '12-Lead Plastic QFN (3mm x 2mm), DWG 05-08-1985'
uv run kifab adopt --symbol Memory_EEPROM:24LC256 \
                   --footprint Package_SO:SOIC-8_3.9x4.9mm_P1.27mm --mpn 24LC256
```

`adopt` writes `parts/<MPN>.yaml`, so a reused part is a normal citizen: it
builds, it validates, and a wrong pin is still a one-line edit.

### Results come back in two lists, and that is the point

```
footprints
  CONFIDENT — none. Package identity was not established.
  REVIEW — near misses, NOT verified; a human must judge these:
    Package_DFN_QFN:DFN-12-1EP_2x3mm_P0.45mm_EP0.64x2.4mm  family: QFN != DFN; sides: 4 != 2
```

**A body-size match is not a package match.** KiCad's `DFN-12-1EP_2x3mm…` has
the same pin count, body, pitch and exposed pad as the LTC5552's UDB package —
and is a different land pattern from a different mechanical drawing. Returning
it confidently would ship a wrong footprint that looks right, so identity is
established from **family, measured edge count, pin count, pitch, body,
exposed-pad presence and size, and drawing number** — and an attribute nobody
stated blocks confidence just as hard as one that disagrees. `.confident` and
`.review` are separate lists in the API too; there is no combined accessor to
take `[0]` from by accident.

## Verify it

```sh
./scripts/verify.sh
```

That builds the corpus, runs the test suite, and then hands the generated files
to `kicad-cli … upgrade` — KiCad's own parser — as the conformance gate. Nothing
is "done" until it passes.

The suite also asserts the stronger property that `kicad-cli` rewrites our
output **byte-for-byte identically** apart from the `(generator …)` token, i.e.
we emit KiCad's canonical form rather than merely something it will accept.

## Write a part

```yaml
mpn: 24LC256
reference: U
datasheet: http://ww1.microchip.com/downloads/en/devicedoc/21203m.pdf
description: I2C Serial EEPROM, 256 Kbit, SOIC-8

symbol:
  pins:
    - { number: 1, name: A0,  type: input,         side: left,  slot: 0 }
    - { number: 5, name: SDA, type: bidirectional, side: right, slot: 0 }
    - { number: 8, name: VCC, type: power_in,      side: top }
    # ...

footprint:
  name: SOIC-8_3.9x4.9mm_P1.27mm
  package:
    family: dual_gullwing        # lands computed from IPC-7351B
    pin_count: 8
    pitch: 1.27
    body: { x: 3.9, y: 4.9 }
    lead_span: { nominal: 6.0, tolerance: 0.2 }
    lead_width: 0.31 .. 0.51
    lead_length: 0.40 .. 1.27
```

Pins declare a **side and slot**, never coordinates — the emitter places them on
the 100 mil grid, so an off-grid pin is not expressible. Packages declare
**datasheet dimensions**, and IPC-7351B produces the copper; `family: custom`
lets you state lands directly when no family fits.

See `src/kifab/ir/` for the full field reference — every non-obvious field
carries its semantics in its `description`.

## Layout

| Path | What |
|---|---|
| `src/kifab/ir/` | the Part IR — the contract |
| `src/kifab/emit/` | S-expression writer + `.kicad_sym` / `.kicad_mod` emitters |
| `src/kifab/ipc/` | IPC-7351B land arithmetic and package families |
| `src/kifab/index/` | SQLite/FTS index over the local corpus + package identity |
| `src/kifab/resolve/` | resolver tiers; `local.py` is T0, `adopt.py` turns a hit into IR |
| `src/kifab/uuids.py` | derived (never random) UUIDs |
| `parts/` | the part corpus |
| `tests/golden/` | reviewed reference output, byte-compared |

`DECISIONS.md` records the locked decisions; don't relitigate them without
reading it.
