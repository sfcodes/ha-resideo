# Contributing

Resideo publishes no documentation for the API behind this integration. Everything in the
code was learned by recording what the cloud actually sends, and it keeps surprising:

- A `Sensor` push is labelled with the ids of the accessory *before* the one it describes.
- `setPointCapabilities` with one field missing answers `202 Accepted` and changes nothing.
- `freezeProtection` takes `41` and rejects `41.0`.
- `systemSwitch` with an empty body switched a live thermostat off.

So there is one rule:

> **A claim about the API is worth the capture behind it.** Every payload key, enum value,
> route and device type in the code or the tests appears in a capture kept in this repo.

Making a capture takes no proxy and no special rig. The integration records its own traffic,
and one script turns that record into a test fixture. The one thing this can't show is the
request behind a command no entity sends yet (step 4).

**Have hardware that isn't supported, and don't write code?** Open a
[Hardware report](https://github.com/sfcodes/ha-resideo/issues/new?template=hardware_report.yml).
The diagnostics file it asks for is enough for someone else to write and test the support.

## What the integration records

**Download diagnostics** (Settings → Devices & Services → Resideo → ⋮) gives one JSON file
with, for each thermostat:

| part | what it is | what it proves |
| --- | --- | --- |
| `shadow`, `rooms`, `configuration`, `priority` | the cloud's snapshots, as held in memory | what this hardware reports |
| `pushes` | every live update received, by type, with the newest three samples of each shape | that a push type exists, and what it looks like |
| `writes` | every command sent, the cloud's answer, the device's confirmation | that a command is accepted *and* applied |
| `drift` | where the state built from pushes differs from a fresh read of the cloud | that pushes are merged into the right place |

and every device in the account by model and kind (`account_devices`), supported or not.

- Recording starts when the integration loads, and starts over when it reloads or Home
  Assistant restarts. **Do the thing first, then download.**
- Tokens, MAC addresses, serial numbers, names, e-mail, phone and address are blanked before
  the file leaves Home Assistant.

## Adding support

### 1. Set up

```bash
python -m venv .venv && source .venv/bin/activate
pip install ruff -r requirements.txt -r requirements.test.txt
ruff check . && pytest
```

Run your branch in your own Home Assistant: copy or link `custom_components/resideo` into
its `custom_components` folder and restart. For the script to download diagnostics, create a
long-lived access token (your Home Assistant profile → Security) and put it in the
environment, never on the command line:

```bash
export HASS_URL=http://homeassistant.local:8123
export HASS_TOKEN=...
```

### 2. Capture what the cloud does

```bash
python -m scripts.evidence fetch -o a.json     # or use a file downloaded by hand
python -m scripts.evidence summary a.json
```

`summary` shows the devices in the account, the accessories, every push type and whether it
is merged, the commands sent, and the drift. Start from what it shows, not from what the
hardware ought to send. To see where one value lives, and what it has been:

```bash
# every snapshot, push, command, confirmation and drift row with that key, or named like it
python -m scripts.evidence find a.json rssi
python -m scripts.evidence check a.json        # before a download goes anywhere public
```

To learn what one action does, capture on both sides of it:

```bash
# change one thing in the Resideo Connect app, wait a few seconds
python -m scripts.evidence fetch -o b.json
python -m scripts.evidence diff a.json b.json
```

`diff` lists the state that changed, the pushes that arrived and the confirmations.

- A push type that is not merged costs a REST re-read each time it arrives. It is a
  candidate for a merge.
- A change made in the app appears as a confirmation marked `(not ours)`, under the name the
  cloud gives it.

### 3. File the capture as a home

```bash
python -m scripts.evidence add-home b.json elitepro-eim-redlink \
    --hardware "ElitePRO S1200, equipment interface module, 11 RedLINK sensors"
```

This writes `tests/homes/elitepro-eim-redlink.json`, scrubbed again by the current rules.
From then on the test suite replays it: the integration is set up on its snapshots and fed
every push it captured. Never edit a home file by hand.

**Read the file before you commit it.** The script blanks what it recognizes. It cannot
recognize a child's name typed into a field nobody has seen before, and a public repository
does not forget.

### 4. Write the code against the home

Tests load their payloads from a capture: your home, or an older fixture in
`tests/aioresideo/fixtures`. Changing a value in a loaded payload to set up a case is fine
(an enum only to a value some capture shows); typing in a key or a shape is not.

```python
from tests.homes import captured_pushes, load_home

device = load_home("elitepro-eim-redlink")["data"]["devices"][0]   # your home's name
pushes = [value for name, value in captured_pushes(device) if name == "Sensor"]
```

- **Use only what a capture shows.** A value or shape that isn't in one stays unmapped. An
  unmapped push type falls back to a REST re-read, which is slower and always right.
- **A push is not shaped like the snapshot of the same data.** Keys are renamed and blocks
  move. Map each key you can point to.
- **A command needs all three:** the cloud accepted it, the device confirmed it, and the
  state afterwards changed the way you meant and in no other way.
- **Commands act on real equipment.** Never send one with a guessed or empty body to see
  what happens. Try a route by sending its current value back.
- **A command no entity sends yet can't be captured this way,** whether or not the client
  has a method for it. The record shows what the app's change *did* (the pushes, the
  confirmation's name, the state afterwards), not the request the app sent. Recording that
  takes a proxy between the app and the cloud, which this tooling doesn't do. Do it in the
  app, download, and open a Hardware report with the file. The command waits there for
  someone with such a recording; with its body known, it is added like any other.

### 5. Prove it on your hardware

Run your branch in your Home Assistant, repeat the actions (from Home Assistant too, if you
added a command), and capture again:

```bash
python -m scripts.evidence fetch -o after.json
python -m scripts.evidence summary after.json
python -m scripts.evidence add-home after.json elitepro-eim-redlink --replace
```

`--replace` drops whatever the old capture held and the new one doesn't, which is why the
actions are repeated first; the test suite fails if a push type or a command the code relies
on went missing. The summary is the proof, and the refiled home carries it into the pull
request, where anyone can run `summary` on it:

- a push type you merged says **merged in memory: yes**;
- a command you added says **accepted** and **confirmed**;
- **Drift from the cloud** lists nothing that stays on the paths your code writes.

Not every drift row is a fault. The summary says how to read each kind it lists, and when
the comparison proves nothing because no push has been merged since the last full read. A
row still there in a second download, on a path you didn't touch, is worth a bug report, not
a fix in the same pull request.

A change that only reads a value from another place merges and sends nothing, so the summary
has nothing to show for it. Say what you watched instead, under "Ran on": the entity's
history moving at the time of a push, for example.

## Pull requests

The template's comments say what a review needs. Beyond that:

- **One change per pull request.** A push merge, a new entity and a refactor are three.
- **CI runs `ruff check .` and `pytest`.** The suite replays every home, checks each one for
  anything private, and fails when a merged push type, a command, or a command an entity
  sends is added without a capture (`tests/aioresideo/test_evidence.py`). The ones older
  than this rule are listed at the top of that file; when your home captures one, the suite
  fails until you take it off the list.
- **Open an issue first** for changes under `.github/`, to `requirements*.txt` or to
  `requirements` in `manifest.json`, and for new network hosts, dependencies or binary
  files. They get a security review of their own, so they stay out of feature pull requests.
- **Review is of a specific commit.** Pushing afterwards restarts it.

## AI assistants

Use one if you like; `AGENTS.md` gives it this project's rules. What is asked of you stays
the same: you have read every line you submit, you can explain it, and the capture came from
your hardware. A model asked for a payload it has never seen writes a convincing one, which
is what the evidence rule is there to catch.
