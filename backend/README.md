# RepairOS backend

Python 3.12, FastAPI gateway and the design engine client. Plan and rules: PLAN.md and AGENTS.md.

```
make bootstrap      # backend/.venv (uv if present, else python3 -m venv) + deps from requirements.lock
cp backend/.env.example backend/.env   # then fill in the keys your profile needs (build/doctor.py says which)
make test           # pytest backend/tests
make codegen        # regenerate repairos/contracts/ from spec/contracts/ (never hand-edit them)
```

| Path | What |
|---|---|
| `repairos/config.py` | Settings from the environment + `backend/.env`; secrets redacted; release lockout |
| `repairos/registry.py` | `slot("STATE")` returns the implementation `config/providers.yaml` selects |
| `repairos/slots/` | one Protocol per slot (`<slot>.py`), implementations in `<slot>_<impl>/` (state, blob) |
| `repairos/grain/`, `repairos/report/` | the design engine client and the report built from its answer |
| `repairos/contracts/` | generated Pydantic v2 models; `contracts.validate(name, data)` checks the full schema |
| `tests/unit/`, `tests/conformance/<slot>/` | unit tests; per-slot conformance suites (`--impl <impl>`) |

Dependencies are pinned in `pyproject.toml` (direct) and `requirements.lock` (all, `make lock`). Allowed licences:
Apache-2.0, MIT, BSD (PLAN.md invariant L2). Current core set: pydantic, pydantic-settings, PyYAML, jsonschema, numpy, pillow (MIT, BSD, permissive), httpx2 (BSD-3), anthropic (Apache-2.0);
transitive: typing-extensions (PSF-2.0), python-dotenv (BSD-3), packaging (Apache-2.0/BSD-2), the rest MIT.
