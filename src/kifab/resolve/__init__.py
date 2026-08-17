"""Resolver tiers.

* **T0** — `local` + `adopt`: reuse a part that already ships with KiCad.
* **T1** — `easyeda`: import from LCSC/EasyEDA. An *ingester*, not a
  generator — its data is normalised into the IR and re-emitted by our own
  emitters, so it never reaches the user's library directly.

`Candidate` is re-exported from `local` (a T0 library match). T1's own
`Candidate` (an LCSC search hit) is a different thing and stays in
`kifab.resolve.easyeda`.
"""

from .local import (
    REVIEW_FLOOR,
    Basis,
    Candidate,
    Confidence,
    LocalResolution,
    MatchSet,
    search,
    wildcard_equal,
)

__all__ = [
    "REVIEW_FLOOR",
    "Basis",
    "Candidate",
    "Confidence",
    "LocalResolution",
    "MatchSet",
    "search",
    "wildcard_equal",
]
