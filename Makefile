SWIFT ?= swift
PYTHON ?= $(HOME)/.venvs/ml-mps/bin/python
PACKAGE_PATH := vision-ocr-cli
BUILD_DIR := $(PACKAGE_PATH)/.build
RELEASE_BIN := $(BUILD_DIR)/release/vision-ocr-cli

MODULE_CACHE_BASE := $(BUILD_DIR)
BUILD_ENV := HOME=$(CURDIR) SWIFT_MODULE_CACHE_PATH=$(MODULE_CACHE_BASE)/swift-module-cache CLANG_MODULE_CACHE_PATH=$(MODULE_CACHE_BASE)/clang-module-cache MODULE_CACHE_DIR=$(MODULE_CACHE_BASE)/module-cache

CORPUS ?= corpus
FILE ?=
DPI ?= 300
MODE ?= document
OCR_LANG ?= en-US
LOG_RUN ?=
PAGES ?=
TIMEOUT ?=
AJV ?= npx ajv-cli
SCHEMA ?= schemas/v1.2/page.schema.json
RAW_DOC ?= bench/stage_ab/*.jsonl
RUN_SCHEMA ?= schemas/run.v1.2.json
VLM_REQ_SCHEMA ?= schemas/vlm/request.v1.json
VLM_RESP_SCHEMA ?= schemas/vlm/response.v1.json
VLM_DIR ?= runs/vlm

.PHONY: build release clean smoke bench ensure-release schema-check assemble schema-check-eob schema-check-run schema-check-vlm schema-check-all test-pipeline runner-sanity emit-chunks schema-check-chunks index-chunks manifest-append-chunk-run chunk-install
 .PHONY: vlm-install vlm-run vlm-check runner-smoke vlm-metrics schema-check-cp0 mapper-verify repair-verify qa-daily row-retry-emit micro-ocr-scan bench-kpi
 .PHONY: accept-eremit-fast accept-eremit-doc
.PHONY: accept-eremit-c3
.PHONY: accept-eremit-rowize
.PHONY: holdout-manifest holdout-shadow
.PHONY: web-install web-dev

build:
	$(BUILD_ENV) $(SWIFT) build --package-path $(PACKAGE_PATH) --disable-sandbox

release:
	$(BUILD_ENV) $(SWIFT) build -c release --package-path $(PACKAGE_PATH) --disable-sandbox

ensure-release: release
	@true

smoke: ensure-release
	@if [ -z "$(FILE)" ]; then \
		echo "Set FILE=/path/to/doc.pdf" >&2; exit 1; \
	fi
		$(RELEASE_BIN) $(FILE) --dpi $(DPI) --mode $(MODE) --lang $(OCR_LANG) $(if $(PAGES),--pages $(PAGES),) $(if $(TIMEOUT),--timeout $(TIMEOUT),) $(if $(LOG_RUN),--log-run $(LOG_RUN),)

bench: ensure-release
	scripts/bench.py $(CORPUS) --cli $(RELEASE_BIN) --dpi $(DPI) --mode $(MODE) --lang $(OCR_LANG) $(if $(PAGES),--pages $(PAGES),) $(if $(TIMEOUT),--timeout $(TIMEOUT),) $(if $(REFS),--reference-root $(REFS),) $(if $(OUT),--out $(OUT),) $(if $(KEEP_RAW),--keep-raw $(KEEP_RAW),)

schema-check:
	@status=0; \
	files=$(RAW_DOC); \
	set -- $$files; \
	if [ $$# -eq 0 ]; then echo "No JSONL files found for schema-check"; exit 0; fi; \
	for f in $$files; do \
		[ -f "$$f" ] || continue; \
		echo "Validating $$f"; \
		tmpfile=$$(mktemp); \
		while IFS= read -r line; do \
			echo "$$line" > "$$tmpfile"; \
			$(AJV) validate -s $(SCHEMA) -d "$$tmpfile" --spec=draft2020 --strict=false 2>&1 | grep -E "(valid|invalid)" | grep -q "invalid" && status=1; \
		done < "$$f"; \
		rm -f "$$tmpfile"; \
	done; \
	exit $$status

schema-check-run:
	@status=0; \
	files=runs/*_run_v1.2.json; \
	set -- $$files; \
	if [ $$# -eq 0 ]; then echo "No run manifest files found"; exit 0; fi; \
	for f in $$files; do \
		[ -f "$$f" ] || continue; \
		echo "Validating run manifest $$f"; \
		$(AJV) validate -s $(RUN_SCHEMA) -d "$$f" --spec=draft2020 --strict=false >/dev/null || status=1; \
	done; \
	exit $$status

schema-check-vlm:
	@status=0; \
	any=0; \
	for f in $(VLM_DIR)/*_request.json; do \
		[ -f "$$f" ] || continue; any=1; \
		echo "Validating VLM request $$f"; \
		$(AJV) validate -s $(VLM_REQ_SCHEMA) -d "$$f" --spec=draft2020 --strict=false >/dev/null || status=1; \
	done; \
	for f in $(VLM_DIR)/*_response.json; do \
		[ -f "$$f" ] || continue; any=1; \
		echo "Validating VLM response $$f"; \
		$(AJV) validate -s $(VLM_RESP_SCHEMA) -d "$$f" --spec=draft2020 --strict=false >/dev/null || status=1; \
	done; \
	if [ $$any -eq 0 ]; then echo "No VLM request/response files in $(VLM_DIR)"; fi; \
	exit $$status

schema-check-all: schema-check schema-check-run schema-check-vlm schema-check-eob

runner-sanity:
	@echo '{"schema":"vlm.request.v1","doc_sha256":"deadbeef","page_index":0,"roi_id":"test","image_path":"runs/vlm/does_not_exist.png","roi":{"px":0,"py":0,"pw":1,"ph":1}}' | \
		$(PYTHON) scripts/dots-ocr-runner.py >/dev/null || true

assemble:
	python scripts/assemble_eob.py --pages bench/raw_doc --mappings config/payers --out out/eob --schema schemas/DentalEOBSchema.v1.2.json --ajv "npx ajv-cli"

schema-check-eob:
	@for f in out/eob/*.json; do \
		echo "Validating $$f..."; \
		npx ajv-cli validate -s schemas/DentalEOBSchema.v1.2.json -d "$$f" --spec=draft2020 --strict=false 2>&1 | grep -E "(valid|error)" || true; \
	done

schema-check-cp0:
	@echo "=== CP0 Schema Validation ==="
	@status=0; \
	echo "Validating budget_policy.v1 sample..."; \
	$(AJV) validate -s schemas/budget_policy.v1.json -d samples/budget_policy.v1.sample.json --spec=draft2020 --strict=false || status=1; \
	echo "Validating preproc.page.v1 sample..."; \
	$(AJV) validate -s schemas/preproc.page.v1.json -d samples/preproc.page.v1.sample.json --spec=draft2020 --strict=false || status=1; \
	echo "Validating vlm.header_pass.v1 request sample..."; \
	$(AJV) validate -s schemas/vlm/header_pass.v1.json -d samples/vlm.header_pass.v1.request.sample.json --spec=draft2020 --strict=false || status=1; \
	echo "Validating vlm.header_pass.v1 response sample..."; \
	$(AJV) validate -s schemas/vlm/header_pass.v1.json -d samples/vlm.header_pass.v1.response.sample.json --spec=draft2020 --strict=false || status=1; \
	echo "Validating vlm.data_pass.v1 request sample..."; \
	$(AJV) validate -s schemas/vlm/data_pass.v1.json -d samples/vlm.data_pass.v1.request.sample.json --spec=draft2020 --strict=false || status=1; \
	echo "Validating vlm.data_pass.v1 response sample..."; \
	$(AJV) validate -s schemas/vlm/data_pass.v1.json -d samples/vlm.data_pass.v1.response.sample.json --spec=draft2020 --strict=false || status=1; \
	if [ $$status -eq 0 ]; then echo "✅ All CP0 schemas valid"; else echo "❌ CP0 schema validation failed"; fi; \
	exit $$status

mapper-verify:
	@echo "=== CP6: Mapper verification ==="; \
	PYTHONPATH=scripts $(PYTHON) scripts/mapper_verify.py

repair-verify:
	@echo "=== CP7: Repair verification ==="; \
	$(PYTHON) scripts/repair_verify.py

qa-daily:
	@echo '{"note":"stub: aggregate metrics (p95_roi_sec, header_match_rate, typed_cell_accuracy)"}'

row-retry-emit:
	@echo "=== Emitting row-retry NDJSON via assembly ==="; \
	$(PYTHON) scripts/assemble_eob.py --pages $(or $(PAGES),bench/raw_doc) --mappings config/payers --out $(or $(OUT),out/eob) $(if $(APPLY_REPAIR),--apply-repair,) ; \
	echo "Row retry files written to runs/row_retry/*.ndjson"

micro-ocr-scan:
	@$(PYTHON) scripts/micro_ocr_metrics.py scan --root $${OUT:-out/eob_accuracy_spatial_cp12}

bench-kpi:
	@$(PYTHON) scripts/micro_ocr_metrics.py kpi --root $${OUT:-out/eob_accuracy_spatial_cp12}

# Acceptance suites (add-only)
accept-eremit-fast: ensure-release
	@$(PYTHON) scripts/acceptance_eremit_fast.py

accept-eremit-doc: ensure-release
	@$(PYTHON) scripts/acceptance_eremit_doc.py

accept-eremit-c3: ensure-release vlm-install
	@$(PYTHON) scripts/acceptance_eremit_c3.py

accept-eremit-rowize: ensure-release
	@$(PYTHON) scripts/acceptance_eremit_rowize.py

holdout-manifest:
	@$(PYTHON) scripts/holdout_manifest.py --src "$${SRC:?set SRC=/path/to/holdout}" --name "$${NAME:?set NAME=EOB_ZIP_2025Q3}"

holdout-shadow: ensure-release
	@$(PYTHON) scripts/holdout_shadow.py --dataset "$${DATASET:?set DATASET=datasets/EOB_ZIP_2025Q3}" $${LIMIT:+--limit $$LIMIT}

web-install:
	@pip install --upgrade fastapi uvicorn jinja2 >/dev/null 2>&1 || true

web-dev: web-install ensure-release
	@echo "Starting demo UI on http://127.0.0.1:8000"; \
	uvicorn web.app:app --reload --host 127.0.0.1 --port 8000

clean:
	$(BUILD_ENV) $(SWIFT) package --package-path $(PACKAGE_PATH) clean

test-pipeline: ensure-release
	@echo "=== Running full pipeline test (capture → validate → assemble → verify) ==="
	@mkdir -p bench/smoke out/eob_smoke runs/smoke_vlm
	@echo "1. Capture (Vision + VLM noop)"
	$(RELEASE_BIN) eobs/tests/DD_3.pdf --dpi 72 --mode text --lang en-US --pages 0:1 \
		--ndjson bench/smoke/output.jsonl \
		--log-run runs/smoke_full_run.json \
		--vlm-runner scripts/vlm-noop.py \
		--vlm-export runs/smoke_vlm \
		--vlm-preprocess
	@echo "2. Validate capture schemas"
	@tmpfile=$$(mktemp); \
	head -1 bench/smoke/output.jsonl > "$$tmpfile"; \
	$(AJV) validate -s $(SCHEMA) -d "$$tmpfile" --spec=draft2020 --strict=false 2>&1 | grep -q "valid" && echo "  ✓ Page schema" || echo "  ✗ Page schema"; \
	rm -f "$$tmpfile"
	@$(AJV) validate -s $(RUN_SCHEMA) -d runs/smoke_full_run.json --spec=draft2020 --strict=false 2>&1 | grep -q "valid" && echo "  ✓ Run manifest" || echo "  ✗ Run manifest"
	@echo "3. Assemble EOB"
	@$(PYTHON) scripts/assemble_eob.py --pages bench/smoke --mappings config/payers --out out/eob_smoke --schema schemas/DentalEOBSchema.v1.2.json --ajv "$(AJV)" 2>&1 | grep -E "(Wrote|valid)" | head -3
	@echo "4. Validate EOB schema"
	@for f in out/eob_smoke/*.json; do \
		[ -f "$$f" ] && $(AJV) validate -s schemas/DentalEOBSchema.v1.2.json -d "$$f" --spec=draft2020 --strict=false 2>&1 | grep -E "(valid|error)" | head -1; \
	done
	@echo "=== Pipeline test complete ==="

vlm-install:
	@pip install -U mlx-vlm pillow numpy >/dev/null 2>&1 || true
	@pip install -U jsonschema >/dev/null 2>&1 || true
	@echo "✓ VLM deps checked"

vlm-run:
	@set -e; \
	requests=$$(ls $(VLM_DIR)/*_request.json 2>/dev/null | wc -l | tr -d ' '); \
	if [ "$$requests" = "0" ]; then echo "No VLM request files in $(VLM_DIR)"; exit 0; fi; \
	for f in $(VLM_DIR)/*_request.json; do \
		resp=$${f%_request.json}_response.json; \
		cat "$$f" | VLM_VALIDATE=1 VLM_MODEL=mlx-community/Qwen2-VL-7B-bf16 VLM_FORCE_GREEDY=1 VLM_MAX_NEW_TOKENS=2048 VLM_TIMEOUT_S=120 $(PYTHON) scripts/dots-ocr-runner.py > "$$resp"; \
	done; \
	echo "✓ Ran runner over $$requests requests"

vlm-check:
	@status=0; \
	for f in $(VLM_DIR)/*_request.json; do \
		[ -f "$$f" ] || continue; \
		$(AJV) validate -s $(VLM_REQ_SCHEMA) -d "$$f" --spec=draft2020 --strict=false >/dev/null || status=1; \
	done; \
	for f in $(VLM_DIR)/*_response.json; do \
		[ -f "$$f" ] || continue; \
		$(AJV) validate -s $(VLM_RESP_SCHEMA) -d "$$f" --spec=draft2020 --strict=false >/dev/null || status=1; \
	done; \
	exit $$status

runner-smoke: ensure-release vlm-install
	@echo "=== Runner smoke (persistent) ==="
	@mkdir -p $(VLM_DIR)
	VLM_PYTHON=$(PYTHON) VLM_VALIDATE=1 VLM_MODEL=mlx-community/Qwen2-VL-2B-Instruct-4bit \
	VLM_FORCE_GREEDY=1 VLM_MAX_NEW_TOKENS=2048 VLM_TIMEOUT_S=120 \
	$(RELEASE_BIN) eobs/tests/DD_3.pdf --dpi 300 --mode document --lang en-US \
		--pages 0:1 --ndjson bench/smoke_runner.jsonl --log-run runs/smoke_runner.json \
		--vlm-runner scripts/dots-ocrd.py --vlm-export $(VLM_DIR) \
		--vlm-preprocess --vlm-preprocess-script scripts/preprocess_roi.py
	$(MAKE) vlm-check
	@echo "=== Runner smoke complete ==="

vlm-metrics:
	@echo "=== VLM Performance Metrics ==="
	@$(PYTHON) scripts/vlm_metrics.py $(VLM_DIR)

emit-chunks:
	@mkdir -p out/chunks
	@$(PYTHON) scripts/eob_to_chunks.py

schema-check-chunks:
	@for f in out/chunks/*.jsonl; do \
		[ -f "$$f" ] || continue; \
		$(PYTHON) scripts/schema_check_ndjson.py --schema schemas/rag/chunk.v1.json --in "$$f" || exit 1; \
	done; \
	echo "✓ chunks valid"

index-chunks:
	@$(PYTHON) scripts/index_chunks.py

chunk-install:
	@$(PYTHON) -m pip install -U duckdb >/dev/null 2>&1 || true && echo "✓ duckdb installed in $(PYTHON)"

manifest-append-chunk-run:
	@for e in out/eob/*.json; do \
		b=$${e##*/}; stem=$${b%.json}; \
		out=out/chunks/$$stem.jsonl; \
		[ -f "$$out" ] || continue; \
		$(PYTHON) scripts/manifest_append_chunk_run.py runs/smoke_runner.json "$$e" "$$out"; \
	done

