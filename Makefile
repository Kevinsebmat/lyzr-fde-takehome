# Lyzr FDE take-home. Every target runs offline unless it says otherwise.
#
# Environment: conda env `lyzer` (python 3.11). `make install` creates it.
CONDA_BASE := $(shell conda info --base 2>/dev/null || echo /opt/homebrew/Caskroom/miniforge/base)
ENV        := lyzer
PY         := $(CONDA_BASE)/envs/$(ENV)/bin/python
PIP        := $(CONDA_BASE)/envs/$(ENV)/bin/pip
export LLM_PROVIDER ?= mock

.DEFAULT_GOAL := help
.PHONY: help install test smoke lint dev web-build seed memo demo-reset clean

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
	 awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Create the `lyzer` conda env and install everything editable
	@$(CONDA_BASE)/bin/conda env list | grep -q '^$(ENV) ' || \
	 $(CONDA_BASE)/bin/conda create -y -n $(ENV) python=3.11
	$(PIP) install -q -e ./core -e '.[dev]'
	@echo "installed into conda env '$(ENV)'. run 'make smoke' — no API key needed."

test:  ## Run every test suite offline (deterministic, no API key)
	$(PY) -m pytest

smoke:  ## Run all 11 projects end-to-end in mock mode; this backs the README triage
	$(PY) scripts/smoke.py

lint:  ## Ruff check
	$(PY) -m ruff check .

dev:  ## FastAPI (:8000) + Next.js console (:3000)
	@echo "API on :8000, console on :3000 — ctrl-c stops both"
	@trap 'kill 0' EXIT INT TERM; \
	 $(PY) -m uvicorn server.main:app --reload --port 8000 & \
	 (cd web && pnpm dev) & \
	 wait

web-build:  ## Type-check and build the console
	cd web && pnpm install --frozen-lockfile && pnpm build

seed:  ## Regenerate the demo cassettes from their markdown/source definitions
	$(PY) scripts/seed_cassettes.py

memo:  ## Rebuild the Part 1 scoping note PDF (requires pandoc + Chrome)
	./scripts/build_memo.sh

demo-reset:  ## Clear traces and the sqlite db for a clean demo run
	rm -f traces.jsonl agentcore.db agentcore.db-wal agentcore.db-shm
	rm -rf .smoke

clean: demo-reset  ## demo-reset plus caches
	rm -rf .pytest_cache **/__pycache__
