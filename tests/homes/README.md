# Homes

Each `.json` here is one real installation: the diagnostics download of somebody's account,
scrubbed of everything that identifies them. They are the evidence for what the code claims
about hardware its author doesn't own, and the test suite replays every one of them
(`tests/resideo/test_homes.py`).

- Written by `python -m scripts.evidence add-home`, never by hand.
- Named for the hardware: `t10-two-sensors`, `elitepro-eim-redlink`.
- Read by their owner before being committed.

See [CONTRIBUTING.md](../../CONTRIBUTING.md).
