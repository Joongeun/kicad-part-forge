"""The auditor: prove the isolation held, and prove the auditor can fail.

An auditor that has never rejected anything is not evidence. Each of the three
violation classes therefore has a test that stages it and asserts the audit
catches it, alongside the clean case.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fakes import CannedProvider, CheatingProvider
from pdfs import datasheet

from kifab.audit import (
    AUDIT_ESCAPED_SANDBOX,
    AUDIT_LIBRARY_READ,
    AUDIT_NO_TRANSCRIPT,
    AUDIT_REFUSED,
    AUDIT_UNPARSEABLE,
    AUDIT_VENDOR_FETCH,
    audit_run,
    trace,
)
from kifab.generate import GenerationRequest, generate
from kifab.llm import Sandbox, Transcript
from kifab.llm.transcript import TRANSCRIPT_NAME


def _run_dir(tmp_path: Path, entries: list[dict]) -> Path:
    run_dir = tmp_path / "runs" / "PART"
    (run_dir / "scratch").mkdir(parents=True)
    with (run_dir / TRANSCRIPT_NAME).open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
    return run_dir


def _generate(tmp_path: Path, provider_cls, **kwargs) -> Path:
    run_dir = tmp_path / "runs" / "XYZ1234"
    run_dir.mkdir(parents=True)
    sandbox = Sandbox(
        root=run_dir / "scratch", transcript=Transcript(run_dir / TRANSCRIPT_NAME)
    )
    generate(
        GenerationRequest(mpn="XYZ1234", datasheet=datasheet()),
        provider=provider_cls(sandbox, **kwargs),
        run_dir=run_dir,
    )
    return run_dir


# -- the clean case -------------------------------------------------------


def test_a_real_isolated_run_audits_clean(tmp_path: Path) -> None:
    report = audit_run(_generate(tmp_path, CannedProvider))
    assert report.ok(), report.format(verbose=True)
    assert report.errors == 0


def test_the_trace_is_readable_by_a_human(tmp_path: Path) -> None:
    """The audit is itself a deliverable: you read what the model did."""
    text = trace(_generate(tmp_path, CannedProvider))
    assert "provider_call" in text and "provider_result" in text
    assert "sandbox_write" in text


# -- the three violations -------------------------------------------------


def test_reading_a_kicad_library_file_fails_the_run(tmp_path: Path) -> None:
    report = audit_run(_generate(tmp_path, CheatingProvider, how="library"))
    assert not report.ok()
    assert AUDIT_LIBRARY_READ in report.checks_fired()


@pytest.mark.parametrize(
    "url",
    [
        "https://www.snapeda.com/parts/LTC5552/",
        "https://www.snapmagic.com/x",
        "https://www.ultralibrarian.com/x",
        "https://componentsearchengine.com/x",
        "https://easyeda.com/x",
        "https://lcsc.com/product-detail/C123.html",
    ],
)
def test_fetching_from_a_library_aggregator_fails_the_run(tmp_path: Path, url) -> None:
    run_dir = _run_dir(tmp_path, [{"event": "tool_call", "url": url}])
    report = audit_run(run_dir)
    assert not report.ok()
    assert AUDIT_VENDOR_FETCH in report.checks_fired()


def test_a_successful_read_outside_the_scratch_dir_fails_the_run(tmp_path: Path) -> None:
    run_dir = _run_dir(
        tmp_path, [{"event": "tool_call", "tool": "read_pdf", "path": "/etc/passwd"}]
    )
    report = audit_run(run_dir)
    assert not report.ok()
    assert AUDIT_ESCAPED_SANDBOX in report.checks_fired()


def test_a_library_path_hidden_in_an_argv_is_still_caught(tmp_path: Path) -> None:
    """Violations do not have to arrive in a field named `path`."""
    run_dir = _run_dir(
        tmp_path,
        [
            {
                "event": "provider_exec",
                "argv": ["claude", "-p", "read SOIC-8.kicad_mod"],
            }
        ],
    )
    assert AUDIT_LIBRARY_READ in audit_run(run_dir).checks_fired()


# -- what is *not* a violation --------------------------------------------


def test_a_refusal_is_evidence_the_constraint_fired_not_a_violation(
    tmp_path: Path,
) -> None:
    run_dir = _run_dir(
        tmp_path,
        [
            {
                "event": "refused",
                "reason": "KiCad library file",
                "path": "/libs/DFN-12.kicad_mod",
            },
            {"event": "provider_result", "provider": "canned"},
        ],
    )
    report = audit_run(run_dir)
    assert report.ok(), report.format(verbose=True)
    assert AUDIT_REFUSED in report.checks_fired()


# -- the audit's own failure modes ----------------------------------------


def test_a_missing_transcript_fails_rather_than_passing_vacuously(
    tmp_path: Path,
) -> None:
    """No record is not the same as a clean record."""
    run_dir = tmp_path / "runs" / "NOTHING"
    run_dir.mkdir(parents=True)
    report = audit_run(run_dir)
    assert not report.ok()
    assert AUDIT_NO_TRANSCRIPT in report.checks_fired()


def test_an_empty_transcript_also_fails(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path, [])
    assert not audit_run(run_dir).ok()


def test_a_tampered_transcript_line_fails(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path, [{"event": "provider_call"}])
    with (run_dir / TRANSCRIPT_NAME).open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    report = audit_run(run_dir)
    assert not report.ok()
    assert AUDIT_UNPARSEABLE in report.checks_fired()
