#!/usr/bin/env python3
"""
Visualize EOB extraction results in real-time with formatted output.
Shows services, column assignments, ILP alternatives, solver results, and evidence.

Hardened with null-safe utilities to handle missing sidecars gracefully.
"""

import json
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional
from decimal import Decimal

# Import null-safe utilities for graceful degradation
try:
    from null_safe_utils import safe_load_json, safe_get, safe_get_dict, safe_get_list, safe_get_float
except ImportError:
    # Fallback if running from different directory
    sys.path.insert(0, str(Path(__file__).parent))
    from null_safe_utils import safe_load_json, safe_get, safe_get_dict, safe_get_list, safe_get_float


def load_json(path: Path) -> Optional[Dict[str, Any]]:
    """Load JSON file with null-safe fallback."""
    return safe_load_json(path, {})


def format_money(value: str) -> str:
    """Format money value with color."""
    try:
        amount = Decimal(value)
        return f"${amount:>8.2f}"
    except:
        return f"${value:>8}"


def print_header(title: str, width: int = 100):
    """Print section header."""
    print("\n" + "═" * width)
    print(f"  {title}")
    print("═" * width)


def print_subheader(title: str, width: int = 100):
    """Print subsection header."""
    print("\n" + "─" * width)
    print(f"  {title}")
    print("─" * width)


def visualize_document_info(eob: Dict[str, Any]):
    """Display document metadata."""
    print_header("📄 DOCUMENT INFORMATION")

    meta = eob.get("metadata", {})
    claim = eob.get("claim", {})
    patient = eob.get("patient", {})

    print(f"\nDocument ID: {meta.get('document_id', 'N/A')[:16]}...")
    print(f"Schema:      {meta.get('schema_ref', 'N/A')}")
    print(f"Claim #:     {claim.get('claim_number', 'N/A')}")
    print(f"Payer:       {claim.get('payer_name', 'N/A')}")
    print(f"Patient:     {patient.get('patient_name', 'N/A')}")
    print(f"Status:      {claim.get('status', 'N/A')}")


def visualize_services_table(eob: Dict[str, Any]):
    """Display extracted services in table format."""
    print_header("📊 EXTRACTED SERVICES TABLE")

    services = eob.get("services", [])
    if not services:
        print("\n  No services extracted.")
        return

    print(f"\n  Total Services: {len(services)}")
    print(f"  Certified Rows: {sum(1 for s in services if s.get('_row_locked'))}")

    # Table header
    print("\n  " + "─" * 96)
    print(f"  {'Row':<4} {'Code':<8} {'Date':<12} {'Charged':<12} {'Allowed':<12} {'Paid':<12} {'Patient':<12} {'Cert':<4}")
    print("  " + "─" * 96)

    # Table rows
    for i, svc in enumerate(services, 1):
        code = svc.get("procedure_code", "")[:8]
        date = svc.get("service_date", "")[:12]
        charged = format_money(svc.get("charged_amount", "0.00"))
        allowed = format_money(svc.get("allowed_amount", "0.00"))
        paid = format_money(svc.get("insurance_paid", "0.00"))
        patient = format_money(svc.get("patient_responsibility", "0.00"))
        certified = "✓" if svc.get("_row_locked") else " "

        print(f"  {i:<4} {code:<8} {date:<12} {charged} {allowed} {paid} {patient} {certified:^4}")

    print("  " + "─" * 96)

    # Totals
    totals = eob.get("totals", {})
    print(f"\n  TOTALS:  {' '*20} {format_money(totals.get('total_charged', '0.00'))} "
          f"{format_money(totals.get('total_allowed', '0.00'))} "
          f"{format_money(totals.get('total_paid', '0.00'))} "
          f"{format_money(totals.get('total_patient_responsibility', '0.00'))}")


