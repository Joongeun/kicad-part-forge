"""Bring-your-own-key provider: the user's own Anthropic API key, at cost.

For the pay-as-you-go audience and for CI. The key lives in the user's own
environment or config file and is never transmitted anywhere but Anthropic.

Isolation here is total and needs no policing: the request carries **no tool
definitions at all**, so the model has no mechanism to read a file, search a
library or fetch a URL. The only input is the page slice
`kifab.pdf.select` chose, and the only output is text.
"""

from __future__ import annotations

import base64
import os

from .base import ExtractionRequest, LLMProvider, LLMUnavailable, ProviderError

DEFAULT_MODEL = "claude-sonnet-4-6"
ENV_KEY = "ANTHROPIC_API_KEY"
ENV_MODEL = "KIFAB_MODEL"


class ApiKeyProvider(LLMProvider):
    name = "api-key"

    def __init__(self, sandbox, *, api_key: str | None = None, model: str | None = None, **kwargs):
        super().__init__(sandbox, **kwargs)
        self.api_key = api_key or os.environ.get(ENV_KEY, "")
        self.model = model or os.environ.get(ENV_MODEL) or DEFAULT_MODEL

    def _client(self):
        if not self.api_key:
            raise LLMUnavailable(
                f"no Anthropic API key: set {ENV_KEY}, or use "
                "`--provider claude-code` to run inside a Claude Code session "
                "on an existing subscription instead."
            )
        try:
            import anthropic  # noqa: PLC0415 — optional dependency, by design
        except ImportError as exc:
            raise LLMUnavailable(
                "the `anthropic` package is not installed. It is an optional "
                "extra so that the deterministic core has no LLM dependency: "
                "install it with `uv pip install 'kifab[api]'`."
            ) from exc
        return anthropic.Anthropic(api_key=self.api_key)

    def _run(self, request: ExtractionRequest) -> str:
        client = self._client()
        document = base64.standard_b64encode(request.pdf).decode("ascii")
        try:
            message = client.messages.create(
                model=self.model,
                max_tokens=8000,
                system=SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": document,
                                },
                            },
                            {"type": "text", "text": request.instructions},
                        ],
                    }
                ],
                # No `tools` key. The model cannot call anything.
            )
        except Exception as exc:  # network, auth, rate limit
            raise ProviderError(f"the Anthropic API call failed: {exc}") from exc

        parts = [
            block.text
            for block in message.content
            if getattr(block, "type", "") == "text"
        ]
        if not parts:
            raise ProviderError("the model returned no text")
        return "\n".join(parts)


SYSTEM_PROMPT = (
    "You are reading a component datasheet and producing a kifab Part IR "
    "document in YAML. You have been given only the pages that carry the pin "
    "table and the mechanical drawing. Report every dimension exactly as the "
    "drawing states it, including its tolerance. If a dimension you need is "
    "not on the pages you were given, say so in a `# NOTE:` comment rather "
    "than supplying a value from memory — a remembered dimension is the one "
    "failure mode this whole pipeline exists to prevent. Answer with the YAML "
    "document and nothing else."
)
