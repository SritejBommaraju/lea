# Conformance suites

One suite per slot in `backend/tests/conformance/<slot>/` (slot id in lower case), written by the task that adds
the slot. The same assertions run for every implementation, on recorded real data.

```
pytest backend/tests/conformance/det                # the implementation config/providers.yaml selects
pytest backend/tests/conformance/det --impl gdino   # a named implementation
```

Tests take the `impl` fixture (`conftest.py`): it loads the implementation through the registry and skips with
`HUMAN ACTION NEEDED` when the human prerequisites (keys, devices) of the task that lists it in `slots`
(`build/tasks.yaml`) are missing. Never fake them. An implementation is done only when its suite is green; paste
the run summary in the task's PR.
