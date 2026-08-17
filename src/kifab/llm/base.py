"""The one interface the rest of kifab knows about.

Three implementations sit behind it — Claude Code (the default; costs a
subscription holder nothing extra), a bring-your-own-key API client, and none
at all. The deterministic core never constructs any of them: tiers T0, T1 and
T3 and every validator run to completion with no provider in the process.

The extraction contract is deliberately narrow, and that narrowness *is* the
anti-cheating design:

    ExtractionRequest = (mpn, datasheet page slice, page numbers, instructions)

No library index. No filesystem root. No search handle. A provider cannot reach
the answer because it was never given a way to name it — and the two tools it
may use are enumerated in `EXTRACTION_TOOLS`, checked at construction time.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import ClassVar

from .sandbox import Sandbox

#: Read the supplied PDF; emit IR YAML. That is the whole allowlist.
READ_PDF = "read_pdf"
EMIT_IR = "emit_ir"
EXTRACTION_TOOLS: frozenset[str] = frozenset({READ_PDF, EMIT_IR})


class LLMUnavailable(RuntimeError):
    """No model is configured, or the configured one cannot be reached.

    Raised — never swallowed. A tier that needs a model and does not have one
    must fail loudly; emitting something plausible instead is the single worst
    thing this project could do.
    """


class ProviderError(RuntimeError):
    """The provider ran but did not produce a usable answer."""


@dataclass(frozen=True)
class ExtractionRequest:
    """Everything the model gets. Note what is absent."""

    mpn: str
    pdf: bytes
    pages: list[int] = field(default_factory=list)
    instructions: str = ""

    def __post_init__(self) -> None:
        if not self.mpn.strip():
            raise ValueError("an extraction request needs an MPN")
        if not self.pdf:
            raise ValueError("an extraction request needs datasheet bytes")


@dataclass(frozen=True)
class ExtractionResult:
    """What came back: IR YAML, plus the raw response for the record."""

    yaml_text: str
    provider: str
    raw: str = ""


def slice_name(mpn: str) -> str:
    """The sandbox filename the page slice is written under.

    One definition, because two providers disagreeing about it is exactly the
    bug this function exists to prevent: `extract` writes the file and the
    prompt has to name the same one.
    """
    return f"{mpn}.slice.pdf"


class LLMProvider(abc.ABC):
    """Base class. Subclasses implement `_run` and nothing else."""

    name: ClassVar[str] = "abstract"

    #: Whether this provider can actually produce anything.
    available: ClassVar[bool] = True

    def source_clause(self, mpn: str) -> str:
        """How this provider delivers the pages, in the prompt's own words.

        Providers differ on this and the difference is not cosmetic: the API
        attaches the PDF to the message, while Claude Code can only `Read` it
        off disk. A prompt that says "attached" to a provider that attached
        nothing produces a model that correctly refuses — a real failure that
        looks like a safety feature.
        """
        return "the attached datasheet pages"

    def __init__(
        self, sandbox: Sandbox, *, tools: frozenset[str] = EXTRACTION_TOOLS
    ) -> None:
        extra = set(tools) - set(EXTRACTION_TOOLS)
        if extra:
            raise ValueError(
                f"{sorted(extra)} is not in the extraction tool allowlist "
                f"{sorted(EXTRACTION_TOOLS)}. Widening it is a design change, "
                "not a configuration change."
            )
        self.sandbox = sandbox
        self.tools = frozenset(tools)

    # -- the public call --------------------------------------------------

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """Turn a datasheet slice into IR YAML, recording everything."""
        self.sandbox.write_bytes(slice_name(request.mpn), request.pdf)
        self.sandbox.transcript.record(
            "provider_call",
            provider=self.name,
            mpn=request.mpn,
            pages=request.pages,
            pdf_bytes=len(request.pdf),
            tools=sorted(self.tools),
        )
        try:
            raw = self._run(request)
        except LLMUnavailable as exc:
            self.sandbox.transcript.record(
                "provider_unavailable", provider=self.name, error=str(exc)
            )
            raise
        except Exception as exc:
            self.sandbox.transcript.record(
                "provider_error", provider=self.name, error=str(exc)
            )
            raise
        yaml_text = extract_yaml(raw)
        self.sandbox.transcript.record(
            "provider_result",
            provider=self.name,
            tool=EMIT_IR,
            chars=len(raw),
            yaml_chars=len(yaml_text),
        )
        return ExtractionResult(yaml_text=yaml_text, provider=self.name, raw=raw)

    @abc.abstractmethod
    def _run(self, request: ExtractionRequest) -> str:
        """Return the model's raw response."""


def extract_yaml(raw: str) -> str:
    """Pull the YAML document out of a model response.

    Models fence their output more often than not, and a leading sentence of
    prose is common. Both are handled here rather than in a prompt instruction,
    because a parser is enforceable and an instruction is not.
    """
    text = raw.strip()
    if "```" in text:
        blocks: list[str] = []
        parts = text.split("```")
        for i in range(1, len(parts), 2):
            block = parts[i]
            first, _, rest = block.partition("\n")
            if first.strip().lower() in ("yaml", "yml", ""):
                blocks.append(rest)
            else:
                blocks.append(block)
        if blocks:
            return max(blocks, key=len).strip() + "\n"
    return text + "\n" if text else ""
