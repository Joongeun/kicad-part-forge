"""Generate the `.kicad_httplib` file that points KiCad at Part-DB.

This is the *read* side and kifab never calls it. KiCad does: given this file,
KiCad's HTTP library plugin talks to

    {root_url}/{api_version}/categories.json
    {root_url}/{api_version}/parts/category/{id}.json
    {root_url}/{api_version}/parts/{id}.json

and Part-DB serves those from `KiCadApiController`, whose route prefix is
`/kicad-api/v1` under the usual locale segment. So `root_url` is the instance
plus `/en/kicad-api/` **and stops there**: the `v1` is contributed by KiCad from
`api_version`. Putting `v1` in the root produces `/v1/v1/` and a library that
silently lists nothing, which is why `kicad_api_root()` deliberately stops
short of it and keeps the trailing slash Part-DB's own documentation shows.

**The hard requirement to remember:** every value KiCad receives from an HTTP
library must be a *string*. Integers, floats and booleans are all serialised as
strings by Part-DB's endpoint. That constraint is upstream of us — it is why
`DesiredPart` in `sync.py` is entirely `str` — but it is stated here because
this is the file where a reader will look for it.

The token is a secret. `write_httplib` therefore creates the file 0600 and the
CLI says out loud that it should not be committed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import normalise_base_url

#: KiCad's schema version for this file. KiCad 8 and 9 both read `1.0`.
HTTPLIB_META_VERSION = 1.0

#: The KiCad HTTP library API version Part-DB implements.
DEFAULT_API_VERSION = "v1"

#: Part-DB routes the KiCad endpoint under a locale prefix like the rest of the
#: app. `en` is the safe default; a user on a German instance can pass `de`.
DEFAULT_LOCALE = "en"

#: KiCad's client-side cache lifetimes, in seconds, and KiCad's own defaults.
#: Optional in the file and honoured by KiCad 8+; Part-DB's documentation omits
#: them, so they are written explicitly rather than left to a default that
#: might change. Categories are cached far longer than parts because they
#: change far less often.
DEFAULT_TIMEOUT_PARTS = 60
DEFAULT_TIMEOUT_CATEGORIES = 600

#: KiCad recognises an HTTP library by this extension.
SUFFIX = ".kicad_httplib"


def kicad_api_root(base_url: str, locale: str = DEFAULT_LOCALE) -> str:
    """`https://host` -> `https://host/en/kicad-api/`.

    Accepts anything `normalise_base_url` accepts, including a URL a user
    pasted that already has the locale or the api path on it. The trailing
    slash is part of the contract with KiCad, which concatenates `api_version`
    onto this string.
    """
    return f"{normalise_base_url(base_url)}/{locale.strip('/')}/kicad-api/"


@dataclass(frozen=True)
class HttpLibrarySource:
    """The `source` block of a `.kicad_httplib`."""

    root_url: str
    token: str
    api_version: str = DEFAULT_API_VERSION
    timeout_parts_seconds: int = DEFAULT_TIMEOUT_PARTS
    timeout_categories_seconds: int = DEFAULT_TIMEOUT_CATEGORIES

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "REST_API",
            "api_version": self.api_version,
            "root_url": self.root_url,
            "token": self.token,
            "timeout_parts_seconds": self.timeout_parts_seconds,
            "timeout_categories_seconds": self.timeout_categories_seconds,
        }


def httplib_document(
    base_url: str,
    token: str,
    name: str = "Part-DB",
    description: str = "Parts registered by kifab, served from Part-DB",
    locale: str = DEFAULT_LOCALE,
    api_version: str = DEFAULT_API_VERSION,
    timeout_parts: int = DEFAULT_TIMEOUT_PARTS,
    timeout_categories: int = DEFAULT_TIMEOUT_CATEGORIES,
) -> dict[str, Any]:
    """The whole file, as a dict, so it can be asserted on without file I/O."""
    if not (token or "").strip():
        raise ValueError(
            "a .kicad_httplib needs an API token; KiCad has no other way to "
            "authenticate to Part-DB"
        )
    source = HttpLibrarySource(
        root_url=kicad_api_root(base_url, locale),
        token=token.strip(),
        api_version=api_version,
        timeout_parts_seconds=timeout_parts,
        timeout_categories_seconds=timeout_categories,
    )
    return {
        "meta": {"version": HTTPLIB_META_VERSION},
        "name": name,
        "description": description,
        "source": source.to_dict(),
    }


def render_httplib(document: dict[str, Any]) -> str:
    """Byte-stable JSON, so regenerating an unchanged library is a no-op."""
    return json.dumps(document, indent=4, sort_keys=False) + "\n"


def write_httplib(path: str | Path, document: dict[str, Any]) -> Path:
    """Write the file with owner-only permissions. It contains a live token."""
    path = Path(path)
    if path.suffix != SUFFIX:
        path = path.with_suffix(SUFFIX)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_httplib(document), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - not every filesystem has modes
        pass
    return path
