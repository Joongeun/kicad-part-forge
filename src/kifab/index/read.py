"""Read KiCad library files into flat index records.

Deliberately reuses `kifab.emit.sexpr` rather than regexing the files: that
parser is already proven lossless across the whole shipped corpus (Phase 0a),
so the index and the emitter agree about what a file says.

Nothing here interprets house style — it only extracts the facts the index and
the package-identity comparison need.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..emit import sexpr
from .package_id import PackageIdentity, PadGeom, identity_from_footprint


#: Re-exported so existing callers keep working; it now lives next to
#: `sexpr.quote`, whose inverse it is.
unquote = sexpr.unquote


def _atoms(node: sexpr.Node) -> list[str]:
    return [c for c in node if isinstance(c, str)]


def _str_arg(node: sexpr.Node | None, index: int = 1) -> str:
    if node is None:
        return ""
    atoms = _atoms(node)
    return unquote(atoms[index]) if len(atoms) > index else ""


def _floats(node: sexpr.Node | None, count: int) -> tuple[float, ...] | None:
    if node is None:
        return None
    values: list[float] = []
    for atom in _atoms(node)[1:]:
        try:
            values.append(float(atom))
        except ValueError:
            return None
        if len(values) == count:
            break
    return tuple(values) if len(values) == count else None


def _layers_of(node: sexpr.Node) -> set[str]:
    layer = sexpr.find(node, "layer")
    if layer is None:
        return set()
    return {unquote(a) for a in _atoms(layer)[1:]}


# --------------------------------------------------------------------------
# Footprints
# --------------------------------------------------------------------------


@dataclass
class FootprintRecord:
    library: str
    name: str
    path: str
    origin: str
    descr: str = ""
    tags: str = ""
    mount: str = ""
    pads: list[PadGeom] = field(default_factory=list)
    identity: PackageIdentity = field(default_factory=PackageIdentity)


def _fab_bbox(root: sexpr.Node) -> tuple[float, float] | None:
    """Extent of the F.Fab body outline, which is the drawn package body."""
    xs: list[float] = []
    ys: list[float] = []
    for child in root:
        if not isinstance(child, list):
            continue
        head = child[0] if child and isinstance(child[0], str) else ""
        if not head.startswith("fp_"):
            continue
        if "F.Fab" not in _layers_of(child):
            continue
        for token in ("start", "end", "center", "mid"):
            point = _floats(sexpr.find(child, token), 2)
            if point:
                xs.append(point[0])
                ys.append(point[1])
        pts = sexpr.find(child, "pts")
        if pts is not None:
            for xy in sexpr.find_all(pts, "xy"):
                point = _floats(xy, 2)
                if point:
                    xs.append(point[0])
                    ys.append(point[1])
    if len(xs) < 2 or len(ys) < 2:
        return None
    return (round(max(xs) - min(xs), 3), round(max(ys) - min(ys), 3))


def read_footprint(path: Path, library: str, origin: str) -> FootprintRecord | None:
    """Parse one `.kicad_mod`. Returns None if it is not a footprint file."""
    try:
        root = sexpr.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (sexpr.SexprError, OSError):
        return None
    head = root[0] if root and isinstance(root[0], str) else ""
    if head not in ("footprint", "module"):
        return None

    name = _str_arg(root, 1) or path.stem
    descr = _str_arg(sexpr.find(root, "descr"))
    tags = _str_arg(sexpr.find(root, "tags"))
    attr = sexpr.find(root, "attr")
    mount = ""
    if attr is not None:
        flags = {unquote(a) for a in _atoms(attr)[1:]}
        if "smd" in flags:
            mount = "smd"
        elif "through_hole" in flags:
            mount = "through_hole"

    pads: list[PadGeom] = []
    unnumbered = 0
    for pad in sexpr.find_all(root, "pad"):
        number = _str_arg(pad, 1)
        if not number.strip():
            # Unnumbered pads are paste-relief apertures and mechanical
            # features, not lands. Counting them as pins is exactly how a
            # DFN-12 with a two-aperture thermal pad reads as a 14-pin,
            # four-sided package — i.e. how a DFN gets mistaken for a QFN.
            unnumbered += 1
            continue
        at = _floats(sexpr.find(pad, "at"), 2)
        size = _floats(sexpr.find(pad, "size"), 2)
        if at is None or size is None:
            continue
        rot = _floats(sexpr.find(pad, "at"), 3)
        w, h = size
        # A pad rotated an odd multiple of 90 degrees presents its short side
        # along x; identity comparisons must see the placed geometry.
        if rot is not None and round(rot[2]) % 180 == 90:
            w, h = h, w
        pads.append(PadGeom(number=number, x=at[0], y=at[1], w=w, h=h))

    identity = identity_from_footprint(
        name=name, descr=descr, tags=tags, pads=pads, fab_bbox=_fab_bbox(root)
    )
    return FootprintRecord(
        library=library,
        name=name,
        path=str(path),
        origin=origin,
        descr=descr,
        tags=tags,
        mount=mount,
        pads=pads,
        identity=identity,
    )


# --------------------------------------------------------------------------
# Symbols
# --------------------------------------------------------------------------

_UNIT_SUFFIX = re.compile(r"_(\d+)_(\d+)$")


@dataclass
class SymbolRecord:
    library: str
    name: str
    path: str
    origin: str
    extends: str = ""
    description: str = ""
    keywords: str = ""
    fp_filters: str = ""
    footprint: str = ""
    datasheet: str = ""
    reference: str = ""
    pin_count: int = 0


def _properties(node: sexpr.Node) -> dict[str, str]:
    out: dict[str, str] = {}
    for prop in sexpr.find_all(node, "property"):
        atoms = _atoms(prop)
        if len(atoms) >= 3:
            out[unquote(atoms[1])] = unquote(atoms[2])
    return out


def _count_pins(node: sexpr.Node) -> int:
    """Pins across every unit, body style 1 only.

    KiCad stores a symbol's graphics in child sub-symbols named
    `<NAME>_<unit>_<style>`. Style 2 is the De Morgan alternate — the same pins
    drawn differently — so counting it would double every gate.
    """
    total = 0
    for sub in sexpr.find_all(node, "symbol"):
        sub_name = _str_arg(sub, 1)
        match = _UNIT_SUFFIX.search(sub_name)
        if match and int(match.group(2)) not in (0, 1):
            continue
        total += len(sexpr.find_all(sub, "pin"))
    return total


def read_symbol_library(path: Path, library: str, origin: str) -> list[SymbolRecord]:
    """Parse one `.kicad_sym`, resolving `extends` within the file."""
    try:
        root = sexpr.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (sexpr.SexprError, OSError):
        return []
    head = root[0] if root and isinstance(root[0], str) else ""
    if head != "kicad_symbol_lib":
        return []

    raw: dict[str, sexpr.Node] = {}
    order: list[str] = []
    for node in sexpr.find_all(root, "symbol"):
        name = _str_arg(node, 1)
        if not name:
            continue
        raw[name] = node
        order.append(name)

    records: list[SymbolRecord] = []
    for name in order:
        node = raw[name]
        extends = _str_arg(sexpr.find(node, "extends"))
        props = _properties(node)
        pin_count = _count_pins(node)

        # A derived symbol inherits its parent's pins and any property it does
        # not override. Follow the chain, with a guard against a cycle.
        parent_name, seen = extends, {name}
        while parent_name and parent_name in raw and parent_name not in seen:
            seen.add(parent_name)
            parent = raw[parent_name]
            if pin_count == 0:
                pin_count = _count_pins(parent)
            for key, value in _properties(parent).items():
                props.setdefault(key, value)
            parent_name = _str_arg(sexpr.find(parent, "extends"))

        records.append(
            SymbolRecord(
                library=library,
                name=name,
                path=str(path),
                origin=origin,
                extends=extends,
                description=props.get("Description", ""),
                keywords=props.get("ki_keywords", ""),
                fp_filters=props.get("ki_fp_filters", ""),
                footprint=props.get("Footprint", ""),
                datasheet=props.get("Datasheet", ""),
                reference=props.get("Reference", ""),
                pin_count=pin_count,
            )
        )
    return records
