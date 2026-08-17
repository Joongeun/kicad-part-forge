"""Part-DB registration — the downstream end of the pipeline.

Nothing here touches a network. The Part-DB REST API is exercised against
`tests/partdb_fake.py`, and the response shapes are pinned against fixtures in
`tests/fixtures/partdb/`.

**Honesty about what these fixtures are:** they are transcribed from Part-DB's
entity definitions and documented responses (v2.15.0) and from KiCad's HTTP
library specification — not captured from a live instance, because there is no
Part-DB reachable from this machine. They therefore prove that kifab is
*self-consistent with the documented contract*, and would catch a regression in
our own parsing or planning. They cannot prove the documented contract is what
a real server does. The one test that talks to a real server is marked `live`
and is deselected by default.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from kifab.cli import main
from kifab.ir import load_part
from kifab.partdb import (
    Action,
    PartDbClient,
    PartDbError,
    PartDbHttpError,
    RemotePart,
    SyncState,
    apply_plan,
    desired_from_part,
    httplib_document,
    plan_sync,
    write_httplib,
)
from kifab.partdb.client import Response, normalise_base_url
from kifab.partdb.httplib import render_httplib
from partdb_fake import FakePartDb

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "partdb"
URL = "http://localhost:8080"
TOKEN = "tcp_notarealtoken"


@pytest.fixture
def part():
    return load_part(ROOT / "parts" / "24LC256.yaml")


@pytest.fixture
def server() -> FakePartDb:
    return FakePartDb()


@pytest.fixture
def client(server: FakePartDb) -> PartDbClient:
    return PartDbClient(URL, TOKEN, transport=server)


def _state(tmp_path: Path) -> SyncState:
    return SyncState.load(tmp_path / "partdb-sync.json")


def _sync(client, parts, state, force=False):
    plan = plan_sync(client, parts, state, force=force)
    return apply_plan(client, plan, state)


# ---------------------------------------------------------------------------
# The URL, which users paste from wherever they happen to be
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given",
    [
        "http://localhost:8080",
        "http://localhost:8080/",
        "http://localhost:8080/en",
        "http://localhost:8080/en/",
        "http://localhost:8080/api",
        "http://localhost:8080/en/parts",
        "http://localhost:8080/en/kicad-api/v1",
    ],
)
def test_base_url_is_reduced_to_the_instance_root(given: str) -> None:
    assert normalise_base_url(given) == "http://localhost:8080"


def test_a_url_on_a_subpath_keeps_the_mount_point() -> None:
    assert normalise_base_url("https://host/partdb/en/parts") == "https://host/partdb"


@pytest.mark.parametrize("given", ["", "ftp://host", "https://"])
def test_a_url_that_cannot_work_is_refused(given: str) -> None:
    with pytest.raises(PartDbError):
        normalise_base_url(given)


def test_a_client_without_a_token_says_where_to_get_one() -> None:
    with pytest.raises(PartDbError, match="API tokens"):
        PartDbClient(URL, "")


# ---------------------------------------------------------------------------
# .kicad_httplib — the read side
# ---------------------------------------------------------------------------


def test_httplib_matches_the_shape_kicad_documents() -> None:
    doc = httplib_document(URL, TOKEN)
    assert doc["meta"] == {"version": 1.0}
    source = doc["source"]
    assert source["type"] == "REST_API"
    assert source["api_version"] == "v1"
    assert source["token"] == TOKEN
    assert source["timeout_parts_seconds"] == 60
    assert source["timeout_categories_seconds"] == 600


def test_root_url_stops_before_the_api_version() -> None:
    """KiCad appends `api_version` itself; `/v1/v1/` lists nothing, silently."""
    source = httplib_document(URL, TOKEN)["source"]
    assert source["root_url"] == "http://localhost:8080/en/kicad-api/"
    assert not source["root_url"].rstrip("/").endswith("v1")
    # The URL KiCad will actually build.
    assert (
        source["root_url"] + source["api_version"] + "/parts/42.json"
        == "http://localhost:8080/en/kicad-api/v1/parts/42.json"
    )


def test_a_pasted_kicad_api_url_does_not_get_doubled() -> None:
    source = httplib_document("http://localhost:8080/en/kicad-api/v1", TOKEN)["source"]
    assert source["root_url"] == "http://localhost:8080/en/kicad-api/"


def test_a_non_english_instance_can_be_pointed_at() -> None:
    doc = httplib_document(URL, TOKEN, locale="de")
    assert doc["source"]["root_url"] == "http://localhost:8080/de/kicad-api/"


def test_httplib_without_a_token_is_refused() -> None:
    with pytest.raises(ValueError, match="token"):
        httplib_document(URL, "  ")


def test_httplib_file_is_owner_only_and_byte_stable(tmp_path: Path) -> None:
    doc = httplib_document(URL, TOKEN)
    path = write_httplib(tmp_path / "partdb", doc)
    assert path.name == "partdb.kicad_httplib"
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0, "the file holds a live token"
    first = path.read_bytes()
    write_httplib(path, httplib_document(URL, TOKEN))
    assert path.read_bytes() == first
    assert json.loads(render_httplib(doc)) == doc


def test_kicad_serves_only_strings(part) -> None:
    """KiCad's hard requirement, pinned against a recorded response shape.

    Every value KiCad receives from an HTTP library must be a string. That is
    why `DesiredPart` is entirely `str` — asserted here too, so a future field
    typed as an int or a bool fails a test rather than a KiCad import.
    """
    payload = json.loads((FIXTURES / "kicad_part.json").read_text())
    leaves: list[object] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        else:
            leaves.append(node)

    walk(payload)
    assert leaves and all(isinstance(v, str) for v in leaves)

    desired = desired_from_part(part)
    assert all(isinstance(v, str) for v in vars(desired).values())


# ---------------------------------------------------------------------------
# Projecting the IR onto Part-DB
# ---------------------------------------------------------------------------


def test_desired_uses_lib_ids_not_bare_names(part) -> None:
    """Part-DB's `kicad_symbol` becomes KiCad's `symbolIdStr` verbatim."""
    desired = desired_from_part(part)
    assert desired.kicad_symbol == "kifab:24LC256"
    assert desired.kicad_footprint == "kifab:SOIC-8_3.9x4.9mm_P1.27mm"
    assert desired.reference_prefix == "U"
    assert desired.value == "24LC256"


def test_patching_touches_only_the_eda_fields(part) -> None:
    """Stock, storage, price and supplier data are the user's, not ours."""
    assert set(desired_from_part(part).patch_payload()) == {"eda_info"}


