"""Derived UUIDs — the property that makes golden files possible at all.

DECISIONS.md locks this: every `(uuid ...)` is a UUIDv5 over (part identity,
element kind, element index). If a random UUID ever slips in, byte-comparison
of generated output stops meaning anything, so the test suite checks the
*source* as well as the behaviour.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from kifab.uuids import NAMESPACE, UuidSource, derive

SRC = Path(__file__).resolve().parent.parent / "src" / "kifab"


def test_same_inputs_give_the_same_uuid() -> None:
    assert derive("lib:FP", "pad", 3) == derive("lib:FP", "pad", 3)


@pytest.mark.parametrize(
    "a,b",
    [
        (("lib:A", "pad", 1), ("lib:B", "pad", 1)),  # different part
        (("lib:A", "pad", 1), ("lib:A", "silk", 1)),  # different kind
        (("lib:A", "pad", 1), ("lib:A", "pad", 2)),  # different element
    ],
)
def test_different_inputs_give_different_uuids(a, b) -> None:
    assert derive(*a) != derive(*b)


def test_output_is_a_version_5_uuid() -> None:
    value = uuid.UUID(derive("lib:FP", "pad", 1))
    assert value.version == 5, "v5 is what makes it reproducible"


def test_namespace_is_frozen() -> None:
    """Changing this constant re-keys every UUID kifab has ever emitted.

    That would show up as a diff on every part in the corpus, so the value is
    pinned here: changing it must be a deliberate, reviewed act.
    """
    assert str(NAMESPACE) == str(
        uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/kifab/kicad-part-forge")
    )


def test_source_counters_are_per_kind_and_start_at_zero() -> None:
    source = UuidSource("lib:FP")
    assert source.next("pad") == derive("lib:FP", "pad", 0)
    assert source.next("pad") == derive("lib:FP", "pad", 1)
    assert source.next("silk") == derive("lib:FP", "silk", 0)


def test_named_keys_survive_insertion_elsewhere() -> None:
    """A pad's UUID must not change because another pad was added before it."""
    first = UuidSource("lib:FP")
    first.next("silk")
    second = UuidSource("lib:FP")
    second.next("silk")
    second.next("silk")
    assert first.named("pad", "7") == second.named("pad", "7")


def test_no_random_uuids_anywhere_in_the_package() -> None:
    """Grep the source: `uuid4` must not appear outside this test.

    A behavioural test cannot catch a random UUID that is only reachable on
    some code path, and the cost of one slipping in is that every golden file
    becomes noise. So the check is structural.
    """
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"uuid[14]\s*\(|\bimport random\b|\brandom\.\w", text):
            offenders.append(str(path))
    assert not offenders, f"non-deterministic identifier source in: {offenders}"
