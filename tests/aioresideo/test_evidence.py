"""Every push the client merges and every command it sends has a capture behind it.

The API is reverse-engineered, so a shape nobody recorded is a guess, however plausible it
reads. A capture is a home (``tests/homes``, see CONTRIBUTING.md) or one of the hand-sanitized
frames in ``fixtures`` that predate homes.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re

from custom_components.resideo.aioresideo import LIVE_FEED_MERGED_PROPERTIES, client
from tests.homes import home_names, load_home

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# Written before captures were kept in the repo. Nothing may be added to these: new code
# brings its capture with it. A home that captures one of these takes it off the list, in the
# same pull request (a command counts once a device has confirmed it).
PUSHES_WITHOUT_A_CAPTURE = {"DisplayedIndoorTemperature"}
COMMANDS_WITHOUT_A_CAPTURE = {
    "accessories/{}/accessoryValue",
    "fanSwitch",
    "freezeProtection",
    "heatSetpoint",
    "hold",
    "priority",
    "systemSwitch",
}
# Of those, the ones no entity sends: their bodies were never recorded from the app. An entity
# may start sending one once its body is, in the pull request whose home captures the device
# confirming it, which takes it off both lists.
COMMANDS_NO_ENTITY_SENDS = {"priority"}


def _route(command: str) -> str:
    """A command route with its ids taken out: ``accessories/2/x`` -> ``accessories/{}/x``."""
    return re.sub(r"(?<=/)\d+(?=/)", "{}", command)


def _commands_the_client_sends() -> set[str]:
    """The route of every ``self._command(mac, <route>, body)`` call in the client."""
    tree = ast.parse(pathlib.Path(client.__file__).read_text())
    return {_route_of(node) for node in ast.walk(tree) if _is_command(node)}


def _is_command(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "_command"


def _route_of(call: ast.Call) -> str:
    route = call.args[1]
    if isinstance(route, ast.JoinedStr):
        return "".join(p.value if isinstance(p, ast.Constant) else "{}" for p in route.values)
    return ast.literal_eval(route)


def _commands_the_entities_send() -> set[str]:
    """The route behind every ``api.async_set_x`` the Home Assistant layer calls."""
    routes = {}
    for node in ast.walk(ast.parse(pathlib.Path(client.__file__).read_text())):
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("set_"):
            (route,) = {_route_of(call) for call in ast.walk(node) if _is_command(call)}
            routes[f"async_{node.name}"] = route
    integration = pathlib.Path(client.__file__).parent.parent
    return {
        routes[node.attr]
        for path in integration.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute) and node.attr in routes
    }


def _home_devices() -> list[dict]:
    return [device for name in home_names() for device in load_home(name)["data"]["devices"]]


def _captured_pushes() -> set[str]:
    frames = (json.loads(path.read_text()) for path in FIXTURES.glob("live_*.json"))
    return {frame["Body"]["PropertyName"] for frame in frames} | {
        name for device in _home_devices() for name in device.get("pushes") or {}
    }


def _captured_commands() -> set[str]:
    """Commands the cloud accepted. In a home that also takes the device's confirmation."""
    recorded = (json.loads(path.read_text()) for path in FIXTURES.glob("*_write.json"))
    return {write["url"].rsplit("/", 1)[-1] for write in recorded if write["status"] == 202} | {
        _route(write["command"])
        for device in _home_devices()
        for write in device.get("writes") or []
        if (write.get("confirmation") or {}).get("success")
    }


def test_every_merged_push_type_has_a_capture() -> None:
    missing = LIVE_FEED_MERGED_PROPERTIES - _captured_pushes()

    assert missing == PUSHES_WITHOUT_A_CAPTURE, (
        "a push type is merged in memory without a captured payload, or one on the list has "
        "been captured and should come off it (see CONTRIBUTING.md)"
    )


def test_every_command_has_a_capture() -> None:
    sent = _commands_the_client_sends()
    assert len(sent) == 12  # the scan found the client's commands

    missing = sent - _captured_commands()

    assert missing == COMMANDS_WITHOUT_A_CAPTURE, (
        "a command is sent without a capture of the cloud accepting it and the device "
        "confirming it, or one on the list has been captured and should come off it "
        "(see CONTRIBUTING.md)"
    )


def test_no_entity_sends_a_command_without_a_capture() -> None:
    unsent = COMMANDS_WITHOUT_A_CAPTURE - _commands_the_entities_send()

    assert unsent == COMMANDS_NO_ENTITY_SENDS, (
        "an entity now sends a command no device has confirmed. The client having a method "
        "for it is not evidence that the body is right: that takes a recording of the app's "
        "request, then a home showing the device confirm it (see CONTRIBUTING.md, step 4)"
    )


def test_route_ids_are_taken_out() -> None:
    assert _route("accessories/12/accessoryValue") == "accessories/{}/accessoryValue"
    assert _route("schedule/enabled") == "schedule/enabled"
