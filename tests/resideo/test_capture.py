"""The in-memory record of pushes and writes behind the diagnostics download."""

from __future__ import annotations

from custom_components.resideo.aioresideo import (
    ResideoApiError,
    ResideoChangeConfirm,
    ResideoCommand,
    ResideoLiveFeed,
    parse_event,
)
from custom_components.resideo.capture import (
    PUSH_SAMPLES,
    PUSH_SHAPES,
    WRITE_HISTORY,
    ResideoCapture,
)

from .conftest import MAC, load_fixture_json

MAC2 = "112233445566"


def _captured(name: str) -> ResideoLiveFeed:
    event = parse_event(load_fixture_json(name))
    assert isinstance(event, ResideoLiveFeed)
    return event


def _write(capture: ResideoCapture, transaction_id: str, mac: str = MAC) -> None:
    write = load_fixture_json("cool_setpoint_write.json")
    capture.record_command(
        ResideoCommand(
            mac, "coolSetpoint", write["request_body"], result={"TransactionId": transaction_id}
        )
    )


def test_push_types_are_counted_and_flagged_merged_or_not() -> None:
    capture = ResideoCapture()
    capture.record_push(_captured("live_setpoint.json"))
    capture.record_push(_captured("live_setpoint.json"))
    capture.record_push(ResideoLiveFeed(MAC, "ScheduleStatus", {"Value": "Running"}, None))

    pushes = capture.pushes(MAC)

    assert list(pushes) == ["ScheduleStatus", "Setpoint"]
    assert pushes["Setpoint"]["count"] == 2
    assert pushes["Setpoint"]["merged_in_memory"] is True
    # A type the merge doesn't know is exactly the one a contributor needs to see.
    assert pushes["ScheduleStatus"]["merged_in_memory"] is False
    assert pushes["ScheduleStatus"]["shapes"][0]["samples"][0]["value"] == {"Value": "Running"}
    assert capture.pushes(MAC2) == {}


def test_rare_shape_survives_a_flood_of_the_common_one() -> None:
    """The battery sensor's ``Sensor`` push must not be evicted by the thermostat's own."""
    capture = ResideoCapture()
    remote = _captured("live_sensor_remote.json")
    builtin = _captured("live_sensor_builtin.json")

    capture.record_push(remote)
    for _ in range(50):
        capture.record_push(builtin)

    sensor = capture.pushes(MAC)["Sensor"]
    assert sensor["count"] == 51
    common, rare = sensor["shapes"]  # most recently seen shape first
    assert common["count"] == 50
    assert len(common["samples"]) == PUSH_SAMPLES
    assert rare["count"] == 1
    assert rare["samples"][0]["value"] == remote.value


def test_same_keys_with_other_values_are_one_shape_newest_sample_first() -> None:
    capture = ResideoCapture()
    for temperature in (70, 71, 72, 73):
        capture.record_push(
            ResideoLiveFeed(MAC, "DisplayedIndoorTemperature", {"Value": temperature}, None)
        )

    (shape,) = capture.pushes(MAC)["DisplayedIndoorTemperature"]["shapes"]
    assert shape["count"] == 4
    assert [sample["value"]["Value"] for sample in shape["samples"]] == [73, 72, 71]


def test_only_the_most_recently_seen_shapes_are_kept() -> None:
    capture = ResideoCapture()
    for index in range(PUSH_SHAPES + 2):
        capture.record_push(ResideoLiveFeed(MAC, "Sensor", {f"Key{index}": 1}, None))

    sensor = capture.pushes(MAC)["Sensor"]
    assert sensor["count"] == PUSH_SHAPES + 2
    kept = [next(iter(shape["samples"][0]["value"])) for shape in sensor["shapes"]]
    assert kept == [f"Key{index}" for index in range(PUSH_SHAPES + 1, 1, -1)]


def test_sample_is_a_copy_of_the_payload() -> None:
    capture = ResideoCapture()
    value = {"AccessoryValue": {"IndoorTemperature": 70}}
    capture.record_push(ResideoLiveFeed(MAC, "Sensor", value, None))

    value["AccessoryValue"]["IndoorTemperature"] = 99

    sample = capture.pushes(MAC)["Sensor"]["shapes"][0]["samples"][0]
    assert sample["value"] == {"AccessoryValue": {"IndoorTemperature": 70}}


def test_confirmation_attaches_to_the_command_it_answers() -> None:
    capture = ResideoCapture()
    _write(capture, "tx-1")
    _write(capture, "tx-2")

    capture.record_change(ResideoChangeConfirm(MAC, "tx-1", "changeSetpoint", "AppInitiated", True))

    first, second = capture.writes(MAC)
    assert first["command"] == "coolSetpoint"
    assert first["body"]["SetpointValue"] == 75.0
    assert first["result"] == {"TransactionId": "tx-1"}
    assert first["confirmation"]["success"] is True
    assert first["confirmation"]["change"] == "changeSetpoint"
    assert "confirmation" not in second


def test_rejected_change_is_recorded_as_a_failure() -> None:
    capture = ResideoCapture()
    _write(capture, "tx-1")

    capture.record_change(ResideoChangeConfirm(MAC, "tx-1", "changeSetpoint", "AppInitiated", False))

    assert capture.writes(MAC)[0]["confirmation"]["success"] is False


def test_confirmation_without_a_command_is_kept_with_its_transaction() -> None:
    capture = ResideoCapture()
    _write(capture, "tx-1")
    event = parse_event(load_fixture_json("change_request_success.json"))

    capture.record_change(event)

    command, change = capture.writes(MAC)
    assert "confirmation" not in command
    assert change["transaction_id"] == "RHIE-qLo2sThYMXL"
    assert change["change"] == "changeSetpoint"
    assert change["direction"] == "AppInitiated"
    assert change["success"] is True


def test_confirmation_goes_to_the_thermostat_it_names() -> None:
    capture = ResideoCapture()
    _write(capture, "tx-1", mac=MAC)

    capture.record_change(ResideoChangeConfirm(MAC2, "tx-1", "changeSetpoint", "AppInitiated", True))

    assert "confirmation" not in capture.writes(MAC)[0]
    assert capture.writes(MAC2)[0]["transaction_id"] == "tx-1"


def test_refused_command_keeps_the_status_and_response() -> None:
    capture = ResideoCapture()
    error = ResideoApiError("PUT .../freezeProtection -> 400", status=400, body={"error": "bad"})

    capture.record_command(
        ResideoCommand(MAC, "freezeProtection", {"LowLimitDegrees": 41.5}, error=error)
    )

    (entry,) = capture.writes(MAC)
    assert entry["body"] == {"LowLimitDegrees": 41.5}
    assert "result" not in entry
    assert entry["error"] == {
        "type": "ResideoApiError",
        "message": "PUT .../freezeProtection -> 400",
        "status": 400,
        "response": {"error": "bad"},
    }


def test_write_history_is_bounded_keeping_the_newest() -> None:
    capture = ResideoCapture()
    for index in range(WRITE_HISTORY + 5):
        _write(capture, f"tx-{index}")

    writes = capture.writes(MAC)
    assert len(writes) == WRITE_HISTORY
    assert writes[0]["result"] == {"TransactionId": "tx-5"}
    assert writes[-1]["result"] == {"TransactionId": f"tx-{WRITE_HISTORY + 4}"}