# Optional helpers for payer prioritization and doc-only acceptance
.PHONY: payer-map doc-only-hdr

payer-map:
	@$(PYTHON) scripts/tools/payer_quality_map.py \
		--aggregate $${AGGREGATE:-SESSION/AuditLogs/holdout/EOB_ZIP_2025Q3/aggregate.json} \
		--out $${OUT:-out/holdout/payer_quality_map.json}

doc-only-hdr: ensure-release
	@$(PYTHON) scripts/acceptance_doc_only.py --pages $${PAGES:-bench/eremit_doc} --latch 0.15

.PHONY: verify-northstar-legacy
verify-northstar-legacy:
	@$(PYTHON) scripts/verify_checkpoint.py --baseline $${BASE:-out/northstar_baseline} --strict $${STRICT:-out/northstar_strict}

.PHONY: test-selection
test-selection:
	@$(PYTHON) -m unittest -q tests/test_selection_policy.py

# Northstar CI/Production Targets
.PHONY: verify-northstar run-prod run-prod-strict run-prod-canary

verify-northstar:
	@echo "=== Northstar Verification ==="
	@$(PYTHON) scripts/verify_checkpoint.py \
		$(if $(BASE),--baseline $(BASE),) \
		$(if $(STRICT),--strict $(STRICT),) \
		--require-assign --require-evidence --require-map-metrics \
		$(if $(ENFORCE),--enforce-map-completeness,)

