#!/usr/bin/env python3
"""Assemble Dental EOB objects from NativeOCR page payloads."""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml
from dateutil import parser as dateparser

Amount = Optional[str]

# -----------------------------------------------------------------------------
# Constants & regex
# -----------------------------------------------------------------------------

D_CODE_RE = re.compile(r"^D\d{4}[A-Z]?$", re.IGNORECASE)
TOOTH_RE = re.compile(r"^(?:[1-9]|[1-2][0-9]|3[0-2]|[A-T])$")
SURFACE_RE = re.compile(r"^[A-Z]{1,5}$")

THOUSANDS_RE = re.compile(r"(?<=\d)[,'](?=\d{3}(?:\D|$))")
MULTIDOT_RE = re.compile(r"\.(?=.*\.)")
CR_RE = re.compile(r"\s*(CR|DR)$", re.IGNORECASE)
PARENS_RE = re.compile(r"^\((.+)\)$")


@dataclass
class TableTemplate:
    payer: str
    name_regex: Optional[re.Pattern]
    column_map: Dict[str, Dict[str, Any]]
    header_aliases: Dict[str, str]
    header_row: int
    kv_left: Dict[str, Dict[str, Any]]
    kv_right: Dict[str, Dict[str, Any]]


# -----------------------------------------------------------------------------
# Sanitizers & parsers
# -----------------------------------------------------------------------------


def sanitize_amount(raw: Any) -> Amount:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    text = text.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"(?i)(?:USD|US\$)", "", text)
    text = text.replace("$", "").strip()

    parens = PARENS_RE.match(text)
    if parens:
        text = f"-{parens.group(1)}"

    m = CR_RE.search(text)
    if m:
        flag = m.group(1).upper()
        text = text[: m.start()].strip()
        if flag == "CR" and not text.startswith("-"):
            text = f"-{text}"

    text = THOUSANDS_RE.sub("", text)
    text = MULTIDOT_RE.sub("", text)
    if "." not in text:
        text = f"{text}.00"
    elif text.endswith("."):
        text = f"{text}00"

    digits = sum(ch.isdigit() for ch in text)
    if digits and digits / max(1, len(text)) >= 0.7:
        text = text.replace("O", "0").replace("o", "0")

    text = text.replace("--", "-")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    value = value.quantize(Decimal("0.01"))
    if abs(value) >= Decimal("1000000000"):
        return None
    return f"{value:.2f}"


def parse_date(text: str, patterns: Optional[List[str]] = None) -> Optional[str]:
    text = text.strip()
    if not text:
        return None
    if patterns:
        for fmt in patterns:
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
            except Exception:
                continue
    try:
        dt = dateparser.parse(text, dayfirst=False, yearfirst=False)
    except (ValueError, OverflowError):
        return None
    return dt.strftime("%Y-%m-%d")


# -----------------------------------------------------------------------------
# Mapping loader
# -----------------------------------------------------------------------------


