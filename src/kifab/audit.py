"""Audit a generation run: prove the isolation held.

Structural isolation (`kifab.llm.sandbox`) makes cheating impossible by
construction. This module is the second layer — it reads what actually
happened and asserts it, because a constraint nobody checks is a constraint
that quietly stops holding the day someone refactors it.

Three things must be **absent** from `runs/<mpn>/transcript.jsonl`:

1. any read of a `*.kicad_mod` or `*.kicad_sym` — an existing footprint is the
   answer, and a generator that saw one has proved nothing;
2. any fetch of `snapeda | snapmagic | ultralibrarian | componentsearchengine |
   lcsc | easyeda` — the same answer, from someone else's library;
3. any successful read outside the run's own scratch directory.

A *refused* attempt is not a violation: it is the sandbox working, and it is
reported as a note. A successful one fails the run regardless of how good the
output looks.

Exposed as `kifab audit runs/<mpn>/`, which also prints the tool-call trace, so
a human can read what the model did rather than what it says it did.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .llm.transcript import TRANSCRIPT_NAME, read
from .validate.report import Report, Severity

#: Library aggregators. Downloading an answer is not generating one.
VENDOR_PATTERN = re.compile(
    r"snapeda|snapmagic|ultralibrarian|ultra-librarian|componentsearchengine|"
    r"\blcsc\b|easyeda|digikey\.com/.*symbol|mouser\.com/.*cad",
    re.I,
)

#: KiCad library artefacts. Reading one during generation is the failure.
LIBRARY_PATTERN = re.compile(r"\.kicad_mod\b|\.kicad_sym\b|\.pretty\b", re.I)

#: Keys whose values are paths or URLs worth inspecting.
_LOCATION_KEYS = ("path", "file", "url", "target", "source", "argv", "command")

AUDIT_LIBRARY_READ = "AUDIT001"
AUDIT_VENDOR_FETCH = "AUDIT002"
AUDIT_ESCAPED_SANDBOX = "AUDIT003"
AUDIT_NO_TRANSCRIPT = "AUDIT004"
AUDIT_REFUSED = "AUDIT005"
AUDIT_TRACE = "AUDIT006"
AUDIT_UNPARSEABLE = "AUDIT007"

#: Events that record something the run *did*, as opposed to something it was
#: stopped from doing.
_SUCCESSFUL_EVENTS = {
    "tool_call",
    "sandbox_write",
    "provider_exec",
    "provider_call",
    "provider_result",
}


def _locations(entry: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in _LOCATION_KEYS:
        value = entry.get(key)
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, (list, tuple)):
            out.extend(str(item) for item in value)
    return out


def audit_run(run_dir: Path, *, scratch: Path | None = None) -> Report:
    """Audit one `runs/<mpn>/` directory."""
    run_dir = Path(run_dir).resolve()
    scratch = (scratch or run_dir / "scratch").resolve()
    transcript_path = run_dir / TRANSCRIPT_NAME
    subject = run_dir.name

    report = Report()
    entries = read(transcript_path)

    if not transcript_path.exists() or not entries:
        report.add(
            AUDIT_NO_TRANSCRIPT,
            Severity.ERROR,
            f"no provider transcript at {transcript_path}. A run with no "
            "record of what it did cannot be audited, and an unaudited "
            "generation run is not evidence of anything.",
            subject=subject,
        )
        return report

    for entry in entries:
        event = str(entry.get("event", ""))

        if event == "unparseable":
            report.add(
                AUDIT_UNPARSEABLE,
                Severity.ERROR,
                f"transcript line {entry.get('line')} is not valid JSON: "
                f"{str(entry.get('raw', ''))[:120]!r}",
                subject=subject,
            )
            continue

        if event == "refused":
            report.add(
                AUDIT_REFUSED,
                Severity.INFO,
                f"sandbox refused {entry.get('path', '?')} "
                f"({entry.get('reason', 'no reason recorded')}) — the "
                "constraint fired, which is what it is for",
                subject=subject,
            )
            continue

        locations = _locations(entry)

        for value in locations:
            if LIBRARY_PATTERN.search(value):
                report.add(
                    AUDIT_LIBRARY_READ,
                    Severity.ERROR,
                    f"the run touched a KiCad library artefact: {value!r} "
                    f"(event {event!r}). The generated part may be a copy, so "
                    "this run proves nothing about generation.",
                    subject=subject,
                )
            if VENDOR_PATTERN.search(value):
                report.add(
                    AUDIT_VENDOR_FETCH,
                    Severity.ERROR,
                    f"the run reached a library aggregator: {value!r} "
                    f"(event {event!r}). Downloading an answer is not "
                    "generating one.",
                    subject=subject,
                )

        if event in _SUCCESSFUL_EVENTS:
            for key in ("path", "file"):
                value = entry.get(key)
                if not isinstance(value, str):
                    continue
                try:
                    resolved = Path(value).resolve()
                except OSError:  # pragma: no cover - exotic paths
                    resolved = Path(value)
                if not resolved.is_relative_to(scratch):
                    report.add(
                        AUDIT_ESCAPED_SANDBOX,
                        Severity.ERROR,
                        f"{event} succeeded on {value!r}, which is outside the "
                        f"run scratch directory {scratch}",
                        subject=subject,
                    )

    report.add(
        AUDIT_TRACE,
        Severity.INFO,
        f"{len(entries)} transcript event(s): "
        + ", ".join(sorted({str(e.get('event', '?')) for e in entries})),
        subject=subject,
    )
    return report


def trace(run_dir: Path) -> str:
    """The tool-call trace, in the order it happened, for a human to read."""
    entries = read(Path(run_dir) / TRANSCRIPT_NAME)
    if not entries:
        return f"(no transcript in {run_dir})"
    lines = []
    for entry in entries:
        event = entry.get("event", "?")
        detail = {
            k: v for k, v in entry.items() if k not in ("ts", "event") and v not in (None, "", [])
        }
        rendered = " ".join(f"{k}={v!r}" for k, v in sorted(detail.items()))
        lines.append(f"  {event:<22} {rendered}")
    return "\n".join(lines)
