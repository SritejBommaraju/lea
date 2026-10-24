# Conformance suites

One suite per slot in `backend/tests/conformance/<slot>/` (slot id in lower case), written by the slot's
`M.<SLOT>.0` card. The same assertions run for every implementation, on recorded real data (BUILD.md §3.3).

```
pytest backend/tests/conformance/grain              # the implementation config/providers.yaml selects
pytest backend/tests/conformance/grain --impl live  # a named implementation
```

Tests take the `impl` fixture (`conftest.py`): it loads the implementation through the registry and skips with
`HUMAN ACTION NEEDED` when the card's human prerequisites (keys, devices) are missing. Never fake them.
An implementation card is `done` only when its suite is green; paste the run summary in the card's PR.