def test_parsing_a_recorded_part_item() -> None:
    remote = RemotePart.from_json(json.loads((FIXTURES / "part_item.json").read_text()))
    assert remote.iri == "/api/parts/42"
    assert remote.mpn == "24LC256"
    assert remote.eda_field("kicad_symbol") == "kifab:24LC256"
    # Part-DB writes unset EDA members as null; KiCad wants strings, so null
    # and "" must read the same or every unset field looks like drift.
    assert remote.eda_field("visibility") == ""
    assert remote.eda_field("nonexistent") == ""


def test_a_collection_response_is_walked_and_filtered(client, server) -> None:
    """`?manufacturer_product_number=` is an ILIKE, so results are re-checked."""
    recorded = json.loads((FIXTURES / "parts_collection.json").read_text())
    server.parts = {
        m["id"]: {**m, "eda_info": {}} for m in recorded["hydra:member"]
    }
    assert [p.iri for p in client.find_parts_by_mpn("24LC256")] == ["/api/parts/42"]
    # Case-insensitively equal is still the same part, and must not be
    # duplicated by a case-sensitive comparison on our side.
    assert [p.iri for p in client.find_parts_by_mpn("24lc256")] == ["/api/parts/42"]


# ---------------------------------------------------------------------------
# Idempotency — the property the whole command exists to have
# ---------------------------------------------------------------------------


