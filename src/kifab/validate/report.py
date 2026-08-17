"""What a check produces: findings, and what they mean.

Severity is the load-bearing part of this module, so it is defined once and
means exactly one thing:

* **error** — the part is wrong. It blocks: `kifab check` exits non-zero and
  `verify.sh` fails. Reserved for defects that are wrong under *any* house
  style — copper that overlaps, a pad the courtyard does not contain, a symbol
  and footprint that disagree about pin numbers, a file KiCad's own parser
  rejects.
* **warning** — the part is questionable but buildable. It never blocks unless
  the caller asks for `--strict`. Reserved for convention deviations and for
  heuristics, which is where every threshold-based rule lives.
* **info** — something a human should know that is not a judgement, including
  "this check could not run" (kicad-cli absent). Never blocks, ever.

The distinction is meaningful only if it is enforced, so `Report.ok` is the
single place that decides, and `Severity.blocks` is the only predicate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @property
    def blocks(self) -> bool:
        """True if this severity alone fails a build."""
        return self is Severity.ERROR

    @property
    def rank(self) -> int:
        return {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}[self]


#: Which representation a check read. A defect that exists in only one of them
#: is the reason both are validated, so every finding records where it was seen.
LAYER_IR = "ir"
LAYER_FOOTPRINT = "footprint"
LAYER_SYMBOL = "symbol"


@dataclass(frozen=True)
class Finding:
    """One actionable statement: which element, where, and why.

    `check` is a stable identifier (`GEO001`, `KLC-F5.3`) so CI can suppress or
    track a specific rule without matching on prose.
    """

    check: str
    severity: Severity
    message: str
    subject: str = ""
    where: str = ""
    layer: str = ""
    at: tuple[float, float] | None = None

    def to_dict(self) -> dict:
        return {
            "check": self.check,
            "severity": self.severity.value,
            "subject": self.subject,
            "where": self.where,
            "layer": self.layer,
            "at": list(self.at) if self.at is not None else None,
            "message": self.message,
        }

    def __str__(self) -> str:
        head = f"{self.severity.value:<7} {self.check:<9}"
        where = f"{self.where}: " if self.where else ""
        at = f"  at ({self.at[0]:g}, {self.at[1]:g})" if self.at is not None else ""
        return f"{head} {where}{self.message}{at}"


@dataclass
class Report:
    """Findings from one or more checks, plus the verdict."""

    findings: list[Finding] = field(default_factory=list)

    def add(
        self,
        check: str,
        severity: Severity,
        message: str,
        *,
        subject: str = "",
        where: str = "",
        layer: str = "",
        at: tuple[float, float] | None = None,
    ) -> None:
        self.findings.append(
            Finding(
                check=check,
                severity=severity,
                message=message,
                subject=subject,
                where=where,
                layer=layer,
                at=at,
            )
        )

    def extend(self, other: Report, *, subject: str | None = None) -> Report:
        """Absorb another report, optionally stamping a subject onto it."""
        for finding in other.findings:
            if subject is not None and not finding.subject:
                self.findings.append(
                    Finding(
                        check=finding.check,
                        severity=finding.severity,
                        message=finding.message,
                        subject=subject,
                        where=finding.where,
                        layer=finding.layer,
                        at=finding.at,
                    )
                )
            else:
                self.findings.append(finding)
        return self

    def __iter__(self):
        return iter(self.findings)

    def __len__(self) -> int:
        return len(self.findings)

    def of(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity is severity]

    def checks_fired(self) -> set[str]:
        return {f.check for f in self.findings}

    @property
    def errors(self) -> int:
        return len(self.of(Severity.ERROR))

    @property
    def warnings(self) -> int:
        return len(self.of(Severity.WARNING))

    @property
    def infos(self) -> int:
        return len(self.of(Severity.INFO))

    def ok(self, *, strict: bool = False) -> bool:
        """The verdict. `strict` promotes warnings to blocking, nothing else."""
        if self.errors:
            return False
        return not (strict and self.warnings)

    def sorted(self) -> list[Finding]:
        return sorted(
            self.findings, key=lambda f: (f.subject, f.severity.rank, f.check, f.where)
        )

    def to_dict(self, *, strict: bool = False) -> dict:
        return {
            "ok": self.ok(strict=strict),
            "strict": strict,
            "counts": {
                "error": self.errors,
                "warning": self.warnings,
                "info": self.infos,
            },
            "findings": [f.to_dict() for f in self.sorted()],
        }

    def format(self, *, verbose: bool = False) -> str:
        """Human output: grouped by subject, errors first."""
        lines: list[str] = []
        current = object()
        for finding in self.sorted():
            if finding.severity is Severity.INFO and not verbose:
                continue
            if finding.subject != current:
                current = finding.subject
                lines.append(finding.subject or "(unnamed)")
            lines.append(f"  {finding}")
        return "\n".join(lines)

    def summary(self) -> str:
        parts = [f"{self.errors} error(s)", f"{self.warnings} warning(s)"]
        if self.infos:
            parts.append(f"{self.infos} note(s)")
        return ", ".join(parts)
