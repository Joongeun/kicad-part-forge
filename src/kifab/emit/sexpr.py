"""S-expression parse/write for KiCad 9 files.

Design note — why atoms stay as raw strings
--------------------------------------------
The parser deliberately does NOT coerce atoms to int/float. It keeps the exact
source text of every token, including its quotes. That makes round-tripping a
question of *structure and whitespace only*, which is the real fidelity risk;
number formatting is a separate concern owned by the emitter (`fmt_num`), and
only applies to values we generate ourselves.

KiCad's formatting rule (validated against the shipped libraries in
tests/test_roundtrip.py): a node whose children are all atoms is written on one
line; any node containing a child list is written multi-line, indented with
tabs. `pts` is the one exception — it packs its `(xy ...)` children several per
line (see PACKED_CHILDREN).

Byte-exactness: what is and isn't guaranteed
--------------------------------------------
We guarantee byte-exact *idempotence* (emit -> parse -> emit is stable) and
byte-exact round-trip of everything this package generates. We deliberately do
NOT guarantee byte-exact round-trip of arbitrary third-party files, for a
reason established by measurement during the Phase 0 spike:

  * KiCad's shipped libraries are not uniformly formatted. They were written by
    several generators over many years, so no single rule reproduces them all.
  * Even within `kicad-cli`-canonicalised output, `pts` packing could not be
    reduced to a consistent rule: the longest line KiCad kept was 122 chars
    while the shortest it rejected was 113, across 486 width-bound samples, so
    it is not a pure column limit. The only clean invariant found was a hard
    ceiling of 8 points per line.

The requirement that actually matters is no *data* loss, which
`test_semantic_roundtrip_all_sampled` enforces over the whole corpus, plus
`kicad-cli` accepting what we write. Byte-matching a third party's whitespace
is not a requirement of anything in this project.
"""

from __future__ import annotations

from typing import TypeAlias, Union

Atom: TypeAlias = str
Node: TypeAlias = list[Union[Atom, "Node"]]

# Parents whose list children are packed several-per-line rather than one per
# line. Value is the max children per line.
PACKED_CHILDREN: dict[str, int] = {"pts": 8}

# Soft column limit used alongside the per-line ceiling above. Chosen from the
# observed range (see module docstring); it makes output look native without
# claiming to be KiCad's exact rule.
PACK_WIDTH = 120

# Tokens KiCad always writes multi-line even when every child is an atom.
MULTILINE_ALWAYS: frozenset[str] = frozenset()

# Tokens KiCad always writes on a single line even when they contain child
# lists.
INLINE_ALWAYS: frozenset[str] = frozenset()


class SexprError(ValueError):
    """Raised when input is not well-formed S-expression source."""


def parse(text: str) -> Node:
    """Parse KiCad S-expression source into a nested list of raw tokens.

    Returns the single top-level node. Raises SexprError on malformed input.
    """
    pos = 0
    n = len(text)
    stack: list[Node] = []
    current: Node | None = None
    root: Node | None = None

    while pos < n:
        ch = text[pos]

        if ch in " \t\r\n":
            pos += 1
            continue

        if ch == "(":
            new: Node = []
            if current is not None:
                current.append(new)
                stack.append(current)
            current = new
            pos += 1
            continue

        if ch == ")":
            if current is None:
                raise SexprError(f"unbalanced ')' at offset {pos}")
            if stack:
                current = stack.pop()
            else:
                if root is not None:
                    raise SexprError(f"multiple top-level nodes at offset {pos}")
                root = current
                current = None
            pos += 1
            continue

        if ch == '"':
            start = pos
            pos += 1
            while pos < n:
                if text[pos] == "\\":
                    pos += 2
                    continue
                if text[pos] == '"':
                    pos += 1
                    break
                pos += 1
            else:
                raise SexprError(f"unterminated string starting at offset {start}")
            if current is None:
                raise SexprError(f"atom outside any list at offset {start}")
            current.append(text[start:pos])
            continue

        # Bare atom: runs until whitespace or a paren.
        start = pos
        while pos < n and text[pos] not in ' \t\r\n()"':
            pos += 1
        if pos == start:
            raise SexprError(f"unexpected character {ch!r} at offset {start}")
        if current is None:
            raise SexprError(f"atom outside any list at offset {start}")
        current.append(text[start:pos])

    if current is not None or root is None:
        raise SexprError("unexpected end of input: unbalanced '('")
    return root


