"""The only patch of filesystem a provider is allowed to touch.

This is the structural half of the anti-cheating design. The generator is
constructed with a `Sandbox` and nothing else; there is no code path from a
provider to the library index, to `parts/`, or to KiCad's shipped footprints,
because no provider is ever handed a path outside this directory.

Two rules, both enforced here rather than requested in a prompt:

1. every path is resolved and must stay inside the sandbox root;
2. `*.kicad_mod` and `*.kicad_sym` are refused **even inside it** — no legitimate
   extraction reads an existing footprint, so a request to do so is either a bug
   or the failure mode the whole exercise is designed to detect.

Refusals are recorded. A refusal in the transcript is evidence the constraint
fired, not a violation of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .transcript import Transcript

#: Extensions no extraction has any business reading.
FORBIDDEN_SUFFIXES = (".kicad_mod", ".kicad_sym", ".pretty", ".kicad_pcb")


class SandboxError(PermissionError):
    """A provider tried to reach outside its sandbox."""


@dataclass
class Sandbox:
    """A run's scratch directory, plus the rules for reaching into it."""

    root: Path
    transcript: Transcript

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- path discipline --------------------------------------------------

    def resolve(self, name: str | Path) -> Path:
        """Resolve a name inside the sandbox, or refuse loudly."""
        candidate = Path(name)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self.root / candidate).resolve()

        if not resolved.is_relative_to(self.root):
            self.transcript.record(
                "refused",
                reason="path outside the run sandbox",
                path=str(resolved),
                sandbox=str(self.root),
            )
            raise SandboxError(
                f"{resolved} is outside the run sandbox {self.root}. The "
                "generator is constructed with the datasheet and the MPN and "
                "nothing else; if it needs this file, the design is wrong."
            )
        if resolved.name.endswith(FORBIDDEN_SUFFIXES):
            self.transcript.record(
                "refused",
                reason="KiCad library file",
                path=str(resolved),
            )
            raise SandboxError(
                f"refusing to read {resolved.name}: generation must not look "
                "at an existing symbol or footprint"
            )
        return resolved

    # -- io ---------------------------------------------------------------

    def write_bytes(self, name: str, data: bytes) -> Path:
        path = self.resolve(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.transcript.record(
            "sandbox_write", path=str(path), bytes=len(data), tool="write_scratch"
        )
        return path

    def write_text(self, name: str, text: str) -> Path:
        return self.write_bytes(name, text.encode("utf-8"))

    def read_bytes(self, name: str) -> bytes:
        path = self.resolve(name)
        data = path.read_bytes()
        self.transcript.record(
            "tool_call", tool="read_pdf", path=str(path), bytes=len(data)
        )
        return data