def visualize_column_assignments(assign_proof: Dict[str, Any]):
    """Display column assignment details and ILP alternatives."""
    print_header("🎯 COLUMN ASSIGNMENT ANALYSIS")

    tables = assign_proof.get("tables", [])
    if not tables:
        print("\n  No assignment proof data.")
        return

    for i, table in enumerate(tables):
        print_subheader(f"Table {table.get('table_id', 'unknown')} (Page {table.get('page_index', 0)})")

        # Extraction variant
        variant = table.get("extraction_variant", "unknown")
        print(f"\n  Extraction Method: {variant.upper()}")

        # Current column mapping
        print(f"\n  📍 CHOSEN MAPPING:")
        column_map = table.get("column_map", {})
        for role, col_idx in sorted(column_map.items(), key=lambda x: x[1]):
            print(f"     Column {col_idx} → {role}")

        # Costs
        costs = table.get("costs", {})
        print(f"\n  💰 COSTS:")
        print(f"     Certified Rows Sample:  {costs.get('certified_rows_sample', 'N/A')}")
        print(f"     Typedness Gap Sum:      {costs.get('typedness_gap_sum', 'N/A')}")
        print(f"     Header Match:           {costs.get('header_match', 'N/A')}")

        # ILP alternatives
        ilp = table.get("ilp")
        if ilp:
            print(f"\n  🔍 ILP ANALYSIS:")

            # Best (use safe_get_dict to handle None)
            best = safe_get_dict(ilp, "best", None)
            if best:
                best_certified = safe_get(best, 'certified_rows_sample', 0)
                print(f"\n     ✓ BEST MAPPING (Certified: {best_certified}):")
                best_column_map = safe_get_dict(best, "column_map", {})
                for role, col_idx in sorted(best_column_map.items(), key=lambda x: x[1]):
                    print(f"        Column {col_idx} → {role}")

            # Runner-up (use safe_get_dict to handle None)
            runner_up = safe_get_dict(ilp, "runner_up", None)
            if runner_up:
                runner_up_certified = safe_get(runner_up, 'certified_rows_sample', 0)
                print(f"\n     ⚠ RUNNER-UP (Certified: {runner_up_certified}):")
                runner_up_column_map = safe_get_dict(runner_up, "column_map", {})
                for role, col_idx in sorted(runner_up_column_map.items(), key=lambda x: x[1]):
                    print(f"        Column {col_idx} → {role}")

            # Compare (safe access to best)
            chosen_certified = costs.get('certified_rows_sample', 0)
            best_certified = safe_get(best, 'certified_rows_sample', 0) if best else 0
            delta = best_certified - chosen_certified

            if delta > 0:
                print(f"\n     ⚡ ILP ADVANTAGE: +{delta} certified rows vs chosen mapping")
            elif delta < 0:
                print(f"\n     ✅ Chosen mapping is optimal ({abs(delta)} better than ILP best)")
            else:
                print(f"\n     🟰 Chosen mapping ties with ILP best")


def visualize_evidence_plane(evidence: Dict[str, Any]):
    """Display evidence plane data including swap energy."""
    print_header("🔬 EVIDENCE PLANE")

    tables = evidence.get("tables", [])
    if not tables:
        print("\n  No evidence data.")
        return

    for table in tables:
        print_subheader(f"Table {table.get('table_id', 'unknown')}")

        # Dimensions
        print(f"\n  Dimensions: {table.get('rows', 0)} rows × {table.get('cols', 0)} columns")

        # Typedness
        typedness = table.get("typedness", {})
        print(f"\n  📊 COLUMN TYPEDNESS:")
        for role, scores in typedness.items():
            if not scores:
                continue
            cols_with_score = [(i, s) for i, s in enumerate(scores) if s > 0.05]
            if cols_with_score:
                print(f"     {role:>6}: " + ", ".join(f"col{i}={s:.2f}" for i, s in cols_with_score))

        # Swap energy
        swap_energy = table.get("swap_energy")
        if swap_energy:
            print(f"\n  ⚡ SWAP ENERGY (adjacent column swaps):")
            for swap in swap_energy:
                cols = swap.get("cols", [])
                delta = swap.get("delta_certified", 0)
                symbol = "+" if delta > 0 else ("-" if delta < 0 else "=")
                print(f"     Swap columns {cols[0]} ↔ {cols[1]}: {symbol:>2} {abs(delta)} certified rows")

        # Quantization
        quant = table.get("quantization", {})
        print(f"\n  🔐 QUANTIZATION:")
        print(f"     Row Method: {quant.get('row_method', 'N/A')}")
        print(f"     Col Method: {quant.get('col_method', 'N/A')}")
        print(f"     Hash:       {quant.get('hash', 'N/A')[:32]}...")


