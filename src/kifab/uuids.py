"""Deterministic UUID derivation.

KiCad 9 requires a `(uuid ...)` on most footprint elements. Random UUIDs would
make emission impure: the same IR would produce a different file on every run,
so golden files could never be byte-compared and every regeneration would show
a spurious git diff.

So every UUID here is a **UUIDv5** — a hash-derived, reproducible UUID — over
`(part identity, element kind, element index)`. Same IR in, same bytes out. No
random identifier is generated anywhere in this package; `tests/test_uuids.py`
asserts that by grepping the source.

See DECISIONS.md, "UUIDs are derived, never random".
"""

from __future__ import annotations

import uuid

# A fixed private namespace. Changing this string re-keys every UUID we have
# ever emitted, which would show up as a diff on every part in the corpus — so
# treat it as frozen.
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/kifab/kicad-part-forge")


def derive(identity: str, kind: str, index: int | str) -> str:
    """A stable UUID for one element of one part.

    `identity` distinguishes parts (so two parts never collide), `kind` and
    `index` distinguish elements within a part.
    """
    return str(uuid.uuid5(NAMESPACE, f"{identity}|{kind}|{index}"))


class UuidSource:
    """Hands out derived UUIDs, counting per element kind.

    The emitter walks its elements in a fixed order, so a per-kind counter is
    enough to key them — and it keeps the emitter from having to thread indices
    through every call site. Reusing a source across two different files would
    silently continue the counters, so each file builds its own.
    """

    def __init__(self, identity: str) -> None:
        self.identity = identity
        self._counters: dict[str, int] = {}

    def next(self, kind: str) -> str:
        index = self._counters.get(kind, 0)
        self._counters[kind] = index + 1
        return derive(self.identity, kind, index)

    def named(self, kind: str, name: str) -> str:
        """A UUID keyed by name rather than by position.

        Used where an element has a stable identifier of its own (a pad number,
        a property name); keying on that instead of ordinal means inserting an
        element earlier in the file does not renumber everything after it.
        """
        return derive(self.identity, kind, name)
