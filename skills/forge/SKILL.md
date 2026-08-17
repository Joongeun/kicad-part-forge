---
name: forge
description: Turn a manufacturer part number into a KiCad 9 symbol + footprint. Use when the user asks to create, generate or find a KiCad part, symbol or footprint for a specific component, or mentions an MPN alongside KiCad.
---

# forge — MPN in, reviewed KiCad part out

`kifab` does the work. Your job is to route the request to the cheapest tier
that can answer it, and to be the human-readable half of the review gate.

**Never hand-write a `.kicad_sym` or `.kicad_mod`.** Every artefact comes out of
`kifab build` from a `parts/<MPN>.yaml`. That file is the reviewable thing.

## Route it, in this order. Stop at the first tier that answers.

### T0 — it probably already exists

```
kifab search <MPN> --package '<package exactly as the datasheet states it>'
```

Pass `--package`. Without it no footprint can be confirmed, and the tool will
tell you so. Results come back in two groups and the split is load-bearing:

* **CONFIDENT** — package identity was established. Adopt it:
  `kifab adopt --footprint LIB:NAME [--symbol LIB:NAME] --mpn <MPN>`
* **REVIEW** — near misses. **A body-size match is not a package match.** KiCad
  ships a 12-lead DFN and a 12-lead QFN with the same 2x3 mm body and different
  land patterns. Show the user the near miss and the reason it was rejected;
  never adopt one silently.

### T1 — LCSC / EasyEDA (huge JLCPCB coverage)

```
kifab lcsc <MPN-or-LCSC-code> --list      # see the candidates first
kifab lcsc C2040
```

It is an *ingester*: the data lands in `parts/<MPN>.yaml` and our own emitters
rebuild it in house style. Read the `# NOTE:` lines it writes — they mark
everything it could not normalise provably.

### T2 — generate from the datasheet (needs this session, costs tokens)

Only when T0 and T1 have failed and you have the actual datasheet PDF.

```
kifab generate <MPN> --datasheet <file.pdf> --provider claude-code
```

Page selection is automatic, local and free — only the pin-table and
mechanical-drawing pages are sent. Do not paste the whole datasheet into
context yourself; that is the expensive mistake this command exists to avoid.

### T3 — write the YAML by hand

Always available, needs no model. See `parts/*.yaml` for the shape, and
`skills/datasheet/SKILL.md` for how to read a mechanical drawing into it.

## The review gate — this is your actual job

`kifab generate` writes **nothing** to the user's library. It stages a proposal
under `runs/<MPN>/`. Before suggesting acceptance:

1. Read `runs/<MPN>/proposal/<MPN>.yaml` and show the user the **pin table** as
   a table, not as YAML.
2. Report every `# NOTE:` the model wrote. These are the places it was unsure.
3. Run `kifab audit runs/<MPN>/` and say what it found.
4. Point at `runs/<MPN>/proposal/preview/` for the rendered SVG.
5. State the three things most likely to be wrong: the exposed-pad size, the
   electrical type of each supply pin, and pin 1's position relative to the
   package marker.

Only then: `kifab accept runs/<MPN>/`. It re-runs every validator and the audit
and refuses on any error — so if it refuses, do not work around it.

## Always finish with

```
kifab build parts/ -o build
kifab check parts/ build/ --strict
```

A part that does not pass `--strict` is not done.

## Rules

* **Never invent a dimension.** If the drawing does not state it, say so.
* **Never edit a generated `.kicad_mod` to make a check pass.** Fix the YAML.
* A wrong pad ships a scrapped board. Prefer refusing to guessing, every time.
