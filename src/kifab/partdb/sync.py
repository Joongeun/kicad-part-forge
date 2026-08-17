"""`kifab sync` — reconcile `parts/` against the Part-DB inventory.

Running it twice must change nothing. That single requirement decides the whole
design, so the reasoning is written down here rather than discovered later.

## What a part is identified by

**The MPN, as a natural key, checked against the server every run.** Not a
locally-remembered row id.

The alternatives, and why they lose:

* *A kifab-owned `ipn`* (`kifab:<library>:<mpn>`) is unambiguous and Part-DB
  enforces its uniqueness — but it only ever matches parts kifab created. The
  real workflow is that the inventory row already exists (someone bought the
  chip before anyone drew it), and a sync that cannot see those creates a
  duplicate of every part the user already owns. That is the exact failure
  mode "idempotent" is supposed to exclude.
* *A local state file as the identity* is worse still: lose it, or sync from a
  second machine, and every part is created again.

So the server is asked `?manufacturer_product_number=<mpn>` every run, and the
answer is re-checked locally for an exact match. Zero matches means create; one
means reconcile; more than one is ambiguous and is **reported, never guessed**
— two rows with the same MPN is a fact about the user's inventory that a
library tool has no business resolving.

## What the state file is for

`partdb-sync.json` records, per instance, the IRI we last wrote and **the exact
values we last wrote to the EDA fields**. It is a cache and a drift detector,
never the identity. That is what lets sync tell the two interesting cases apart:

* the server field differs from what we want, and equals what we last wrote
  → *we* are the source of the change. Update.
* the server field differs from what we want *and* from what we last wrote
  → somebody edited it in Part-DB. **Conflict**: report it, change nothing.

Because it is a record of shared state rather than a machine-local cache, it is
meant to be committed. It contains no timestamps for the same reason the
emitters are byte-stable: a file that churns on every run cannot be used to see
that nothing happened.

## What kifab owns

Exactly the four EDA fields in `MANAGED_FIELDS`. On **create** it also fills
name, description and MPN, because a new row has to have them. On **update** it
touches nothing else — stock, storage, price, supplier and the user's own
description are inventory data, and a library generator overwriting them would
be a bug that destroys work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from ..ir import Part
from .client import PartDbClient, PartDbError, RemotePart

#: The fields kifab writes on every sync, and the only ones it will overwrite.
#: All four live in Part-DB's `eda_info` object and are served straight to
#: KiCad by the HTTP library.
MANAGED_FIELDS = ("kicad_symbol", "kicad_footprint", "reference_prefix", "value")

#: Where the record of what we last wrote lives, relative to the project root.
DEFAULT_STATE_PATH = Path("partdb-sync.json")

#: Part-DB requires a category on every part; this is the one we create.
DEFAULT_CATEGORY = "kifab"

STATE_VERSION = 1


class Action(str, Enum):
    """What sync decided to do about one part. Ordered worst-last for reports."""

    UNCHANGED = "unchanged"
    CREATE = "create"
    UPDATE = "update"
    CONFLICT = "conflict"
    AMBIGUOUS = "ambiguous"

    @property
    def writes(self) -> bool:
        return self in (Action.CREATE, Action.UPDATE)

    @property
    def blocks(self) -> bool:
        """True if this alone should make `kifab sync` exit non-zero."""
        return self in (Action.CONFLICT, Action.AMBIGUOUS)


@dataclass(frozen=True)
class DesiredPart:
    """What `parts/<MPN>.yaml` says Part-DB should hold for this part.

    Everything here is a string. That is not laziness: KiCad's HTTP library
    requires every value it receives to be a string, so this is the type the
    whole downstream path is in.
    """

    mpn: str
    name: str
    description: str
    manufacturer: str
    datasheet: str
    kicad_symbol: str
    kicad_footprint: str
    reference_prefix: str
    value: str

    def managed(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in MANAGED_FIELDS}

    def eda_info(self) -> dict[str, str]:
        return self.managed()

    def create_payload(self, category: str | None = None) -> dict[str, Any]:
        """The JSON-LD body for `POST /api/parts`."""
        payload: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "manufacturer_product_number": self.mpn,
            "eda_info": self.eda_info(),
        }
        if self.datasheet:
            # Recorded for the human reading the inventory row, not for KiCad:
            # Part-DB builds KiCad's `datasheet` field from *attachments*, which
            # kifab does not manage. Set on create only — on an existing row
            # this is the user's field.
            payload["manufacturer_product_url"] = self.datasheet
        if category:
            payload["category"] = category
        return payload

    def patch_payload(self) -> dict[str, Any]:
        """The merge-patch body for `PATCH /api/parts/{id}`.

        `eda_info` only. An existing inventory row's name, description, stock
        and supplier data belong to the user.
        """
        return {"eda_info": self.eda_info()}


def desired_from_part(part: Part) -> DesiredPart:
    """Project the IR onto what Part-DB stores. Pure; no network, no state."""
    return DesiredPart(
        mpn=part.mpn,
        name=part.mpn,
        description=part.description or part.mpn,
        manufacturer=part.manufacturer,
        datasheet=part.datasheet,
        # Part-DB's `kicad_symbol` becomes KiCad's `symbolIdStr`, so it must be
        # a LIB_ID — `library:name` — not a bare symbol name.
        kicad_symbol=f"{part.library}:{part.symbol_name}",
        kicad_footprint=part.footprint_id,
        reference_prefix=part.reference,
        value=part.display_value,
    )


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class SyncState:
    """What we last wrote, per instance, keyed by MPN.

    Deliberately dumb: a dict on disk with no timestamps and sorted keys, so
    two identical syncs produce byte-identical files.
    """

    path: Path
    instances: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> SyncState:
        path = Path(path)
        if not path.is_file():
            return cls(path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise PartDbError(f"{path}: sync state is not readable JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise PartDbError(f"{path}: sync state must be a JSON object")
        instances = data.get("instances")
        if not isinstance(instances, dict):
            instances = {}
        return cls(path=path, instances=instances)

    def parts(self, instance: str) -> dict[str, dict[str, Any]]:
        return self.instances.setdefault(instance, {}).setdefault("parts", {})

    def record(self, instance: str, mpn: str) -> dict[str, Any] | None:
        entry = self.parts(instance).get(mpn)
        return entry if isinstance(entry, dict) else None

    def last_written(self, instance: str, mpn: str) -> dict[str, str] | None:
        entry = self.record(instance, mpn)
        if not entry:
            return None
        written = entry.get("written")
        return written if isinstance(written, dict) else None

    def remember(self, instance: str, mpn: str, iri: str, written: dict[str, str]) -> None:
        self.parts(instance)[mpn] = {
            "id": iri,
            "written": dict(sorted(written.items())),
        }

    def forget(self, instance: str, mpn: str) -> None:
        self.parts(instance).pop(mpn, None)

    def to_json(self) -> str:
        instances = {
            url: {"parts": dict(sorted(body.get("parts", {}).items()))}
            for url, body in sorted(self.instances.items())
            if body.get("parts")
        }
        return (
            json.dumps({"version": STATE_VERSION, "instances": instances}, indent=2)
            + "\n"
        )

    def save(self) -> bool:
        """Write only if the bytes changed. Returns True if it wrote."""
        text = self.to_json()
        if self.path.is_file() and self.path.read_text(encoding="utf-8") == text:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")
        return True


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass
class SyncStep:
    """One part's verdict, with the evidence for it."""

    mpn: str
    action: Action
    desired: DesiredPart
    remote: RemotePart | None = None
    #: field -> (what Part-DB holds, what we want)
    differences: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: field -> (what Part-DB holds, what we last wrote, what we want)
    conflicts: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    note: str = ""
    #: Filled in by `apply_plan` once the write has happened.
    applied: bool = False

    def describe(self) -> str:
        head = f"{self.action.value:<9} {self.mpn}"
        if self.action is Action.UNCHANGED:
            return head
        if self.action is Action.CREATE:
            return f"{head}  -> new part, symbol {self.desired.kicad_symbol!r}"
        if self.action is Action.UPDATE:
            changes = ", ".join(
                f"{k}: {old or '(unset)'!s} -> {new!r}"
                for k, (old, new) in sorted(self.differences.items())
            )
            return f"{head}  {self.remote.iri if self.remote else ''}  {changes}"
        if self.action is Action.CONFLICT:
            lines = [f"{head}  {self.remote.iri if self.remote else ''}"]
            for key, (server, ours, want) in sorted(self.conflicts.items()):
                lines.append(
                    f"    {key}: Part-DB holds {server or '(unset)'!r}, "
                    f"kifab last wrote {ours or '(never)'!r}, "
                    f"parts/ says {want!r}"
                )
            lines.append(
                "    left alone. Fix it in Part-DB or in parts/, or re-run with "
                "--force to make parts/ win."
            )
            return "\n".join(lines)
        return f"{head}  {self.note}"


