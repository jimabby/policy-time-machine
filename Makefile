.PHONY: help up down seed logs demo reset test lint unit style storyboard video charts charts-check rule stability confirm cost sweep grid dev preflight calibrate rules propose drafts readjudicate draft crosscheck export prune vacuum retain adopt discard gate rulings power tour coverage noise confirmed second blast versions compare adopt-plan reruns history-export history-rulings history-resolve history-prune explorer tampering

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
# Which database every target below reads. Overridable, and it has to be: the
# import targets default to include/history.db while this hardcoded
# include/ptm.db, so after `make import-cases` the whole measurement half of
# this file - export, gate, power, sweep, blast, versions - silently went on
# reading the synthetic demo. Somebody importing two years of their own
# decisions then measured the fixture and had no way to tell from the output.
#
#     make export DB=include/history.db
#     make gate   DB=include/history.db
#
# The demo default stays what it was, so `make tour` and every target in the
# README behave identically to before.
DB ?= ./include/ptm.db
ENV = PTM_INCLUDE_DIR=./include PTM_DB=$(DB) PTM_OFFLINE=1
# Which domain the draft-lifecycle targets act on. Every other target names
# `expenses` inline because it is demonstrating one thing; adopt and discard
# take a version the caller has to look up first, so the domain has to be
# overridable in the same breath: make adopt D=refunds V=v2-draft1 BY="..."
D ?= expenses
.DEFAULT_GOAL := help
POLICY ?= v2
FILE ?= examples/expenses.csv
# The import targets' own default. Separate from DB above because these are the
# targets about *your* history and the rest are about the shipped demo; point
# DB at this to measure what you imported.
DATA_DB ?= include/history.db

.PHONY: import-preview import-cases replay-history replay-coverage snapshots
.PHONY: history-export history-rulings history-resolve history-prune
.PHONY: history-rule history-gate
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

history-export: ## Everything the Explorer shows for your imported history, as one file
	$(PY) manage.py --db "$(DATA_DB)" export $(D) $(POLICY) -o "ptm-$(D)-$(POLICY).json"

history-rulings: ## Export the human rulings held against your imported history
	$(PY) manage.py --db "$(DATA_DB)" rulings $(D) -o "ptm-$(D)-precedents.json"

# The other half of that, and the one that was missing: a ruling had to be made
# through the Airflow review UI, so a database full of your own history could
# be measured, exported and gated and never actually ruled on. CASE, OUTCOME
# and BY are required; NOTE is the reviewer's reason and is worth the typing,
# because it is the only free text in the system written by the person
# accountable for the decision.
#
#     make history-rule CASE=exp-0042 OUTCOME=deny BY=finance.lead NOTE="..."
history-rule: ## Record one human ruling against your imported history
	$(PY) manage.py --db "$(DATA_DB)" rule $(D) "$(CASE)" "$(OUTCOME)" --by "$(BY)" --note "$(NOTE)" $(ARGS)

history-gate: ## Hold a policy to the rulings recorded against your imported history
	$(PY) manage.py --db "$(DATA_DB)" gate $(D) $(POLICY) $(ARGS)

history-resolve: ## Clear replay runs left pending by an interrupted import or replay
	$(PY) manage.py --db "$(DATA_DB)" resolve $(D)

history-prune: ## Reclaim disk in your own database. Add ARGS="--days 30 --vacuum"
	$(PY) manage.py --db "$(DATA_DB)" prune --dry-run $(ARGS)

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
	# New DAGs start paused, and a paused DAG's backfill runs sit queued
	# forever - so everything is unpaused, but the replay only *after* its
	# backfill exists. The demo's metadata database is SQLite, which takes one
	# writer at a time: unpaused first, the replay's early months start running
	# while the command is still inserting the later ones, and it dies partway
	# with "database is locked", leaving a backfill with months missing.
	docker compose exec airflow airflow dags unpause adjudicate_expenses
	docker compose exec airflow airflow dags unpause precedent_gate_expenses
	docker compose exec airflow airflow backfill create \
		--dag-id replay_expenses \
		--from-date 2024-09-01 --to-date 2026-09-01 \
		--run-backwards
	docker compose exec airflow airflow dags unpause replay_expenses

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

# The demo video's timing contract, checked without rendering anything. The
# shot durations and the narration have to reconcile per scene, or every scene
# after a mismatch plays under the wrong sentence.
storyboard: ## Check the demo video's shot timings against its narration
	$(PY) scripts/storyboard.py

# The whole video, rebuilt: 2x stills from the dashboard, two stills of Airflow
# itself (the backfill's runs and a waiting review - so `make up`, `make demo`
# and a review queued first), the narration (one neural-voice line per shot,
# with word timings), the music bed, then every frame drawn and muxed with
# burned-in subtitles. Needs Chrome, the network for the voice, and
# `pip install -r requirements-video.txt`.
video:     ## Rebuild policy_time_machine_demo.mp4 from the storyboard
	$(PY) scripts/build_shots.py
	$(PY) scripts/build_airflow_shots.py
	$(PY) scripts/generate_audio.py
	$(PY) scripts/generate_music.py
	$(PY) scripts/assemble_video.py

# The README's and the demo script's figures, drawn from a real offline replay
# of the shipped fixture. `charts-check` is what CI runs: it never writes, and
# it fails when a committed picture has stopped agreeing with the numbers the
# fixture produces - which is the same contract tests/test_docs.py holds the
# prose to, applied to the medium a reader trusts more and can check less.
charts:    ## Redraw docs/charts/*.svg from the fixture
	$(ENV) $(PY) scripts/build_charts.py

charts-check: ## Fail if a committed chart no longer matches the fixture
	$(ENV) $(PY) scripts/build_charts.py --check

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

# Recording a ruling against the demo, for rehearsing the loop the review UI
# runs. Same arguments as history-rule above; this one acts on the fixture.
rule:      ## Record one human ruling. CASE=.. OUTCOME=.. BY=.. [NOTE=..]
	$(ENV) $(PY) -m ptm.precedents expenses --rule "$(CASE)" "$(OUTCOME)" --by "$(BY)" --note "$(NOTE)" $(ARGS)

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

# The question next to `versions` and `compare`, which both ask about two
# *policies*. This asks about two runs of one policy: the number moved, and the
# answer is the policy text, the history underneath it, or the judge - each read
# from the hashes those runs archived rather than guessed at from a count.
reruns:    ## Two runs of one version. Was it the policy, the data, or the judge?
	$(ENV) $(PY) -m ptm.report expenses $(POLICY) --runs
	$(ENV) $(PY) -m ptm.report expenses $(POLICY) --rerun

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

# The half of `export` that somebody can actually read. The JSON has always
# carried its caveats out of the dashboard; opening it still meant starting a
# scheduler, which the person being asked to approve the change is the least
# likely person in the building to do.
explorer:  ## The whole Diff Explorer as one file you can email. No server needed
	$(ENV) $(PY) -m ptm.report expenses v2 --html -o ptm-expenses-v2.html
	@echo "open ptm-expenses-v2.html"

# Free, and it reads the cases already on file. A case arguing with the judge
# rather than with the policy is worth a human's attention whether or not the
# fence in ptm.judge held - which it does, and silently.
tampering: ## Which recorded cases contain text aimed at the judge, not the policy
	$(ENV) $(PY) -m ptm.injection expenses v2

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
