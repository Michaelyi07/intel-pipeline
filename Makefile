.PHONY: setup test demo migrate downgrade

setup:
	pip install -r requirements.txt -r requirements-dev.txt

test:
	pytest tests -v

demo:
	python scripts/smoke_demo.py

migrate:
	alembic -x db_url="$${DATABASE_URL}" upgrade head

downgrade:
	alembic -x db_url="$${DATABASE_URL}" downgrade base