def test_first_sync_creates_second_sync_writes_nothing(client, server, part, tmp_path):
    state = _state(tmp_path)
    first = _sync(client, [part], state)
    assert [s.action for s in first.steps] == [Action.CREATE]
    assert server.writes, "the first run must actually create the part"
    assert server.eda("/api/parts/1")["kicad_symbol"] == "kifab:24LC256"

    server.calls.clear()
    state = _state(tmp_path)  # a fresh process, reading the committed state
    second = _sync(client, [part], state)
    assert [s.action for s in second.steps] == [Action.UNCHANGED]
    assert server.writes == [], f"a second sync must write nothing: {server.writes}"


def test_the_state_file_is_byte_stable_across_runs(client, part, tmp_path):
    state = _state(tmp_path)
    _sync(client, [part], state)
    first = state.path.read_bytes()
    state = _state(tmp_path)
    _sync(client, [part], state)
    assert state.path.read_bytes() == first


def test_a_part_created_by_hand_is_adopted_not_duplicated(
    client, server, part, tmp_path
):
    """The common case: the chip was in inventory before anyone drew it."""
    server.add_part(
        name="24LC256 EEPROM",
        description="bought 100, in drawer B4",
        manufacturer_product_number="24LC256",
    )
    plan = _sync(client, [part], _state(tmp_path))
    assert [s.action for s in plan.steps] == [Action.UPDATE]
    assert len(server.parts) == 1, "adopting must not create a second row"
    row = server.parts[1]
    assert row["eda_info"]["kicad_symbol"] == "kifab:24LC256"
    assert row["description"] == "bought 100, in drawer B4", "inventory data is theirs"


def test_the_collection_gap_does_not_cause_perpetual_updates(
    client, server, part, tmp_path
):
    """Regression guard for the trap in Part-DB's serialisation groups.

    `eda_info` comes back on `GET /api/parts/{id}` but not on the collection.
    A sync that compared against the collection row would see four empty fields
    every run and PATCH forever. `FakePartDb` reproduces that split, so this
    test fails if `plan_sync` stops re-reading the item.
    """
    _sync(client, [part], _state(tmp_path))
    server.calls.clear()
    _sync(client, [part], _state(tmp_path))
    assert ("GET", "/api/parts/1") in server.calls
    assert server.writes == []


def test_editing_parts_yaml_produces_exactly_one_patch(
    client, server, part, tmp_path
):
    state = _state(tmp_path)
    _sync(client, [part], state)
    moved = part.model_copy(update={"reference": "IC"})
    server.calls.clear()
    plan = _sync(client, [moved], _state(tmp_path))
    assert [s.action for s in plan.steps] == [Action.UPDATE]
    assert plan.steps[0].differences["reference_prefix"] == ("U", "IC")
    assert server.writes == [("PATCH", "/api/parts/1")]
    assert server.eda("/api/parts/1")["reference_prefix"] == "IC"


# ---------------------------------------------------------------------------
# Drift — reported, never silently overwritten
# ---------------------------------------------------------------------------


def test_a_hand_edit_in_partdb_is_a_conflict_not_an_overwrite(
    client, server, part, tmp_path
):
    _sync(client, [part], _state(tmp_path))
    server.eda("/api/parts/1")["kicad_symbol"] = "Memory_EEPROM:24LC256"

    server.calls.clear()
    plan = plan_sync(client, [part], _state(tmp_path))
    step = plan.steps[0]
    assert step.action is Action.CONFLICT
    assert step.conflicts["kicad_symbol"] == (
        "Memory_EEPROM:24LC256",
        "kifab:24LC256",
        "kifab:24LC256",
    )
    assert not plan.ok()
    apply_plan(client, plan, _state(tmp_path))
    assert server.writes == [], "a conflict must leave Part-DB alone"
    assert server.eda("/api/parts/1")["kicad_symbol"] == "Memory_EEPROM:24LC256"
    assert "--force" in step.describe()


