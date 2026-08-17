"""Default provider: run the extraction inside the user's Claude Code session.

This is the answer to the constraint the whole project was designed around —
users have Claude *subscriptions*, not API credits. Running as Claude Code
costs them nothing beyond the plan they already pay for, and costs the author
nothing at all.

Isolation is enforced by the argv, not by asking:

* `--add-dir <sandbox>` and `cwd=<sandbox>` — the only directory in scope;
* `--allowed-tools Read` — the one tool the extraction needs;
* `--disallowed-tools` naming every tool that could reach a library, the web or
  a shell, so a permissive default configuration cannot re-open them.

`build_argv` is a pure function and is unit-tested; the subprocess run itself
is marked `live`, because it needs a Claude Code installation.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

from .base import ExtractionRequest, LLMProvider, LLMUnavailable, ProviderError

CLI = "claude"

#: Everything that could reach outside the sandbox. Named explicitly so that a
#: new tool appearing in Claude Code is a test failure, not a silent hole.
DENIED_TOOLS = (
    "Bash",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
    "Agent",
    "Edit",
    "Write",
    "NotebookEdit",
)

ALLOWED_TOOLS = ("Read",)


def build_argv(
    *,
    prompt_file: str,
    sandbox: str,
    model: str | None = None,
    cli: str = CLI,
) -> list[str]:
    """The exact command line. Isolation lives here, in one readable list."""
    argv = [
        cli,
        "-p",
        f"@{prompt_file}",
        "--output-format",
        "json",
        "--add-dir",
        sandbox,
        "--allowed-tools",
        ",".join(ALLOWED_TOOLS),
        "--disallowed-tools",
        ",".join(DENIED_TOOLS),
        "--max-turns",
        "12",
    ]
    if model:
        argv += ["--model", model]
    return argv


class ClaudeCodeProvider(LLMProvider):
    name = "claude-code"

    def __init__(self, sandbox, *, model: str | None = None, cli: str = CLI, timeout: int = 900, **kwargs):
        super().__init__(sandbox, **kwargs)
        self.model = model or os.environ.get("KIFAB_MODEL")
        self.cli = cli
        self.timeout = timeout

    def _run(self, request: ExtractionRequest) -> str:
        if shutil.which(self.cli) is None:
            raise LLMUnavailable(
                f"the `{self.cli}` command was not found on PATH. Install "
                "Claude Code, or use `--provider api-key` with your own "
                "Anthropic key, or `--provider none` to stay deterministic."
            )

        prompt_path = self.sandbox.write_text("prompt.md", request.instructions)
        argv = build_argv(
            prompt_file=prompt_path.name,
            sandbox=str(self.sandbox.root),
            model=self.model,
            cli=self.cli,
        )
        self.sandbox.transcript.record(
            "provider_exec", provider=self.name, argv=argv, cwd=str(self.sandbox.root)
        )

        try:
            run = subprocess.run(
                argv,
                cwd=self.sandbox.root,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                f"{self.cli} did not finish within {self.timeout}s"
            ) from exc

        if run.returncode != 0:
            raise ProviderError(
                f"{self.cli} exited {run.returncode}: {run.stderr.strip()[:500]}"
            )
        return parse_response(run.stdout)


def parse_response(stdout: str) -> str:
    """Pull the assistant text out of `claude -p --output-format json`.

    Falls back to the raw stdout: an output format that changes shape should
    degrade to "we got something" rather than to a crash, and the transcript
    keeps the original either way.
    """
    text = stdout.strip()
    if not text:
        raise ProviderError("claude produced no output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(payload, dict):
        for key in ("result", "text", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return text
