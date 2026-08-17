"""A minimal REST client for Part-DB's `/api` surface.

Scope is deliberately small: read parts, create parts, patch parts, and resolve
the one entity Part-DB requires a part to have (a category). Everything else
Part-DB can do — stock, storage, suppliers, prices — is inventory management,
which is the user's job and not ours.

Design notes, all of which are load-bearing:

* **stdlib only.** `urllib.request`, as in `resolve/easyeda.py`. Adding an HTTP
  dependency to a library-generation tool for four endpoints is not a trade
  worth making, and the core install stays small.
* **The transport is injectable.** Every test in the suite runs offline against
  recorded fixtures; `Transport` is the seam. Nothing above this module knows
  the network exists.
* **API Platform shapes, not our own.** Part-DB's API is API Platform, so
  collections come back as JSON-LD with `hydra:member` and entity references
  are IRIs (`/api/categories/1`), not integers. We speak that dialect rather
  than inventing a friendlier one and being wrong at the boundary.
* **The client never decides.** It has no notion of "should this be updated".
  That judgement lives in `sync.py`, where it can be unit-tested without a
  server.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol

#: Sent so a Part-DB admin reading their access log can tell what this was.
USER_AGENT = "kifab/0.1 (+https://github.com/clash/kicad-part-forge)"

#: API Platform's content types. POST creates a resource and wants the full
#: JSON-LD document; PATCH is a *merge patch* (RFC 7386) and must say so, or
#: Part-DB answers 415 Unsupported Media Type.
CONTENT_TYPE_CREATE = "application/ld+json"
CONTENT_TYPE_PATCH = "application/merge-patch+json"

#: The fields of a part's `eda_info` object that concern KiCad. Part-DB's KiCad
#: HTTP library serves these straight through to KiCad.
EDA_FIELDS = (
    "reference_prefix",
    "value",
    "kicad_symbol",
    "kicad_footprint",
)


class PartDbError(RuntimeError):
    """Anything that stopped us talking to Part-DB."""


class PartDbHttpError(PartDbError):
    """A non-2xx response, with enough of the body to be diagnosable.

    Part-DB returns API Platform's problem+json, whose `hydra:description`
    usually names the exact field that was rejected. Losing that and printing
    "HTTP 422" would make every schema disagreement an afternoon of guessing.
    """

    def __init__(self, status: int, method: str, url: str, body: str) -> None:
        self.status = status
        self.method = method
        self.url = url
        self.body = body
        detail = _problem_detail(body)
        super().__init__(
            f"{method} {url} -> HTTP {status}" + (f": {detail}" if detail else "")
        )


def _problem_detail(body: str) -> str:
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return body.strip()[:400]
    if not isinstance(data, dict):
        return body.strip()[:400]
    for key in ("hydra:description", "detail", "description", "message", "error"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value[:400]
    return body.strip()[:400]


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))


class Transport(Protocol):
    """The entire network surface, so tests can replace it with a dict."""

    def __call__(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> Response: ...


@dataclass
class UrlTransport:
    """The real one. `urllib`, a timeout, and no retries.

    No retry logic on purpose: `sync` is idempotent, so the correct response to
    a transient failure is to run it again, and a client that silently retries a
    POST is how duplicate inventory rows get created.
    """

    timeout: float = 30.0

    def __call__(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> Response:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return Response(response.status, response.read())
        except urllib.error.HTTPError as exc:  # a *response*, not a failure
            return Response(exc.code, exc.read())
        except urllib.error.URLError as exc:
            raise PartDbError(f"could not reach {url}: {exc.reason}") from exc
        except OSError as exc:  # pragma: no cover - platform dependent
            raise PartDbError(f"could not reach {url}: {exc}") from exc


@dataclass(frozen=True)
class RemotePart:
    """A part as Part-DB currently holds it — only the fields kifab reads."""

    iri: str
    name: str = ""
    description: str = ""
    mpn: str = ""
    ipn: str = ""
    eda: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RemotePart:
        eda = data.get("eda_info") or {}
        if not isinstance(eda, dict):
            eda = {}
        return cls(
            iri=str(data.get("@id") or ""),
            name=data.get("name") or "",
            description=data.get("description") or "",
            mpn=data.get("manufacturer_product_number") or "",
            ipn=data.get("ipn") or "",
            eda=eda,
            raw=data,
        )

    def eda_field(self, name: str) -> str:
        """An EDA field as a string. Absent and null both read as empty.

        Part-DB leaves unset `eda_info` members as `null`, and KiCad's HTTP
        library requires strings, so `None` and `""` mean the same thing here.
        Treating them as different would make every unset field look like drift.
        """
        value = self.eda.get(name)
        return "" if value is None else str(value)


def normalise_base_url(url: str) -> str:
    """Instance root, no trailing slash, no locale segment, no `/api`.

    Users paste whatever is in their address bar — `https://host/en/parts/12`
    is the common one. The REST API lives at `{root}/api`, so keep only the
    scheme and host and reject anything that is not http(s).
    """
    url = (url or "").strip()
    if not url:
        raise PartDbError("no Part-DB URL given (pass --url or set PARTDB_URL)")
    if "://" not in url:
        url = "https://" + url
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise PartDbError(f"{url!r}: Part-DB URL must be http:// or https://")
    if not parsed.netloc:
        raise PartDbError(f"{url!r}: Part-DB URL has no host")
    path = parsed.path.rstrip("/")
    # Strip anything that is clearly *inside* the app rather than a mount point:
    # a locale prefix, the API itself, or a page the user happened to be on.
    parts = [p for p in path.split("/") if p]
    while parts and (
        parts[-1] == "api"
        or (len(parts[-1]) == 2 and parts[-1].isalpha())
        or parts[-1] in ("parts", "kicad-api", "v1")
    ):
        parts.pop()
    root = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "/" + "/".join(parts) if parts else "", "", "")
    )
    return root.rstrip("/")


class PartDbClient:
    """Talks to one Part-DB instance as one API token."""

    def __init__(
        self,
        base_url: str,
        token: str,
        transport: Transport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = normalise_base_url(base_url)
        self.token = (token or "").strip()
        if not self.token:
            raise PartDbError(
                "no Part-DB API token given (pass --token or set PARTDB_TOKEN). "
                "Create one in Part-DB under User Settings > API tokens, and "
                "make sure the token's permissions include read+edit on parts."
            )
        self._transport: Transport = transport or UrlTransport(timeout=timeout)

    # -- plumbing ------------------------------------------------------

    def url(self, path: str) -> str:
        """Absolute URL for an API path or an IRI returned by the server."""
        if path.startswith(("http://", "https://")):
            return path
        return self.base_url + "/" + path.lstrip("/")

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        content_type: str = CONTENT_TYPE_CREATE,
        allow_404: bool = False,
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/ld+json",
            "User-Agent": USER_AGENT,
        }
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = content_type
        url = self.url(path)
        response = self._transport(method, url, body, headers)
        if response.status == 404 and allow_404:
            return None
        if response.status == 401:
            raise PartDbHttpError(
                401,
                method,
                url,
                json.dumps(
                    {
                        "hydra:description": "the API token was rejected. Check "
                        "that it has not expired and that the user it belongs "
                        "to has API access enabled (Part-DB permission group "
                        "'API': 'Access the API')."
                    }
                ),
            )
        if response.status == 403:
            raise PartDbHttpError(
                403,
                method,
                url,
                json.dumps(
                    {
                        "hydra:description": "the token authenticated but is not "
                        "permitted to do this. Grant the token's scope the "
                        "parts read/edit/create permissions."
                    }
                ),
            )
        if not 200 <= response.status < 300:
            raise PartDbHttpError(
                response.status, method, url, response.body.decode("utf-8", "replace")
            )
        try:
            return response.json()
        except ValueError as exc:
            raise PartDbError(
                f"{method} {url}: response was not JSON — is {self.base_url!r} "
                "really a Part-DB instance?"
            ) from exc

    def _collection(self, path: str, params: dict[str, Any]) -> Iterator[dict]:
        """Walk an API Platform collection, following `hydra:view`.

        API Platform 3 emits `hydra:member`; some deployments (and the JSON-LD
        3.0 context) emit a bare `member`. Read both rather than betting on one.
        """
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "")}
        )
        next_path: str | None = f"{path}?{query}" if query else path
        seen: set[str] = set()
        while next_path:
            if next_path in seen:  # pragma: no cover - server-side loop
                break
            seen.add(next_path)
            data = self.request("GET", next_path)
            if not isinstance(data, dict):
                return
            members = data.get("hydra:member")
            if members is None:
                members = data.get("member")
            for item in members or []:
                if isinstance(item, dict):
                    yield item
            view = data.get("hydra:view") or data.get("view") or {}
            nxt = view.get("hydra:next") or view.get("next") if view else None
            next_path = nxt if isinstance(nxt, str) else None

    # -- parts ---------------------------------------------------------

    def find_parts_by_mpn(self, mpn: str, limit: int = 25) -> list[RemotePart]:
        """Candidate parts whose `manufacturer_product_number` equals `mpn`.

        Two things about this are not obvious and both cause silent bugs:

        1. Part-DB filters this field with a `LikeFilter`, which passes the
           value straight into SQL `ILIKE`. So the match is *case-insensitive*,
           and an MPN containing `%` or `_` would be a wildcard. The results are
           therefore re-checked locally, case-folded, before being believed.
        2. **`eda_info` is not serialised on the collection endpoint** — only on
           `GET /api/parts/{id}`. The parts returned here have empty `eda`.
           Comparing against them would make every part look like it needed
           updating, forever. Call `get_part()` on the one you mean.
        """
        wanted = mpn.casefold()
        found = [
            RemotePart.from_json(item)
            for item in self._collection(
                "/api/parts",
                {"manufacturer_product_number": mpn, "itemsPerPage": limit},
            )
        ]
        return [p for p in found if p.mpn.casefold() == wanted]

    def get_part(self, iri: str) -> RemotePart | None:
        """One part, hydrated. This is the only shape that carries `eda_info`."""
        data = self.request("GET", iri, allow_404=True)
        if data is None:
            return None
        return RemotePart.from_json(data)

    def create_part(self, payload: dict[str, Any]) -> RemotePart:
        data = self.request("POST", "/api/parts", payload, CONTENT_TYPE_CREATE)
        if not isinstance(data, dict):  # pragma: no cover - server contract
            raise PartDbError("POST /api/parts did not return the created part")
        return RemotePart.from_json(data)

    def patch_part(self, iri: str, payload: dict[str, Any]) -> RemotePart:
        data = self.request("PATCH", iri, payload, CONTENT_TYPE_PATCH)
        if not isinstance(data, dict):  # pragma: no cover - server contract
            raise PartDbError(f"PATCH {iri} did not return the updated part")
        return RemotePart.from_json(data)

    # -- categories ----------------------------------------------------
    #
    # Part-DB requires every part to belong to a category, so creating one is
    # not optional. We resolve by name and create on miss, because demanding
    # the user look up a numeric id before their first sync is a bad first run.

    def find_category(self, name: str) -> str | None:
        for item in self._collection("/api/categories", {"name": name}):
            if (item.get("name") or "") == name and item.get("@id"):
                return str(item["@id"])
        return None

    def ensure_category(self, name: str) -> str:
        found = self.find_category(name)
        if found:
            return found
        created = self.request(
            "POST",
            "/api/categories",
            {
                "name": name,
                "comment": "Created by kifab. Parts here have generated KiCad "
                "symbols and footprints in this project's library.",
            },
            CONTENT_TYPE_CREATE,
        )
        if not isinstance(created, dict) or not created.get("@id"):
            raise PartDbError(  # pragma: no cover - server contract
                f"could not create category {name!r}"
            )
        return str(created["@id"])