def visualize_solver_results(row_cert: Dict[str, Any], table_cert: Dict[str, Any]):
    """Display solver certification results."""
    print_header("⚖️  SOLVER CERTIFICATION")

    # Table-level metrics
    print_subheader("Table-Level Metrics")
    print(f"\n  Certified Rows:         {table_cert.get('certified_rows', 0)}")
    print(f"  Row Locked %:           {table_cert.get('row_locked_pct', 0.0)*100:.1f}%")
    print(f"  Conservation:           {table_cert.get('conservation', 0.0)*100:.1f}%")
    print(f"  Inequality Violations:  {table_cert.get('inequality_violations', 0)}")

    # Row-level details
    rows = row_cert.get("rows", [])
    if rows:
        print_subheader("Row-Level Certification")

        certified = [r for r in rows if r.get("certified")]
        print(f"\n  {len(certified)}/{len(rows)} rows certified")

        # Law distribution
        law_counts = {}
        for r in rows:
            law = r.get("law", "unknown")
            law_counts[law] = law_counts.get(law, 0) + 1

        print(f"\n  Law Distribution:")
        for law, count in sorted(law_counts.items()):
            pct = (count / len(rows)) * 100 if rows else 0
            cert_count = sum(1 for r in rows if r.get("law") == law and r.get("certified"))
            print(f"     {law:>20}: {count:>2} rows ({pct:>5.1f}%) - {cert_count} certified")

        # Violations
        violations = [r for r in rows if r.get("violation_energy", 0) > 0]
        if violations:
            print(f"\n  ⚠  {len(violations)} rows with violations:")
            for r in violations[:5]:  # Show first 5
                print(f"     Row {r.get('row_index', '?')}: "
                      f"law={r.get('law', '?')}, "
                      f"energy={r.get('violation_energy', 0)}")


def visualize_map_metrics(map_metrics: Dict[str, Any]):
    """Display extraction map metrics (NEW doc-level sidecar)."""
    print_header("🗺️  EXTRACTION MAP METRICS")

    if not map_metrics:
        print("\n  No map metrics data available.")
        return

    # Role coverage
    role_coverage = safe_get_float(map_metrics, "role_coverage", 0.0)
    print(f"\n  Role Coverage:       {role_coverage*100:.1f}% (fraction of target roles mapped)")

    # Map completeness
    map_completeness = safe_get_int(map_metrics, "map_completeness", 0)
    print(f"  Map Completeness:    {map_completeness} roles mapped")

    # Anchors per row histogram
    anchors_histogram = safe_get_list(map_metrics, "anchors_per_row", [])
    if anchors_histogram:
        # Find mode (most common anchors count)
        mode_idx = anchors_histogram.index(max(anchors_histogram)) if anchors_histogram else 0
        print(f"\n  Anchors Per Row (histogram): {anchors_histogram}")
        print(f"  Mode:                {mode_idx} anchors/row (most common)")


def visualize_selection_evidence(selection_evidence: Dict[str, Any]):
    """Display selection evidence (NEW doc-level sidecar)."""
    print_header("🎯 SELECTION EVIDENCE")

    if not selection_evidence:
        print("\n  No selection evidence data available.")
        return

    # Chosen variant
    chosen = safe_get(selection_evidence, "chosen", "unknown")
    print(f"\n  Chosen Variant:      {chosen.upper()}")

    # Mapper header match (if mapper chosen)
    if chosen == "mapper":
        mapper_header_match = safe_get_float(selection_evidence, "mapper_header_match", 0.0)
        print(f"  Mapper Header Match: {mapper_header_match*100:.1f}%")

    # Conservation samples
    conservation_samples = safe_get_list(selection_evidence, "conservation_samples", [])
    if conservation_samples:
        print(f"\n  Conservation Samples ({len(conservation_samples)} tables):")
        for i, sample in enumerate(conservation_samples[:3], 1):  # Show first 3
            table_id = safe_get(sample, "table_id", f"table_{i}")
            conservation = safe_get_float(sample, "conservation", 0.0)
            print(f"     {table_id}: {conservation*100:.1f}%")