@dataclass
class SyncPlan:
    """Everything sync intends to do, computable without writing anything."""

    instance: str
    steps: list[SyncStep] = field(default_factory=list)
    #: MPNs the state file knows about that are no longer in `parts/`.
    stale: list[str] = field(default_factory=list)

    def by_action(self, action: Action) -> list[SyncStep]:
        return [s for s in self.steps if s.action is action]

    @property
    def writes(self) -> list[SyncStep]:
        return [s for s in self.steps if s.action.writes]

    def ok(self) -> bool:
        return not any(s.action.blocks for s in self.steps)

    def summary(self) -> str:
        counts = {a: len(self.by_action(a)) for a in Action}
        parts = [f"{counts[a]} {a.value}" for a in Action if counts[a]]
        if not parts:
            parts = ["nothing to do"]
        if self.stale:
            parts.append(f"{len(self.stale)} stale")
        return ", ".join(parts)

    def format(self) -> str:
        lines = [s.describe() for s in self.steps]
        for mpn in self.stale:
            lines.append(
                f"stale     {mpn}  in {DEFAULT_STATE_PATH.name} but no longer in "
                "parts/. Left in Part-DB; inventory is not kifab's to delete."
            )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance": self.instance,
            "ok": self.ok(),
            "summary": self.summary(),
            "stale": list(self.stale),
            "steps": [
                {
                    "mpn": s.mpn,
                    "action": s.action.value,
                    "id": s.remote.iri if s.remote else None,
                    "applied": s.applied,
                    "differences": {
                        k: {"partdb": old, "wanted": new}
                        for k, (old, new) in sorted(s.differences.items())
                    },
                    "conflicts": {
                        k: {"partdb": a, "last_written": b, "wanted": c}
                        for k, (a, b, c) in sorted(s.conflicts.items())
                    },
                    "note": s.note,
                }
                for s in self.steps
            ],
        }


