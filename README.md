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

## Import from LCSC / EasyEDA (tier T1)

For the parts KiCad doesn't ship — most of the JLCPCB assembly catalogue —
EasyEDA has geometry. It is used as an **ingester, never as a generator**:
nothing it returns reaches your library. It lands in `parts/<MPN>.yaml`, and
kifab's own emitters rebuild it in house style, lint it and restyle it.

```sh
uv run kifab lcsc C2040                  # by LCSC code
uv run kifab lcsc TL072CDT               # by exact MPN
uv run kifab lcsc RP2040 --list          # ambiguous? see the candidates
```

It writes the part, fetches the STEP model into `models/<library>.3dshapes/`,
and runs `kifab check` on what it wrote before it claims success.

**What it will not do is guess.** Pad rotations of 90/270 are folded into the
pad size (exactly equivalent copper); pin numbers come from the *drawn* number,
not EasyEDA's sequence field, which disagrees with it on real parts; the body
comes from the `L…-W…` in the package name, with the drawing used only to
decide which dimension is which axis. Anything that could not be normalised
provably — an unstated body size, a slotted hole, a 3D model that wants an
offset, EasyEDA's famously loose pin electrical types — is written into the
file as a `# NOTE:` and reported by the linter. A polygon pad is refused
outright rather than approximated. See `easyeda.NORMALISATIONS` and
`easyeda.NOT_NORMALISED`.

Expect an imported part to carry warnings. That is the point: `kifab check`
passing with `SCH002 pin "1" (GND) … is typed 'unspecified'` is the tool
telling you exactly which line to fix.

## Check it

```sh
uv run kifab check parts/                  # a part, a directory, a whole tree
uv run kifab check build/kifab.pretty      # or files with no IR behind them
uv run kifab check parts/ --strict --json  # for CI: warnings block, output parses
```

Four families of check, and each says which representation it read:

| | reads | catches |
|---|---|---|
| **schema** | the IR | well-typed statements that contradict each other — a lead span narrower than the body, `GND` typed `output`, a footprint name stating a pitch its geometry does not have |
| **geometry** | emitted files | pad-to-pad and exposed-pad shorts, pads outside the courtyard, silk on copper, off-grid pins, duplicate pins/pads, symbol↔footprint pin-set disagreement |
| **KLC** | emitted files | the conventions where a deviation is a defect: layer line widths, `(attr …)` matching the pads, pin-1 indicator, 3D model path, the canonical property set |
| **conformance** | emitted files | `kicad-cli … upgrade` — KiCad's own parser — accepting the file *and* rewriting it byte-identically |

Output names the element, its position and the reason:

```
parts/BAD1.yaml
  error   GEO001    pad "1" <-> pad "2": copper of two different pad numbers
                    overlaps or touches (gap 0.000 mm) — this is a short  at (0, 0)
```

**An error blocks; a warning does not** (`--strict` promotes them). A third
level, *info*, is for notes that never block — including "kicad-cli was not
found, so that gate did not run", because a check that could not run must never
look like one that passed.

## Verify it

```sh
./scripts/verify.sh
```

That builds the corpus, runs the test suite, and then runs `kifab check … --strict`
over both the IR and the generated files. Nothing is "done" until it passes.

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
| `src/kifab/resolve/` | resolver tiers; `local.py`/`adopt.py` are T0, `easyeda.py` is T1 |
| `src/kifab/validate/` | schema lint, geometry sanity, KLC, the `kicad-cli` gate |
| `src/kifab/uuids.py` | derived (never random) UUIDs |
| `parts/` | the part corpus |
| `tests/golden/` | reviewed reference output, byte-compared |

`DECISIONS.md` records the locked decisions; don't relitigate them without
reading it.
