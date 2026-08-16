.PHONY: dev up down logs
dev:
	uvicorn main:app --reload --reload-exclude '.venv/*'

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f
