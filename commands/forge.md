---
description: Turn a manufacturer part number into a validated KiCad 9 symbol and footprint.
argument-hint: <MPN> [--datasheet path.pdf]
---

Forge the KiCad part for: **$ARGUMENTS**

Follow the `forge` skill. In short, and in this order:

1. **Reuse before you generate.** `kifab search <MPN> --package '<package exactly
   as the datasheet states it>'`. Adopt a CONFIDENT hit. Never silently adopt a
   REVIEW hit — a body-size match is not a package match.
2. **LCSC/EasyEDA next** if the part is JLCPCB-assembled: `kifab lcsc <MPN>`.
3. **Generate from the datasheet last** — `kifab generate <MPN> --datasheet
   <pdf>` — and only with a datasheet in hand. Nothing reaches the user's
   library until they have seen the proposal and the SVG preview.
4. **Always finish with the gates:** `kifab build parts/ && kifab check parts/
   build/ --strict`. Report what the validators said, not what you expect them
   to say.

If no argument was given, ask for the MPN and the package as the datasheet
states it.
