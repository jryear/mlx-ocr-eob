#!/usr/bin/env python3
"""
Analyze Stage 3 KPIs across holdout corpus.

Collects:
- certified_rows, conservation, row_locked_pct
- applied_for_extraction rate
- map_completeness distribution
- anchors_per_row mode
- role_coverage
- selection chosen rates
- summary residuals
"""

import json
import sys
from pathlib import Path
from collections import Counter, defaultdict
from typing import Dict, List, Any
import statistics

def load_json(path: Path) -> Any:
    """Load JSON file."""
    with open(path, 'r') as f:
        return json.load(f)

def analyze_output_dir(out_dir: Path) -> Dict[str, Any]:
    """Analyze all .json files in output directory."""

    # Metrics to collect
    metrics = {
        'total_docs': 0,
        'total_services': 0,
        'certified_rows_total': 0,
        'services_with_conservation': [],
        'row_locked_pct': [],
        'applied_for_extraction_count': 0,
        'total_tables': 0,
        'map_completeness_counts': Counter(),
        'anchors_per_row': [],
        'role_coverage': defaultdict(int),
        'selection_chosen': Counter(),
        'summary_residuals': defaultdict(list),
        'fallback_count': 0,
        'docs_with_data': 0,
        'payer_distribution': Counter(),
    }

    # Process each JSON file (skip sidecars)
    for json_file in sorted(out_dir.glob('*.json')):
        # Skip sidecar files (they have extra dots before .json)
        if json_file.stem.count('.') > 0:
            continue

        metrics['total_docs'] += 1

        try:
            doc = load_json(json_file)
        except Exception as e:
            print(f"Error reading {json_file.name}: {e}", file=sys.stderr)
            continue

        # Check if fallback (no real data)
        services = doc.get('services', [])
        if services and services[0].get('_source') == 'fallback':
            metrics['fallback_count'] += 1
            continue

        if services:
            metrics['docs_with_data'] += 1

        metrics['total_services'] += len(services)

        # Payer distribution
        payer = doc.get('claim', {}).get('payer_name', 'unknown')
        metrics['payer_distribution'][payer] += 1

        # Check for conservation sidecar
        conservation_path = json_file.with_suffix('').with_suffix('.conservation.json')
        if conservation_path.exists():
            try:
                cons_data = load_json(conservation_path)
                cons_rate = cons_data.get('conservation_rate')
                if cons_rate is not None:
                    metrics['services_with_conservation'].append(cons_rate)
            except Exception as e:
                print(f"Error reading conservation for {json_file.name}: {e}", file=sys.stderr)

        # Check for map_metrics sidecar
        map_metrics_path = json_file.with_suffix('').with_suffix('.extraction.map_metrics.v1.json')
        if map_metrics_path.exists():
            try:
                map_metrics = load_json(map_metrics_path)
                tables = map_metrics.get('tables', [])

                for tbl in tables:
                    metrics['total_tables'] += 1

                    # Applied for extraction
                    if tbl.get('applied_for_extraction', False):
                        metrics['applied_for_extraction_count'] += 1

                    # Map completeness
                    mc = tbl.get('map_completeness')
                    if mc is not None:
                        metrics['map_completeness_counts'][mc] += 1

                    # Anchors per row (expand dict to individual values)
                    apr_dict = tbl.get('anchors_per_row')
                    if apr_dict and isinstance(apr_dict, dict):
                        for anchor_count_str, frequency in apr_dict.items():
                            try:
                                anchor_count = int(anchor_count_str)
                                metrics['anchors_per_row'].extend([anchor_count] * frequency)
                            except (ValueError, TypeError):
                                pass

                    # Role coverage
                    role_cov = tbl.get('role_coverage', {})
                    for role, has_it in role_cov.items():
                        if has_it:
                            metrics['role_coverage'][role] += 1
            except Exception as e:
                print(f"Error reading map_metrics for {json_file.name}: {e}", file=sys.stderr)

        # Check for selection evidence
        evidence_path = json_file.with_suffix('').with_suffix('.selection.evidence.v1.json')
        if evidence_path.exists():
            try:
                evidence = load_json(evidence_path)
                tables = evidence.get('tables', [])

                for tbl in tables:
                    chosen = tbl.get('chosen')
                    if chosen:
                        metrics['selection_chosen'][chosen] += 1
            except Exception as e:
                print(f"Error reading evidence for {json_file.name}: {e}", file=sys.stderr)

        # Check for summary
        summary_path = json_file.with_suffix('').with_suffix('.summary.v1.json')
        if summary_path.exists():
            try:
                summary = load_json(summary_path)
                residuals = summary.get('residuals', {})
                for key, val in residuals.items():
                    if isinstance(val, (int, float)):
                        metrics['summary_residuals'][key].append(val)
            except Exception as e:
                print(f"Error reading summary for {json_file.name}: {e}", file=sys.stderr)

    return metrics

def print_report(metrics: Dict[str, Any]) -> None:
    """Print KPI report."""

    print("=" * 80)
    print("STAGE 3 KPI REPORT")
    print("=" * 80)
    print()

    print(f"Total documents processed: {metrics['total_docs']}")
    print(f"Documents with data: {metrics['docs_with_data']}")
    print(f"Fallback documents (no extraction): {metrics['fallback_count']}")
    print()

    print("Payer Distribution:")
    for payer, count in metrics['payer_distribution'].most_common():
        print(f"  {payer}: {count}")
    print()

    print(f"Total services: {metrics['total_services']}")
    print(f"Total certified rows: {metrics['certified_rows_total']}")
    print()

    # Conservation
    if metrics['services_with_conservation']:
        cons = metrics['services_with_conservation']
        print("Conservation Rate:")
        print(f"  Mean: {statistics.mean(cons):.2%}")
        print(f"  Median: {statistics.median(cons):.2%}")
        print(f"  Min: {min(cons):.2%}")
        print(f"  Max: {max(cons):.2%}")
        print(f"  Services with conservation data: {len(cons)}")
    else:
        print("Conservation Rate: No data")
    print()

    # Row locked percentage
    if metrics['row_locked_pct']:
        locked = metrics['row_locked_pct']
        print("Row Locked Percentage:")
        print(f"  Mean: {statistics.mean(locked):.2%}")
        print(f"  Median: {statistics.median(locked):.2%}")
    else:
        print("Row Locked Percentage: No data")
    print()

    # Applied for extraction rate
    print(f"Total tables: {metrics['total_tables']}")
    if metrics['total_tables'] > 0:
        applied_rate = metrics['applied_for_extraction_count'] / metrics['total_tables']
        print(f"Applied for extraction: {metrics['applied_for_extraction_count']} ({applied_rate:.1%})")
    else:
        print("Applied for extraction: No tables found")
    print()

    # Map completeness
    if metrics['map_completeness_counts']:
        print("Map Completeness Distribution:")
        for mc in sorted(metrics['map_completeness_counts'].keys()):
            count = metrics['map_completeness_counts'][mc]
            pct = count / metrics['total_tables'] * 100 if metrics['total_tables'] > 0 else 0
            print(f"  {mc}: {count} ({pct:.1f}%)")

        # Calculate median and p95
        all_mc = []
        for mc, count in metrics['map_completeness_counts'].items():
            all_mc.extend([mc] * count)
        if all_mc:
            print(f"  Median: {statistics.median(all_mc)}")
            all_mc_sorted = sorted(all_mc)
            p95_idx = int(len(all_mc_sorted) * 0.95)
            print(f"  P95: {all_mc_sorted[p95_idx] if p95_idx < len(all_mc_sorted) else all_mc_sorted[-1]}")
    else:
        print("Map Completeness: No data")
    print()

    # Anchors per row
    if metrics['anchors_per_row']:
        apr = metrics['anchors_per_row']
        print("Anchors Per Row:")
        print(f"  Mean: {statistics.mean(apr):.2f}")
        print(f"  Median: {statistics.median(apr):.2f}")
        print(f"  Mode: {statistics.mode(apr):.2f}")
    else:
        print("Anchors Per Row: No data")
    print()

    # Role coverage
    if metrics['role_coverage']:
        print("Role Coverage (tables with role):")
        total_tables = metrics['total_tables']
        for role in ['charged_amount', 'allowed_amount', 'insurance_paid', 'patient_responsibility']:
            count = metrics['role_coverage'].get(role, 0)
            pct = count / total_tables * 100 if total_tables > 0 else 0
            print(f"  {role}: {count}/{total_tables} ({pct:.1f}%)")
    else:
        print("Role Coverage: No data")
    print()

    # Selection chosen
    if metrics['selection_chosen']:
        print("Selection Chosen:")
        total = sum(metrics['selection_chosen'].values())
        for chosen, count in metrics['selection_chosen'].most_common():
            pct = count / total * 100 if total > 0 else 0
            print(f"  {chosen}: {count} ({pct:.1f}%)")
    else:
        print("Selection Chosen: No data")
    print()

    # Summary residuals
    if metrics['summary_residuals']:
        print("Summary Residuals:")
        for key, vals in sorted(metrics['summary_residuals'].items()):
            if vals:
                print(f"  {key}: mean={statistics.mean(vals):.2f}, median={statistics.median(vals):.2f}")
    else:
        print("Summary Residuals: No data")
    print()

    print("=" * 80)
    print("GUARDRAIL CHECKS")
    print("=" * 80)
    print()

    # Check guardrails
    warnings = []

    # Applied for extraction rate
    if metrics['total_tables'] > 0:
        applied_rate = metrics['applied_for_extraction_count'] / metrics['total_tables']
        if applied_rate < 0.3:
            warnings.append(f"⚠️  LOW applied_for_extraction rate: {applied_rate:.1%}")

    # Map completeness on applied tables
    if metrics['map_completeness_counts']:
        low_completeness = sum(metrics['map_completeness_counts'][mc] for mc in [0, 1, 2] if mc in metrics['map_completeness_counts'])
        if low_completeness > 0:
            warnings.append(f"⚠️  {low_completeness} tables with map_completeness < 3")

    # Conservation trending
    if metrics['services_with_conservation']:
        mean_cons = statistics.mean(metrics['services_with_conservation'])
        if mean_cons < 0.5:
            warnings.append(f"⚠️  LOW conservation rate: {mean_cons:.1%}")

    if warnings:
        for w in warnings:
            print(w)
    else:
        print("✓ All guardrails passed")
    print()

def main():
    if len(sys.argv) < 2:
        print("Usage: analyze_stage3_kpis.py <output_dir>")
        sys.exit(1)

    out_dir = Path(sys.argv[1])
    if not out_dir.exists():
        print(f"Error: {out_dir} does not exist")
        sys.exit(1)

    metrics = analyze_output_dir(out_dir)
    print_report(metrics)

if __name__ == '__main__':
    main()
