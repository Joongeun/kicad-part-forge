"""Publish the built library as a KiCad Plugin & Content Manager repository.

The point of this module is that *other designers never run kifab*. CI rebuilds
`build/` from `parts/`, this turns it into a PCM repository, GitHub Pages serves
it for free, and a KiCad user pastes one URL into Preferences -> Plugin and
Content Manager -> Manage. From then on they get an Update button.

Three documents, and they nest:

    repository.json   -> points at packages.json (url + sha256 + timestamp)
    packages.json     -> one Package per library, each version pointing at a .zip
    <pkg>-<ver>.zip   -> metadata.json + symbols/ + footprints/ + 3dmodels/

The shape is not guessed. KiCad ships `pcm.v1.schema.json` and the archive's
top-level directory names are the literal strings in the KiCad binary
(`plugins`, `footprints`, `3dmodels`, `symbols`, `resources`); `validate()`
below checks our output against that schema rather than against our reading of
it.

Two deliberate choices worth knowing:

* **The archive is byte-reproducible.** Fixed zip timestamps, sorted entries,
  fixed permissions. A rebuild that changed no part must produce the same
  sha256 — otherwise every CI run would look like a new release to KiCad and
  users would get an Update button that updates nothing.
* **Version defaults to the UTC date**, not to `kifab.__version__`. The library
  content changes independently of the tool that built it, and PCM decides
  "is there an update?" by comparing version strings.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "PCM_SCHEMA_URL",
    "Publication",
    "RepoIdentity",
    "find_schema",
    "publish",
    "validate",
]

PCM_SCHEMA_URL = "https://go.kicad.org/pcm/schemas/v1"

# The minimum KiCad that can read what we emit. Our symbols are format version
# 20241209 and our footprints 20241229 — both KiCad 9.
KICAD_VERSION = "9.0"

# A fixed, valid zip timestamp (zip cannot store anything before 1980). Every
# entry gets it, so the archive is a pure function of its contents.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

_SCHEMA_CANDIDATES = (
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/schemas/pcm.v1.schema.json",
    "/usr/share/kicad/schemas/pcm.v1.schema.json",
    "/usr/local/share/kicad/schemas/pcm.v1.schema.json",
    "C:/Program Files/KiCad/9.0/share/kicad/schemas/pcm.v1.schema.json",
)

# Vendored verbatim from KiCad 9.0.4:
#   /Applications/KiCad/KiCad.app/Contents/SharedSupport/schemas/pcm.v1.schema.json
#   sha256 fbd4338169142cafcef38baf1154e22f64216ac950f0698eab22fff85e94dbed
# It lives *inside the package*, not in vendor/, on purpose: a `uvx kifab` user
# has no repo checkout, and CI has no KiCad. A validator that is only present in
# a source tree is a validator that never runs where it matters.
_VENDORED_SCHEMA = Path(__file__).with_name("pcm.v1.schema.json")


class PcmError(Exception):
    """The repository could not be built, or does not match KiCad's schema."""


@dataclass(frozen=True)
class RepoIdentity:
    """Everything about the repository that is not derived from the files."""

    base_url: str
    identifier: str = "com.github.kifab.library"
    name: str = "KiCad Part Forge library"
    description: str = "Symbols and footprints built and validated by kifab"
    description_full: str = (
        "Every part in this library is generated from a reviewable Part IR YAML "
        "file, emitted by kifab's own S-expression writer in one house style, and "
        "gated by kicad-cli format conformance, KLC lint and IPC-7351B geometry "
        "checks before it is published."
    )
    # PCM's licence field is an enum. The library is *content*, not code: KiCad's
    # own libraries are CC-BY-SA-4.0, so a derived library that people drop into
    # their boards should be too. The tool itself is GPL-3.0-or-later; that is a
    # separate question and lives in pyproject.toml.
    license: str = "CC-BY-SA-4.0"
    author: str = "KiCad Part Forge"
    homepage: str = "https://github.com/clash/kicad-part-forge"
    tags: tuple[str, ...] = ("kicad", "footprints", "symbols", "kifab")
    status: str = "stable"

    def contact(self) -> dict:
        return {"name": self.author, "contact": {"web": self.homepage}}


@dataclass
class Publication:
    """What `publish()` wrote, in a form the CLI and the tests can both read."""

    out_dir: Path
    archive: Path
    version: str
    sha256: str
    download_size: int
    install_size: int
    entries: list[str] = field(default_factory=list)
    repository: dict = field(default_factory=dict)
    packages: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    @property
    def repository_url(self) -> str:
        return str(self.repository.get("packages", {}).get("url", "")).rsplit("/", 1)[0] + "/repository.json"


def default_version(now: datetime | None = None) -> str:
    """`YYYY.MM.DD` — matches PCM's `\\d{1,4}(\\.\\d{1,4}(\\.\\d{1,6})?)?`.

    Date-based because the *library* is what is versioned here, and it changes
    when someone adds a part, not when kifab is released. Two publishes on the
    same day carry the same version: pass `--version` in that case.
    """
    now = now or datetime.now(timezone.utc)
    return f"{now.year}.{now.month:02d}.{now.day:02d}"


