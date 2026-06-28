.PHONY: install init-db sync extract decide pipeline export-features serve dev

PYTHON ?= python3
VENV ?= .venv

install:
	$(PYTHON) -m venv $(VENV)
	. $(VENV)/bin/activate && pip install -r backend/requirements.txt

init-db:
	. $(VENV)/bin/activate && $(PYTHON) backend/cli.py init-db

sync:
	. $(VENV)/bin/activate && $(PYTHON) backend/cli.py sync

extract:
	. $(VENV)/bin/activate && $(PYTHON) backend/cli.py extract

decide:
	. $(VENV)/bin/activate && $(PYTHON) backend/cli.py decide

pipeline: sync extract decide

export-features:
	. $(VENV)/bin/activate && $(PYTHON) backend/cli.py export-features --out ml/exports/features.csv

serve:
	. $(VENV)/bin/activate && uvicorn backend.api.main:app --reload --host 0.0.0.0 --port 8000

dev: pipeline serve