def test_force_resolves_a_conflict_in_favour_of_parts(client, server, part, tmp_path):
    _sync(client, [part], _state(tmp_path))
    server.eda("/api/parts/1")["kicad_symbol"] = "Memory_EEPROM:24LC256"
    plan = _sync(client, [part], _state(tmp_path), force=True)
    assert [s.action for s in plan.steps] == [Action.UPDATE]
    assert server.eda("/api/parts/1")["kicad_symbol"] == "kifab:24LC256"


def test_a_preexisting_eda_value_is_a_conflict_on_the_very_first_sync(
    client, server, part, tmp_path
):
    """No record of what we wrote means we have no right to assume it was ours."""
    server.add_part(
        name="24LC256",
        manufacturer_product_number="24LC256",
        eda_info={"kicad_symbol": "Memory_EEPROM:24LC256"},
    )
    plan = plan_sync(client, [part], _state(tmp_path))
    assert plan.steps[0].action is Action.CONFLICT
    # ...but a field nobody has ever set is not drift, it is a blank to fill.
    assert "reference_prefix" in plan.steps[0].differences


def test_two_rows_with_the_same_mpn_are_reported_not_guessed(
    client, server, part, tmp_path
):
    server.add_part(name="a", manufacturer_product_number="24LC256")
    server.add_part(name="b", manufacturer_product_number="24LC256")
    plan = _sync(client, [part], _state(tmp_path))
    step = plan.steps[0]
    assert step.action is Action.AMBIGUOUS
    assert "/api/parts/1" in step.note and "/api/parts/2" in step.note
    assert not plan.ok()
    assert server.writes == []


def test_a_part_removed_from_parts_is_reported_not_deleted(
    client, server, part, tmp_path
):
    state = _state(tmp_path)
    _sync(client, [part], state)
    plan = plan_sync(client, [], _state(tmp_path))
    assert plan.stale == ["24LC256"]
    assert "not kifab's to delete" in plan.format()
    assert len(server.parts) == 1


# ---------------------------------------------------------------------------
# Failure reporting — a wrong token must not read as a wrong part
# ---------------------------------------------------------------------------


def test_401_names_the_permission_that_is_usually_missing(client, server, part, tmp_path):
    server.fail_with = (401, {})
    with pytest.raises(PartDbHttpError, match="Access the API"):
        plan_sync(client, [part], _state(tmp_path))


def test_a_rejected_field_is_quoted_back(client, server, part, tmp_path):
    server.fail_with = (422, {"hydra:description": "category: This value should not be null."})
    with pytest.raises(PartDbHttpError, match="should not be null"):
        plan_sync(client, [part], _state(tmp_path))


