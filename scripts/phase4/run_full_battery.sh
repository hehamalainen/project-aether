#!/bin/bash
set -e

# Export required semantic conflict and synthesis CLI environment flags
export AETHER_SEMANTIC_MERGE_ENABLED=1
export AETHER_SYNTHESIS_WORKER_ENABLED=1

SCENARIOS=("class1" "class2" "class3" "class2_subtle_reject" "class2_obvious_reject" "oracle_false_green" "manifest_failure" "worker_failure" "hotspot")

# Expected outcome classification mapping
get_expected_classification() {
    local scenario=$1
    case "$scenario" in
        "class2_subtle_reject") echo "FAIL_ORACLE_SUFFICIENCY" ;;
        "oracle_false_green") echo "FAIL_ORACLE_SUFFICIENCY" ;;
        *) echo "PASS" ;;
    esac
}

for SCENARIO in "${SCENARIOS[@]}"; do
    echo "========================================"
    echo "Running Scenario: $SCENARIO"
    echo "========================================"
    
    OUT_DIR=".pytest-artifacts/phase4_$SCENARIO"
    rm -rf "$OUT_DIR"
    mkdir -p "$OUT_DIR"
    
    PYTHONPATH=. .venv/bin/python scripts/phase4/run_semantic_cascade_load.py \
        --scenario "$SCENARIO" \
        --agents 10 \
        --worker-mode fake \
        --db-profile sqlite \
        --repo-fixture "$SCENARIO" \
        --output-dir "$OUT_DIR" || {
            echo "❌ Failed to run script for Scenario: $SCENARIO"
            exit 1
        }
        
    # Read outcome and counts using python JSON parser for robustness
    PYTHON_PARSE_JSON="
import json, sys
try:
    res = json.load(open('$OUT_DIR/semantic_oracle_result.json'))
    print(f\"{res.get('classification', 'ERROR')}|{str(res.get('passed', False)).lower()}|{str(res.get('vector_match', False)).lower()}|{json.dumps(res.get('expected_terminal_counts'))}|{json.dumps(res.get('actual_terminal_counts'))}\")
except Exception as e:
    print(f'ERROR|false|false|{{}}|{{}}')
"
    PARSED=$(.venv/bin/python -c "$PYTHON_PARSE_JSON" 2>/dev/null || echo "ERROR|false|false|{}|{}")
    IFS='|' read -r ACTUAL PASSED VECTOR_MATCH EXPECTED_COUNTS ACTUAL_COUNTS <<< "$PARSED"
    EXPECTED_CLASS=$(get_expected_classification "$SCENARIO")
    
    echo "Verification for $SCENARIO:"
    echo "  Expected Classification: $EXPECTED_CLASS"
    echo "  Actual Classification:   $ACTUAL"
    echo "  Expected Row Counts:     $EXPECTED_COUNTS"
    echo "  Actual Row Counts:       $ACTUAL_COUNTS"
    
    if [ "$ACTUAL" != "$EXPECTED_CLASS" ]; then
        echo "❌ Mismatched classification result for scenario: $SCENARIO (got '$ACTUAL', expected '$EXPECTED_CLASS')"
        exit 1
    fi
    if [ "$VECTOR_MATCH" != "true" ]; then
        echo "❌ Mismatched row-state vector for scenario: $SCENARIO (got '$ACTUAL_COUNTS', expected '$EXPECTED_COUNTS')"
        exit 1
    fi
    
    echo "✅ Passed Scenario: $SCENARIO (Classification: $ACTUAL, Row-State Vector: $ACTUAL_COUNTS matched Expected: $EXPECTED_COUNTS)"
done

echo "========================================"
echo "ALL SCENARIOS MATCHED EXPECTED CLASSIFICATIONS & ROW-STATE VECTORS"
echo "========================================"
