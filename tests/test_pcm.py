"""The PCM repository we publish, judged by KiCad's own schema.

This layer is pure distribution: nothing here changes a pad. What it can do is
publish a repository KiCad silently refuses to load, which is a failure nobody
notices until a stranger tries to subscribe. So the tests are mostly "does
KiCad's shipped `pcm.v1.schema.json` accept this", plus the two properties the
schema cannot express: the archive's internal layout, and reproducibility.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kifab.build import build
from kifab.cli import main
from kifab.ir import load_part
from kifab.pcm import PcmError, Publication, RepoIdentity, find_schema, publish, validate

BASE_URL = "https://example.github.io/kicad-part-forge"
PARTS = Path(__file__).resolve().parents[1] / "parts"


@pytest.fixture
def built(tmp_path: Path) -> Path:
    """A real built library — the same artefacts `kifab build` writes."""
    parts = [load_part(p) for p in sorted(PARTS.glob("*.yaml"))]
    assert parts, "the corpus in parts/ is what this test publishes"
    out = tmp_path / "build"
    build(parts, out)
    return out


@pytest.fixture
def published(built: Path, tmp_path: Path) -> Publication:
    return publish(
        built,
        tmp_path / "site",
        RepoIdentity(base_url=BASE_URL),
        version="2026.08.17",
        now=datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc),
    )


def test_schema_is_findable_without_kicad():
    """The schema must ship inside the package, not only in the repo.

    A `uvx kifab` user has no checkout and CI has no KiCad; if this file were
    only reachable through a source tree, validation would silently skip in
    exactly the two places it has to be automatic.
    """
    schema = find_schema()
    assert schema is not None and schema.is_file()
    packaged = Path(__import__("kifab").__file__).parent / "pcm.v1.schema.json"
    assert packaged.is_file(), "pcm.v1.schema.json is not package data"
    assert json.loads(packaged.read_text())["$id"] == "https://go.kicad.org/pcm/schemas/v1"


def test_repository_matches_kicads_schema(published: Publication):
    """The gate: KiCad's parser spec judges our output, not our reading of it."""
    schema = find_schema()
    assert schema is not None
    assert validate(published, schema) == []


def test_repository_matches_the_packaged_schema_too(published: Publication):
    """Pin the vendored copy as well, so CI is checking the same thing macOS is."""
    packaged = Path(__import__("kifab").__file__).parent / "pcm.v1.schema.json"
    assert validate(published, packaged) == []


def test_archive_layout_is_what_kicad_installs(published: Publication):
    """`symbols/` and `footprints/<lib>.pretty/` — the names KiCad looks for.

    KiCad unpacks a library package into `${KICAD9_3RD_PARTY}/symbols/<id>/` and
    `.../footprints/<id>/`, then adds the `.kicad_sym` files and `.pretty`
    directories it finds there to the library tables. The `.pretty` suffix has
    to survive into the archive or the footprint library never appears.
    """
    with zipfile.ZipFile(published.archive) as zf:
        names = sorted(zf.namelist())
        metadata = json.loads(zf.read("metadata.json"))

    assert "metadata.json" in names
    assert any(n.startswith("symbols/") and n.endswith(".kicad_sym") for n in names)
    assert any(
        n.startswith("footprints/") and ".pretty/" in n and n.endswith(".kicad_mod")
        for n in names
    )
    assert not any(n.startswith("build/") or n.startswith("/") for n in names)
    assert metadata["type"] == "library"
    # download_* describe the archive and therefore cannot live inside it.
    assert set(metadata["versions"][0]) == {"version", "status", "kicad_version"}


def test_hashes_and_sizes_describe_the_real_files(published: Publication):
    """A wrong sha256 makes KiCad reject the download with no useful message."""
    blob = published.archive.read_bytes()
    version = published.packages["packages"][0]["versions"][0]
    assert version["download_sha256"] == hashlib.sha256(blob).hexdigest()
    assert version["download_size"] == len(blob)
    assert version["download_url"] == f"{BASE_URL}/{published.archive.name}"

    packages_bytes = (published.out_dir / "packages.json").read_bytes()
    assert published.repository["packages"]["sha256"] == hashlib.sha256(packages_bytes).hexdigest()
    assert published.repository["packages"]["url"] == f"{BASE_URL}/packages.json"

    with zipfile.ZipFile(published.archive) as zf:
        uncompressed = sum(i.file_size for i in zf.infolist())
    assert version["install_size"] == uncompressed


def test_archive_is_reproducible(built: Path, tmp_path: Path):
    """Same parts in, same bytes out.

    Otherwise every CI run publishes a new sha256, KiCad sees a changed package
    and offers an Update that updates nothing — the fastest way to teach users
    to ignore the Update button.
    """
    first = publish(built, tmp_path / "a", RepoIdentity(base_url=BASE_URL), version="1.0.0")
    second = publish(built, tmp_path / "b", RepoIdentity(base_url=BASE_URL), version="1.0.0")
    assert first.sha256 == second.sha256
    assert first.archive.read_bytes() == second.archive.read_bytes()


def test_changed_content_changes_the_hash(built: Path, tmp_path: Path):
    """The negative half of reproducibility: it must not be constant."""
    first = publish(built, tmp_path / "a", RepoIdentity(base_url=BASE_URL), version="1.0.0")
    next(built.glob("*.pretty")).joinpath("extra.kicad_mod").write_text("(module extra)\n")
    second = publish(built, tmp_path / "b", RepoIdentity(base_url=BASE_URL), version="1.0.0")
    assert first.sha256 != second.sha256


def test_empty_build_directory_is_refused(tmp_path: Path):
    """Publishing an empty repository would look like a successful release."""
    empty = tmp_path / "build"
    empty.mkdir()
    with pytest.raises(PcmError, match="run `kifab build` first"):
        publish(empty, tmp_path / "site", RepoIdentity(base_url=BASE_URL))
    with pytest.raises(PcmError, match="no such build directory"):
        publish(tmp_path / "nope", tmp_path / "site", RepoIdentity(base_url=BASE_URL))


def test_default_version_is_a_legal_pcm_version():
    """PCM's version pattern is `\\d{1,4}(\\.\\d{1,4}(\\.\\d{1,6})?)?`."""
    import re

    from kifab.pcm import default_version

    assert re.fullmatch(r"\d{1,4}(\.\d{1,4}(\.\d{1,6})?)?", default_version())


def test_bad_identifier_is_caught_by_validation(built: Path, tmp_path: Path):
    """Proof the validator is wired up and not just returning [] for everything."""
    bad = publish(
        built,
        tmp_path / "site",
        RepoIdentity(base_url=BASE_URL, identifier="1-starts-with-a-digit"),
        version="1.0.0",
    )
    schema = find_schema()
    assert schema is not None
    problems = validate(bad, schema)
    assert problems, "an identifier violating PCM's pattern must be reported"
    assert any("identifier" in p for p in problems)


def test_cli_publishes_and_validates(built: Path, tmp_path: Path, capsys):
    site = tmp_path / "site"
    code = main(
        [
            "pcm",
            str(built),
            "-o",
            str(site),
            "--base-url",
            BASE_URL + "/",  # trailing slash must not double up
            "--pcm-version",
            "2026.08.17",
            "--json",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == "2026.08.17"
    assert (site / "repository.json").is_file()
    assert (site / "packages.json").is_file()
    packages = json.loads((site / "packages.json").read_text())
    assert packages["packages"][0]["versions"][0]["download_url"] == (
        f"{BASE_URL}/com.github.kifab.library-2026.08.17.zip"
    )
