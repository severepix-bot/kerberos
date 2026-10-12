PYTHON := .venv/bin/python
RUFF := .venv/bin/ruff
MYPY := .venv/bin/mypy
PYTEST := .venv/bin/pytest
PIP_AUDIT := .venv/bin/pip-audit

.PHONY: check test lint format-check typecheck build audit

check: build lint format-check typecheck test

test:
	$(PYTEST) --cov --cov-report=term-missing

lint:
	$(RUFF) check .

format-check:
	$(RUFF) format --check .

typecheck:
	$(MYPY)

build:
	$(PYTHON) -m compileall -q assistente tests

audit:
	$(PIP_AUDIT) -r requirements.txt
