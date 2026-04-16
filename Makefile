.PHONY: dev test test-fast lint fmt up down clean migrate

dev:
	uv run uvicorn omniscience_server.app:create_app --factory --reload --host 0.0.0.0 --port 8000

test:
	uv run pytest

test-fast:
	uv run pytest -x -q

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy apps/ packages/

fmt:
	uv run ruff format .
	uv run ruff check . --fix

up:
	docker compose up -d

down:
	docker compose down

clean:
	docker compose down -v
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +

migrate:
	uv run alembic upgrade head
