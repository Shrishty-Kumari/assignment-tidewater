SHELL := /bin/bash
.DEFAULT_GOAL := help
CLUSTER    ?= settle
VERSION    ?= 1.9.0
ACT_IMAGE  ?= settle-ci-runner:local
# the runner image is built natively, so act must run it with the host architecture
ACT_ARCH   ?= linux/$(shell uname -m | sed -e 's/x86_64/amd64/' -e 's/aarch64/arm64/')
PY         ?= $(shell [ -x .venv/bin/python ] && echo $(CURDIR)/.venv/bin/python || echo python3)
export PYTHON = $(PY)
WORKTREE    = /tmp/settle-release-$(VERSION)
REF        ?= v$(VERSION)

define prepare_worktree
	@git rev-parse -q --verify "$(REF)^{commit}" >/dev/null || { echo "unknown ref $(REF)"; exit 1; }
	@rm -rf $(WORKTREE); git worktree prune; git worktree add -q --detach $(WORKTREE) $(REF)
	@rm -rf $(WORKTREE)/.github $(WORKTREE)/scripts/ci
	@cp -R .github $(WORKTREE)/ && cp -R scripts/ci $(WORKTREE)/scripts/ && cp scripts/check_db_budget.py $(WORKTREE)/scripts/
	@echo "release $(VERSION): source $(REF) ($$(git rev-parse --short "$(REF)^{commit}")), tooling $$(git rev-parse --short HEAD)"
endef

help: ## list targets
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

up: ## create the k3d cluster with ingress, monitoring, postgres, redis, bank mock
	local/bootstrap.sh

down: ## delete the cluster
	k3d cluster delete $(CLUSTER)

# --- delivery ----------------------------------------------------------------
ci-runner: ## build the local act runner image (catthehacker/ubuntu + kubectl, trivy, cosign)
	@docker image inspect $(ACT_IMAGE) >/dev/null 2>&1 || docker build -q -f local/ci-runner.Dockerfile -t $(ACT_IMAGE) local/ >/dev/null
	@mkdir -p $(HOME)/.cache/settle-trivy

deploy: ci-runner ## release VERSION (git tag vVERSION) through the GitHub Actions workflow, run locally with act
	$(prepare_worktree)
	@# the cluster credential goes to act through a 0600 secret file, never on
	@# the command line (argv is visible to every local user via ps)
	@umask 077; { printf 'KUBECONFIG_B64=%s\n' "$$(k3d kubeconfig get $(CLUSTER) | sed 's#https://0.0.0.0:6550#https://host.docker.internal:6550#' | base64 | tr -d '\n')"; \
	  printf 'COSIGN_KEY_B64=%s\n' "$$(base64 < local/.secrets/cosign.key | tr -d '\n')"; \
	  printf 'COSIGN_PASSWORD=%s\n' "$$(cat local/.secrets/cosign.password)"; } > $(WORKTREE)/.act-secrets
	cd $(WORKTREE) && act workflow_dispatch -W .github/workflows/deploy.yml \
	  -P ubuntu-latest=$(ACT_IMAGE) --pull=false --container-architecture $(ACT_ARCH) --container-daemon-socket /var/run/docker.sock \
	  --container-options "-v $(HOME)/.cache/settle-trivy:/root/.cache/trivy --add-host=host.docker.internal:host-gateway" \
	  --input version=$(VERSION) --input git_sha=$$(git rev-parse "$(REF)^{commit}") \
	  --secret-file .act-secrets \
	  --env REGISTRY_PUSH=localhost:5001 --env REGISTRY_PULL=settle-registry:5000; \
	  rc=$$?; rm -f .act-secrets; exit $$rc

