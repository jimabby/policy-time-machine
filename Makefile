.PHONY: up down seed logs demo reset test

up:        ## Start Airflow 3.1 at http://localhost:8080 (admin/admin)
	docker compose up --build -d
	@echo "Airflow starting -> http://localhost:8080  |  Diff Explorer -> http://localhost:8080/ptm/"

down:
	docker compose down

reset:     ## Wipe the demo database and re-seed
	rm -f include/ptm.db && docker compose exec airflow python -m ptm.seed

logs:
	docker compose logs -f airflow

demo:      ## Replay two years of history through the backfill engine
	docker compose exec airflow airflow backfill create \
		--dag-id replay_expenses \
		--from-date 2024-09-01 --to-date 2026-09-01 \
		--run-backwards

test:      ## Run the engine end to end with no Airflow
	PTM_INCLUDE_DIR=./include PTM_DB=./include/ptm.db PTM_OFFLINE=1 .venv/bin/python -m ptm.selftest
