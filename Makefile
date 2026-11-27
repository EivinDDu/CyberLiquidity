# Convenience targets. `make` alone lists them.
PY ?= python3.13
VENV := .venv
BIN := $(VENV)/bin

.PHONY: help setup app test experiment brief robustness clean-runs

help:
	@echo "make setup       create .venv and install pinned dependencies + the package"
	@echo "make app         start the app at http://localhost:8501"
	@echo "make test        run the full test suite"
	@echo "make experiment  rerun both research experiments (redundancy vs liquidity; gridlock and hoarding)"
	@echo "make brief       print the plain-language answer for the default scenario"
	@echo "make robustness  does the best intervention stay best? (circular-trade scenario)"

$(BIN)/streamlit:
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -r requirements.txt
	$(BIN)/python -m pip install -e .

setup: $(BIN)/streamlit

app: setup
	$(BIN)/streamlit run app/streamlit_app.py

test: setup
	$(BIN)/python -m pytest

experiment: setup
	$(BIN)/python experiments/redundancy_vs_liquidity.py
	$(BIN)/python experiments/gridlock_and_hoarding.py

brief: setup
	$(BIN)/cyberliquidity brief scenarios/default_72h.json

robustness: setup
	$(BIN)/cyberliquidity robustness scenarios/cluster_gridlock_48h.json

clean-runs:
	rm -rf runs
