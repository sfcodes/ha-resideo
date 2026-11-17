# Instructions for AI coding agents

Home Assistant custom integration for Resideo thermostats. It talks to a private cloud API
with **no documentation anywhere**: your training data does not contain it, and nothing
about it can be looked up. Plausible guesses about it are usually wrong; `CONTRIBUTING.md`
opens with four real examples. Read it in full before you change anything: it is the
workflow, and this file is the part of it you must not get wrong.

## The evidence rule

Every payload key, enum value, route and device type you put into code or tests must appear
in a capture in this repo:

- `tests/homes/*.json`: real installations, written by `scripts/evidence.py add-home`
- `tests/aioresideo/fixtures/`: older frames, sanitized by hand

If it isn't in a capture, you don't know it. Write no code for it, and tell the person what
would show it: for anything the cloud sends, a download taken after doing it in the app
(`CONTRIBUTING.md`, step 2). Nothing breaks meanwhile: an unmapped push type already falls
back to a REST re-read, which is correct. Never:

- write or edit a home file or a fixture by hand, or "complete" one with likely values;
- derive a push's shape from the REST snapshot of the same data, or one type's from another;
- extend a mapping to values you haven't seen;
- send a command to find out what it does. Commands act on someone's heating and cooling;
- make the integration send a command it doesn't send yet: one the client lacks, or one on
  `COMMANDS_NO_ENTITY_SENDS` in `tests/aioresideo/test_evidence.py`, whose method and
  docstring are not evidence. `CONTRIBUTING.md`, step 4, says why, and what the person does
  instead;
- write "verified live", "tested" or "captured" about something the person didn't run.

## Working on a change

1. Read the capture: `python -m scripts.evidence summary <file>` on a home or a download,
   `find <file> <key>` for where a key lives and the values seen, `diff <before> <after>` on
   a pair taken around one action.
2. Write a test that fails without your change, with its payloads loaded from a capture
   (`CONTRIBUTING.md`, step 4, has the helpers and what may be changed in a loaded payload).
   If only an older fixture can make the test fail, use it and say so under "What the capture
   doesn't show".
3. Make the smallest change that passes. Asked for several things, do each on its own
   branch, and tell the person which ones have no evidence yet instead of writing them.
4. Run `ruff check .` and `pytest` with the venv's Python. Both must pass.
5. The proof is the person running the branch on their hardware and capturing again
   (`CONTRIBUTING.md`, step 5). You cannot produce it. Until they have, the pull request is
   a draft and says "not run yet".
6. Fill the pull request template with facts. Its comments say what each section wants.

## Where things are

| path | what |
| --- | --- |
| `custom_components/resideo/aioresideo/` | the API client: `client.py` (REST), `stream.py` (SignalR), `merge.py` (pushes into cached state), `objects/` (typed views over raw dicts) |
| `custom_components/resideo/` | the Home Assistant layer: `coordinator.py`, one file per platform |
| `custom_components/resideo/capture.py`, `diagnostics.py` | what the integration records about its own traffic |
| `scripts/evidence.py` | reads a diagnostics download: `fetch`, `summary`, `find`, `diff`, `check`, `add-home` |
| `tests/homes/` | the homes; `tests/resideo/test_homes.py` replays each one |
| `tests/aioresideo/test_evidence.py` | fails when a merged push type, a command, or a command an entity sends is added without a capture; the ones older than the rule are listed at its top |
| `docs/CLIENT.md` | the client's public surface |

The docstrings in `merge.py` and `client.py` record what was learned and how; read the ones
next to the code you change. They cite a `resideo-api-spec.md` that is not in this repo.
Don't reconstruct it.

## Code conventions

- Match the file you are in: naming, docstring style, how tests are named and laid out.
- `merge.py` functions are pure and never clobber: only keys present in the push are written.
- Models in `objects/` are read-only views; they return `None` for what is absent.
- Entities are created only when the device reports the value behind them (`exists_fn`).
- Comments say why, and what was observed. No comment restates the code.
- Don't refactor, rename or reformat code your change doesn't need.

## Boundaries

- Never print, log or commit a token, MAC address, serial number, e-mail or a name somebody
  typed. `add-home` blanks what it recognizes, which is not everything, so the person still
  reads a home file before it is committed. A download going anywhere else public (an issue,
  a comment) must pass `python -m scripts.evidence check <file>` first.
- `HASS_TOKEN` stays in the environment. Don't echo it or pass it as an argument.
- Don't touch `.github/`, `requirements*.txt` or `requirements` in `manifest.json`, and don't
  add dependencies, network hosts or binary files. If the task seems to need it, stop and
  tell the person.
- Don't call the Resideo API from your own scripts. The login rotates its refresh token, so
  a second client signs the person's Home Assistant out.