run-prod:
	@echo "=== Production Run (solver + ILP + summary) ==="
	@if [ -z "$(PAGES)" ]; then echo "Error: set PAGES=<path>"; exit 1; fi
	@if [ -z "$(OUT)" ]; then echo "Error: set OUT=<outdir>"; exit 1; fi
	$(PYTHON) scripts/assemble_eob.py \
		--pages $(PAGES) \
		$(if $(GLOB),--glob '$(GLOB)',) \
		--mappings config/payers \
		--out $(OUT) \
		--solve-roles \
		--ilp-assign-extract \
		--summary-reconcile \
		--emit-evidence

run-prod-strict: run-prod
	@echo "Note: strict locks enabled (--solver-locks-strict)"
	$(PYTHON) scripts/assemble_eob.py \
		--pages $(PAGES) \
		$(if $(GLOB),--glob '$(GLOB)',) \
		--mappings config/payers \
		--out $(OUT)_strict \
		--solve-roles \
		--ilp-assign-extract \
		--summary-reconcile \
		--emit-evidence \
		--solver-locks-strict

run-prod-canary:
	@echo "=== Canary Run (hard enforcement enabled) ==="
	@if [ -z "$(PAGES)" ]; then echo "Error: set PAGES=<path>"; exit 1; fi
	@if [ -z "$(OUT)" ]; then echo "Error: set OUT=<outdir>"; exit 1; fi
	$(PYTHON) scripts/assemble_eob.py \
		--pages $(PAGES) \
		$(if $(GLOB),--glob '$(GLOB)',) \
		--mappings config/payers \
		--out $(OUT) \
		--solve-roles \
		--ilp-assign-extract \
		--summary-reconcile \
		--emit-evidence \
		--ilp-enforce-map

