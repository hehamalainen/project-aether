#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Global Config
export SOAK_REPOS=10
export SOAK_RUN_SECONDS="${SOAK_RUN_SECONDS:-1800}"
export SOAK_WITH_GATES="${SOAK_WITH_GATES:-1}"

DENSITIES=(1 2 3 4 5)
LANES=("cas" "advisory")

echo "==========================================================="
echo "  Starting Sprint 4 Dual-Lane Bakeoff"
echo "  Repos: $SOAK_REPOS | Run: ${SOAK_RUN_SECONDS}s per cell"
echo "==========================================================="

# Ensure clean results file
RESULTS_FILE="$PROJECT_DIR/docs/sprint4_bakeoff_results.md"
mkdir -p "$PROJECT_DIR/docs"

for density in "${DENSITIES[@]}"; do
    for lane in "${LANES[@]}"; do
        export SOAK_AGENTS=$((density * SOAK_REPOS))
        
        if [[ "$lane" == "advisory" ]]; then
            export SOAK_CRDT_ENABLED=1
        else
            export SOAK_CRDT_ENABLED=0
        fi
        
        export AETHER_SOAK_HOME="$HOME/.aether/soak_pg_sprint4/${lane}_d${density}"
        echo ""
        echo "==========================================================="
        echo "  Running Cell: Lane=$lane | Density=$density ($SOAK_AGENTS agents)"
        echo "  Home: $AETHER_SOAK_HOME"
        echo "==========================================================="
        
        # Execute the harness
        "$SCRIPT_DIR/soak_native_rust_pg.sh" || {
            echo "❌ Cell $lane/$density failed. Halting bakeoff."
            exit 1
        }
        
        # Extract telemetry
        python3 "$SCRIPT_DIR/aggregate_sprint4_metrics.py" \
            --home "$AETHER_SOAK_HOME" \
            --lane "$lane" \
            --density "$density" \
            >> "$RESULTS_FILE"
    done
done

echo ""
echo "✅ Sprint 4 Bakeoff completed successfully!"
echo "Results written to: $RESULTS_FILE"