def visualize_budget_stability(budget: Dict[str, Any], stability: Dict[str, Any]):
    """Display budget and stability metrics."""
    print_header("📈 PERFORMANCE METRICS")

    if budget:
        print_subheader("Budget")
        print(f"\n  OCR Calls per Page:  {budget.get('ocr_calls_per_page', 0.0):.2f}")
        print(f"  P95 Seconds/Page:    {budget.get('p95_s_per_page', 0.0):.2f}s")

    if stability:
        print_subheader("Byte Stability")
        print(f"\n  Schema:              {stability.get('schema', 'N/A')}")
        print(f"  Stable:              {'✓' if stability.get('stable') else '✗'}")


def main():
    if len(sys.argv) < 2:
        print("Usage: visualize_extraction.py <output_dir>")
        print("\nExample:")
        print("  python3 scripts/visualize_extraction.py out/visual_demo")
        sys.exit(1)

    outdir = Path(sys.argv[1])
    if not outdir.exists():
        print(f"Error: Directory {outdir} does not exist")
        sys.exit(1)

    # Find EOB JSON files
    eob_files = list(outdir.glob("*.json"))
    eob_files = [f for f in eob_files if not any(
        suffix in f.name for suffix in ['.ccert.', '.proof.', '.evidence.',
                                        '.budget.', '.stability.', '.summary_box.',
                                        '.conservation.', '.coherence.', '.sha256']
    )]

    if not eob_files:
        print(f"Error: No EOB JSON files found in {outdir}")
        sys.exit(1)

    # Process first EOB file
    eob_path = eob_files[0]
    print(f"\n🔍 Analyzing: {eob_path.name}")

    # Load all related files
    eob = load_json(eob_path)

    stem = eob_path.stem
    assign_proof_path = outdir / f"{stem}.assign.proof.v1.json"
    evidence_path = outdir / f"{stem}.evidence.v1.json"
    row_cert_path = outdir / f"{stem}.row.ccert.v1.json"
    table_cert_path = outdir / f"{stem}.table.ccert.v1.json"
    budget_path = outdir / f"{stem}.budget.v1.json"
    stability_path = outdir / f"{stem}.stability.v1.json"
    map_metrics_path = outdir / f"{stem}.extraction.map_metrics.v1.json"
    selection_evidence_path = outdir / f"{stem}.selection.evidence.v1.json"

    # Visualize each section
    visualize_document_info(eob)
    visualize_services_table(eob)

    # NEW: Doc-level rollup sidecars (CP13)
    if map_metrics_path.exists():
        map_metrics = load_json(map_metrics_path)
        visualize_map_metrics(map_metrics)

    if selection_evidence_path.exists():
        selection_evidence = load_json(selection_evidence_path)
        visualize_selection_evidence(selection_evidence)

    if assign_proof_path.exists():
        assign_proof = load_json(assign_proof_path)
        visualize_column_assignments(assign_proof)

    if evidence_path.exists():
        evidence = load_json(evidence_path)
        visualize_evidence_plane(evidence)

    if row_cert_path.exists() and table_cert_path.exists():
        row_cert = load_json(row_cert_path)
        table_cert = load_json(table_cert_path)
        visualize_solver_results(row_cert, table_cert)

    if budget_path.exists() or stability_path.exists():
        budget = load_json(budget_path) if budget_path.exists() else {}
        stability = load_json(stability_path) if stability_path.exists() else {}
        visualize_budget_stability(budget, stability)

    print("\n" + "═" * 100)
    print("  ✓ Visualization Complete")
    print("═" * 100 + "\n")


if __name__ == "__main__":
    main()
