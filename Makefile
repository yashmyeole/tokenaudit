.PHONY: corpus check

corpus:
	python corpus/synthetic/generate.py

check:
	ruff check .
	ruff format --check .
	mypy
	pytest
