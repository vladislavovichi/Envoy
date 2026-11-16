COMPOSE = docker compose -f compose.yaml -f compose.dev.yaml
TEST_COMPOSE = docker compose -f compose.yaml -f compose.test.yaml --profile test

.PHONY: up down migrate test lint

up:
	docker compose -f compose.yaml -f compose.dev.yaml up -d --wait postgres
	$(COMPOSE) run --rm migrate

down:
	docker compose -f compose.yaml -f compose.dev.yaml -f compose.test.yaml --profile test down

migrate:
	$(COMPOSE) run --rm migrate

test:
	$(TEST_COMPOSE) run --rm tests

lint:
	$(TEST_COMPOSE) run --rm --no-deps tests sh -ec "black --check . && ruff check && mypy && lint-imports"
