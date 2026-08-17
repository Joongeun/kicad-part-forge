"""The provider that does nothing, on purpose.

`NullProvider` is the default in every context where an LLM has not been asked
for, and it is the negative control for the whole design: with it selected,
`--force-tier=generate` must fail loudly. A tool that quietly produced a
plausible footprint when it had no way to read the datasheet would be worse
than useless — it would be actively dangerous, because the output looks exactly
like the real thing.

Everything else in kifab — T0 local search, T1 LCSC import, T3 hand-written
YAML, the emitters and all validators — works perfectly well with this
selected. That is the point.
"""

from __future__ import annotations

from .base import ExtractionRequest, LLMProvider, LLMUnavailable


class NullProvider(LLMProvider):
    name = "null"
    available = False

    def _run(self, request: ExtractionRequest) -> str:
        raise LLMUnavailable(
            f"no LLM provider is configured, so {request.mpn} cannot be "
            "generated from its datasheet (tier T2 is the only tier that "
            "needs one).\n"
            "  * pick an existing part instead:  kifab search "
            f"{request.mpn} --package '<package from the datasheet>'\n"
            "  * or import it from LCSC:         kifab lcsc "
            f"{request.mpn}\n"
            "  * or write parts/"
            f"{request.mpn}.yaml by hand — tier T3 needs no model at all\n"
            "  * or enable a provider:           kifab generate ... "
            "--provider claude-code | --provider api-key\n"
            "kifab will not guess pad geometry."
        )
