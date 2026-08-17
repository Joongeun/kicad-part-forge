"""LLM providers, behind one interface, selected by configuration.

| Provider           | For                          | Cost to the user      |
|--------------------|------------------------------|-----------------------|
| `claude-code`      | subscription holders (default)| nothing beyond the plan |
| `api-key`          | pay-as-you-go, CI, scripting | their own API bill    |
| `none`             | deterministic only           | nothing               |

Importing this package pulls in no HTTP client and no SDK: `ApiKeyProvider`
imports `anthropic` lazily, inside the call. That keeps the promise that the
deterministic core has no LLM dependency an actual property of the code rather
than a statement in a README.
"""

from __future__ import annotations

from pathlib import Path

from .apikey import ApiKeyProvider
from .base import (
    EMIT_IR,
    EXTRACTION_TOOLS,
    READ_PDF,
    ExtractionRequest,
    ExtractionResult,
    LLMProvider,
    LLMUnavailable,
    ProviderError,
    extract_yaml,
)
from .claudecode import ClaudeCodeProvider
from .null import NullProvider
from .sandbox import Sandbox, SandboxError
from .transcript import TRANSCRIPT_NAME, Transcript

PROVIDERS: dict[str, type[LLMProvider]] = {
    ClaudeCodeProvider.name: ClaudeCodeProvider,
    ApiKeyProvider.name: ApiKeyProvider,
    NullProvider.name: NullProvider,
    "none": NullProvider,
}

DEFAULT_PROVIDER = ClaudeCodeProvider.name


def make_provider(
    name: str, *, run_dir: Path, **kwargs
) -> LLMProvider:
    """Build the named provider over the run's own sandbox and transcript."""
    try:
        cls = PROVIDERS[name]
    except KeyError:
        raise ValueError(
            f"unknown provider {name!r}; known: "
            f"{sorted(set(PROVIDERS) - {'null'})}"
        ) from None
    run_dir = Path(run_dir)
    sandbox = Sandbox(
        root=run_dir / "scratch",
        transcript=Transcript(run_dir / TRANSCRIPT_NAME),
    )
    return cls(sandbox, **kwargs)


__all__ = [
    "DEFAULT_PROVIDER",
    "EMIT_IR",
    "EXTRACTION_TOOLS",
    "PROVIDERS",
    "READ_PDF",
    "TRANSCRIPT_NAME",
    "ApiKeyProvider",
    "ClaudeCodeProvider",
    "ExtractionRequest",
    "ExtractionResult",
    "LLMProvider",
    "LLMUnavailable",
    "NullProvider",
    "ProviderError",
    "Sandbox",
    "SandboxError",
    "Transcript",
    "extract_yaml",
    "make_provider",
]
