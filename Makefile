SWIFT ?= swift
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
SCHEMA ?= schemas/page.v1.2.json
RAW_DOC ?= bench/stage_ab/*.jsonl
RUN_SCHEMA ?= schemas/run.v1.2.json
VLM_REQ_SCHEMA ?= schemas/vlm/request.v1.json
VLM_RESP_SCHEMA ?= schemas/vlm/response.v1.json
VLM_DIR ?= runs/vlm

.PHONY: build release clean smoke bench ensure-release schema-check assemble schema-check-eob schema-check-run schema-check-vlm schema-check-all test-pipeline

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

assemble:
	python scripts/assemble_eob.py --pages bench/raw_doc --mappings config/payers --out out/eob --schema schemas/DentalEOBSchema.v1.2.json --ajv "npx ajv-cli"

schema-check-eob:
	@for f in out/eob/*.json; do \
		echo "Validating $$f..."; \
		npx ajv-cli validate -s schemas/DentalEOBSchema.v1.2.json -d "$$f" --spec=draft2020 --strict=false 2>&1 | grep -E "(valid|error)" || true; \
	done

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
	@python3 scripts/assemble_eob.py --pages bench/smoke --mappings config/payers --out out/eob_smoke --schema schemas/DentalEOBSchema.v1.2.json --ajv "$(AJV)" 2>&1 | grep -E "(Wrote|valid)" | head -3
	@echo "4. Validate EOB schema"
	@for f in out/eob_smoke/*.json; do \
		[ -f "$$f" ] && $(AJV) validate -s schemas/DentalEOBSchema.v1.2.json -d "$$f" --spec=draft2020 --strict=false 2>&1 | grep -E "(valid|error)" | head -1; \
	done
	@echo "=== Pipeline test complete ==="
