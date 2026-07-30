.PHONY: install test lint fmt demo api bot indexer clean

install:
	python3 -m venv .venv
	.venv/bin/pip install -e ".[dev]"

test:
	.venv/bin/python -m pytest -q

lint:
	.venv/bin/ruff check .

fmt:
	.venv/bin/ruff check --fix .
	.venv/bin/ruff format .

# One indexer cycle against mock venues, alerts evaluated but not sent.
demo:
	.venv/bin/giftpulse indexer --once --dry-run

indexer:
	.venv/bin/giftpulse indexer

api:
	.venv/bin/giftpulse api

bot:
	.venv/bin/giftpulse bot

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__ *.db
