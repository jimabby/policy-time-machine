.PHONY: help up down seed logs demo reset test lint unit stability confirm cost sweep grid dev preflight calibrate rules propose drafts readjudicate draft crosscheck

# A venv puts the interpreter in Scripts/ on Windows and bin/ everywhere else,
# and the bootstrap command is python3 on one and python on the other. Both are
# picked here rather than in each target, so `make test` works from Git Bash on
# Windows as well as from a shell on Linux or macOS. Override PY to point at
# any other interpreter.
ifeq ($(OS),Windows_NT)
PY        ?= .venv/Scripts/python
BOOTSTRAP ?= python
else
PY        ?= .venv/bin/python
BOOTSTRAP ?= python3
endif
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

confirm:   ## Re-judge the biggest flips to check each one reproduces
	docker compose exec airflow airflow dags trigger judge_stability_expenses \
		--conf '{"target":"flips","sample_cases":25,"samples_per_case":3}'

draft:     ## Have the proposal DAG write the next version of the policy
	docker compose exec airflow airflow dags trigger propose_expenses

dev:       ## Create the local venv used by test/lint/unit
	$(BOOTSTRAP) -m venv .venv && $(PY) -m pip install -q -r requirements-dev.txt

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

preflight: ## Read the policies for problems before paying to replay them
	$(ENV) $(PY) -m ptm.preflight expenses
	$(ENV) $(PY) -m ptm.preflight refunds

calibrate: ## Score the judge against the humans who ruled on the same cases
	$(ENV) $(PY) -m ptm.calibration expenses v2

rules:     ## Do the offline rules agree with the judge they stand in for?
	$(ENV) $(PY) -m ptm.rules expenses v2

propose:   ## Draft the next version of the policy from the evidence (writes nothing)
	$(ENV) $(PY) -m ptm.proposal expenses v2
	@echo
	@echo "add --write to draft it into include/drafts/ as a real policy version"

drafts:    ## List drafted amendments and what the gate made of each
	$(ENV) $(PY) -m ptm.proposal expenses --list

readjudicate: ## Re-ask rulings made about a clause the policy has since rewritten
	docker compose exec airflow airflow dags trigger adjudicate_expenses --conf '{"target":"stale"}'

sweep:     ## Ask what the threshold should be, not just which clause it is in
	$(ENV) $(PY) -m ptm.sweep expenses v2
	@echo
	$(ENV) $(PY) -m ptm.sweep expenses v2 1.1 amount_gbp 25,50,75,100,150,250

grid:      ## Move two thresholds together - one curve cannot show them interacting
	$(ENV) $(PY) -m ptm.sweep expenses v2 --joint \
		1.1:amount_gbp=25,50,75,100,150 3.1:days_notice=3,7,14,21

crosscheck: ## Ask a second model the same questions (needs PTM_OFFLINE=0)
	docker compose exec airflow airflow dags trigger judge_stability_expenses \
		--conf '{"compare_model":"anthropic:claude-haiku-4-5","sample_cases":40}'
