default:
    @just --list

sync:
    uv sync --group dev

lint:
    uv run ruff check .
    uv run ruff format --check .

fmt:
    uv run ruff format .

test:
    uv run pytest -q

api:
    uv run nasatrack api

# The fiscal-year report reads the same local mirror, so it refreshes with it.
mirror:
    uv run nasatrack mirror
    uv run python cancellations_for_convenience_by_fiscal_year.py

doge:
    uv run nasatrack doge

merge:
    uv run nasatrack merge

# mirror and api each end in their own merge, so api runs last and its merge publishes.
all: mirror doge api
