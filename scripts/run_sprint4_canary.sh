#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

export SOAK_REPOS=2
export SOAK_RUN_SECONDS=60
export SOAK_WITH_GATES=1

DENSITIES=(1)
LANES=("cas" "advisory")

RESULTS_FILE="$PROJECT_DIR/docs/sprint4_bakeoff_results.md"
mkdir -p "$PROJECT_DIR/docs"

echo "| Lane | Density (Agents/Repo) | Submit OK | Promote OK | 409 Rate | Inv. State Rej | P99 Latency | P99 Wait / Hold | Max Q | Max Idle | Violations |" > "$RESULTS_FILE"
echo "|---|---|---|---|---|---|---|---|---|---|---|" >> "$RESULTS_FILE"

for density in "${DENSITIES[@]}"; do
    for lane in "${LANES[@]}"; do
        export SOAK_AGENTS=$((density * SOAK_REPOS))
        
        if [[ "$lane" == "advisory" ]]; then
            export SOAK_CRDT_ENABLED=1
        else
            export SOAK_CRDT_ENABLED=0
        fi
        
        export AETHER_SOAK_HOME="$HOME/.aether/soak_pg_sprint4_canary/${lane}_d${density}"
        echo ""
        echo "==========================================================="
        echo "  Running CANARY Cell: Lane=$lane | Density=$density ($SOAK_AGENTS agents)"
        echo "  Home: $AETHER_SOAK_HOME"
        echo "==========================================================="
        
        "$SCRIPT_DIR/soak_native_rust_pg.sh" || {
            echo "❌ Cell $lane/$density failed."
        }
        
        python3 "$SCRIPT_DIR/aggregate_sprint4_metrics.py" \
            --home "$AETHER_SOAK_HOME" \
            --lane "$lane" \
            --density "$density" \
            >> "$RESULTS_FILE"
    done
done

echo ""
echo "✅ Canary Bakeoff completed!"
cat "$RESULTS_FILE"
