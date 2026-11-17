"""The evidence script: reading, checking, summarizing and filing a diagnostics download."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from scripts import evidence

FIXTURES = pathlib.Path(__file__).parent.parent / "aioresideo" / "fixtures"
MAC = "AABBCCDDEEFF"
REDACTED = "**REDACTED**"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _push(at: str, value: Any, count: int = 1) -> dict[str, Any]:
    return {
        "merged_in_memory": True,
        "count": count,
        "first_seen": at,
        "last_seen": at,
        "shapes": [{"count": count, "samples": [{"at": at, "value": value}]}],
    }


def _download(**device: Any) -> dict[str, Any]:
    """A diagnostics file as Home Assistant wraps it, by an integration that scrubbed it."""
    from custom_components.resideo.diagnostics import scrub

    data = {
        "account": _fixture("accounts.json"),
        "account_devices": [
            {"model": "Denali_S1200", "kind": "LyricThermostatDevice", "supported": True}
        ],
        "capture_since": "2026-11-15T10:00:00.000+00:00",
        "devices": [
            {
                "shadow": _fixture("device.json"),
                "rooms": _fixture("rooms.json"),
                "configuration": _fixture("configuration.json"),
                "priority": {},
                "pushes": {},
                "writes": [],
                "drift": {"pushes_merged_since_read": 0, "shadow": [], "rooms": []},
                **device,
            }
        ],
    }
    return {
        "home_assistant": {"version": "2026.11.1", "timezone": "America/New_York"},
        "custom_components": {"resideo": {}, "something_else": {}},
        "integration_manifest": {"domain": "resideo", "version": "0.4.0"},
        "data": scrub(data, (MAC,)),
    }


def _write(tmp_path: pathlib.Path, content: Any, name: str = "download.json") -> pathlib.Path:
    path = tmp_path / name
    path.write_text(json.dumps(content))
    return path


@pytest.fixture
def homes(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setattr(evidence, "HOMES", tmp_path / "homes")
    return tmp_path / "homes"


# -- load ----------------------------------------------------------------------
def test_load_keeps_the_versions_and_drops_the_rest_of_the_host(tmp_path: pathlib.Path) -> None:
    loaded = evidence.load(_write(tmp_path, _download()))

    assert loaded["home"] == {
        "integration_version": "0.4.0",
        "home_assistant_version": "2026.11.1",
    }
    assert set(loaded) == {"home", "data"}
    assert "America/New_York" not in json.dumps(loaded)
    assert "something_else" not in json.dumps(loaded)


def test_load_takes_a_home_file_as_it_is(tmp_path: pathlib.Path) -> None:
    home = {"home": {"name": "t10"}, "data": {"devices": []}}

    assert evidence.load(_write(tmp_path, home)) == home


@pytest.mark.parametrize("content", [[], {}, {"data": {}}, {"data": {"states": []}}])
def test_load_refuses_what_is_not_a_resideo_diagnostics_file(
    tmp_path: pathlib.Path, content: Any
) -> None:
    with pytest.raises(evidence.EvidenceError, match="not a Resideo diagnostics file"):
        evidence.load(_write(tmp_path, content))


# -- check ---------------------------------------------------------------------
def test_a_scrubbed_download_has_no_leaks(tmp_path: pathlib.Path) -> None:
    assert evidence.find_leaks(evidence.load(_write(tmp_path, _download()))) == []


def test_leaks_are_named_by_path_and_never_by_value(tmp_path: pathlib.Path) -> None:
    download = _download()
    device = download["data"]["devices"][0]
    device["rooms"]["Rooms"][0]["Name"] = "Daniel's Room"
    device["writes"] = [{"error": {"message": f"PUT /device/{MAC}/hold -> 400"}}]
    device["shadow"]["Reported"]["Contact"] = "someone@example.com"
    device["shadow"]["Reported"]["GlobalId"] = "THlyaWNUaGVybW9zdGF0RGV2aWNlOkFBQkJDQ0RERUVGRg=="

    leaks = evidence.find_leaks(evidence.load(_write(tmp_path, download)))

    assert leaks == [
        "devices[0].rooms.Rooms[0].Name: not redacted",
        "data.devices[0].shadow.Reported.Contact: looks like an e-mail address",
        "data.devices[0].shadow.Reported.GlobalId: looks like an id with a MAC address inside",
        "data.devices[0].writes[0].error.message: looks like a MAC address",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "9480f96f-fa29-45b4-843b-0123456789ab",  # the tail of a UUID
        "RHIE-qLo2sThYMXL",  # a transaction id
        "DENALI_TSTAT-001",
        "2026-11-15T10:00:00.000+00:00",
    ],
)
def test_ids_that_only_resemble_a_mac_are_left_alone(text: str) -> None:
    assert evidence._identifiers(text) == []


@pytest.mark.parametrize("text", ["A0B1C2D3E4F5", "a0:b1:c2:d3:e4:f5", "/device/A0B1C2D3E4F5/hold"])
def test_a_mac_is_recognized_however_it_is_written(text: str) -> None:
    assert evidence._identifiers(text) == ["a MAC address"]


# -- summary -------------------------------------------------------------------
def test_summary_shows_the_hardware_the_pushes_the_commands_and_the_drift(
    tmp_path: pathlib.Path,
) -> None:
    download = _download(
        pushes={
            "ScheduleStatus": {
                **_push("2026-11-15T10:05:00.000+00:00", {"Value": "Running"}),
                "merged_in_memory": False,
            },
            "Setpoint": _push("2026-11-15T10:06:00.000+00:00", {"CoolSetpoint": 77}, count=4),
        },
        writes=[
            {
                "at": "2026-11-15T10:06:00.000+00:00",
                "command": "coolSetpoint",
                "body": {"SetpointValue": 77.0},
                "result": {"TransactionId": "tx-1"},
                "confirmation": {"change": "changeSetpoint", "success": True},
            },
            {
                "at": "2026-11-15T10:07:00.000+00:00",
                "command": "freezeProtection",
                "body": {"LowLimitDegrees": 41.5},
                "error": {"type": "ResideoApiError", "status": 400},
            },
            {
                "at": "2026-11-15T10:08:00.000+00:00",
                "transaction_id": "tx-9",
                "change": "changeFanSwitch",
                "success": True,
            },
        ],
        drift={
            "pushes_merged_since_read": 4,
            "shadow": [{"path": "Reported.Setpoint.CoolSetpoint", "held": 77, "cloud": 75.0}],
            "rooms": [
                {"path": "Rooms[0].AccessorySource", "held": [0]},
                {"path": "Rooms[0].Name2", "cloud": "x"},
            ],
        },
    )

    text = evidence.summary(evidence.load(_write(tmp_path, download)))

    assert "- Integration 0.4.0, Home Assistant 2026.11.1" in text
    assert "- Account: country US, locale en-US" in text
    assert "| Denali_S1200 | LyricThermostatDevice | yes |" in text
    assert "- 1 x Thermostat / WI / Unknown OS" in text
    assert "| ScheduleStatus | no | 1 | 1 | 2026-11-15T10:05:00.000+00:00 |" in text
    assert "| Setpoint | yes | 4 | 1 | 2026-11-15T10:06:00.000+00:00 |" in text
    assert "Not merged, so each one costs a REST resync: ScheduleStatus." in text
    assert '| coolSetpoint | {"SetpointValue":77.0} | accepted | confirmed: changeSetpoint |' in text
    assert '| freezeProtection | {"LowLimitDegrees":41.5} | refused: 400 |  |' in text
    assert "| (not ours) |  |  | confirmed: changeFanSwitch |" in text
    assert "| shadow | Reported.Setpoint.CoolSetpoint | 77 | 75.0 | differs |" in text
    assert "| rooms | Rooms[0].AccessorySource | [0] | (absent) | only in memory |" in text
    assert "| rooms | Rooms[0].Name2 | (absent) | x | only in the cloud |" in text
    assert "Compared after 4 merged push(es) since the last full read." in text
    # After the thermostats, how to read each kind of row listed.
    assert "## Reading the drift rows" in text
    assert "- `differs`: a reading that moved since its last push comes and goes" in text
    assert "- `only in memory`: a key that pushes carry and the snapshot doesn't." in text
    assert "- `only in the cloud`: a key the cloud added since the last full read" in text


def test_summary_of_a_quiet_capture_and_of_a_download_that_predates_it(
    tmp_path: pathlib.Path,
) -> None:
    quiet = evidence.summary(evidence.load(_write(tmp_path, _download())))
    assert quiet.count("None since the integration loaded.") == 2
    # Memory that is itself a read of the cloud cannot show whether pushes are merged right.
    assert "Nothing to compare: no push has been merged since the last full read" in quiet

    # Rows with no merged push behind them are about the cloud, not about a merge.
    unpushed = _download(
        drift={
            "pushes_merged_since_read": 0,
            "shadow": [{"path": "Reported.Quality", "held": "Good", "cloud": "Fair"}],
            "rooms": [],
        }
    )
    text = evidence.summary(evidence.load(_write(tmp_path, unpushed)))
    assert "| shadow | Reported.Quality | Good | Fair | differs |" in text
    assert "They say nothing about merges." in text
    assert "## Reading the drift rows" not in text

    merged = _download(drift={"pushes_merged_since_read": 3, "shadow": [], "rooms": []})
    assert (
        "None after 3 merged push(es) since the last full read: what the pushes built equals "
        "what the cloud reports."
    ) in evidence.summary(evidence.load(_write(tmp_path, merged)))

    old = _download()
    for key in ("account_devices", "capture_since"):
        del old["data"][key]
    for key in ("pushes", "writes", "drift"):
        del old["data"]["devices"][0][key]
    assert evidence.summary(evidence.load(_write(tmp_path, old))).count("not in this file") == 5


# -- find ----------------------------------------------------------------------
def test_find_lists_where_a_key_turns_up_and_the_values_there(tmp_path: pathlib.Path) -> None:
    download = _download(
        pushes={"Setpoint": _push("2026-11-15T10:06:00.000+00:00", {"CoolSetpoint": 77})},
        writes=[
            {
                "at": "2026-11-15T10:06:00.000+00:00",
                "command": "coolSetpoint",
                "body": {"CoolSetpoint": 77.0},
                "result": {"TransactionId": "tx-1"},
                "confirmation": {"change": "changeSetpoint", "success": True},
            }
        ],
        drift={
            "pushes_merged_since_read": 1,
            "shadow": [{"path": "Reported.Setpoint.CoolSetpoint", "held": 77, "cloud": 78}],
            "rooms": [],
        },
    )
    loaded = evidence.load(_write(tmp_path, download))

    text = evidence.find(loaded, "coolsetpoint")
    assert "| 1 | confirmation of coolSetpoint | changeSetpoint | true |" in text
    assert "confirmation of coolSetpoint" in evidence.find(loaded, "changesetpoint")
    # Drift on the key is listed with both sides.
    assert "| 1 | drift shadow | Reported.Setpoint.CoolSetpoint.held | 77 |" in text
    assert "| 1 | drift shadow | Reported.Setpoint.CoolSetpoint.cloud | 78 |" in text

    assert "| 1 | shadow | Reported.Setpoint.CoolSetpoint | 76.0 |" in text
    assert "| 1 | push Setpoint | CoolSetpoint | 77 |" in text
    assert "| 1 | command coolSetpoint | CoolSetpoint | 77.0 |" in text
    # A push type or a command is found by its name, with everything it carried.
    assert "| 1 | push Setpoint | CoolSetpoint | 77 |" in evidence.find(loaded, "push setpoint")
    # The same key in every room is one row, with each value seen.
    rooms = evidence.find(loaded, "Rooms[].Name")
    assert rooms.count("| rooms | Rooms[].Name |") == 1
    assert evidence.find(loaded, "NoSuchKey").startswith("Nothing matching 'NoSuchKey'")


# -- diff ----------------------------------------------------------------------
def test_diff_shows_the_state_the_pushes_and_the_commands_one_action_brought(
    tmp_path: pathlib.Path,
) -> None:
    earlier = {"at": "2026-11-15T10:01:00.000+00:00", "command": "hold", "body": {}, "result": {}}
    before = _download(
        pushes={"Setpoint": _push("2026-11-15T10:02:00.000+00:00", {"CoolSetpoint": 75})},
        writes=[earlier],
    )
    after = _download(
        pushes={
            "Setpoint": _push("2026-11-15T10:06:00.000+00:00", {"CoolSetpoint": 77}, count=3),
            "FanSwitch": _push("2026-11-15T10:06:01.000+00:00", {"Position": "On"}),
        },
        writes=[
            earlier,
            {
                "at": "2026-11-15T10:06:00.000+00:00",
                "command": "coolSetpoint",
                "body": {"SetpointValue": 77.0},
                "result": {"TransactionId": "tx-1"},
            },
        ],
    )
    after["data"]["devices"][0]["shadow"]["Reported"]["Setpoint"]["CoolSetpoint"] = 77.0
    del after["data"]["devices"][0]["shadow"]["Reported"]["FeelsLikeEnabled"]

    text = evidence.diff(
        evidence.load(_write(tmp_path, before, "before.json")),
        evidence.load(_write(tmp_path, after, "after.json")),
    )

    assert "| shadow | Reported.Setpoint.CoolSetpoint | 76.0 | 77.0 |" in text
    assert "| shadow | Reported.FeelsLikeEnabled | true | (absent) |" in text
    assert text.count("| shadow |") == 2
    assert '- 2 x **Setpoint**: merged in memory\n  - `{"CoolSetpoint":77}`' in text
    assert "- 1 x **FanSwitch**: merged in memory, first seen" in text
    assert '| coolSetpoint | {"SetpointValue":77.0} | accepted | no confirmation |' in text
    assert "| hold |" not in text
    assert "reloaded" not in text


def test_diff_of_identical_downloads_is_empty_and_a_reload_is_called_out(
    tmp_path: pathlib.Path,
) -> None:
    push = {"Setpoint": _push("2026-11-15T10:02:00.000+00:00", {"CoolSetpoint": 75})}
    same = evidence.load(_write(tmp_path, _download(pushes=push)))

    text = evidence.diff(same, same)
    assert "No change." in text
    assert text.count("None.") == 2

    reloaded = _download(pushes=push)
    reloaded["data"]["capture_since"] = "2026-11-15T11:00:00.000+00:00"
    text = evidence.diff(same, evidence.load(_write(tmp_path, reloaded)))
    assert "The integration was reloaded in between" in text
    assert "- 1 x **Setpoint**: merged in memory, first seen" in text


# -- add-home ------------------------------------------------------------------
def test_add_home_files_the_download_with_its_hardware(
    tmp_path: pathlib.Path, homes: pathlib.Path
) -> None:
    target = evidence.add_home(
        evidence.load(_write(tmp_path, _download())), "t10-two-sensors", "T10+ and two sensors"
    )

    assert target == homes / "t10-two-sensors.json"
    home = json.loads(target.read_text())
    assert home["home"] == {
        "integration_version": "0.4.0",
        "home_assistant_version": "2026.11.1",
        "name": "t10-two-sensors",
        "hardware": "T10+ and two sensors",
    }
    assert home["data"]["devices"][0]["shadow"]["Reported"]["DisplayedIndoorTemperature"] == 76.0
    assert evidence.find_leaks(home) == []


def test_add_home_scrubs_a_download_that_predates_the_current_rules(
    tmp_path: pathlib.Path, homes: pathlib.Path
) -> None:
    """Up to 0.3.3 the download carried room names, the e-mail in the entry title, and nothing
    blanked a MAC inside a string."""
    old = _download()
    old["data"]["entry"] = {"title": "Resideo (someone@example.com)", "data": {}}
    old["data"]["account"] = {"error": f"GET /device/{MAC} failed for someone@example.com"}
    old["data"]["devices"][0]["rooms"]["Rooms"][0]["Name"] = "Daniel's Room"

    target = evidence.add_home(evidence.load(_write(tmp_path, old)), "old", "T10+")

    text = target.read_text()
    for private in ("someone@example.com", MAC, "Daniel"):
        assert private not in text
    home = json.loads(text)
    assert "entry" not in home["data"]
    assert home["data"]["account"] == {"error": f"GET /device/{REDACTED} failed for {REDACTED}"}
    assert home["data"]["devices"][0]["rooms"]["Rooms"][0]["Name"] == REDACTED


def test_add_home_refuses_what_it_cannot_scrub(tmp_path: pathlib.Path, homes: pathlib.Path) -> None:
    download = _download()
    download["data"]["devices"][0]["shadow"]["Reported"]["GlobalId"] = (
        "THlyaWNUaGVybW9zdGF0RGV2aWNlOkFBQkJDQ0RERUVGRg=="
    )

    with pytest.raises(evidence.EvidenceError, match=r"Reported\.GlobalId: looks like an id"):
        evidence.add_home(evidence.load(_write(tmp_path, download)), "t10", "T10+")

    assert not homes.exists()


def test_add_home_overwrites_only_when_told_to(tmp_path: pathlib.Path, homes: pathlib.Path) -> None:
    loaded = evidence.load(_write(tmp_path, _download()))
    evidence.add_home(loaded, "t10", "T10+")

    with pytest.raises(evidence.EvidenceError, match="--replace"):
        evidence.add_home(loaded, "t10", "T10+ with a sensor")

    evidence.add_home(loaded, "t10", "T10+ with a sensor", replace=True)
    assert json.loads((homes / "t10.json").read_text())["home"]["hardware"] == "T10+ with a sensor"

    # A newer capture of the same home keeps what was said about its hardware.
    evidence.add_home(loaded, "t10", replace=True)
    assert json.loads((homes / "t10.json").read_text())["home"]["hardware"] == "T10+ with a sensor"
    with pytest.raises(evidence.EvidenceError, match="--hardware"):
        evidence.add_home(loaded, "t10-new")


@pytest.mark.parametrize("name", ["", "My Home", "../escape", "t10_", "-t10", "t10.json"])
def test_add_home_refuses_a_name_that_is_not_a_plain_slug(
    tmp_path: pathlib.Path, homes: pathlib.Path, name: str
) -> None:
    with pytest.raises(evidence.EvidenceError, match="lowercase"):
        evidence.add_home(evidence.load(_write(tmp_path, _download())), name, "T10+")


# -- fetch ---------------------------------------------------------------------
def _serve(monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Answer ``evidence._get`` like Home Assistant's API; returns the calls it received."""
    calls: list[tuple[str, str]] = []

    def _get(url: str, token: str) -> Any:
        calls.append((url, token))
        return entries if "/config_entries/" in url else _download()

    monkeypatch.setattr(evidence, "_get", _get)
    return calls