def _classify(
    desired: DesiredPart,
    remote: RemotePart,
    last_written: dict[str, str] | None,
    force: bool,
) -> SyncStep:
    differences: dict[str, tuple[str, str]] = {}
    conflicts: dict[str, tuple[str, str, str]] = {}
    for name in MANAGED_FIELDS:
        server = remote.eda_field(name)
        want = getattr(desired, name)
        if server == want:
            continue
        remembered = (last_written or {}).get(name)
        # Drift is "the server holds something that is neither what we want nor
        # what we put there". An unset field is not drift: nobody chose it.
        theirs = server != "" and (
            last_written is None or remembered is None or server != remembered
        )
        if theirs and not force:
            conflicts[name] = (server, remembered or "", want)
        else:
            differences[name] = (server, want)

    if conflicts:
        return SyncStep(desired.mpn, Action.CONFLICT, desired, remote, differences, conflicts)
    if differences:
        return SyncStep(desired.mpn, Action.UPDATE, desired, remote, differences)
    return SyncStep(desired.mpn, Action.UNCHANGED, desired, remote)


def plan_sync(
    client: PartDbClient,
    parts: Iterable[Part],
    state: SyncState,
    force: bool = False,
) -> SyncPlan:
    """Work out what would change. Performs reads only — never writes."""
    instance = client.base_url
    plan = SyncPlan(instance=instance)
    seen: set[str] = set()

    for part in parts:
        desired = desired_from_part(part)
        seen.add(desired.mpn)
        matches = client.find_parts_by_mpn(desired.mpn)
        if len(matches) > 1:
            plan.steps.append(
                SyncStep(
                    desired.mpn,
                    Action.AMBIGUOUS,
                    desired,
                    note=(
                        f"{len(matches)} parts in Part-DB share this MPN "
                        f"({', '.join(m.iri for m in matches)}). kifab will not "
                        "guess which one the library belongs to — merge or "
                        "de-duplicate them in Part-DB first."
                    ),
                )
            )
            continue
        if not matches:
            plan.steps.append(SyncStep(desired.mpn, Action.CREATE, desired))
            continue
        # The collection endpoint does not serialise `eda_info`, so the match
        # has to be re-read as an item before its EDA fields can be compared.
        # Skipping this would make every part look permanently out of date.
        remote = client.get_part(matches[0].iri) or matches[0]
        plan.steps.append(
            _classify(desired, remote, state.last_written(instance, desired.mpn), force)
        )

    plan.stale = sorted(mpn for mpn in state.parts(instance) if mpn not in seen)
    return plan


def apply_plan(
    client: PartDbClient,
    plan: SyncPlan,
    state: SyncState,
    category: str | None = DEFAULT_CATEGORY,
) -> SyncPlan:
    """Perform the plan's writes and update the state file.

    Idempotency is structural rather than defensive: a plan whose steps are all
    `unchanged` has nothing to iterate over, so the second run of an unchanged
    library issues no POST and no PATCH at all.
    """
    category_iri: str | None = None
    if any(s.action is Action.CREATE for s in plan.steps) and category:
        category_iri = client.ensure_category(category)

    for step in plan.steps:
        if step.action is Action.CREATE:
            created = client.create_part(step.desired.create_payload(category_iri))
            step.remote = created
            step.applied = True
            state.remember(plan.instance, step.mpn, created.iri, step.desired.managed())
        elif step.action is Action.UPDATE:
            assert step.remote is not None
            updated = client.patch_part(step.remote.iri, step.desired.patch_payload())
            step.remote = updated
            step.applied = True
            state.remember(plan.instance, step.mpn, updated.iri, step.desired.managed())
        elif step.action is Action.UNCHANGED and step.remote is not None:
            # Record it even though nothing was written: on the very first sync
            # against an inventory that already matches, this is what turns a
            # later hand-edit in Part-DB into a reportable conflict instead of a
            # silent overwrite.
            state.remember(
                plan.instance, step.mpn, step.remote.iri, step.desired.managed()
            )

    state.save()
    return plan
