.PHONY: init up demo test down logs
init:
	python scripts/setup.py
up:
	docker compose --profile nvr --profile ai up -d --build
demo:
	python scripts/setup.py --demo
	docker compose up -d --build
test:
	python -m pytest -q
down:
	docker compose --profile nvr --profile ai down
logs:
	docker compose --profile nvr --profile ai logs --tail=100 -f
