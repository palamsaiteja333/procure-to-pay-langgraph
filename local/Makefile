# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Local development harness for aria-p2p-agent. See local/README.md.
#
#   cp local/env.example local/.env      # fill in AWS creds (real Bedrock)
#   make -C local erpnext                 # start local ERPNext (compose)
#   make -C local up                      # emulators + shims + agents + backend
#   make -C local ui                      # SPA (Vite) in another terminal
#
# Prefer the checkout's venv, with the existing home-directory fallback.

REPO := $(shell cd .. && pwd)
PY := $(if $(wildcard ../.venv/bin/python),$(REPO)/.venv/bin/python,$(HOME)/.venv/bin/python)
ERP_COMPOSE := $(REPO)/infra/docker/docker-compose.local-test.yaml
DYNAMODB_COMPOSE := $(REPO)/local/docker-compose.dynamodb.yaml
DOCKER ?= docker
# Include Docker Desktop's macOS CLI and credential helper locations.
COMPOSE = PATH="$(PATH):$(HOME)/.docker/bin:/Applications/Docker.app/Contents/Resources/bin" "$(DOCKER)" compose

# Load local/.env if present so creds/overrides reach the child processes.
ifneq (,$(wildcard .env))
include .env
export
endif

.PHONY: help env erpnext erpnext-down dynamodb dynamodb-stop up seed ui clean

help:
	@echo "aria-p2p-agent local harness targets:"
	@echo "  make -C local erpnext       start local ERPNext (docker compose)"
	@echo "  make -C local dynamodb      start persistent DynamoDB Local (docker compose)"
	@echo "  make -C local dynamodb-stop stop DynamoDB Local and keep its data"
	@echo "  make -C local up            DynamoDB Local + emulators + shims + agents + backend"
	@echo "  make -C local ui            run the SPA (Vite dev server) with local config"
	@echo "  make -C local seed          seed local ERPNext with demo data"
	@echo "  make -C local erpnext-down  stop + remove ERPNext containers/volumes"
	@echo "  make -C local clean         remove generated local artifacts"

env:
	@test -f .env || (echo "no local/.env — run: cp local/env.example local/.env" && exit 1)

erpnext:
	$(COMPOSE) -f "$(ERP_COMPOSE)" up -d
	@echo "ERPNext starting on http://localhost:8080 — complete the Frappe setup wizard once."

erpnext-down:
	$(COMPOSE) -f "$(ERP_COMPOSE)" down -v

dynamodb:
	$(COMPOSE) -f "$(DYNAMODB_COMPOSE)" up -d

dynamodb-stop:
	$(COMPOSE) -f "$(DYNAMODB_COMPOSE)" stop

up: env dynamodb
	"$(PY)" supervisor.py

seed: env
	"$(PY)" scripts/seed_local.py

# Serve the SPA with the local runtime config injected (guest mode + local agent
# proxy). Copies local-config.js into the Vite public dir (gitignored there).
ui:
	cp config/local-config.js "$(REPO)/frontend/public/local-config.js"
	cd "$(REPO)/frontend" && ARIA_LOCAL_PROXY=1 npm run dev

clean:
	rm -f $(REPO)/frontend/public/local-config.js
	@echo "removed injected local-config.js"
