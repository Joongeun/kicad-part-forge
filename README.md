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
| `src/kifab/uuids.py` | derived (never random) UUIDs |
| `parts/` | the part corpus |
| `tests/golden/` | reviewed reference output, byte-compared |

`DECISIONS.md` records the locked decisions; don't relitigate them without
reading it.