def test_fetch_downloads_the_one_resideo_entry_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _serve(monkeypatch, [{"entry_id": "abc", "domain": "resideo"}])

    fetched = evidence.fetch("http://ha.local:8123/", "secret")

    assert calls == [
        ("http://ha.local:8123/api/config/config_entries/entry?domain=resideo", "secret"),
        ("http://ha.local:8123/api/diagnostics/config_entry/abc", "secret"),
    ]
    assert fetched["home"]["integration_version"] == "0.4.0"
    assert evidence.find_leaks(fetched) == []


def test_fetch_needs_to_know_which_entry_when_there_are_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [{"entry_id": "abc", "domain": "resideo"}, {"entry_id": "def", "domain": "resideo"}]
    calls = _serve(monkeypatch, entries)

    with pytest.raises(evidence.EvidenceError, match="found 2 Resideo config entries"):
        evidence.fetch("http://ha.local:8123", "secret")

    evidence.fetch("http://ha.local:8123", "secret", entry="def")
    assert calls[-1][0].endswith("/api/diagnostics/config_entry/def")


def test_fetch_says_when_there_is_nothing_to_download(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, [])

    with pytest.raises(evidence.EvidenceError, match="no Resideo config entry"):
        evidence.fetch("http://ha.local:8123", "secret")
    with pytest.raises(evidence.EvidenceError, match="not an http"):
        evidence.fetch("file:///etc/passwd", "secret")


