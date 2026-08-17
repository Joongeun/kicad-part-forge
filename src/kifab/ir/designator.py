"""How a pad/pin number is read out of YAML.

One rule, shared by `Pin.number` and `Pad.number`, because the same key
behaving differently in the same file is a trap: Phase 2 found that
`number: 1` was valid for a pin and invalid for a pad, so a hand-edited part
failed to load for a reason the error message could not explain.

The rule: the value is a **string**, because KiCad pad numbers are not all
integers (BGA `A1`, exposed pad `EP`, `MP` for a mounting pad) — but a YAML
author who writes the common case `number: 1` gets it coerced, not rejected.
A non-integral float is refused rather than truncated: `number: 1.5` is a typo
in every real datasheet, and silently reading it as pad 1 is exactly the kind
of quiet wrongness this project exists to prevent.
"""

from __future__ import annotations


def coerce_designator(value: object) -> object:
    """Accept a bare YAML integer for a pad/pin number; keep everything else."""
    if isinstance(value, bool):
        raise ValueError(f"{value!r} is not a pad/pin number")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(
                f"{value!r} is not a whole number; write a pad/pin number as an "
                'integer (1) or a quoted string ("A1", "EP")'
            )
        return str(int(value))
    return value


def require_non_empty(value: str, label: str) -> str:
    if not value.strip():
        raise ValueError(f"{label} must not be empty")
    return value
