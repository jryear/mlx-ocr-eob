#!/usr/bin/env python3
"""
Compute payer quality map from extraction outputs with NEW doc-level sidecars.

Inputs:
  - --root out (scan out/* directories for sidecars)
  - --out out/reports/payer_quality_map.json

Outputs a JSON with per-payer metrics including:
  - NEW: map_completeness, role_coverage, anchors_per_row mode
  - NEW: applied_for_extraction_rate, selection_chosen rates
  - NEW: certified_rows_mean, summary_residual_mean
  - LEGACY: doc_mode_accept, mapper_win, conservation, p95
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add parent to path for null_safe_utils
sys.path.insert(0, str(Path(__file__).parent.parent))
from null_safe_utils import safe_load_json, safe_get, safe_get_dict, safe_get_float, safe_get_int, safe_get_list


@dataclass
class PayerRow:
    payer: str
    docs: int
    services: int
    # NEW metrics from doc-level sidecars
    map_completeness_mean: float
    map_completeness_distribution: List[int]  # histogram [0,1,2,3,4,5,6+]
    anchors_per_row_mode: float
    role_coverage_mean: float
    applied_for_extraction_rate: float
    selection_chosen_mapper_rate: float
    selection_chosen_him_rate: float
    certified_rows_mean: float
    summary_residual_mean: float
    # LEGACY metrics (kept for compatibility)
    doc_mode_accept_rate: float
    mapper_win_rate: float
    money_rows_ratio_mean: float
    code_date_ratio_mean: float
    uncertain_rate: float
    rows_3of4_rate: float
    conservation_mean: float
    p95_s_per_page: float
    roi: float


def _safe_get(d: Dict[str, Any], *path: str, default: float = 0.0) -> float:
    cur: Any = d
    for p in path:
        cur = (cur or {}).get(p)
    try:
        return float(cur)
    except Exception:
        return float(default)


@dataclass
class DocMetrics:
    """Metrics extracted from a single document."""
    payer: str
    services_count: int
    # NEW sidecar metrics
    map_completeness: Optional[int]
    role_coverage: Optional[float]
    anchors_per_row_hist: List[int]
    applied_for_extraction: bool
    selection_chosen: str  # mapper/him/unknown
    certified_rows: int
    conservation: float
    summary_residual: Optional[float]


def find_eob_files(root: Path) -> List[Path]:
    """Find all main EOB JSON files in root/* (handles both direct files and subdirectories)."""
    eob_files = []

    # If root is a directory with JSON files, scan it directly
    if root.is_dir():
        for json_file in root.glob("*.json"):
            if not any(x in json_file.name for x in ['.ccert.', '.proof.', '.evidence.',
                                                       '.budget.', '.stability.', '.summary_box.',
                                                       '.conservation.', '.coherence.', '.sha256',
                                                       '.map_metrics.', '.selection.']):
                eob_files.append(json_file)

    # Also scan subdirectories
    for dir_path in root.glob("*"):
        if not dir_path.is_dir():
            continue
        # Find JSON files excluding sidecars
        for json_file in dir_path.glob("*.json"):
            if not any(x in json_file.name for x in ['.ccert.', '.proof.', '.evidence.',
                                                       '.budget.', '.stability.', '.summary_box.',
                                                       '.conservation.', '.coherence.', '.sha256',
                                                       '.map_metrics.', '.selection.']):
                eob_files.append(json_file)
    return eob_files


def load_doc_metrics(eob_path: Path) -> Optional[DocMetrics]:
    """Load metrics from a single document and its sidecars."""
    eob = safe_load_json(eob_path, {})
    if not eob:
        return None

    # Extract payer
    payer = safe_get(eob.get("claim", {}), "payer_name", "unknown")
    services = safe_get_list(eob, "services", [])

    # Load NEW sidecars
    stem = eob_path.stem
    parent = eob_path.parent

    map_metrics = safe_load_json(parent / f"{stem}.extraction.map_metrics.v1.json", {})
    selection_evidence = safe_load_json(parent / f"{stem}.selection.evidence.v1.json", {})
    assign_proof = safe_load_json(parent / f"{stem}.assign.proof.v1.json", {})
    table_cert = safe_load_json(parent / f"{stem}.table.ccert.v1.json", {})
    summary_box = safe_load_json(parent / f"{stem}.summary_box.v1.json", {})

    # Extract metrics - aggregate across tables in map_metrics
    map_tables = safe_get_list(map_metrics, "tables", [])
    if map_tables:
        # Aggregate map_completeness (mean across tables)
        map_comp_values = [safe_get_int(t, "map_completeness", 0) for t in map_tables]
        map_completeness = sum(map_comp_values) / len(map_comp_values) if map_comp_values else 0

        # Aggregate role_coverage (mean of all 4 money roles across tables)
        role_cov_values = []
        for t in map_tables:
            role_cov_dict = safe_get_dict(t, "role_coverage", {})
            if role_cov_dict:
                values = [
                    safe_get_float(role_cov_dict, "charged_amount", 0.0),
                    safe_get_float(role_cov_dict, "allowed_amount", 0.0),
                    safe_get_float(role_cov_dict, "insurance_paid", 0.0),
                    safe_get_float(role_cov_dict, "patient_responsibility", 0.0)
                ]
                role_cov_values.append(sum(values) / 4.0)  # Mean of 4 money roles
        role_coverage = sum(role_cov_values) / len(role_cov_values) if role_cov_values else 0.0

        # Aggregate anchors_per_row histogram
        anchors_hist = [0] * 10  # Support up to 9 anchors
        for t in map_tables:
            anchors_dict = safe_get_dict(t, "anchors_per_row", {})
            for key, count in anchors_dict.items():
                idx = int(key)
                if idx < len(anchors_hist):
                    anchors_hist[idx] += count
    else:
        map_completeness = None
        role_coverage = None
        anchors_hist = []

    # applied_for_extraction (check if any table was applied from map_metrics)
    applied = any(safe_get(t, "applied_for_extraction", False) for t in map_tables) if map_tables else False

    # selection chosen variant
    chosen = safe_get(selection_evidence, "chosen", "unknown")

    # certified_rows from table.ccert
    certified_rows = safe_get_int(table_cert, "certified_rows", 0)
    conservation = safe_get_float(table_cert, "conservation", 0.0)

    # summary_residual from summary_box
    summary_residual = safe_get_float(summary_box, "residual", None) if summary_box else None

    return DocMetrics(
        payer=payer,
        services_count=len(services),
        map_completeness=map_completeness,
        role_coverage=role_coverage,
        anchors_per_row_hist=anchors_hist,
        applied_for_extraction=applied,
        selection_chosen=chosen,
        certified_rows=certified_rows,
        conservation=conservation,
        summary_residual=summary_residual
    )


def aggregate_by_payer(docs: List[DocMetrics]) -> Dict[str, PayerRow]:
    """Aggregate document metrics by payer."""
    payer_docs: Dict[str, List[DocMetrics]] = {}
    for doc in docs:
        if doc.payer not in payer_docs:
            payer_docs[doc.payer] = []
        payer_docs[doc.payer].append(doc)

    rows: Dict[str, PayerRow] = {}
    for payer, payer_doc_list in payer_docs.items():
        # Compute NEW metrics
        map_completeness_values = [d.map_completeness for d in payer_doc_list if d.map_completeness is not None]
        map_completeness_mean = sum(map_completeness_values) / len(map_completeness_values) if map_completeness_values else 0.0

        # map_completeness_distribution: histogram [0,1,2,3,4,5,6+]
        distribution = [0] * 7
        for val in map_completeness_values:
            idx = min(int(val), 6)  # Convert to int for indexing
            distribution[idx] += 1

        # anchors_per_row_mode: most common anchors count
        all_anchors_hists = [d.anchors_per_row_hist for d in payer_doc_list if d.anchors_per_row_hist]
        anchors_mode = 0.0
        if all_anchors_hists:
            # Find mode across all histograms
            counter: Counter[int] = Counter()
            for hist in all_anchors_hists:
                for idx, count in enumerate(hist):
                    if count > 0:
                        counter[idx] += count
            if counter:
                anchors_mode = float(counter.most_common(1)[0][0])

        role_coverage_values = [d.role_coverage for d in payer_doc_list if d.role_coverage is not None]
        role_coverage_mean = sum(role_coverage_values) / len(role_coverage_values) if role_coverage_values else 0.0

        applied_count = sum(1 for d in payer_doc_list if d.applied_for_extraction)
        applied_rate = applied_count / len(payer_doc_list) if payer_doc_list else 0.0

        mapper_count = sum(1 for d in payer_doc_list if d.selection_chosen == "mapper")
        him_count = sum(1 for d in payer_doc_list if d.selection_chosen == "him")
        mapper_rate = mapper_count / len(payer_doc_list) if payer_doc_list else 0.0
        him_rate = him_count / len(payer_doc_list) if payer_doc_list else 0.0

        certified_rows_mean = sum(d.certified_rows for d in payer_doc_list) / len(payer_doc_list) if payer_doc_list else 0.0

        summary_residual_values = [d.summary_residual for d in payer_doc_list if d.summary_residual is not None]
        summary_residual_mean = sum(summary_residual_values) / len(summary_residual_values) if summary_residual_values else 0.0

        conservation_mean = sum(d.conservation for d in payer_doc_list) / len(payer_doc_list) if payer_doc_list else 0.0

        # LEGACY metrics (set to 0.0 for now - would need old aggregate.json)
        doc_mode_accept_rate = 0.0
        mapper_win_rate = mapper_rate  # Approximate as mapper selection rate
        money_rows_ratio_mean = conservation_mean  # Approximate
        code_date_ratio_mean = 0.0
        uncertain_rate = 0.0
        rows_3of4_rate = 0.0
        p95_s_per_page = 0.0

        # Compute ROI
        m = {
            "doc_mode_accept_rate": doc_mode_accept_rate,
            "mapper_win_rate": mapper_win_rate,
            "conservation_mean": conservation_mean,
            "rows_3of4_rate": rows_3of4_rate,
            "money_rows_ratio_mean": money_rows_ratio_mean,
            "code_date_ratio_mean": code_date_ratio_mean,
            "uncertain_rate": uncertain_rate,
            "p95_s_per_page": p95_s_per_page,
        }
        roi = compute_roi(m)

        total_services = sum(d.services_count for d in payer_doc_list)

        rows[payer] = PayerRow(
            payer=payer,
            docs=len(payer_doc_list),
            services=total_services,
            map_completeness_mean=map_completeness_mean,
            map_completeness_distribution=distribution,
            anchors_per_row_mode=anchors_mode,
            role_coverage_mean=role_coverage_mean,
            applied_for_extraction_rate=applied_rate,
            selection_chosen_mapper_rate=mapper_rate,
            selection_chosen_him_rate=him_rate,
            certified_rows_mean=certified_rows_mean,
            summary_residual_mean=summary_residual_mean,
            doc_mode_accept_rate=doc_mode_accept_rate,
            mapper_win_rate=mapper_win_rate,
            money_rows_ratio_mean=money_rows_ratio_mean,
            code_date_ratio_mean=code_date_ratio_mean,
            uncertain_rate=uncertain_rate,
            rows_3of4_rate=rows_3of4_rate,
            conservation_mean=conservation_mean,
            p95_s_per_page=p95_s_per_page,
            roi=roi
        )

    return rows


def compute_roi(m: Dict[str, Any]) -> float:
    """
    Compute deterministic ROI using NEW sidecar metrics.

    Formula:
      roi = 0.30·applied_for_extraction_rate
          + 0.25·(map_completeness_mean/4)
          + 0.20·role_coverage_mean
          + 0.15·(1 − selection_headerless_rate)
          + 0.10·conservation_mean
    """
    applied = _safe_get(m, "applied_for_extraction_rate")
    map_comp = _safe_get(m, "map_completeness_mean") / 4.0  # Normalize to [0,1] assuming max=4
    role_cov = _safe_get(m, "role_coverage_mean")
    headerless_rate = _safe_get(m, "selection_chosen_him_rate")
    conservation = _safe_get(m, "conservation_mean")

    acc = 0.30 * applied
    acc += 0.25 * map_comp
    acc += 0.20 * role_cov
    acc += 0.15 * (1.0 - headerless_rate)
    acc += 0.10 * conservation

    return max(0.0, min(1.0, acc))


def write_quality_landscape_md(rows: List[PayerRow], docs: List[DocMetrics], out_path: Path) -> None:
    """Write quality_landscape.md with deterministic formatting."""
    lines: List[str] = []

    # Header
    lines.append("# Quality Landscape Report")
    lines.append("")
    lines.append("Generated from doc-level sidecars using payer_quality_map.py")
    lines.append("")

    # Overall KPIs
    total_docs = len(docs)
    extracted_docs = sum(1 for d in docs if d.services_count > 0)
    applied_rate = sum(1 for d in docs if d.applied_for_extraction) / total_docs if total_docs > 0 else 0.0

    map_completeness_values = [d.map_completeness for d in docs if d.map_completeness is not None]
    if map_completeness_values:
        map_completeness_values.sort()
        median_idx = len(map_completeness_values) // 2
        map_comp_median = map_completeness_values[median_idx]
        p95_idx = int(len(map_completeness_values) * 0.95)
        map_comp_p95 = map_completeness_values[p95_idx]
    else:
        map_comp_median = 0.0
        map_comp_p95 = 0.0

    # Compute anchors mode across all docs
    counter: Counter[int] = Counter()
    for d in docs:
        if d.anchors_per_row_hist:
            for idx, count in enumerate(d.anchors_per_row_hist):
                if count > 0:
                    counter[idx] += count
    anchors_mode = counter.most_common(1)[0][0] if counter else 0

    conservation_mean = sum(d.conservation for d in docs) / total_docs if total_docs > 0 else 0.0

    mapper_count = sum(1 for d in docs if d.selection_chosen == "mapper")
    him_count = sum(1 for d in docs if d.selection_chosen == "him")
    mapper_rate = mapper_count / total_docs if total_docs > 0 else 0.0
    him_rate = him_count / total_docs if total_docs > 0 else 0.0

    lines.append("## Overall KPIs")
    lines.append("")
    lines.append(f"- **Total Documents**: {total_docs}")
    lines.append(f"- **Extracted Documents**: {extracted_docs} ({extracted_docs/total_docs*100:.1f}%)")
    lines.append(f"- **Applied for Extraction Rate**: {applied_rate*100:.1f}%")
    lines.append(f"- **Map Completeness**: median={map_comp_median}, p95={map_comp_p95}")
    lines.append(f"- **Anchors Per Row Mode**: {anchors_mode}")
    lines.append(f"- **Conservation Mean**: {conservation_mean*100:.1f}%")
    lines.append(f"- **Selection Rates**: mapper={mapper_rate*100:.1f}%, him={him_rate*100:.1f}%")
    lines.append("")

    # Per-payer table (sorted by ROI descending)
    lines.append("## Per-Payer Metrics")
    lines.append("")
    lines.append("| Payer | Docs | Services | Applied% | MapComp | Anchors | Cons% | Mapper% | Residual¢ | ROI |")
    lines.append("|-------|------|----------|----------|---------|---------|-------|---------|-----------|-----|")

    sorted_rows = sorted(rows, key=lambda r: r.roi, reverse=True)
    for row in sorted_rows:
        lines.append(f"| {row.payer} | {row.docs} | {row.services} | "
                    f"{row.applied_for_extraction_rate*100:.1f}% | "
                    f"{row.map_completeness_mean:.2f} | "
                    f"{row.anchors_per_row_mode:.1f} | "
                    f"{row.conservation_mean*100:.1f}% | "
                    f"{row.selection_chosen_mapper_rate*100:.1f}% | "
                    f"{row.summary_residual_mean*100:.1f} | "
                    f"{row.roi:.3f} |")
    lines.append("")

    # Highlights
    lines.append("## Highlights")
    lines.append("")

    # Top 5 best payers
    top5 = sorted_rows[:5]
    lines.append("### Top 5 Payers (by ROI)")
    for i, row in enumerate(top5, 1):
        reason = []
        if row.map_completeness_mean >= 4.0:
            reason.append("high map completeness")
        if row.applied_for_extraction_rate >= 0.8:
            reason.append("high admission rate")
        if row.conservation_mean >= 0.8:
            reason.append("strong conservation")
        reason_str = ", ".join(reason) if reason else "consistent quality"
        lines.append(f"{i}. **{row.payer}** (ROI: {row.roi:.3f}) - {reason_str}")
    lines.append("")

    # Bottom 5 needs attention
    bottom5 = sorted_rows[-5:][::-1]  # Reverse to show worst first
    lines.append("### Needs Attention (by ROI)")
    for i, row in enumerate(bottom5, 1):
        reasons = []
        if row.map_completeness_mean < 3.0:
            reasons.append("low map completeness (<3)")
        if row.anchors_per_row_mode == 0.0:
            reasons.append("zero anchors mode")
        if row.applied_for_extraction_rate < 0.5:
            reasons.append("low admission rate")
        if row.conservation_mean < 0.5:
            reasons.append("weak conservation")
        reason_str = ", ".join(reasons) if reasons else "below median quality"
        lines.append(f"{i}. **{row.payer}** (ROI: {row.roi:.3f}) - {reason_str}")
    lines.append("")

    # Write deterministically
    out_path.write_text("\n".join(lines), encoding="utf-8")


def write_gap_matrix_csv(rows: List[PayerRow], out_path: Path) -> None:
    """Write gap_matrix.csv with deterministic sorting (ROI descending)."""
    import csv

    # Sort by ROI descending
    sorted_rows = sorted(rows, key=lambda r: r.roi, reverse=True)

    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)

        # Fixed header
        writer.writerow([
            'payer', 'docs', 'services', 'applied_for_extraction_rate',
            'map_completeness_mean', 'anchors_mode', 'role_coverage_mean',
            'selection_headerless_rate', 'conservation_mean',
            'summary_residual_cents_mean', 'roi'
        ])

        # Data rows
        for row in sorted_rows:
            writer.writerow([
                row.payer,
                row.docs,
                row.services,
                f"{row.applied_for_extraction_rate:.4f}",
                f"{row.map_completeness_mean:.4f}",
                f"{row.anchors_per_row_mode:.4f}",
                f"{row.role_coverage_mean:.4f}",
                f"{row.selection_chosen_him_rate:.4f}",
                f"{row.conservation_mean:.4f}",
                f"{row.summary_residual_mean*100:.2f}",  # Convert to cents
                f"{row.roi:.4f}"
            ])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="Root directory to scan for EOB outputs (e.g., out)")
    ap.add_argument("--out", default="out/reports/payer_quality_map.json", help="Output path for JSON")
    ap.add_argument("--emit-md", help="Output path for quality_landscape.md")
    ap.add_argument("--emit-gaps", help="Output path for gap_matrix.csv")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"Error: Root directory {root} does not exist", file=sys.stderr)
        sys.exit(1)

    # Find all EOB files
    eob_files = find_eob_files(root)
    print(f"Found {len(eob_files)} EOB files in {root}", file=sys.stderr)

    # Load metrics from each document
    docs: List[DocMetrics] = []
    for eob_path in eob_files:
        doc_metrics = load_doc_metrics(eob_path)
        if doc_metrics:
            docs.append(doc_metrics)

    print(f"Loaded metrics from {len(docs)} documents", file=sys.stderr)

    if not docs:
        print("No documents found with valid metrics", file=sys.stderr)
        sys.exit(1)

    # Aggregate by payer
    payer_rows = aggregate_by_payer(docs)
    print(f"Aggregated into {len(payer_rows)} payers", file=sys.stderr)

    # Write JSON output
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {
        "schema": "payer.quality_map.v2",  # v2 includes NEW sidecar metrics
        "rows": [asdict(r) for r in sorted(payer_rows.values(), key=lambda r: r.payer)]
    }
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(str(out_path))

    # Write optional outputs
    if args.emit_md:
        md_path = Path(args.emit_md)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        write_quality_landscape_md(list(payer_rows.values()), docs, md_path)
        print(str(md_path))

    if args.emit_gaps:
        csv_path = Path(args.emit_gaps)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        write_gap_matrix_csv(list(payer_rows.values()), csv_path)
        print(str(csv_path))


if __name__ == "__main__":
    main()

