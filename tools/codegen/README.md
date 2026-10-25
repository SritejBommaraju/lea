# Contract codegen

`make codegen` (= `python3 -I tools/codegen/codegen.py`) turns every `spec/contracts/*.schema.json` into:

- `backend/repairos/contracts/<name>_schema.py`: Pydantic v2 (datamodel-code-generator 0.86.0 via `uvx`, black +
  isort pinned). `contracts.MODELS[name]` is the top-level model; `contracts.validate(name, data)` runs the full
  JSON Schema first (the models cannot express `if/then` rules) and returns the model.
- `ios/RepairOS/Contracts/Contracts.swift`: Swift Codable + Sendable structs (quicktype 26.0.0 via `npx`).

`make codegen-check` regenerates into a temp dir and fails on any difference. Never hand-edit the outputs.

Add a schema: put `<name>.schema.json` (with a `title`) and `examples/<name>.json` in `spec/contracts/`, run
`python3 -I spec/contracts/check.py`, then `make codegen` and `make test` (round-trip and Swift decode tests pick
it up automatically).
