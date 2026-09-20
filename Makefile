.PHONY: help up down seed logs demo reset test lint unit style stability confirm cost sweep grid dev preflight calibrate rules propose drafts readjudicate draft crosscheck export prune vacuum retain adopt discard gate rulings power tour coverage noise confirmed second blast versions compare adopt-plan

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
# Which domain the draft-lifecycle targets act on. Every other target names
# `expenses` inline because it is demonstrating one thing; adopt and discard
# take a version the caller has to look up first, so the domain has to be
# overridable in the same breath: make adopt D=refunds V=v2-draft1 BY="..."
D ?= expenses
.DEFAULT_GOAL := help
POLICY ?= v2
FILE ?= examples/expenses.csv
DATA_DB ?= include/history.db

.PHONY: import-preview import-cases replay-history replay-coverage snapshots
import-preview: ## Validate a CSV/JSON import without writing cases (FILE=... D=...)
	$(PY) manage.py --db "$(DATA_DB)" import $(D) "$(FILE)"

import-cases: ## Import a validated CSV/JSON file atomically
	$(PY) manage.py --db "$(DATA_DB)" import $(D) "$(FILE)" --write

replay-history: ## Replay imported history offline without seeding
	$(PY) manage.py --db "$(DATA_DB)" replay $(D) $(POLICY)

replay-coverage: ## Inspect replay completeness and stale evidence
	$(PY) manage.py --db "$(DATA_DB)" coverage $(D) $(POLICY)

snapshots: ## Export the facts and policies archived with each replay
	$(PY) manage.py --db "$(DATA_DB)" snapshots $(D) $(POLICY) -o "ptm-$(D)-$(POLICY)-snapshots.json"

help:      ## List targets
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t20

# --wait blocks until the healthcheck in docker-compose.yaml passes, which is a
# request to the Diff Explorer's own API. Without it this returned while Airflow
# was still migrating and the next line invited you to open a page that would
# not exist for another half-minute.
up:        ## Start Airflow 3.1 at http://localhost:8080 (no login), and wait for it
	docker compose up --build -d --wait
	@echo "Airflow ready -> http://localhost:8080  |  Diff Explorer -> http://localhost:8080/ptm/"
	@echo "No login: the compose file sets simple_auth_manager_all_admins for the demo."
	@echo "Set PTM_OPEN_UI=false to turn that off; Airflow then generates a password"
	@echo "into simple_auth_manager_passwords.json.generated and prints it to the logs."

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

tour:      ## The whole engine end to end, one command, no Airflow. Also: python demo.py
	$(BOOTSTRAP) demo.py

dev:       ## Create the local venv used by test/lint/unit
	$(BOOTSTRAP) -m venv .venv && $(PY) -m pip install -q -r requirements-dev.txt

lint:      ## Check every domain YAML against the policies it claims to implement
	$(ENV) $(PY) -m ptm.lint

unit:      ## Run the test suite
	$(ENV) $(PY) -m pytest -q

# Scoped to ptm/ because that is the part with a coverage question worth asking:
# dags/ and ptm_dags/ are covered by parsing under a real Airflow, which is a
# different job and is not what a line count measures. The floor lives in
# pyproject.toml and is a ratchet rather than an aspiration - raise it when the
# number rises, never lower it to make a build pass.
coverage:  ## Run the test suite with a line-coverage report over ptm/
	$(ENV) $(PY) -m pytest -q --cov=ptm --cov-report=term-missing --cov-report=xml

style:     ## Check the code the way CI does (see ruff.toml for what and why)
	$(PY) -m ruff check .

test: lint style unit  ## Lint, test, then run the whole engine end to end with no Airflow
	$(ENV) $(PY) -m ptm.selftest
	$(ENV) $(PY) -m ptm.pit_check
	$(ENV) $(PY) -m ptm.gate expenses v2 --introduced-only

gate:      ## Run the precedent regression suite without Airflow. Non-zero on a reversal
	$(ENV) $(PY) -m ptm.gate expenses v2 --introduced-only

rulings:   ## Export every human ruling, with what each one replaced
	$(ENV) $(PY) -m ptm.precedents expenses -o ptm-expenses-precedents.json
	@echo
	@echo "load it into another database with:"
	@echo "  python -m ptm.precedents expenses --import ptm-expenses-precedents.json"

power:     ## How big a change could this much history actually detect?
	$(ENV) $(PY) -m ptm.report expenses v2 --power