def collect(build_dir: Path) -> list[tuple[str, Path]]:
    """Map `build/` onto PCM's archive layout, sorted and deduplicated.

    KiCad installs `symbols/` into `${KICAD9_3RD_PARTY}/symbols/<identifier>/`
    and `footprints/` into `.../footprints/<identifier>/`, then adds the
    `.kicad_sym` files and `.pretty` directories it finds there to the library
    tables. So the `.pretty` directory name has to survive into the archive.
    """
    build_dir = Path(build_dir)
    if not build_dir.is_dir():
        raise PcmError(f"no such build directory: {build_dir}")

    entries: list[tuple[str, Path]] = []
    for sym in sorted(build_dir.glob("*.kicad_sym")):
        entries.append((f"symbols/{sym.name}", sym))
    for pretty in sorted(build_dir.glob("*.pretty")):
        for mod in sorted(pretty.glob("*.kicad_mod")):
            entries.append((f"footprints/{pretty.name}/{mod.name}", mod))
    for shapes in sorted(build_dir.glob("*.3dshapes")):
        for model in sorted(p for p in shapes.rglob("*") if p.is_file()):
            entries.append((f"3dmodels/{shapes.name}/{model.relative_to(shapes)}", model))

    if not entries:
        raise PcmError(
            f"{build_dir} holds no .kicad_sym and no .pretty/ — run `kifab build` first"
        )
    return sorted(entries)


def _write_archive(dest: Path, entries: list[tuple[str, Path]], metadata: dict) -> tuple[str, int, int]:
    """Write the package zip reproducibly. Returns (sha256, size, install_size)."""
    payload = json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    members: list[tuple[str, bytes]] = [("metadata.json", payload)]
    members += [(arcname, path.read_bytes()) for arcname, path in entries]

    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, data in members:
            info = zipfile.ZipInfo(arcname, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3  # unix, so the mode above is honoured
            zf.writestr(info, data)

    blob = dest.read_bytes()
    return (
        hashlib.sha256(blob).hexdigest(),
        len(blob),
        sum(len(data) for _, data in members),
    )


def _package(identity: RepoIdentity, version: dict) -> dict:
    """A PCM `Package`. Every key here is required by the schema."""
    return {
        "name": identity.name,
        "description": identity.description,
        "description_full": identity.description_full,
        "identifier": identity.identifier,
        "type": "library",
        "author": identity.contact(),
        "maintainer": identity.contact(),
        "license": identity.license,
        "resources": {"homepage": identity.homepage},
        "tags": list(identity.tags),
        "versions": [version],
    }


def publish(
    build_dir: Path,
    out_dir: Path,
    identity: RepoIdentity,
    version: str | None = None,
    now: datetime | None = None,
) -> Publication:
    """Turn `build/` into a servable PCM repository under `out_dir`."""
    now = now or datetime.now(timezone.utc)
    version = version or default_version(now)
    base = identity.base_url.rstrip("/")
    out_dir = Path(out_dir)
    entries = collect(Path(build_dir))

    archive_name = f"{identity.identifier}-{version}.zip"
    archive_path = out_dir / archive_name

    # metadata.json travels *inside* the archive and therefore cannot name the
    # archive's own hash. PCM does not ask it to: download_* belong to
    # packages.json, which sits outside.
    inner_version = {
        "version": version,
        "status": identity.status,
        "kicad_version": KICAD_VERSION,
    }
    metadata = _package(identity, inner_version)
    sha, download_size, install_size = _write_archive(archive_path, entries, metadata)

    outer_version = dict(inner_version)
    outer_version.update(
        {
            "download_url": f"{base}/{archive_name}",
            "download_sha256": sha,
            "download_size": download_size,
            "install_size": install_size,
        }
    )
    packages = {"packages": [_package(identity, outer_version)]}
    packages_bytes = json.dumps(packages, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    (out_dir / "packages.json").write_bytes(packages_bytes)

    repository = {
        "$schema": PCM_SCHEMA_URL,
        "name": identity.name,
        "maintainer": identity.contact(),
        "packages": {
            "url": f"{base}/packages.json",
            "sha256": hashlib.sha256(packages_bytes).hexdigest(),
            "update_timestamp": int(now.timestamp()),
            "update_time_utc": now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
    (out_dir / "repository.json").write_bytes(
        json.dumps(repository, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    )

    return Publication(
        out_dir=out_dir,
        archive=archive_path,
        version=version,
        sha256=sha,
        download_size=download_size,
        install_size=install_size,
        entries=[arcname for arcname, _ in entries],
        repository=repository,
        packages=packages,
        metadata=metadata,
    )


def find_schema(explicit: Path | None = None) -> Path | None:
    """KiCad's own copy if it is installed, else the vendored one.

    Preferring the installed copy is the whole value: when KiCad 10 changes the
    schema, the first `kifab pcm` run on an upgraded machine says so.
    """
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise PcmError(f"no such schema: {path}")
        return path
    for candidate in _SCHEMA_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return path
    return _VENDORED_SCHEMA if _VENDORED_SCHEMA.is_file() else None


def validate(publication: Publication, schema_path: Path) -> list[str]:
    """Check the three documents against KiCad's schema. Returns problems.

    Raises `PcmError` if `jsonschema` is not installed — a validator that
    quietly does nothing is worse than no validator, so the caller decides
    whether to skip, and says so out loud when it does.
    """
    try:
        import jsonschema  # noqa: PLC0415 — optional, dev-only dependency
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        raise PcmError(
            "jsonschema is not installed, so the PCM documents cannot be checked "
            "against KiCad's schema (pip install jsonschema)"
        ) from exc

    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    definitions = schema.get("definitions", {})

    def check(label: str, document: dict, definition: str) -> list[str]:
        sub = dict(schema)
        sub.pop("$ref", None)
        sub["$ref"] = f"#/definitions/{definition}"
        validator = jsonschema.Draft7Validator(sub)
        if definition not in definitions:
            return [f"{label}: KiCad's schema has no definition {definition!r}"]
        return [
            f"{label}: {'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
            for error in sorted(validator.iter_errors(document), key=str)
        ]

    problems = check("repository.json", publication.repository, "Repository")
    problems += check("packages.json", publication.packages, "PackageArray")
    problems += check("metadata.json", publication.metadata, "Package")
    return problems
