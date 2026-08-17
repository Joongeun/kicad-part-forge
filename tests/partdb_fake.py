"""An in-memory stand-in for Part-DB's REST API.

Every test in the suite runs offline, so this is the seam. It is deliberately
faithful to the two shapes that actually cause bugs, rather than being a
convenient dict:

* **`eda_info` is serialised on `GET /api/parts/{id}` but not on the
  collection.** That is real Part-DB behaviour (the item operation adds the
  `eda_info:read` group; the resource-level normalisation context used by
  `GetCollection` does not). A fake that returned it on both would let a client
  that never re-reads the item pass, and then update every part on every run
  forever against a real server.
* **`manufacturer_product_number` is matched with SQL `ILIKE`,** so the filter
  is case-insensitive.

It also records every request, which is how the idempotency test can assert
that the second run performs no writes rather than merely producing no visible
change.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from kifab.partdb.client import Response

#: Only these are hidden from the collection; enough to reproduce the trap.
ITEM_ONLY_FIELDS = ("eda_info",)


@dataclass
class FakePartDb:
    """A tiny Part-DB. `calls` is the evidence an idempotency test needs."""

    parts: dict[int, dict[str, Any]] = field(default_factory=dict)
    categories: dict[int, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    #: Set to a status to make the next request fail with it.
    fail_with: tuple[int, dict] | None = None
    _next_part: int = 1
    _next_category: int = 1

    # -- fixture helpers ------------------------------------------------

    def add_part(self, **fields: Any) -> str:
        pid = self._next_part
        self._next_part += 1
        record = {
            "@id": f"/api/parts/{pid}",
            "@type": "Part",
            "id": pid,
            "name": "",
            "description": "",
            "manufacturer_product_number": "",
            "ipn": None,
            "eda_info": {},
        }
        record.update(fields)
        self.parts[pid] = record
        return record["@id"]

    def eda(self, iri: str) -> dict[str, Any]:
        return self.parts[int(iri.rsplit("/", 1)[1])]["eda_info"]

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] in ("POST", "PATCH", "PUT", "DELETE")]

    # -- the transport --------------------------------------------------

    def __call__(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> Response:
        assert headers.get("Authorization", "").startswith("Bearer "), (
            "every Part-DB request must carry the API token"
        )
        split = urllib.parse.urlsplit(url)
        path, query = split.path, urllib.parse.parse_qs(split.query)
        self.calls.append((method, path))
        if self.fail_with is not None:
            status, payload = self.fail_with
            self.fail_with = None
            return Response(status, json.dumps(payload).encode())
        payload = json.loads(body.decode()) if body else None

        if path == "/api/parts" and method == "GET":
            wanted = (query.get("manufacturer_product_number") or [""])[0].casefold()
            members = [
                _without(p, ITEM_ONLY_FIELDS)
                for p in self.parts.values()
                if p["manufacturer_product_number"].casefold() == wanted
            ]
            return _collection(members)
        if path == "/api/parts" and method == "POST":
            iri = self.add_part(**_normalise(payload or {}))
            return Response(201, json.dumps(self.parts[_id(iri)]).encode())
        if path.startswith("/api/parts/"):
            pid = _id(path)
            if pid not in self.parts:
                return Response(404, b'{"hydra:description":"Not Found"}')
            if method == "GET":
                return Response(200, json.dumps(self.parts[pid]).encode())
            if method == "PATCH":
                assert headers["Content-Type"] == "application/merge-patch+json"
                _merge(self.parts[pid], _normalise(payload or {}))
                return Response(200, json.dumps(self.parts[pid]).encode())

        if path == "/api/categories" and method == "GET":
            name = (query.get("name") or [""])[0]
            return _collection(
                [c for c in self.categories.values() if c["name"] == name]
            )
        if path == "/api/categories" and method == "POST":
            cid = self._next_category
            self._next_category += 1
            record = {"@id": f"/api/categories/{cid}", "id": cid, **(payload or {})}
            self.categories[cid] = record
            return Response(201, json.dumps(record).encode())

        return Response(405, b'{"hydra:description":"no such route in the fake"}')


def _id(path: str) -> int:
    return int(path.rsplit("/", 1)[1])


def _without(record: dict, keys: tuple[str, ...]) -> dict:
    return {k: v for k, v in record.items() if k not in keys}


def _normalise(payload: dict) -> dict:
    """Part-DB stores `eda_info` members as null when unset."""
    out = dict(payload)
    eda = out.get("eda_info")
    if isinstance(eda, dict):
        out["eda_info"] = {k: (None if v == "" else v) for k, v in eda.items()}
    return out


def _merge(record: dict, patch: dict) -> None:
    """RFC 7386 merge patch, one level deep — enough for `eda_info`."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(record.get(key), dict):
            record[key].update(value)
        else:
            record[key] = value


def _collection(members: list[dict]) -> Response:
    return Response(
        200,
        json.dumps(
            {
                "@context": "/api/contexts/Part",
                "@type": "hydra:Collection",
                "hydra:member": members,
                "hydra:totalItems": len(members),
            }
        ).encode(),
    )