def _head(node: Node) -> str | None:
    """The node's leading token, if it has one."""
    if node and isinstance(node[0], str):
        return node[0]
    return None


def _is_inline(node: Node) -> bool:
    """True if KiCad would write this node on a single line."""
    head = _head(node)
    if head in INLINE_ALWAYS:
        return True
    if head in MULTILINE_ALWAYS:
        return False
    return all(isinstance(child, str) for child in node)


def write(node: Node, indent: int = 0) -> str:
    """Render a node back to KiCad's exact on-disk formatting."""
    pad = "\t" * indent
    head = _head(node)

    if _is_inline(node):
        return f"{pad}({' '.join(c for c in node if isinstance(c, str))})"

    # Leading atoms share the opening line; every child list gets its own.
    idx = 0
    while idx < len(node) and isinstance(node[idx], str):
        idx += 1
    lead = [c for c in node[:idx] if isinstance(c, str)]

    lines = [f"{pad}(" + " ".join(lead)]

    per_line = PACKED_CHILDREN.get(head or "")
    if per_line is not None:
        lines.extend(_pack(node[idx:], indent + 1, per_line))
    else:
        for child in node[idx:]:
            if isinstance(child, str):
                # An atom after a list is rare but legal; give it its own line.
                lines.append("\t" * (indent + 1) + child)
            else:
                lines.append(write(child, indent + 1))

    lines.append(f"{pad})")
    return "\n".join(lines)


def _pack(children: list[Atom | Node], indent: int, per_line: int) -> list[str]:
    """Lay out children several per line, as KiCad does inside `pts`."""
    pad = "\t" * indent
    lines: list[str] = []
    row: list[str] = []

    def flush() -> None:
        if row:
            lines.append(pad + " ".join(row))
            row.clear()

    for child in children:
        text = child if isinstance(child, str) else write(child, 0)
        width = len(pad) + sum(len(t) + 1 for t in row) + len(text)
        if row and (len(row) >= per_line or width > PACK_WIDTH):
            flush()
        row.append(text)
    flush()
    return lines


def dumps(node: Node) -> str:
    """Render a full file, including the trailing newline KiCad writes."""
    return write(node, 0) + "\n"


def fmt_num(value: float, precision: int = 6) -> str:
    """Format a number the way KiCad does: no trailing zeros, no trailing dot.

    Also normalises negative zero to "0", which KiCad never writes as "-0".
    """
    if value == 0:
        return "0"
    text = f"{value:.{precision}f}".rstrip("0").rstrip(".")
    return text or "0"


def quote(value: str) -> str:
    """Quote a string atom the way KiCad does."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def unquote(value: str) -> str:
    """The exact inverse of `quote`: strip the quoting the parser preserves.

    Lives here, next to `quote`, rather than in the index reader where it was
    first written. A validator that needs to read a quoted atom must not have
    to import the local-library index to do it — tier T2 asserts that the index
    is not in its import graph at all (see `kifab/generate/__init__.py`), and
    this one misplaced helper was the only thing that made it so.
    """
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def find(node: Node, token: str) -> Node | None:
    """First direct child list whose head token matches."""
    for child in node:
        if isinstance(child, list) and _head(child) == token:
            return child
    return None


def find_all(node: Node, token: str) -> list[Node]:
    """All direct child lists whose head token matches."""
    return [
        child
        for child in node
        if isinstance(child, list) and _head(child) == token
    ]
