"""The provider interface, the sandbox, and the transcript.

The assertions here are about *structure*, not about model quality: a provider
that could reach outside its sandbox would invalidate every blind-holdout run
ever made with it, and no amount of grading the output would reveal that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kifab.llm import (
    DEFAULT_PROVIDER,
    EXTRACTION_TOOLS,
    ApiKeyProvider,
    ClaudeCodeProvider,
    ExtractionRequest,
    LLMProvider,
    LLMUnavailable,
    NullProvider,
    Sandbox,
    SandboxError,
    Transcript,
    extract_yaml,
    make_provider,
)
from kifab.generate.prompt import build_instructions
from kifab.llm.base import slice_name
from kifab.llm.claudecode import ALLOWED_TOOLS, DENIED_TOOLS, build_argv, parse_response

PDF = b"%PDF-1.4 pretend"


class RecordingProvider(LLMProvider):
    """A provider that returns a canned answer, for testing the plumbing."""

    name = "recording"

    def __init__(self, sandbox, *, reply: str = "mpn: X\n", **kwargs):
        super().__init__(sandbox, **kwargs)
        self.reply = reply
        self.seen: list[ExtractionRequest] = []

    def _run(self, request: ExtractionRequest) -> str:
        self.seen.append(request)
        return self.reply


def _sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(
        root=tmp_path / "scratch", transcript=Transcript(tmp_path / "transcript.jsonl")
    )


# -- the interface --------------------------------------------------------


def test_the_default_provider_is_the_subscription_one() -> None:
    """Users have subscriptions, not credits. That decides the default."""
    assert DEFAULT_PROVIDER == "claude-code"


def test_all_three_providers_are_selectable_by_name(tmp_path: Path) -> None:
    for name, cls in (
        ("claude-code", ClaudeCodeProvider),
        ("api-key", ApiKeyProvider),
        ("none", NullProvider),
    ):
        assert isinstance(make_provider(name, run_dir=tmp_path / name), cls)
    with pytest.raises(ValueError, match="unknown provider"):
        make_provider("gpt", run_dir=tmp_path)


def test_the_tool_allowlist_is_exactly_two_tools_and_cannot_be_widened(
    tmp_path: Path,
) -> None:
    """This is the structural half of the anti-cheating design."""
    assert set(EXTRACTION_TOOLS) == {"read_pdf", "emit_ir"}
    sandbox = _sandbox(tmp_path)
    with pytest.raises(ValueError, match="not in the extraction tool allowlist"):
        RecordingProvider(sandbox, tools=frozenset({"read_pdf", "glob"}))
    with pytest.raises(ValueError, match="not in the extraction tool allowlist"):
        RecordingProvider(sandbox, tools=frozenset({"web_fetch"}))


def test_an_extraction_request_carries_only_the_mpn_and_the_datasheet() -> None:
    """If it cannot name the answer, it cannot copy it."""
    fields = set(ExtractionRequest.__dataclass_fields__)
    assert fields == {"mpn", "pdf", "pages", "instructions"}
    assert not any("index" in f or "library" in f or "root" in f for f in fields)


def test_a_request_without_a_datasheet_is_refused() -> None:
    with pytest.raises(ValueError, match="datasheet bytes"):
        ExtractionRequest(mpn="X", pdf=b"")
    with pytest.raises(ValueError, match="needs an MPN"):
        ExtractionRequest(mpn="  ", pdf=PDF)


# -- the null provider (the negative control) -----------------------------


def test_null_provider_fails_loudly_and_says_what_to_do_instead(tmp_path: Path) -> None:
    provider = NullProvider(_sandbox(tmp_path))
    with pytest.raises(LLMUnavailable) as excinfo:
        provider.extract(ExtractionRequest(mpn="LTC5552", pdf=PDF))
    message = str(excinfo.value)
    assert "will not guess pad geometry" in message
    # It must point at the tiers that still work, not just refuse.
    assert "kifab search" in message and "kifab lcsc" in message
    assert not NullProvider.available


def test_null_provider_records_the_refusal(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    provider = NullProvider(sandbox)
    with pytest.raises(LLMUnavailable):
        provider.extract(ExtractionRequest(mpn="X", pdf=PDF))
    events = [e["event"] for e in sandbox.transcript.entries()]
    assert "provider_unavailable" in events


# -- the sandbox ----------------------------------------------------------


def test_sandbox_refuses_paths_outside_itself(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    for escape in ("/etc/passwd", "../../secrets", str(tmp_path / "elsewhere")):
        with pytest.raises(SandboxError, match="outside the run sandbox"):
            sandbox.resolve(escape)


def test_sandbox_refuses_kicad_library_files_even_inside_itself(tmp_path: Path) -> None:
    """No legitimate extraction reads an existing footprint."""
    sandbox = _sandbox(tmp_path)
    for name in ("QFN-12.kicad_mod", "kifab.kicad_sym", "Package_DFN_QFN.pretty"):
        with pytest.raises(SandboxError, match="refusing to read"):
            sandbox.resolve(name)


def test_refusals_are_recorded_as_evidence(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(SandboxError):
        sandbox.resolve("/etc/passwd")
    entries = sandbox.transcript.entries()
    assert [e["event"] for e in entries] == ["refused"]
    assert "outside the run sandbox" in entries[0]["reason"]


def test_sandbox_round_trips_its_own_files(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_bytes("slice.pdf", PDF)
    assert sandbox.read_bytes("slice.pdf") == PDF
    events = [e["event"] for e in sandbox.transcript.entries()]
    assert events == ["sandbox_write", "tool_call"]


# -- the transcript -------------------------------------------------------


def test_every_provider_call_is_transcribed(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    provider = RecordingProvider(sandbox, reply="```yaml\nmpn: X\n```")
    result = provider.extract(
        ExtractionRequest(mpn="X", pdf=PDF, pages=[1, 3], instructions="hi")
    )
    assert result.yaml_text.strip() == "mpn: X"

    entries = sandbox.transcript.entries()
    events = [e["event"] for e in entries]
    assert events == ["sandbox_write", "provider_call", "provider_result"]
    call = entries[1]
    assert call["mpn"] == "X" and call["pages"] == [1, 3]
    assert sorted(call["tools"]) == ["emit_ir", "read_pdf"]


def test_a_malformed_transcript_line_is_surfaced_not_skipped(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_text('{"event": "tool_call"}\nnot json\n', encoding="utf-8")
    entries = Transcript(path).entries()
    assert [e["event"] for e in entries] == ["tool_call", "unparseable"]


def test_transcript_is_append_only_json_lines(tmp_path: Path) -> None:
    transcript = Transcript(tmp_path / "t.jsonl")
    transcript.record("a", n=1)
    transcript.record("b", n=2)
    lines = (tmp_path / "t.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["a", "b"]


# -- response parsing -----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("mpn: X", "mpn: X"),
        ("```yaml\nmpn: X\n```", "mpn: X"),
        ("Here you go:\n```\nmpn: X\n```\nHope that helps", "mpn: X"),
        ("```yml\nmpn: X\n```", "mpn: X"),
    ],
)
def test_extract_yaml_survives_the_ways_models_wrap_output(raw, expected) -> None:
    assert extract_yaml(raw).strip() == expected


def test_extract_yaml_picks_the_longest_block() -> None:
    raw = "```\nshort\n```\nand\n```yaml\nmpn: X\nmanufacturer: ACME\n```"
    assert "manufacturer: ACME" in extract_yaml(raw)


# -- the Claude Code argv (isolation lives in this list) ------------------


def test_claude_code_argv_denies_every_escape_route() -> None:
    argv = build_argv(prompt_file="prompt.md", sandbox="/tmp/run/scratch")
    assert argv[0] == "claude" and "-p" in argv

    allowed = argv[argv.index("--allowed-tools") + 1].split(",")
    denied = argv[argv.index("--disallowed-tools") + 1].split(",")
    assert allowed == list(ALLOWED_TOOLS) == ["Read"]
    # The three routes to the answer: a shell, a file search, the web.
    for tool in ("Bash", "Glob", "Grep", "WebFetch", "WebSearch", "Task"):
        assert tool in denied
    assert set(denied) == set(DENIED_TOOLS)
    assert argv[argv.index("--add-dir") + 1] == "/tmp/run/scratch"


def test_claude_code_response_parsing() -> None:
    assert parse_response('{"result": "mpn: X"}') == "mpn: X"
    assert parse_response("plain text") == "plain text"


# -- the prompt has to name the pages the provider actually delivered -------
#
# Regression: the template said "the attached datasheet pages" for every
# provider, but `claude -p` attaches nothing — the slice is only on disk. With
# `Glob` and `Bash` denied, the model could not find the file, and refused. The
# refusal was correct behaviour on a prompt that was lying to it.


def test_claude_code_prompt_names_the_slice_it_can_read(tmp_path: Path) -> None:
    provider = ClaudeCodeProvider(_sandbox(tmp_path))
    clause = provider.source_clause("LTC5552")
    assert slice_name("LTC5552") in clause
    assert "attached" not in clause

    prompt = build_instructions(
        mpn="LTC5552", pages=[1, 13], total_pages=18, source=clause
    )
    assert "LTC5552.slice.pdf" in prompt
    assert "attached datasheet pages" not in prompt


def test_api_key_prompt_still_says_attached(tmp_path: Path) -> None:
    # This provider really does attach the PDF, so pointing it at a file would
    # be the same bug in the other direction.
    provider = ApiKeyProvider(_sandbox(tmp_path), api_key="x")
    assert provider.source_clause("LTC5552") == "the attached datasheet pages"


def test_the_prompt_names_the_file_extract_actually_writes(tmp_path: Path) -> None:
    """The two halves of the bug, asserted against each other."""
    sandbox = _sandbox(tmp_path)
    provider = NullProvider(sandbox)
    request = ExtractionRequest(mpn="LTC5552", pdf=PDF, pages=[1], instructions="x")
    with pytest.raises(LLMUnavailable):
        provider.extract(request)
    written = {p.name for p in sandbox.root.iterdir()}
    assert slice_name("LTC5552") in written


def test_claude_code_reports_a_missing_cli_as_unavailable(tmp_path: Path) -> None:
    provider = ClaudeCodeProvider(_sandbox(tmp_path), cli="definitely-not-installed")
    with pytest.raises(LLMUnavailable, match="was not found on PATH"):
        provider.extract(ExtractionRequest(mpn="X", pdf=PDF))


# -- the BYOK provider ----------------------------------------------------


def test_api_key_provider_without_a_key_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = ApiKeyProvider(_sandbox(tmp_path))
    with pytest.raises(LLMUnavailable, match="no Anthropic API key"):
        provider.extract(ExtractionRequest(mpn="X", pdf=PDF))


def test_importing_the_llm_package_pulls_in_no_sdk() -> None:
    """The deterministic core must not acquire an HTTP dependency by import."""
    import subprocess
    import sys

    code = (
        "import sys, kifab.llm; "
        "print(int(any(m == 'anthropic' or m.startswith('anthropic.') "
        "for m in sys.modules)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "0"
