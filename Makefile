.PHONY: help up down seed logs demo reset test lint unit stability cost dev

PY ?= .venv/bin/python
ENV = PTM_INCLUDE_DIR=./include PTM_DB=./include/ptm.db PTM_OFFLINE=1

help:      ## List targets
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t20

up:        ## Start Airflow 3.1 at http://localhost:8080 (admin/admin)
	docker compose up --build -d
	@echo "Airflow starting -> http://localhost:8080  |  Diff Explorer -> http://localhost:8080/ptm/"

down:      ## Stop Airflow
	docker compose down

reset:     ## Wipe the demo database and re-seed
	rm -f include/ptm.db include/ptm.db-wal include/ptm.db-shm
	docker compose exec airflow python -m ptm.seed

logs:      ## Tail the Airflow logs
	docker compose logs -f airflow

demo:      ## Replay two years of history through the backfill engine
	docker compose exec airflow airflow backfill create \
		--dag-id replay_expenses \
		--from-date 2024-09-01 --to-date 2026-09-01 \
		--run-backwards

stability: ## Measure how often the judge contradicts itself
	docker compose exec airflow airflow dags trigger judge_stability_expenses

dev:       ## Create the local venv used by test/lint/unit
	python3 -m venv .venv && $(PY) -m pip install -q -r requirements-dev.txt

lint:      ## Check every domain YAML against the policies it claims to implement
	$(ENV) $(PY) -m ptm.lint

unit:      ## Run the test suite
	$(ENV) $(PY) -m pytest -q

test: lint unit  ## Lint, test, then run the whole engine end to end with no Airflow
	$(ENV) $(PY) -m ptm.selftest
	$(ENV) $(PY) -m ptm.pit_check

cost:      ## Forecast what a full LLM-backed replay would cost
	$(ENV) $(PY) -c "import json; from ptm import report; \
		print(json.dumps(report.cost_report('expenses','v2')['forecast'], indent=2))"
