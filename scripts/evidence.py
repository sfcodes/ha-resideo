"""Turn a diagnostics download into evidence a pull request can stand on.

The API is reverse-engineered, so a claim about it is worth the capture behind it. The
integration records that capture itself (snapshots, pushes, commands, drift — see
``custom_components/resideo/diagnostics.py``); this reads the download. Run from the repo root:

    python -m scripts.evidence fetch                    # download it from a running Home Assistant
    python -m scripts.evidence summary FILE             # what it shows, as Markdown for an issue or PR
    python -m scripts.evidence find FILE KEY            # where a key turns up, and its values
    python -m scripts.evidence diff BEFORE AFTER        # what one action changed
    python -m scripts.evidence check FILE...            # nothing private left in it
    python -m scripts.evidence add-home FILE NAME --hardware "..."  # keep it as tests/homes/NAME.json
    python -m scripts.evidence add-home FILE NAME --replace         # put a newer capture in its place

Every command takes the file as Home Assistant downloads it, as ``fetch`` writes it, or as it
sits in ``tests/homes``. See CONTRIBUTING.md for when to use which.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

HOMES = Path(__file__).parent.parent / "tests" / "homes"
DOMAIN = "resideo"
SNAPSHOTS = ("shadow", "rooms", "configuration", "priority")
HOME_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
TABLE_ROWS = 40  # a longer table is cut, and says so
CELL_WIDTH = 100
REDACTED = "**REDACTED**"  # as Home Assistant's diagnostics write it

# What no key gives away: an identifier sitting inside a value. A MAC is twelve hex digits
# standing alone (the lookarounds skip the tail of a UUID).
_MAC = re.compile(
    r"(?<![0-9A-Za-z-])[0-9A-Fa-f]{12}(?![0-9A-Za-z-])"
    r"|\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"
)
_PATTERNS = {
    "a MAC address": _MAC,
    "an e-mail address": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "a token": re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}"),
}
_BASE64 = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")


class EvidenceError(Exception):
    """The file or the Home Assistant instance isn't usable; the message says why."""


