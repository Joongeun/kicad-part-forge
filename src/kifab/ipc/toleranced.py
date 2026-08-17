"""Toleranced dimensions with RMS tolerance propagation.

IPC-7351B does not add tolerances linearly. When a dimension is derived from
others (e.g. the inner lead span S = L - 2T), the combined tolerance is the
root-sum-square of the contributors, not their sum. Statistically, independent
tolerances rarely all hit their extremes at once, so RMS gives a realistic
worst case instead of a pessimistic one — which is why RMS-derived pads are
smaller (and denser) than naive ones.

`ipc_tol_RMS` therefore tracks the RMS tolerance separately from the true
min/max span, and `minimum_RMS` / `maximum_RMS` are the extremes shrunk inward
by half the difference. This mirrors kilibs' `TolerancedSize` so our geometry
matches the official KiCad libraries; see tests/test_ipc.py for the
footprint-by-footprint comparison that holds us to it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Tol:
    """A dimension with a min/max span and an RMS-combined tolerance."""

    minimum: float
    maximum: float
    ipc_tol_RMS: float

    def __post_init__(self) -> None:
        if self.maximum < self.minimum:
            raise ValueError(
                f"maximum {self.maximum} < minimum {self.minimum} — "
                "tolerance range given backwards?"
            )

    # -- constructors -----------------------------------------------------

    @classmethod
    def exact(cls, value: float) -> Tol:
        """A dimension with no tolerance (e.g. a JEDEC basic dimension)."""
        return cls(value, value, 0.0)

    @classmethod
    def span(cls, minimum: float, maximum: float) -> Tol:
        """A dimension given as min..max, as datasheets usually state it."""
        return cls(minimum, maximum, maximum - minimum)

    @classmethod
    def plus_minus(cls, nominal: float, tolerance: float) -> Tol:
        return cls.span(nominal - tolerance, nominal + tolerance)

    @classmethod
    def parse(cls, text: str | float | int) -> Tol:
        """Parse KiCad's `a .. b` / `a .. nom .. b` / `a` dimension syntax."""
        if isinstance(text, (int, float)):
            return cls.exact(float(text))
        parts = [p.strip() for p in str(text).split("..")]
        if len(parts) == 1:
            return cls.exact(float(parts[0]))
        if len(parts) == 2:
            return cls.span(float(parts[0]), float(parts[1]))
        if len(parts) == 3:
            # min .. nominal .. max — the nominal does not affect IPC maths.
            return cls.span(float(parts[0]), float(parts[2]))
        raise ValueError(f"cannot parse toleranced dimension {text!r}")

    # -- derived properties ----------------------------------------------

    @property
    def nominal(self) -> float:
        return (self.minimum + self.maximum) / 2

    @property
    def ipc_tol(self) -> float:
        return self.maximum - self.minimum

    @property
    def _rms_inset(self) -> float:
        return (self.ipc_tol - self.ipc_tol_RMS) / 2

    @property
    def minimum_RMS(self) -> float:
        return self.minimum + self._rms_inset

    @property
    def maximum_RMS(self) -> float:
        return self.maximum - self._rms_inset

    # -- arithmetic -------------------------------------------------------

    def _combined(self, minimum: float, maximum: float, *rms: float) -> Tol:
        combined = math.sqrt(sum(r * r for r in rms))
        true_tol = maximum - minimum
        # Floating-point noise can push RMS a hair above the true tolerance;
        # clamp rather than fail, matching kilibs.
        return Tol(minimum, maximum, min(combined, true_tol))

    def __add__(self, other: Tol | float) -> Tol:
        if isinstance(other, (int, float)):
            return Tol(self.minimum + other, self.maximum + other, self.ipc_tol_RMS)
        return self._combined(
            self.minimum + other.minimum,
            self.maximum + other.maximum,
            self.ipc_tol_RMS,
            other.ipc_tol_RMS,
        )

    def __sub__(self, other: Tol | float) -> Tol:
        if isinstance(other, (int, float)):
            return Tol(self.minimum - other, self.maximum - other, self.ipc_tol_RMS)
        return self._combined(
            self.minimum - other.maximum,
            self.maximum - other.minimum,
            self.ipc_tol_RMS,
            other.ipc_tol_RMS,
        )

    def __mul__(self, factor: float) -> Tol:
        if not isinstance(factor, (int, float)):
            raise TypeError("Tol can only be multiplied by a number")
        return self._combined(
            self.minimum * factor,
            self.maximum * factor,
            self.ipc_tol_RMS * math.sqrt(factor),
        )

    __rmul__ = __mul__