release: ## same release steps without act (fallback): build, scan, deploy, verify, auto-rollback
	$(prepare_worktree)
	cd $(WORKTREE) && rm -f .release/outputs && scripts/ci/build.sh $(VERSION) $$(git rev-parse "$(REF)^{commit}") \
	  && trivy image --quiet --image-src docker --scanners vuln --ignore-unfixed --severity HIGH,CRITICAL --exit-code 1 \
	       "$$(grep '^scan_ref=' .release/outputs | cut -d= -f2)" \
	  && trivy image --quiet --image-src docker --format cyclonedx --output .release/sbom.cdx.json "$$(grep '^scan_ref=' .release/outputs | cut -d= -f2)" \
	  && COSIGN_KEY_B64="$$(base64 < $(CURDIR)/local/.secrets/cosign.key | tr -d '\n')" COSIGN_PASSWORD="$$(cat $(CURDIR)/local/.secrets/cosign.password)" \
	       scripts/ci/sign.sh "$$(grep '^scan_ref=' .release/outputs | cut -d= -f2)" .release/sbom.cdx.json \
	  && scripts/ci/deploy.sh "$$(grep '^image_ref=' .release/outputs | cut -d= -f2)" $(VERSION) \
	  && { python3 scripts/ci/verify_release.py --version $(VERSION) \
	       || { scripts/ci/rollback.sh "$$(grep '^previous_image=' .release/outputs | cut -d= -f2)" "verification of $(VERSION) failed"; exit 1; }; }

images: ## build settle-api:1.9.0 and :1.9.1-rc from their tags and save them to images/*.tar.gz
	@for v in 1.9.0 1.9.1-rc; do \
	  wt=/tmp/settle-image-$$v; rm -rf $$wt; git worktree prune; git worktree add -q --detach $$wt v$$v; \
	  echo "building settle-api:$$v from v$$v ($$(git rev-parse --short "v$$v^{commit}"))"; \
	  docker build -q -f $$wt/app/Dockerfile --build-arg VERSION=$$v --build-arg GIT_SHA=$$(git rev-parse "v$$v^{commit}") -t settle-api:$$v $$wt >/dev/null || exit 1; \
	  docker save settle-api:$$v | gzip > images/settle-api-$$v.tar.gz; \
	  rm -rf $$wt; \
	done; git worktree prune; ls -lh images/*.tar.gz
	@echo "load with: docker load < images/settle-api-1.9.0.tar.gz"

rollback: ## manual rollback to the previous ReplicaSet of api and worker
	kubectl -n settle rollout undo deployment/settle-api
	kubectl -n settle rollout undo deployment/settle-worker
	kubectl -n settle rollout status deployment/settle-api --timeout=240s
	kubectl -n settle rollout status deployment/settle-worker --timeout=240s

status: ## what is running
	@kubectl -n settle get deploy -o custom-columns='NAME:.metadata.name,VERSION:.metadata.annotations.settle\.paylane\.io/version,READY:.status.readyReplicas,IMAGE:.spec.template.spec.containers[0].image'
	@kubectl -n settle get pods -o wide

# --- checks --------------------------------------------------------------------
test: ## unit tests (+ integration if PG_TEST_URL is set)
	cd app && $(PY) -m pytest -q

lint: ## ruff, migration lint, DB connection budget
	cd app && ../.venv/bin/ruff check .
	$(PY) scripts/lint_migrations.py
	kubectl kustomize deploy/k8s/overlays/local | $(PY) scripts/check_db_budget.py -

tf-check: ## terraform fmt/validate + tflint + checkov for staging and prod
	infra/terraform/check.sh

tf-plan-localstack: ## terraform plan of staging and prod against LocalStack (nothing applied)
	infra/terraform/localstack-plan.sh staging
	infra/terraform/localstack-plan.sh prod

# --- observability / chaos -------------------------------------------------------
grafana: ## open Grafana on http://localhost:3000
	kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80
grafana-password:
	@kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d; echo
prometheus: ## open Prometheus on http://localhost:9090
	kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090
alertmanager: ## open Alertmanager on http://localhost:9093
	kubectl -n monitoring port-forward svc/kube-prometheus-stack-alertmanager 9093:9093
alerts-log: ## follow alerts as delivered by Alertmanager
	kubectl -n monitoring logs -f deploy/alert-sink

alert-demo: ## make the bank pay one reference twice -> SettleDuplicatePayout fires within ~1-2 min
	scripts/alert-demo.sh

admission-demo: ## show that unsigned or tag-referenced images are rejected at admission
	scripts/admission-demo.sh

chaos-db: ## add 3 s latency to every Postgres packet (toxiproxy)
	scripts/chaos-db.sh on
chaos-db-off: ## remove the latency
	scripts/chaos-db.sh off

.PHONY: help up down ci-runner deploy release images rollback status test lint tf-check tf-plan-localstack grafana grafana-password prometheus alertmanager alerts-log alert-demo admission-demo chaos-db chaos-db-off