# -- reading -------------------------------------------------------------------
def load(path: str | Path) -> dict[str, Any]:
    """A diagnostics file as ``{"home": {...}, "data": {...}}``, whichever form it came in."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as err:
        raise EvidenceError(f"{path}: {err}") from err
    return _normalize(raw, str(path))


def _normalize(raw: Any, source: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict):
        raise EvidenceError(f"{source}: not a Resideo diagnostics file")
    if "devices" not in raw["data"]:
        raise EvidenceError(f"{source}: not a Resideo diagnostics file (no devices in it)")
    if "home" in raw:
        return raw
    # As Home Assistant wraps it. The rest of the wrapper (the host's time zone, every other
    # custom integration installed) says nothing about Resideo, so it isn't carried along.
    return {
        "home": {
            "integration_version": (raw.get("integration_manifest") or {}).get("version"),
            "home_assistant_version": (raw.get("home_assistant") or {}).get("version"),
        },
        "data": raw["data"],
    }


def _sanitized(evidence: dict[str, Any]) -> dict[str, Any]:
    """The evidence scrubbed by the integration's current rules.

    A download from an older version of the integration predates some of them (it carried
    room names, and the entry title with the account's e-mail), so nothing is written to disk
    without this. The integration blanks the MACs it knows wherever they sit; here they are
    recognized by their look instead.
    """
    data = _blanked(_scrub()(evidence["data"]))
    data.pop("entry", None)  # a redacted token and, in old downloads, the e-mail
    return {**evidence, "data": data}


def _blanked(value: Any) -> Any:
    """``value`` with every identifier a pattern recognizes blanked inside its strings."""
    if isinstance(value, str):
        for pattern in _PATTERNS.values():
            value = pattern.sub(REDACTED, value)
        return value
    if isinstance(value, list):
        return [_blanked(item) for item in value]
    if isinstance(value, dict):
        return {key: _blanked(item) for key, item in value.items()}
    return value


def _scrub() -> Any:
    """The integration's own redaction, so a file is held to the rules in force today."""
    try:
        from custom_components.resideo.diagnostics import scrub
    except ImportError as err:
        raise EvidenceError(
            "check and add-home use the integration's redaction, which needs Home Assistant "
            f"installed (pip install -r requirements.txt): {err}"
        ) from err
    return scrub


# -- check ---------------------------------------------------------------------
def find_leaks(evidence: dict[str, Any]) -> list[str]:
    """Where something private is left, as ``path: what`` (never the value itself)."""
    data = evidence["data"]
    leaks = [f"{path}: not redacted" for path in _changed(data, _scrub()(data))]
    for path, value in _leaves(evidence):
        if isinstance(value, str):
            leaks += [f"{path}: looks like {what}" for what in _identifiers(value)]
    return leaks


def _identifiers(text: str) -> list[str]:
    found = [what for what, pattern in _PATTERNS.items() if pattern.search(text)]
    # The cloud's global ids are base64 of "<Type>:<MAC>".
    for candidate in _BASE64.findall(text):
        try:
            decoded = base64.b64decode(candidate + "=" * (-len(candidate) % 4), altchars=b"-_")
        except (binascii.Error, ValueError):
            continue
        if _MAC.search(decoded.decode("utf-8", "ignore")):
            found.append("an id with a MAC address inside")
    return found


def _leaves(value: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaves(item, f"{path}[{index}]")
    else:
        yield path, value


def _changed(before: Any, after: Any) -> list[str]:
    """Paths of the leaves that differ, either side missing included."""
    old, new = dict(_leaves(before)), dict(_leaves(after))
    return [path for path in sorted(old.keys() | new.keys()) if old.get(path) != new.get(path)]


# -- summary -------------------------------------------------------------------
def summary(evidence: dict[str, Any], title: str = "Resideo evidence") -> str:
    """What the file shows, as Markdown."""
    home, data = evidence["home"], evidence["data"]
    account = (data.get("account") or {}).get("data") or {}
    lines = [f"# {title}", ""]
    if home.get("hardware"):
        lines.append(f"- Hardware: {home['hardware']}")
    lines += [
        f"- Integration {home.get('integration_version') or 'unknown'}, "
        f"Home Assistant {home.get('home_assistant_version') or 'unknown'}",
        f"- Capturing since: {data.get('capture_since') or _TOO_OLD}",
        f"- Account: country {account.get('countryCode')}, locale {account.get('locale')}",
        "",
        "## Devices in the account",
        "",
    ]
    if "account_devices" in data:
        lines += _table(
            ("model", "kind", "supported"),
            [(d["model"], d["kind"], _yes(d["supported"])) for d in data["account_devices"]],
        )
    else:
        lines.append(_TOO_OLD)
    for number, device in enumerate(data["devices"], start=1):
        lines += ["", f"## Thermostat {number}", "", *_device_summary(device)]
    lines += _drift_notes(data["devices"])
    return "\n".join(lines) + "\n"


_TOO_OLD = "not in this file (the integration that wrote it is older than the capture)"


def _device_summary(device: dict[str, Any]) -> list[str]:
    rooms = (device.get("rooms") or {}).get("Rooms") or []
    accessories = Counter(
        " / ".join(
            str((acc.get("AccessoryAttribute") or {}).get(key))
            for key in ("Type", "ConnectionMethod", "Model")
        )
        for room in rooms
        for acc in room.get("Accessories") or []
    )
    lines = [f"{len(rooms)} room(s). Accessories by type / connection / model:", ""]
    lines += [f"- {count} x {kind}" for kind, count in sorted(accessories.items())] or ["- none"]

    lines += ["", "### Pushes received", ""]
    pushes = device.get("pushes")
    if pushes is None:
        lines.append(_TOO_OLD)
    elif not pushes:
        lines.append("None since the integration loaded.")
    else:
        lines += _table(
            ("type", "merged in memory", "count", "shapes", "last seen"),
            [
                (name, _yes(p["merged_in_memory"]), p["count"], len(p["shapes"]), p["last_seen"])
                for name, p in pushes.items()
            ],
        )
        unmerged = [name for name, push in pushes.items() if not push["merged_in_memory"]]
        if unmerged:
            lines += ["", f"Not merged, so each one costs a REST resync: {', '.join(unmerged)}."]

    lines += ["", "### Commands sent", ""]
    writes = device.get("writes")
    if writes is None:
        lines.append(_TOO_OLD)
    elif not writes:
        lines.append("None since the integration loaded.")
    else:
        lines += _table(("at", "command", "body", "cloud", "device"), map(_write_row, writes))

    lines += ["", "### Drift from the cloud", ""]
    drift = device.get("drift")
    if drift is None:
        lines.append(_TOO_OLD)
    elif "error" in drift:
        lines.append(f"Could not be read: {drift['error']}")
    else:
        lines += _drift_summary(drift)
    return lines


def _drift_summary(drift: dict[str, Any]) -> list[str]:
    rows = [
        (
            snapshot,
            d["path"],
            _cell(d.get("held", "(absent)")),
            _cell(d.get("cloud", "(absent)")),
            _drift_kind(d),
        )
        for snapshot in ("shadow", "rooms")
        for d in drift[snapshot]
    ]
    merged = drift.get("pushes_merged_since_read")
    if merged == 0 and not rows:
        return [
            "Nothing to compare: no push has been merged since the last full read, so the "
            "state in memory is that read. The integration re-reads after each command it "
            "sends and each push type it doesn't merge; wait for pushes and download again."
        ]
    since = "" if merged is None else f" after {merged} merged push(es) since the last full read"
    if not rows:
        return [f"None{since}: what the pushes built equals what the cloud reports."]
    lines = _table(("snapshot", "path", "held in memory", "cloud", "kind"), rows)
    if merged == 0:
        return [
            *lines,
            "",
            "No push has been merged since the last full read, so these are values the cloud "
            "changed without a push the integration merges. They say nothing about merges.",
        ]
    if merged is not None:
        lines += ["", f"Compared{since}."]
    return lines


def _drift_notes(devices: list[dict[str, Any]]) -> list[str]:
    """How to read each kind of drift row the summary lists, said once for all thermostats."""
    kinds = {
        _drift_kind(difference)
        for drift in (device.get("drift") or {} for device in devices)
        if drift.get("pushes_merged_since_read") != 0
        for snapshot in ("shadow", "rooms")
        for difference in drift.get(snapshot, ())
    }
    if not kinds:
        return []
    notes = [f"- `{kind}`: {note}" for kind, note in _DRIFT_NOTES.items() if kind in kinds]
    return ["", "## Reading the drift rows", "", *notes]


_DRIFT_NOTES = {
    "differs": (
        "a reading that moved since its last push comes and goes between downloads. A row "
        "that stays is a wrong merge, or a path no push writes: stale until the next full "
        "read, like any entity that reads it."
    ),
    "only in memory": (
        "a key that pushes carry and the snapshot doesn't. If your merge wrote it, check "
        "that it isn't a snapshot key under another name."
    ),
    "only in the cloud": "a key the cloud added since the last full read, or one a merge dropped.",
}


def _drift_kind(difference: dict[str, Any]) -> str:
    if "cloud" not in difference:
        return "only in memory"
    if "held" not in difference:
        return "only in the cloud"
    return "differs"


def _write_row(write: dict[str, Any]) -> tuple[Any, ...]:
    if "command" not in write:  # a confirmation that answers no command on record
        outcome = "confirmed" if write.get("success") else "REJECTED"
        return (write["at"], "(not ours)", "", "", f"{outcome}: {write.get('change')}")
    if "error" in write:
        cloud = f"refused: {write['error'].get('status') or write['error'].get('type')}"
        return (write["at"], write["command"], _cell(write["body"], width=None), cloud, "")
    confirmation = write.get("confirmation")
    if confirmation is None:
        device = "no confirmation"
    else:
        outcome = "confirmed" if confirmation["success"] else "REJECTED"
        device = f"{outcome}: {confirmation.get('change')}"
    return (write["at"], write["command"], _cell(write["body"], width=None), "accepted", device)


def _yes(flag: Any) -> str:
    return "yes" if flag else "no"


def _cell(value: Any, width: int | None = CELL_WIDTH) -> str:
    """A value as one table cell, cut to ``width`` (``None`` keeps it whole)."""
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if width is None or len(text) <= width else text[: width - 1] + "…"


def _table(header: tuple[str, ...], rows: Any, limit: int | None = TABLE_ROWS) -> list[str]:
    rows = [tuple(str(cell) for cell in row) for row in rows]
    limit = len(rows) if limit is None else limit
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows[:limit]]
    if len(rows) > limit:
        lines += ["", f"…and {len(rows) - TABLE_ROWS} more."]
    return lines


# -- diff ----------------------------------------------------------------------
def diff(before: dict[str, Any], after: dict[str, Any]) -> str:
    """What changed between two downloads: state, and the pushes and commands in between."""
    old, new = before["data"], after["data"]
    lines = ["# What changed between the two downloads", ""]
    if old.get("capture_since") != new.get("capture_since"):
        lines += [
            "The integration was reloaded in between, so the capture started over: pushes and "
            "commands below are everything since the reload.",
            "",
        ]
        old = {**old, "devices": [{**d, "pushes": {}, "writes": []} for d in old["devices"]]}
    if len(old["devices"]) != len(new["devices"]):
        raise EvidenceError("the two files don't hold the same number of thermostats")
    for number, (was, now) in enumerate(zip(old["devices"], new["devices"], strict=True), 1):
        lines += [f"## Thermostat {number}", "", "### State", ""]
        state = [
            (snapshot, path, _cell(a.get(path, "(absent)")), _cell(b.get(path, "(absent)")))
            for snapshot in SNAPSHOTS
            for a, b in [(dict(_leaves(was.get(snapshot))), dict(_leaves(now.get(snapshot))))]
            for path in sorted(a.keys() | b.keys())
            if a.get(path, _ABSENT) != b.get(path, _ABSENT)
        ]
        lines += _table(("snapshot", "path", "before", "after"), state) if state else ["No change."]

        lines += ["", "### Pushes that arrived in between", ""]
        arrived = list(_new_pushes(was.get("pushes") or {}, now.get("pushes") or {}))
        lines += arrived or ["None."]

        lines += ["", "### Commands and confirmations in between", ""]
        seen = [json.dumps(w, sort_keys=True) for w in was.get("writes") or []]
        fresh = [
            w for w in now.get("writes") or [] if json.dumps(w, sort_keys=True) not in seen
        ]
        if fresh:
            lines += _table(("at", "command", "body", "cloud", "device"), map(_write_row, fresh))
        else:
            lines.append("None.")
        lines.append("")
    return "\n".join(lines)


_ABSENT = object()


def _new_pushes(was: dict[str, Any], now: dict[str, Any]) -> Iterator[str]:
    """One line per push type that arrived, with the newest payload of each shape that did."""
    for name, push in now.items():
        arrived = push["count"] - was.get(name, {}).get("count", 0)
        if arrived <= 0:
            continue
        merged = "merged in memory" if push["merged_in_memory"] else "NOT merged (resync)"
        new_type = "" if name in was else ", first seen"
        yield f"- {arrived} x **{name}**: {merged}{new_type}"
        last_before = was.get(name, {}).get("last_seen", "")
        for shape in push["shapes"]:
            newest = shape["samples"][0]
            if newest["at"] > last_before:
                yield f"  - `{json.dumps(newest['value'], separators=(',', ':'))}`"


# -- find ----------------------------------------------------------------------
_INDEX = re.compile(r"\[\d+\]")


def find(evidence: dict[str, Any], key: str) -> str:
    """Where a key turns up in each thermostat's snapshots, push samples, commands,
    confirmations and drift, and the values there.

    A push type or a command matches by its name too, and then lists everything it carried.
    List positions are folded together (``Rooms[]``).
    """
    needle = key.lower()
    rows = []
    for number, device in enumerate(evidence["data"]["devices"], start=1):
        seen: dict[tuple[str, str], list[Any]] = {}
        for where, value in _places(device):
            for indexed, leaf in _leaves(value):
                path = _INDEX.sub("[]", indexed)
                if needle in path.lower() or needle in where.lower():
                    values = seen.setdefault((where, path), [])
                    if leaf not in values:
                        values.append(leaf)
        for (where, path), values in seen.items():
            shown = ", ".join(json.dumps(value) for value in values)
            rows.append((number, where, path, _cell(shown, width=None)))
    if not rows:
        return f"Nothing matching {key!r} in this file.\n"
    header = ("thermostat", "where", "path", "values seen")
    return "\n".join(_table(header, rows, limit=None)) + "\n"


def _places(device: dict[str, Any]) -> Iterator[tuple[str, Any]]:
    for snapshot in SNAPSHOTS:
        yield snapshot, device.get(snapshot)
    for name, push in (device.get("pushes") or {}).items():
        for shape in push["shapes"]:
            for sample in shape["samples"]:
                yield f"push {name}", sample["value"]
    for write in device.get("writes") or []:
        if "command" in write:
            yield f"command {write['command']}", write["body"]
        confirmation = write.get("confirmation") or write
        if "change" in confirmation:
            answered = write.get("command", "(not ours)")
            yield f"confirmation of {answered}", {
                confirmation["change"]: bool(confirmation.get("success"))
            }
    drift = device.get("drift") or {}
    for snapshot in ("shadow", "rooms"):
        for difference in drift.get(snapshot) or ():
            sides = {side: difference[side] for side in ("held", "cloud") if side in difference}
            yield f"drift {snapshot}", {difference["path"]: sides}


# -- fetch ---------------------------------------------------------------------
def fetch(url: str, token: str, entry: str | None = None) -> dict[str, Any]:
    """Download the Resideo diagnostics from a running Home Assistant, already sanitized."""
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise EvidenceError(f"{url}: not an http(s) address")
    base = url.rstrip("/")
    entries = _get(f"{base}/api/config/config_entries/entry?domain={DOMAIN}", token)
    ids = [e["entry_id"] for e in entries if e.get("domain") == DOMAIN]
    if entry is not None:
        ids = [entry_id for entry_id in ids if entry_id == entry]
    if len(ids) != 1:
        raise EvidenceError(
            f"found {len(ids)} Resideo config entries at {base}; "
            "pass --entry with the id of the one to read"
            if ids
            else f"no Resideo config entry at {base}"
        )
    raw = _get(f"{base}/api/diagnostics/config_entry/{ids[0]}", token)
    return _sanitized(_normalize(raw, base))


def _get(url: str, token: str) -> Any:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        hint = " (the token must belong to an administrator)" if err.code in (401, 403) else ""
        raise EvidenceError(f"{url}: HTTP {err.code}{hint}") from err
    except (urllib.error.URLError, TimeoutError, ValueError) as err:
        raise EvidenceError(f"{url}: {err}") from err


# -- add-home ------------------------------------------------------------------
def add_home(
    evidence: dict[str, Any], name: str, hardware: str | None = None, *, replace: bool = False
) -> Path:
    """Write the sanitized evidence to ``tests/homes/<name>.json`` for the tests to replay.

    Replacing a home keeps the hardware it was filed with unless a new one is given.
    """
    if not HOME_NAME.fullmatch(name):
        raise EvidenceError(f"{name!r}: use lowercase letters, digits and dashes, e.g. t10-eim")
    target = HOMES / f"{name}.json"
    if target.exists() and not replace:
        raise EvidenceError(f"{target} exists; pass --replace to overwrite it")
    if hardware is None and target.exists():
        hardware = json.loads(target.read_text())["home"].get("hardware")
    if not hardware:
        raise EvidenceError("say what is installed with --hardware")
    home = _sanitized(evidence)
    home["home"] = {**home["home"], "name": name, "hardware": hardware}
    leaks = find_leaks(home)
    if leaks:
        raise EvidenceError("something private is left in the file:\n  " + "\n  ".join(leaks))
    HOMES.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(home, indent=2) + "\n")
    return target


