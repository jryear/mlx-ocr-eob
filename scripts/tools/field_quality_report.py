#!/usr/bin/env python3
"""
Field-level quality analysis from doc-level sidecars.

Inputs:
  - --root out/eob (scan for sidecars)
  - --out-csv field_coverage_report.csv
  - --out-json baseline_summary.json
  - --out-md improvement_roadmap.md

Consumes NEW sidecars:
  - extraction.map_metrics.v1.json (role_coverage, map_completeness)
  - table.ccert.v1.json (certified_rows, conservation)
  - selection.evidence.v1.json (chosen variant)
  - summary_box.v1.json (residual)
  - evidence.v1.json (typedness)

Computes:
  - Correlations: role_coverage vs conservation, certified_rows
  - Per-payer aggregations
  - Ranked improvement opportunities by impact × feasibility
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add parent to path for null_safe_utils
sys.path.insert(0, str(Path(__file__).parent.parent))
from null_safe_utils import safe_load_json, safe_get, safe_get_dict, safe_get_float, safe_get_int, safe_get_list


@dataclass
class DocFieldMetrics:
    """Field-level metrics for a single document."""
    doc_id: str
    payer: str
    # From map_metrics
    map_completeness: float
    role_coverage_mean: float  # Mean of 4 money roles
    anchors_mode: int
    applied_for_extraction: bool
    # From table.ccert
    certified_rows: int
    conservation: float
    # From selection.evidence
    selection_chosen: str
    # From summary_box
    summary_residual: float
    # From services
    row_count: int


def find_eob_files(root: Path) -> List[Path]:
    """Find all main EOB JSON files in root/*."""
    eob_files = []
    if root.is_dir():
        for json_file in root.glob("*.json"):
            if not any(x in json_file.name for x in ['.ccert.', '.proof.', '.evidence.',
                                                       '.budget.', '.stability.', '.summary_box.',
                                                       '.conservation.', '.coherence.', '.sha256',
                                                       '.map_metrics.', '.selection.']):
                eob_files.append(json_file)
    for dir_path in root.glob("*"):
        if not dir_path.is_dir():
            continue
        for json_file in dir_path.glob("*.json"):
            if not any(x in json_file.name for x in ['.ccert.', '.proof.', '.evidence.',
                                                       '.budget.', '.stability.', '.summary_box.',
                                                       '.conservation.', '.coherence.', '.sha256',
                                                       '.map_metrics.', '.selection.']):
                eob_files.append(json_file)
    return eob_files


def load_doc_field_metrics(eob_path: Path) -> Optional[DocFieldMetrics]:
    """Load field metrics from a single document and its sidecars."""
    eob = safe_load_json(eob_path, {})
    if not eob:
        return None

    doc_id = eob_path.stem
    payer = safe_get(eob.get("claim", {}), "payer_name", "unknown")
    services = safe_get_list(eob, "services", [])

    stem = eob_path.stem
    parent = eob_path.parent

    # Load sidecars
    map_metrics = safe_load_json(parent / f"{stem}.extraction.map_metrics.v1.json", {})
    table_cert = safe_load_json(parent / f"{stem}.table.ccert.v1.json", {})
    selection_evidence = safe_load_json(parent / f"{stem}.selection.evidence.v1.json", {})
    summary_box = safe_load_json(parent / f"{stem}.summary_box.v1.json", {})

    # Extract metrics from map_metrics (aggregate across tables)
    map_tables = safe_get_list(map_metrics, "tables", [])
    if map_tables:
        # map_completeness: mean across tables
        map_comp_values = [safe_get_int(t, "map_completeness", 0) for t in map_tables]
        map_completeness = sum(map_comp_values) / len(map_comp_values) if map_comp_values else 0.0

        # role_coverage: mean of 4 money roles across all tables
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
                role_cov_values.append(sum(values) / 4.0)
        role_coverage_mean = sum(role_cov_values) / len(role_cov_values) if role_cov_values else 0.0

        # anchors_mode: most common anchors count
        from collections import Counter
        counter: Counter[int] = Counter()
        for t in map_tables:
            anchors_dict = safe_get_dict(t, "anchors_per_row", {})
            for key, count in anchors_dict.items():
                counter[int(key)] += count
        anchors_mode = counter.most_common(1)[0][0] if counter else 0

        # applied_for_extraction
        applied = any(safe_get(t, "applied_for_extraction", False) for t in map_tables)
    else:
        map_completeness = 0.0
        role_coverage_mean = 0.0
        anchors_mode = 0
        applied = False

    # From table.ccert
    certified_rows = safe_get_int(table_cert, "certified_rows", 0)
    conservation = safe_get_float(table_cert, "conservation", 0.0)

    # From selection.evidence
    chosen = safe_get(selection_evidence, "chosen", "unknown")

    # From summary_box
    summary_residual = safe_get_float(summary_box, "residual", 0.0)

    return DocFieldMetrics(
        doc_id=doc_id,
        payer=payer,
        map_completeness=map_completeness,
        role_coverage_mean=role_coverage_mean,
        anchors_mode=anchors_mode,
        applied_for_extraction=applied,
        certified_rows=certified_rows,
        conservation=conservation,
        selection_chosen=chosen,
        summary_residual=summary_residual,
        row_count=len(services)
    )


def compute_correlations(docs: List[DocFieldMetrics]) -> Dict[str, float]:
    """Compute correlations between key metrics."""
    import math

    # Filter docs with valid metrics
    valid_docs = [d for d in docs if d.row_count > 0]

    def pearson(x_vals: List[float], y_vals: List[float]) -> float:
        """Compute Pearson correlation coefficient."""
        if len(x_vals) < 2 or len(y_vals) < 2 or len(x_vals) != len(y_vals):
            return 0.0
        n = len(x_vals)
        mean_x = sum(x_vals) / n
        mean_y = sum(y_vals) / n
        numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(x_vals, y_vals))
        denom_x = math.sqrt(sum((x - mean_x) ** 2 for x in x_vals))
        denom_y = math.sqrt(sum((y - mean_y) ** 2 for y in y_vals))
        if denom_x == 0 or denom_y == 0:
            return 0.0
        return numerator / (denom_x * denom_y)

    # role_coverage vs conservation
    role_cov_vals = [d.role_coverage_mean for d in valid_docs]
    conservation_vals = [d.conservation for d in valid_docs]
    role_cov_vs_conservation = pearson(role_cov_vals, conservation_vals)

    # role_coverage vs certified_rows
    certified_vals = [float(d.certified_rows) for d in valid_docs]
    role_cov_vs_certified = pearson(role_cov_vals, certified_vals)

    # applied_for_extraction vs conservation
    applied_vals = [1.0 if d.applied_for_extraction else 0.0 for d in valid_docs]
    applied_vs_conservation = pearson(applied_vals, conservation_vals)

    # map_completeness vs certified_rows
    map_comp_vals = [d.map_completeness for d in valid_docs]
    map_comp_vs_certified = pearson(map_comp_vals, certified_vals)

    return {
        "role_coverage_vs_conservation": role_cov_vs_conservation,
        "role_coverage_vs_certified_rows": role_cov_vs_certified,
        "applied_for_extraction_vs_conservation": applied_vs_conservation,
        "map_completeness_vs_certified_rows": map_comp_vs_certified
    }


def aggregate_by_payer(docs: List[DocFieldMetrics]) -> Dict[str, Dict[str, float]]:
    """Aggregate metrics by payer."""
    from collections import defaultdict

    payer_docs: Dict[str, List[DocFieldMetrics]] = defaultdict(list)
    for doc in docs:
        payer_docs[doc.payer].append(doc)

    payer_stats: Dict[str, Dict[str, float]] = {}
    for payer, doc_list in payer_docs.items():
        valid_docs = [d for d in doc_list if d.row_count > 0]
        n = len(valid_docs) if valid_docs else 1

        payer_stats[payer] = {
            "docs": len(doc_list),
            "map_completeness_mean": sum(d.map_completeness for d in valid_docs) / n,
            "role_coverage_mean": sum(d.role_coverage_mean for d in valid_docs) / n,
            "applied_for_extraction_rate": sum(1 for d in doc_list if d.applied_for_extraction) / len(doc_list),
            "certified_rows_mean": sum(d.certified_rows for d in valid_docs) / n,
            "conservation_mean": sum(d.conservation for d in valid_docs) / n
        }

    return payer_stats


def write_field_coverage_csv(docs: List[DocFieldMetrics], out_path: Path) -> None:
    """Write field_coverage_report.csv with one row per document."""
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'doc_id', 'payer', 'map_completeness', 'role_coverage_mean',
            'anchors_mode', 'applied_for_extraction', 'certified_rows',
            'conservation', 'selection_chosen', 'summary_residual', 'row_count'
        ])
        for doc in docs:
            writer.writerow([
                doc.doc_id,
                doc.payer,
                f"{doc.map_completeness:.4f}",
                f"{doc.role_coverage_mean:.4f}",
                doc.anchors_mode,
                1 if doc.applied_for_extraction else 0,
                doc.certified_rows,
                f"{doc.conservation:.4f}",
                doc.selection_chosen,
                f"{doc.summary_residual:.4f}",
                doc.row_count
            ])


def write_baseline_summary_json(docs: List[DocFieldMetrics], correlations: Dict[str, float],
                                  payer_stats: Dict[str, Dict[str, float]], out_path: Path) -> None:
    """Write baseline_summary.json with global stats and correlations."""
    valid_docs = [d for d in docs if d.row_count > 0]
    n = len(valid_docs) if valid_docs else 1

    summary = {
        "schema": "baseline_summary.v1",
        "total_documents": len(docs),
        "extracted_documents": len(valid_docs),
        "global_metrics": {
            "map_completeness_mean": sum(d.map_completeness for d in valid_docs) / n,
            "role_coverage_mean": sum(d.role_coverage_mean for d in valid_docs) / n,
            "applied_for_extraction_rate": sum(1 for d in docs if d.applied_for_extraction) / len(docs),
            "certified_rows_mean": sum(d.certified_rows for d in valid_docs) / n,
            "conservation_mean": sum(d.conservation for d in valid_docs) / n
        },
        "correlations": correlations,
        "payer_breakdowns": payer_stats
    }

    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")


def write_improvement_roadmap_md(docs: List[DocFieldMetrics], correlations: Dict[str, float],
                                   payer_stats: Dict[str, Dict[str, float]], out_path: Path) -> None:
    """Write improvement_roadmap.md with ranked recommendations."""
    lines: List[str] = []

    lines.append("# Field Quality Improvement Roadmap")
    lines.append("")
    lines.append("Generated from doc-level sidecars using field_quality_report.py")
    lines.append("")

    # Executive Summary
    lines.append("## Executive Summary")
    lines.append("")
    valid_docs = [d for d in docs if d.row_count > 0]
    lines.append(f"- **Total Documents Analyzed**: {len(docs)}")
    lines.append(f"- **Documents with Extracted Services**: {len(valid_docs)} ({len(valid_docs)/len(docs)*100:.1f}%)")
    lines.append("")

    # Key Correlations
    lines.append("## Key Correlations")
    lines.append("")
    lines.append("| Metric Pair | Correlation | Interpretation |")
    lines.append("|-------------|-------------|----------------|")

    for key, value in sorted(correlations.items(), key=lambda x: abs(x[1]), reverse=True):
        interp = "Strong positive" if value > 0.6 else "Moderate positive" if value > 0.3 else "Weak/None"
        lines.append(f"| {key.replace('_', ' ')} | {value:.3f} | {interp} |")
    lines.append("")

    # Top 10 Improvement Opportunities (ranked by impact × feasibility)
    lines.append("## Top 10 Improvement Opportunities")
    lines.append("")

    # Calculate gaps
    gaps: List[tuple[str, float, float, float]] = []  # (name, impact, feasibility, roi)

    # Gap 1: Low role_coverage
    low_role_cov = [d for d in valid_docs if d.role_coverage_mean < 0.3]
    if low_role_cov:
        impact = (len(low_role_cov) / len(valid_docs)) * 100  # % docs affected
        feasibility = 7.0  # High (typedness tuning)
        gaps.append(("Low role_coverage (<0.3)", impact, feasibility, impact * feasibility))

    # Gap 2: Applied_for_extraction gate fails
    not_applied = [d for d in docs if not d.applied_for_extraction]
    if not_applied:
        impact = (len(not_applied) / len(docs)) * 100
        feasibility = 6.0  # Medium-high (admission gate tuning)
        gaps.append(("Failed applied_for_extraction gate", impact, feasibility, impact * feasibility))

    # Gap 3: Low map_completeness
    low_map_comp = [d for d in valid_docs if d.map_completeness < 3.0]
    if low_map_comp:
        impact = (len(low_map_comp) / len(valid_docs)) * 100
        feasibility = 8.0  # High (column assignment improvements)
        gaps.append(("Low map_completeness (<3)", impact, feasibility, impact * feasibility))

    # Gap 4: Zero anchors
    zero_anchors = [d for d in valid_docs if d.anchors_mode == 0]
    if zero_anchors:
        impact = (len(zero_anchors) / len(valid_docs)) * 100
        feasibility = 5.0  # Medium (requires VLM or better pattern matching)
        gaps.append(("Zero anchors_mode", impact, feasibility, impact * feasibility))

    # Gap 5: Low conservation
    low_conservation = [d for d in valid_docs if d.conservation < 0.5]
    if low_conservation:
        impact = (len(low_conservation) / len(valid_docs)) * 100
        feasibility = 6.0  # Medium-high (solver tuning)
        gaps.append(("Low conservation (<50%)", impact, feasibility, impact * feasibility))

    # Sort by ROI descending
    gaps.sort(key=lambda x: x[3], reverse=True)

    for i, (name, impact, feasibility, roi) in enumerate(gaps[:10], 1):
        lines.append(f"### {i}. {name}")
        lines.append(f"- **Impact Score**: {impact:.1f}% of documents affected")
        lines.append(f"- **Feasibility**: {feasibility:.1f}/10")
        lines.append(f"- **ROI**: {roi:.1f}")
        lines.append("")

        # Add recommendations based on gap type
        if "role_coverage" in name:
            lines.append("**Recommendations:**")
            lines.append("- Tune typedness thresholds (typedness_tau, typedness_admit_tau)")
            lines.append("- Review column assignment logic for missed money columns")
            lines.append("- Consider VLM fallback for low-typedness regions")
        elif "applied_for_extraction" in name:
            lines.append("**Recommendations:**")
            lines.append("- Lower admission gate thresholds (ilp_min_cert, typedness_min)")
            lines.append("- Enable more aggressive fallback modes (--doc-fallback-rowize)")
        elif "map_completeness" in name:
            lines.append("**Recommendations:**")
            lines.append("- Improve ILP-lite candidate generation")
            lines.append("- Add payer-specific header mappings")
        elif "anchors" in name:
            lines.append("**Recommendations:**")
            lines.append("- Enable VLM preprocessing (--vlm-preprocess)")
            lines.append("- Tune anchor detection patterns")
        elif "conservation" in name:
            lines.append("**Recommendations:**")
            lines.append("- Review solver law weights (in_network, noncovered, cob_secondary)")
            lines.append("- Investigate inequality violations")

        lines.append("")

    # Payer-Specific Recommendations
    lines.append("## Payer-Specific Optimization Strategies")
    lines.append("")

    for payer in sorted(payer_stats.keys(), key=lambda p: payer_stats[p]["conservation_mean"], reverse=True):
        stats = payer_stats[payer]
        lines.append(f"### {payer}")
        lines.append(f"- **Documents**: {int(stats['docs'])}")
        lines.append(f"- **Map Completeness**: {stats['map_completeness_mean']:.2f}")
        lines.append(f"- **Role Coverage**: {stats['role_coverage_mean']*100:.1f}%")
        lines.append(f"- **Applied Rate**: {stats['applied_for_extraction_rate']*100:.1f}%")
        lines.append(f"- **Conservation**: {stats['conservation_mean']*100:.1f}%")

        # Recommendations
        if stats["map_completeness_mean"] < 3.0:
            lines.append(f"- **Action**: Improve column assignment (map_completeness below 3.0)")
        if stats["role_coverage_mean"] < 0.3:
            lines.append(f"- **Action**: Tune typedness for {payer} documents")
        if stats["applied_for_extraction_rate"] < 0.5:
            lines.append(f"- **Action**: Lower admission gate thresholds")

        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="Root directory to scan (e.g., out/eob)")
    ap.add_argument("--out-csv", required=True, help="Output path for field_coverage_report.csv")
    ap.add_argument("--out-json", required=True, help="Output path for baseline_summary.json")
    ap.add_argument("--out-md", required=True, help="Output path for improvement_roadmap.md")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"Error: Root directory {root} does not exist", file=sys.stderr)
        sys.exit(1)

    # Find all EOB files
    eob_files = find_eob_files(root)
    print(f"Found {len(eob_files)} EOB files in {root}", file=sys.stderr)

    # Load metrics from each document
    docs: List[DocFieldMetrics] = []
    for eob_path in eob_files:
        doc_metrics = load_doc_field_metrics(eob_path)
        if doc_metrics:
            docs.append(doc_metrics)

    print(f"Loaded metrics from {len(docs)} documents", file=sys.stderr)

    if not docs:
        print("No documents found with valid metrics", file=sys.stderr)
        sys.exit(1)

    # Compute correlations
    correlations = compute_correlations(docs)
    print(f"Computed {len(correlations)} correlations", file=sys.stderr)

    # Aggregate by payer
    payer_stats = aggregate_by_payer(docs)
    print(f"Aggregated into {len(payer_stats)} payers", file=sys.stderr)

    # Write outputs
    csv_path = Path(args.out_csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_field_coverage_csv(docs, csv_path)
    print(str(csv_path))

    json_path = Path(args.out_json)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    write_baseline_summary_json(docs, correlations, payer_stats, json_path)
    print(str(json_path))

    md_path = Path(args.out_md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    write_improvement_roadmap_md(docs, correlations, payer_stats, md_path)
    print(str(md_path))


if __name__ == "__main__":
    main()