def test_html_from_a_non_partdb_host_says_so() -> None:
    """A 200 that is not JSON is the 'you pointed this at your router' case."""

    def html(method, url, body, headers):
        return Response(200, b"<html>hello</html>")

    broken = PartDbClient(URL, TOKEN, transport=html)
    with pytest.raises(PartDbError, match="really a Part-DB instance"):
        broken.find_parts_by_mpn("24LC256")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_httplib_command_prints_the_document(capsys) -> None:
    assert main(["httplib", "--url", URL, "--token", TOKEN, "--stdout"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["source"]["root_url"] == "http://localhost:8080/en/kicad-api/"


def test_httplib_command_reads_the_token_from_a_file(tmp_path: Path, capsys) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    out = tmp_path / "partdb.kicad_httplib"
    assert (
        main(["httplib", "--url", URL, "--token-file", str(token_file), "-o", str(out)])
        == 0
    )
    assert json.loads(out.read_text())["source"]["token"] == TOKEN
    assert "do not commit" in capsys.readouterr().err


def test_sync_without_a_url_says_which_variable_to_set(monkeypatch, capsys) -> None:
    monkeypatch.delenv("PARTDB_URL", raising=False)
    monkeypatch.delenv("PARTDB_TOKEN", raising=False)
    monkeypatch.delenv("PARTDB_TOKEN_FILE", raising=False)
    assert main(["sync", "--state", os.devnull]) == 1
    assert "$PARTDB_URL" in capsys.readouterr().err


def test_sync_dry_run_writes_nothing(monkeypatch, tmp_path: Path, capsys) -> None:
    import kifab.cli as cli

    server = FakePartDb()
    monkeypatch.setattr(
        cli, "PartDbClient", lambda url, token: PartDbClient(url, token, transport=server)
    )
    state = tmp_path / "sync.json"
    code = main(
        [
            "sync",
            str(ROOT / "parts" / "24LC256.yaml"),
            "--url",
            URL,
            "--token",
            TOKEN,
            "--state",
            str(state),
            "--dry-run",
        ]
    )
    assert code == 0
    out = capsys.readouterr()
    assert "create" in out.out
    assert "nothing was written" in out.err
    assert server.writes == []
    assert not state.exists()


def test_sync_command_is_idempotent_end_to_end(monkeypatch, tmp_path: Path, capsys):
    import kifab.cli as cli

    server = FakePartDb()
    monkeypatch.setattr(
        cli, "PartDbClient", lambda url, token: PartDbClient(url, token, transport=server)
    )
    state = tmp_path / "sync.json"
    argv = [
        "sync",
        str(ROOT / "parts"),
        "--url",
        URL,
        "--token",
        TOKEN,
        "--state",
        str(state),
    ]
    assert main(argv) == 0
    first = state.read_bytes()
    server.calls.clear()
    capsys.readouterr()
    assert main(argv) == 0
    assert server.writes == []
    assert state.read_bytes() == first
    assert "unchanged" in capsys.readouterr().out


def test_sync_exits_non_zero_on_a_conflict(monkeypatch, tmp_path: Path):
    import kifab.cli as cli

    server = FakePartDb()
    server.add_part(
        name="24LC256",
        manufacturer_product_number="24LC256",
        eda_info={"kicad_symbol": "Memory_EEPROM:24LC256"},
    )
    monkeypatch.setattr(
        cli, "PartDbClient", lambda url, token: PartDbClient(url, token, transport=server)
    )
    code = main(
        [
            "sync",
            str(ROOT / "parts" / "24LC256.yaml"),
            "--url",
            URL,
            "--token",
            TOKEN,
            "--state",
            str(tmp_path / "sync.json"),
            "--json",
        ]
    )
    assert code == 1


# ---------------------------------------------------------------------------
# The one test that needs a real server
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_against_a_real_partdb_instance(tmp_path: Path) -> None:
    """Deselected by default. `PARTDB_URL=... PARTDB_TOKEN=... pytest -m live`.

    Everything above proves kifab agrees with Part-DB's *documented* contract.
    Only this proves it agrees with a running one. It is the acceptance test
    for `docker/README.md`, and it has never been run in CI because CI has no
    Part-DB.
    """
    url, token = os.environ.get("PARTDB_URL"), os.environ.get("PARTDB_TOKEN")
    if not url or not token:
        pytest.skip("set PARTDB_URL and PARTDB_TOKEN to run this")
    client = PartDbClient(url, token)
    part = load_part(ROOT / "parts" / "24LC256.yaml")
    state = SyncState.load(tmp_path / "sync.json")
    first = plan_sync(client, [part], state)
    apply_plan(client, first, state)
    assert first.ok(), first.format()
    second = plan_sync(client, [part], SyncState.load(tmp_path / "sync.json"))
    assert [s.action for s in second.steps] == [Action.UNCHANGED], second.format()
