# megalink-viewer -- development and deployment tasks.
#
# Everything runs through `uv`, so a checkout needs no preparation beyond
# `make install` (or nothing at all: uv syncs on demand).

UV ?= uv
HOST ?= stord-pk
RANGE ?= 1-10
LANE ?= 1

.DEFAULT_GOAL := help
.PHONY: help install run gui watch show hosts ranges lanes display fleet config \
	test test-watch lint format check clean build push diagnose pi-install guide screenshots

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  Variables: HOST=$(HOST) RANGE=$(RANGE) LANE=$(LANE)"

install: ## Create the environment and install the package with dev tools
	$(UV) sync

run: gui ## Alias for `gui`

gui: ## Open the windowed display: target face and score table (HOST=, RANGE=, LANE=)
	$(UV) run megalink gui $(HOST) $(RANGE) $(LANE)

watch: ## Run the terminal per-position display (HOST=, RANGE=, LANE=)
	$(UV) run megalink watch $(HOST) $(RANGE) $(LANE)

show: ## Print one firing point once (HOST=, RANGE=, LANE=)
	$(UV) run megalink show $(HOST) $(RANGE) $(LANE)

display: ## Run the display this machine is configured to be (reads the config file)
	$(UV) run megalink display

fleet: ## Serve the dashboard listing every display on this network
	$(UV) run megalink fleet --open

config: ## Show this machine's display configuration
	$(UV) run megalink config

hosts: ## List the clubs streaming right now
	$(UV) run megalink hosts

ranges: ## List a club's live ranges (HOST=)
	$(UV) run megalink ranges $(HOST)

lanes: ## List a range's firing points (HOST=, RANGE=)
	$(UV) run megalink lanes $(HOST) $(RANGE)

test: ## Run the test suite
	$(UV) run pytest

test-watch: ## Re-run the tests on every change (needs entr)
	@find src tests -name '*.py' | entr -c $(UV) run pytest

lint: ## Check formatting and lint rules
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

format: ## Apply formatting and safe lint fixes
	$(UV) run ruff check --fix src tests
	$(UV) run ruff format src tests

check: lint test ## Everything CI would run

build: ## Build the wheel and sdist
	$(UV) build

guide: ## Build the printable setup guide (needs Docker)
	docs/guide/tools/pdf.sh

screenshots: ## Take the guide's screenshots again (needs Docker)
	docs/guide/tools/screenshots.sh

clean: ## Remove build and cache artefacts
	rm -rf dist build .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

push: ## Copy this checkout to a Pi over SSH and install it (PI=user@host, MODE=gui|terminal|browser, LANE2=second HDMI lane, AUTOLOGIN=1, TOKEN=fleet secret)
	@test -n "$(PI)" || { echo "set PI, e.g. make push PI=pi@fp-09 HOST=stord-pk RANGE=1-10 LANE=9"; exit 1; }
	./deploy/push.sh $(PI) \
		$(if $(HOST),--host $(HOST)) \
		$(if $(RANGE),--range $(RANGE)) \
		$(if $(LANE),--lane $(LANE)) \
		$(if $(LANE2),--lane2 $(LANE2)) \
		$(if $(NAME),--name $(NAME)) \
		$(if $(MODE),--mode $(MODE)) \
		$(if $(TOKEN),--token $(TOKEN)) \
		$(if $(AUTOLOGIN),--autologin)

diagnose: ## Ask a Pi why its display is not working (PI=user@host)
	@test -n "$(PI)" || { echo "set PI, e.g. make diagnose PI=pi@fp-09"; exit 1; }
	ssh -t $(PI) 'cd megalink-viewer && ./deploy/diagnose.sh'

pi-install: ## Install on this Raspberry Pi and enable it at boot (needs sudo)
	@test -n "$(HOST)" || { echo "set HOST (and optionally RANGE, LANE)"; exit 1; }
	sudo ./deploy/install.sh --host "$(HOST)" --range "$(RANGE)" --lane "$(LANE)"
