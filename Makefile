.DEFAULT_GOAL := help
VENV ?= .venv
PY := $(VENV)/bin/python

.PHONY: help venv install dev demo test cov lint fmt site docker-up docker-down clean

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## Create virtualenv
	test -d $(VENV) || python3 -m venv $(VENV)

install: venv ## Install with dev extras
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e ".[dev]"

dev: ## Collector + viewer with reload on :4318
	$(VENV)/bin/uvicorn genai_traces.asgi:app --reload --port 4318

demo: ## Send two days of demo traffic to the running collector over OTLP
	$(VENV)/bin/genai-traces demo

test: ## Run tests
	$(PY) -m pytest

cov: ## Tests with coverage
	$(PY) -m pytest --cov=genai_traces --cov-report=term-missing

lint: ## Ruff check + format check
	$(VENV)/bin/ruff check genai_traces tests
	$(VENV)/bin/ruff format --check genai_traces tests

fmt: ## Format + autofix
	$(VENV)/bin/ruff format genai_traces tests
	$(VENV)/bin/ruff check --fix genai_traces tests

site: ## Build the static demo (what GitHub Pages serves) into _site/
	rm -rf _site _site.db
	DATABASE_URL=sqlite:///./_site.db $(VENV)/bin/genai-traces demo --direct
	DATABASE_URL=sqlite:///./_site.db $(VENV)/bin/genai-traces export-static _site
	rm -f _site.db

docker-up: ## collector + postgres
	docker compose up --build -d

docker-down: ## Stop stack
	docker compose down -v

clean: ## Remove caches and local DB
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage traces.db* _site _site.db
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
