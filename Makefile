# Every target runs in containers; the host only needs Docker (or ENGINE=podman) and make.
# Run `make help` for the list.

ENGINE    ?= docker
IMAGE     ?= zip-service:local
DEV_IMAGE ?= zip-service-dev
UID       := $(shell id -u)
GID       := $(shell id -g)

# PORT (and the other settings) for `make run` / `make run-dev` come from .env.
-include .env
PORT ?= 8000

TTY := $(shell [ -t 0 ] && echo -t)
DEV_RUN = $(ENGINE) run --rm $(TTY) \
	-v "$(CURDIR)":/workspace \
	-v zip-service-dev:/opt/dev \
	-e REQUIRE_ZIP_TOOLS=1 \
	$(DEV_IMAGE)

.DEFAULT_GOAL := help
.PHONY: help dev-image lock format lint test test-slow test-all check image smoke run run-dev shell clean

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

dev-image: ## Build the development/test toolbox image
	$(ENGINE) build -f Dockerfile.dev --build-arg UID=$(UID) --build-arg GID=$(GID) -t $(DEV_IMAGE) .

lock: dev-image ## Resolve dependencies and (re)write uv.lock
	$(DEV_RUN) uv lock

format: dev-image ## Auto-format and apply safe lint fixes
	$(DEV_RUN) sh -c 'uv run --locked ruff format . && uv run --locked ruff check --fix .'

lint: dev-image ## ruff (lint + format check) and mypy --strict
	$(DEV_RUN) sh -c 'uv run --locked ruff check . && uv run --locked ruff format --check . && uv run --locked mypy'

test: dev-image ## Fast tests (unit + in-process)
	$(DEV_RUN) uv run --locked pytest $(ARGS)

test-slow: dev-image ## Integration tests against a real server process
	$(DEV_RUN) uv run --locked pytest -m slow $(ARGS)

test-all: test test-slow ## Fast and slow tests

check: lint test-all ## Everything CI runs before building the image

image: ## Build the production image
	$(ENGINE) build -t $(IMAGE) .

smoke: image dev-image ## Build the image and smoke-test it hardened
	ENGINE=$(ENGINE) PROBE_IMAGE=$(DEV_IMAGE) ./scripts/smoke_test.sh $(IMAGE)

run: image ## Run the production image with settings from .env
	$(ENGINE) run --rm -it --env-file .env -p $(PORT):$(PORT) --read-only --cap-drop ALL $(IMAGE)

run-dev: dev-image ## Run the service from source with settings from .env
	$(ENGINE) run --rm -it -v "$(CURDIR)":/workspace -v zip-service-dev:/opt/dev \
		--env-file .env -p $(PORT):$(PORT) \
		$(DEV_IMAGE) uv run --locked python main.py

shell: dev-image ## Interactive shell in the dev container
	$(ENGINE) run --rm -it -v "$(CURDIR)":/workspace -v zip-service-dev:/opt/dev \
		$(DEV_IMAGE) bash

clean: ## Remove the dev volumes and local caches
	-$(ENGINE) volume rm zip-service-dev zip-service-venv zip-service-uv-cache
	rm -rf .mypy_cache .pytest_cache .ruff_cache
