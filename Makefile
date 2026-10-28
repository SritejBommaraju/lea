# LEA (RepairOS). `make bootstrap` once, then `make test`. Targets for tasks that have not landed say so and stop.
SHELL := /bin/bash
VENV  := backend/.venv
PY    := $(VENV)/bin/python
UV    := $(shell command -v uv 2>/dev/null)

.PHONY: bootstrap lock codegen codegen-check contracts check test replay sessions-check run-gateway doctor next status hooks guard

bootstrap: ## create backend/.venv (Python 3.12) and install the backend with dev deps from the lock file
ifdef UV
	uv venv --allow-existing --python 3.12 $(VENV)
	uv pip sync --python $(PY) backend/requirements.lock
	uv pip install --python $(PY) --no-deps -e backend
else
	python3 -m venv $(VENV)
	$(PY) -m pip install -q -r backend/requirements.lock
	$(PY) -m pip install -q --no-deps -e backend
endif
	@if [ ! -f backend/.env ]; then echo "backend/.env missing: cp backend/.env.example backend/.env and fill in the keys"; fi

lock: ## re-pin every dependency (needs uv)
	uv pip compile --python 3.12 --universal backend/pyproject.toml --extra dev -o backend/requirements.lock

codegen: ## spec/contracts -> Pydantic (backend/repairos/contracts) + Swift (ios/RepairOS/Contracts); needs uv + npx
	python3 -I tools/codegen/codegen.py

codegen-check: ## fail when a generated file differs from a fresh `make codegen`
	python3 -I tools/codegen/codegen.py --check

contracts: ## schemas vs their examples
	python3 -I spec/contracts/check.py

check: contracts codegen-check guard test ## everything that exists today (licence check: task B15)

test: ## backend unit + conformance tests (pytest)
	$(VENV)/bin/pytest backend/tests

replay: ## validate one recorded session and load it into a fresh state and blob store (task B6); judges nothing
	@test -n "$(SESSION)" || { echo "usage: make replay SESSION=<session_id under samples/sessions/ | session folder path>"; exit 2; }
	$(PY) tools/replay.py --session $(SESSION)

sessions-check: ## validate every non-held-out recorded session in samples/sessions/ (task B6)
	$(PY) tools/replay.py --check --all

run-gateway: ## FastAPI gateway on :8000 (task B5)
	cd backend && .venv/bin/uvicorn repairos.main:app --reload --host $${GATEWAY_HOST:-0.0.0.0} --port 8000

doctor: ## human prerequisites of the open tasks (HUMAN ACTION NEEDED)
	python3 -I build/doctor.py

next: ## tasks ready to start, per stream (ARGS="--stream app")
	python3 -I build/next.py $(ARGS)

status: ## regenerate build/STATUS.md
	python3 -I build/status.py

hooks: ## install the pre-commit guard once per clone (blocks private files)
	git config core.hooksPath tools/hooks

guard: ## scan every tracked + staged file (or PATHS="a b") for private files
	python3 -I tools/guard_proprietary.py $(if $(PATHS),$(PATHS),--tracked)
