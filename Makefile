.PHONY: sync db-up db-down migrate test lint run demo capacity resources

sync:
	uv sync --all-extras

db-up:
	docker compose up -d postgres
	@echo "等待 PG 就绪..."
	@until docker compose exec -T postgres pg_isready -U ao -d ao >/dev/null 2>&1; do sleep 0.5; done

db-down:
	docker compose down

migrate:
	uv run python -m ao.migrate

test:
	uv run pytest -q

lint:
	uv run ruff check .

run:
	uv run uvicorn ao.web:app --port 8000 --reload

resources:
	uv run python scripts/seed_resources.py

demo:
	uv run python scripts/demo.py

capacity:
	uv run python scripts/capacity.py
