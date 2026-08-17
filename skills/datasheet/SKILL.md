---
name: datasheet
description: Read a component datasheet's pin table and package mechanical drawing into a kifab Part IR YAML file. Use when converting a datasheet PDF into pin and package data, or when checking a generated part against its drawing.
---

# datasheet — drawing in, Part IR out

The one rule: **state what the drawing states.** A dimension you remember,
infer, or average is the failure this whole pipeline exists to prevent. If a
number is not on the page, write `# NOTE: the drawing does not state <X>` and
leave the field out.

## Pages worth reading

`kifab generate` already selects these automatically. Doing it by hand, you
want exactly two kinds of page:

* the **pin function table** — number, name, type, description;
* the **package outline drawing** — the dimension table with `D`, `E`, `e`,
  `b`, `L`, `A`, and the JEDEC/JEITA outline reference.

Ignore everything else. Electrical characteristics and typical performance
curves contain nothing you need and a great deal you can misread.

## Reading the dimension table

| Drawing symbol | What it is | kifab field |
|---|---|---|
| `D`, `E` | body dimensions | `body: {x, y}` (+ `body_tolerance`) |
| `e` | terminal pitch | `pitch` |
| `b` | terminal width | `lead_width` |
| `L` | terminal length / foot length | `lead_length` |
| `D1`, `E1` | exposed pad | `exposed_pad: {size_x, size_y}` |
| lead span (gull-wing only) | outside-to-outside across the leads | `lead_span` |

Write a toleranced dimension as `"2.90 .. 3.10"`. Write a bare number **only**
when the drawing marks it BSC or "basic" — a bare number is treated as exact,
which is optimistic for anything else and makes the computed lands too tight.

## Choosing the family

* Terminals **flush with the body edge**, no leads sticking out: `quad_no_lead`
  (QFN, four sides) or `dual_no_lead` (DFN/SON, two sides). The body dimension
  *is* the lead span. If the drawing shows the terminals set back from the
  edge, add `pull_back`.
* Leads that **extend past the body**: `quad_gullwing` (QFP) or `dual_gullwing`
  (SOIC/SOP/SOT). These need `lead_span`, **not** the body size — using the
  body size pulls every land inward by the lead length.
* Chip / two-terminal parts (resistors, capacitors, ferrites): use `custom`
  with the recommended land pattern stated directly. kifab does not own
  two-terminal IPC maths and will not guess it.
* A drawing that gives only a recommended land pattern: `custom`.

For a rectangular QFN, count `pins_x` (lands on **each** of the top and bottom
rows) and `pins_y` (each of the left and right columns) off the drawing. Do not
divide the total by four.

## Pin numbering and orientation

KiCad's footprint frame has **+y down**. Pin 1 is the top of the left column,
and numbering runs counter-clockwise viewed from the top. Check pin 1 against
the drawing's own marker (a dot, a chamfer, or a chamfered corner terminal) —
this is the single most common orientation error, and it is invisible in the
3D view.

## Electrical types

ERC depends on these, so do not default everything to `passive`:

| Pin | Type |
|---|---|
| supplies and grounds (VDD, VCC, VSS, GND, AVDD) | `power_in` |
| a regulator's or reference's output | `power_out` |
| enables, resets, mode selects, digital inputs | `input` |
| digital outputs, flags | `output` |
| I/O, bus lines | `bidirectional` |
| RF ports, matching pins, exposed pads | `passive` |
| pins the datasheet says must be left open | `no_connect` |

The exposed pad gets a pin like any other — usually `name: EP`, `passive` (or
`power_in` when the datasheet ties it to a supply).

## Before you call it done

```
kifab check parts/<MPN>.yaml --strict
```

Then read the numbers back off the drawing one more time. The linter can prove
a footprint is self-consistent; only you can prove it matches the part.