def load_templates(mapping_dir: Path) -> List[TableTemplate]:
    templates: List[TableTemplate] = []
    for path in sorted(mapping_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        detect = data.get("detect", {})
        regex = detect.get("name_regex")
        name_regex = re.compile(regex) if regex else None
        tables_cfg = data.get("tables", [{}])
        primary = tables_cfg[0]
        column_map = {}
        for key, cfg in primary.get("column_map", {}).items():
            column_map[normalize_header(key)] = cfg
        header_aliases = {}
        for alias, target in primary.get("header_aliases", {}).items():
            header_aliases[normalize_header(alias)] = target
        template = TableTemplate(
            payer=data.get("payer", path.stem),
            name_regex=name_regex,
            column_map=column_map,
            header_aliases=header_aliases,
            header_row=primary.get("header_row", 0),
            kv_left=data.get("kv", {}).get("header_left", {}),
            kv_right=data.get("kv", {}).get("header_right", {})
        )
        templates.append(template)
    if not templates:
        raise RuntimeError(f"No payer templates found in {mapping_dir}")
    return templates


# -----------------------------------------------------------------------------
# Page grouping
# -----------------------------------------------------------------------------


def group_pages(files: Iterable[Path]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for path in files:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                page = json.loads(line)
                doc_key = str(page.get("source_path") or path.stem)
                grouped[doc_key].append(page)
    return grouped


# -----------------------------------------------------------------------------
# Extraction helpers
# -----------------------------------------------------------------------------


def extract_header_text(page: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    header = page.get("regions", {}).get("header", {})
    for side in ("left", "right"):
        region = header.get(side)
        if not region:
            continue
        result[side] = {
            "text": region.get("text", ""),
            "box": (region.get("box") or {}).get("pixel")
        }
    return result


def apply_header_regex(text: str, pattern: Optional[str]) -> Optional[str]:
    if not pattern:
        return None
    match = re.search(pattern, text)
    if not match:
        return None
    return match.group(1).strip()


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def build_table_matrix(table: Dict[str, Any]) -> Dict[Tuple[int, int], Dict[str, Any]]:
    matrix: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for cell in table.get("cells", []):
        matrix[(cell.get("r"), cell.get("c"))] = cell
    return matrix


def transform_value(raw: str, cfg: Dict[str, Any]) -> Optional[str]:
    typ = cfg.get("type")
    if typ == "money":
        return sanitize_amount(raw)
    if typ == "date":
        return parse_date(raw, cfg.get("patterns"))
    if typ == "dcode":
        code = raw.strip().upper()
        return code if D_CODE_RE.match(code) else code
    if typ == "tooth":
        cleaned = raw.strip().upper()
        return cleaned if TOOTH_RE.match(cleaned) else cleaned
    if typ == "tooth_surface":
        cleaned = raw.strip().upper()
        return cleaned if SURFACE_RE.match(cleaned) else cleaned
    return raw.strip()


def build_provenance(ptr: str, source: str, confidence: float, page_index: int, box: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    prov = {
        "ptr": ptr,
        "source": source,
        "confidence": float(confidence),
        "page_index": page_index,
    }
    if box and all(k in box for k in ("x", "y", "w", "h")):
        prov["box_px"] = {k: int(box[k]) for k in ("x", "y", "w", "h")}
    return prov


def choose_template(payer_name: str, templates: List[TableTemplate]) -> TableTemplate:
    if payer_name:
        for tpl in templates:
            if tpl.name_regex and tpl.name_regex.search(payer_name):
                return tpl
        for tpl in templates:
            if tpl.payer.lower() == payer_name.lower():
                return tpl
    return templates[0]


# -----------------------------------------------------------------------------
# Service extraction
# -----------------------------------------------------------------------------

def gather_services(
    pages: List[Dict[str, Any]],
    template: TableTemplate,
    provenance: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    services: List[Dict[str, Any]] = []
    for page in pages:
        page_index = page.get("page_index", 0)
        for table in page.get("tables", []) or []:
            matrix = build_table_matrix(table)
            header_row = template.header_row
            cols = table.get("cols", 0)
            rows = table.get("rows", 0)
            column_configs: Dict[int, Dict[str, Any]] = {}
            for col in range(cols):
                cell = matrix.get((header_row, col), {})
                header_text = str(cell.get("text", ""))
                norm = normalize_header(header_text)
                cfg = template.column_map.get(norm)
                if not cfg:
                    alias = template.header_aliases.get(norm)
                    if alias:
                        cfg = template.column_map.get(normalize_header(alias)) or template.column_map.get(alias)
                if cfg:
                    column_configs[col] = cfg
            for row in range(header_row + 1, rows):
                entry: Dict[str, Any] = {}
                row_prov: List[Dict[str, Any]] = []
                for col, cfg in column_configs.items():
                    cell = matrix.get((row, col))
                    if not cell:
                        continue
                    raw = str(cell.get("text", "")).strip()
                    if not raw:
                        continue
                    value = transform_value(raw, cfg)
                    if value is None:
                        continue
                    field = cfg["field"]
                    entry.setdefault(field, value)
                    row_prov.append(
                        build_provenance(
                            ptr=f"/services/{len(services)}/" + field,
                            source="vision",
                            confidence=cell.get("confidence", 0.0),
                            page_index=page_index,
                            box=cell.get("pixel_box") or cell.get("box")
                        )
                    )
                if entry:
                    services.append(entry)
                    provenance.extend(row_prov)
    return services


# -----------------------------------------------------------------------------
# Totals & metadata
# -----------------------------------------------------------------------------


def sum_amounts(values: Iterable[str]) -> str:
    total = Decimal("0.00")
    for value in values:
        try:
            total += Decimal(value)
        except (InvalidOperation, TypeError):
            continue
    return f"{total:.2f}"


def compute_totals(services: List[Dict[str, Any]]) -> Dict[str, str]:
    charges = [s.get("charged_amount", "0.00") for s in services]
    allowed = [s.get("allowed_amount", "0.00") for s in services]
    paid = [s.get("insurance_paid", "0.00") for s in services]
    patient = [s.get("patient_responsibility", "0.00") for s in services]
    return {
        "total_charged": sum_amounts(charges),
        "total_allowed": sum_amounts(allowed),
        "total_paid": sum_amounts(paid),
        "total_patient_responsibility": sum_amounts(patient),
        "currency": "USD"
    }


# -----------------------------------------------------------------------------
# Document assembly
# -----------------------------------------------------------------------------


def assemble_document(doc_key: str, pages: List[Dict[str, Any]], templates: List[TableTemplate]) -> Dict[str, Any]:
    provenance: List[Dict[str, Any]] = []
    header_chunks: List[str] = []
    for page in pages:
        info = extract_header_text(page)
        for chunk in info.values():
            if chunk.get("text"):
                header_chunks.append(chunk["text"])
    header_text = "\n".join(header_chunks)
    template = choose_template(header_text, templates)

    services = gather_services(pages, template, provenance)
    if not services:
        services = [{"procedure_code": "D0000", "charged_amount": "0.00"}]
    totals = compute_totals(services)

    claim_number = apply_header_regex(header_text, template.kv_right.get("claim_number", {}).get("regex")) or f"{doc_key}"[:20]
    payer_name = apply_header_regex(header_text, template.kv_right.get("payer_name", {}).get("regex")) or template.payer
    subscriber_id = apply_header_regex(header_text, template.kv_right.get("subscriber_id", {}).get("regex")) or "UNKNOWN"
    provider_name = apply_header_regex(header_text, template.kv_left.get("provider_name", {}).get("regex")) or "UNKNOWN PROVIDER"
    npi = apply_header_regex(header_text, template.kv_left.get("npi", {}).get("regex"))
    tax_id = apply_header_regex(header_text, template.kv_left.get("tax_id", {}).get("regex"))

    metadata = {
        "schema_ref": "urn:imogen:eob:dental:1.2",
        "document_id": hashlib.sha256(doc_key.encode("utf-8")).hexdigest()
    }

    eob = {
        "patient": {
            "patient_name": "UNKNOWN PATIENT",
            "subscriber_id": subscriber_id
        },
        "provider": {"provider_name": provider_name},
        "claim": {
            "claim_number": claim_number,
            "payer_name": payer_name,
            "status": "Pending"
        },
        "services": services,
        "totals": totals,
        "reason_codes": [],
        "metadata": metadata,
        "provenance": provenance
    }
    if npi:
        eob["provider"]["npi"] = npi
    if tax_id:
        eob["provider"]["tax_id"] = tax_id

    # Header provenance entries
    if subscriber_id:
        provenance.append(build_provenance("/patient/subscriber_id", "vision", 0.8, 0, None))
    if claim_number:
        provenance.append(build_provenance("/claim/claim_number", "vision", 0.8, 0, None))
    if payer_name:
        provenance.append(build_provenance("/claim/payer_name", "vision", 0.8, 0, None))
    if provider_name:
        provenance.append(build_provenance("/provider/provider_name", "vision", 0.8, 0, None))
    provenance.append(build_provenance("/totals/total_charged", "derived", 1.0, 0, None))
    provenance.append(build_provenance("/totals/total_paid", "derived", 1.0, 0, None))
    provenance.append(build_provenance("/totals/total_patient_responsibility", "derived", 1.0, 0, None))

    return eob


# -----------------------------------------------------------------------------
# Validation harness
# -----------------------------------------------------------------------------


def run_ajv(ajv_cmd: Optional[str], schema: Optional[Path], output_path: Path) -> None:
    if not ajv_cmd or not schema:
        return
    cmd = ajv_cmd.split()
    cmd.extend(["validate", "-s", str(schema), "-d", str(output_path), "--spec=draft2020", "--strict=false"])
    subprocess.run(cmd, check=True)


# -----------------------------------------------------------------------------
# Main entry
# -----------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble dental EOBs from NativeOCR pages")
    parser.add_argument("--pages", required=True, help="Directory containing NDJSON page files")
    parser.add_argument("--mappings", required=True, help="Directory with payer mapping YAML files")
    parser.add_argument("--out", required=True, help="Output directory for EOB JSON files")
    parser.add_argument("--schema", help="Path to DentalEOB JSON Schema for validation")
    parser.add_argument("--ajv", help="AJV command (e.g., 'npx ajv-cli') for schema validation")
    parser.add_argument("--glob", default="*.jsonl", help="Glob pattern for page files")
    args = parser.parse_args()

    pages_dir = Path(args.pages)
    mapping_dir = Path(args.mappings)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    page_files = list(pages_dir.glob(args.glob))
    if not page_files:
        print(f"No page files matched {args.glob} in {pages_dir}")
        return

    templates = load_templates(mapping_dir)
    grouped = group_pages(page_files)

    for doc_key, pages in grouped.items():
        pages.sort(key=lambda x: x.get("page_index", 0))
        eob = assemble_document(doc_key, pages, templates)
        # Canonicalize JSON for stable hashing (JCS-adjacent)
        def canonical_dump(obj: dict) -> str:
            return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

        canon = canonical_dump(eob)
        digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        # Persist checksum alongside the file. Schema allows metadata-only fields; attach there if present.
        eob.setdefault("metadata", {})
        eob["metadata"]["canonical_sha256"] = digest
        # Write canonical bytes so on-disk matches checksum
        slug = re.sub(r"[^A-Za-z0-9]+", "_", Path(doc_key).stem or "document").strip("_") or "document"
        output_path = out_dir / f"{slug}.json"
        with output_path.open("w", encoding="utf-8") as fh:
            fh.write(canon)
        with (output_path.with_suffix(output_path.suffix + ".sha256")).open("w", encoding="utf-8") as sh:
            sh.write(digest + "\n")
        if args.schema:
            run_ajv(args.ajv, Path(args.schema), output_path)
        print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