noise:     ## The judge's noise floor, without Airflow. Inert offline, and says so
	$(ENV) $(PY) -m ptm.stability expenses v2

confirmed: ## Re-judge the recorded flips so an unstable one stays out of the queue
	$(ENV) $(PY) -m ptm.stability expenses v2 --target flips

second:    ## What a second judge made of it. Reads the last one; needs PTM_OFFLINE=0 to make one
	$(ENV) $(PY) -m ptm.crosscheck expenses v2

blast:     ## Which segments carry more of the change than the rest of their field
	$(ENV) $(PY) -m ptm.disparity expenses v2

versions:  ## Every version replayed, side by side. Did the edit help?
	$(ENV) $(PY) -m ptm.report expenses --history

# The pair the loop exists to produce. `make versions` says what each version
# does; this says which cases the two of them actually disagree about, which is
# the half a table of totals cannot show.
compare:   ## Two versions case by case. make compare L=v1 R=v2
	@test -n "$(L)" -a -n "$(R)" || (echo "ERROR set L=<version> R=<version>; 'make versions' lists them" && exit 2)
	$(ENV) $(PY) -m ptm.report expenses --compare $(L) $(R)

cost:      ## Forecast what a full LLM-backed replay would cost
	$(ENV) $(PY) -c "import json; from ptm import report; \
		print(json.dumps(report.cost_report('expenses','v2')['forecast'], indent=2))"

preflight: ## Read the policies for problems before paying to replay them
	$(ENV) $(PY) -m ptm.preflight expenses
	$(ENV) $(PY) -m ptm.preflight refunds

calibrate: ## Score the judge against the humans who ruled, and gate on it
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

export:    ## Everything the Explorer shows, as one file, without starting Airflow
	$(ENV) $(PY) -m ptm.report expenses v2 -o ptm-expenses-v2.json
	$(ENV) $(PY) -m ptm.report expenses v2 --csv -o ptm-expenses-v2-flips.csv

prune:     ## Drop cache and sample rows that have stopped earning their disk
	$(ENV) $(PY) -m ptm.prune --dry-run
	@echo
	@echo "drop the --dry-run to actually remove them, or 'make vacuum' to do both"

vacuum:    ## Prune for real, then rewrite the file so it actually shrinks
	$(ENV) $(PY) -m ptm.prune --vacuum

retain:    ## The same retention pass, on Airflow's schedule instead of yours
	docker compose exec airflow airflow dags trigger ptm_retention
	@echo "runs weekly on its own; this triggers it now. Add"
	@echo "  --conf '{\"dry_run\":true}'  to count without removing."

adopt:     ## Promote a draft into include/policies/ and register it. make adopt V=v2-draft1 BY="your name"
	@test -n "$(V)" || (echo "ERROR set V=<draft version>; 'make drafts' lists them" && exit 2)
	@test -n "$(BY)" || (echo "ERROR set BY=\"your name\" - adopting a policy records who did" && exit 2)
	$(ENV) $(PY) -m ptm.proposal $(D) --adopt $(V) --by "$(BY)" $(if $(AS),--as $(AS),)

# The only irreversible act here - two files written and a hand-maintained YAML
# rewritten in place - and until this flag existed it was also the only one with
# no way to look first. Same arguments as `adopt`, so the two cannot disagree
# about what is about to happen.
adopt-plan: ## What `make adopt` would do, without doing it. Same V= and BY=
	@test -n "$(V)" || (echo "ERROR set V=<draft version>; 'make drafts' lists them" && exit 2)
	@test -n "$(BY)" || (echo "ERROR set BY=\"your name\" - adopting a policy records who did" && exit 2)
	$(ENV) $(PY) -m ptm.proposal $(D) --adopt $(V) --by "$(BY)" $(if $(AS),--as $(AS),) --dry-run

discard:   ## Delete a draft's files, keeping what it proposed and why. make discard V=v2-draft1
	@test -n "$(V)" || (echo "ERROR set V=<draft version>; 'make drafts' lists them" && exit 2)
	$(ENV) $(PY) -m ptm.proposal $(D) --discard $(V)

crosscheck: ## Ask a second model the same questions (needs PTM_OFFLINE=0)
	docker compose exec airflow airflow dags trigger judge_stability_expenses \
		--conf '{"compare_model":"anthropic:claude-haiku-4-5","sample_cases":40}'
