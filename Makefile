.PHONY: install run demo test test-cov lint format eval eval-safety eval-agent docker-build docker-up clean

VENV := .venv
PY   := $(VENV)/bin/python
PORT ?= 8000

install:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -r requirements-dev.txt
	@echo "\nDone. Copy .env.example to .env and add your OPENAI_API_KEY."

## Chat UI + trace panel at http://localhost:$(PORT)
run:
	$(VENV)/bin/uvicorn app.main:app --reload --port $(PORT)

## Same, with no API key required (scripted double)
demo:
	OFFLINE_MODE=1 $(VENV)/bin/uvicorn app.main:app --reload --port $(PORT)

## Deterministic layers — no key, no network (enforced by tests/conftest.py)
test:
	$(PY) -m pytest

## The same run with a coverage report
test-cov:
	$(PY) -m pytest --cov --cov-report=term-missing

lint:
	$(PY) -m ruff check .

## Apply every lint fix ruff can make on its own. Deliberately NOT `ruff format`:
## several modules group short literals and table-like constants onto shared
## lines on purpose, and the formatter would explode each onto its own.
format:
	$(PY) -m ruff check . --fix

## Full suite: new pipeline vs. the original prompt. Needs a key.
eval:
	FROZEN_NOW=2025-03-04T05:40:00 $(PY) -m evals.runner --variant both

## Safety gate only. Passes with no key — crisis detection is deterministic.
eval-safety:
	OFFLINE_MODE=1 FROZEN_NOW=2025-03-04T05:40:00 $(PY) -m evals.runner --variant agent --dimension safety

## New pipeline only, all dimensions. Needs a key.
eval-agent:
	FROZEN_NOW=2025-03-04T05:40:00 $(PY) -m evals.runner --variant agent

docker-build:
	docker compose build

## http://localhost:8910
docker-up:
	docker compose up

clean:
	rm -f traces.jsonl captured_records.jsonl evals/results.json .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache
