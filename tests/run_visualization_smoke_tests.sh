#!/bin/bash
# Visualization smoke tests for Plan 4 (CP13 Hardening)
# Tests null-safe handling of edge cases

set -e  # Exit on first failure

FIXTURES_DIR="tests/fixtures"
PASS=0
FAIL=0

echo "Running visualization smoke tests..."
echo ""

# Test 1: Null ILP (line 148 crash point)
echo "Test 1: Null ILP best/runner_up"
if python3 scripts/visualize_extraction.py "$FIXTURES_DIR/crash_test_null_ilp" > /dev/null 2>&1; then
  echo "  ✓ PASS"
  ((PASS++))
else
  echo "  ✗ FAIL (crashed)"
  ((FAIL++))
fi

# Test 2: Empty column_map (line 64 crash point)
echo "Test 2: Empty column_map"
if python3 scripts/visualize_extraction.py "$FIXTURES_DIR/crash_test_empty_column_map" > /dev/null 2>&1; then
  echo "  ✓ PASS"
  ((PASS++))
else
  echo "  ✗ FAIL (crashed)"
  ((FAIL++))
fi

# Test 3: Missing NEW sidecars (graceful degradation)
echo "Test 3: Missing NEW sidecars (map_metrics, selection.evidence)"
if python3 scripts/visualize_extraction.py "$FIXTURES_DIR/crash_test_missing_new_sidecars" > /dev/null 2>&1; then
  echo "  ✓ PASS"
  ((PASS++))
else
  echo "  ✗ FAIL (crashed)"
  ((FAIL++))
fi

# Test 4: visualize_table_diagram with empty column_map
echo "Test 4: visualize_table_diagram with empty column_map"
if python3 scripts/visualize_table_diagram.py "$FIXTURES_DIR/crash_test_empty_column_map" > /dev/null 2>&1; then
  echo "  ✓ PASS"
  ((PASS++))
else
  echo "  ✗ FAIL (crashed)"
  ((FAIL++))
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"

if [ $FAIL -eq 0 ]; then
  echo "✓ All smoke tests passed"
  exit 0
else
  echo "✗ Some tests failed"
  exit 1
fi