# -- command line --------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.evidence", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    cmd = commands.add_parser("fetch", help="download the diagnostics from a running Home Assistant")
    cmd.add_argument("--url", default=os.environ.get("HASS_URL", "http://localhost:8123"))
    cmd.add_argument("--entry", help="config entry id, when there is more than one")
    cmd.add_argument("-o", "--output", default="resideo-diagnostics.json")

    cmd = commands.add_parser("summary", help="what a file shows, as Markdown")
    cmd.add_argument("file")

    cmd = commands.add_parser("find", help="where a key turns up in a file, and its values")
    cmd.add_argument("file")
    cmd.add_argument(
        "key", help="part of a key, path, push type, command or confirmation, any case: rssi"
    )

    cmd = commands.add_parser("diff", help="what changed between two downloads")
    cmd.add_argument("before")
    cmd.add_argument("after")

    cmd = commands.add_parser("check", help="verify nothing private is left in the files")
    cmd.add_argument("files", nargs="+")

    cmd = commands.add_parser("add-home", help="keep a file as tests/homes/NAME.json")
    cmd.add_argument("file")
    cmd.add_argument("name")
    cmd.add_argument("--hardware", help="what is installed, in a sentence (kept on --replace)")
    cmd.add_argument("--replace", action="store_true", help="overwrite an existing home")

    args = parser.parse_args(argv)
    try:
        return _run(args)
    except EvidenceError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace) -> int:
    if args.command == "fetch":
        # From the environment only: an argument would land in the shell history.
        token = os.environ.get("HASS_TOKEN")
        if not token:
            raise EvidenceError(
                "set HASS_TOKEN to a long-lived access token "
                "(Home Assistant: your profile -> Security -> Long-lived access tokens)"
            )
        evidence = fetch(args.url, token, args.entry)
        Path(args.output).write_text(json.dumps(evidence, indent=2) + "\n")
        print(f"wrote {args.output}")
    elif args.command == "summary":
        print(summary(load(args.file), title=f"Resideo evidence: {Path(args.file).name}"), end="")
    elif args.command == "find":
        print(find(load(args.file), args.key), end="")
    elif args.command == "diff":
        print(diff(load(args.before), load(args.after)), end="")
    elif args.command == "check":
        failed = False
        for file in args.files:
            leaks = find_leaks(load(file))
            failed = failed or bool(leaks)
            print(f"{file}: " + ("clean" if not leaks else "".join(f"\n  {leak}" for leak in leaks)))
        return 1 if failed else 0
    elif args.command == "add-home":
        target = add_home(load(args.file), args.name, args.hardware, replace=args.replace)
        print(f"wrote {target.relative_to(Path.cwd()) if target.is_relative_to(Path.cwd()) else target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