# -- command line --------------------------------------------------------------
def test_fetch_takes_the_token_from_the_environment_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("HASS_TOKEN", raising=False)

    assert evidence.main(["fetch"]) == 1
    assert "set HASS_TOKEN" in capsys.readouterr().err


def test_fetch_writes_the_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve(monkeypatch, [{"entry_id": "abc", "domain": "resideo"}])
    monkeypatch.setenv("HASS_TOKEN", "secret")
    output = tmp_path / "out.json"

    assert evidence.main(["fetch", "-o", str(output)]) == 0

    assert evidence.find_leaks(evidence.load(output)) == []
    assert "secret" not in capsys.readouterr().out


def test_check_fails_on_a_file_with_something_private_in_it(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean = _write(tmp_path, _download(), "clean.json")
    leaky = _download()
    leaky["data"]["devices"][0]["rooms"]["Rooms"][0]["Name"] = "Daniel's Room"
    dirty = _write(tmp_path, leaky, "dirty.json")

    assert evidence.main(["check", str(clean)]) == 0
    assert evidence.main(["check", str(clean), str(dirty)]) == 1
    out = capsys.readouterr().out
    assert f"{clean}: clean" in out
    assert "devices[0].rooms.Rooms[0].Name: not redacted" in out
    assert "Daniel" not in out


def test_summary_and_diff_print_markdown(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = str(_write(tmp_path, _download()))

    assert evidence.main(["summary", path]) == 0
    assert capsys.readouterr().out.startswith("# Resideo evidence: download.json\n")
    assert evidence.main(["diff", path, path]) == 0
    assert capsys.readouterr().out.startswith("# What changed between the two downloads\n")
    assert evidence.main(["summary", str(tmp_path / "missing.json")]) == 1
