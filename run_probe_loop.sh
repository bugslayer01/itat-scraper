#!/usr/bin/env bash
# Runs the rate limit probe in a loop. Each run has a 9-minute timeout.
# If it times out (no limit found yet), it auto-restarts from where it
# left off. Stops when the probe finds the limit or is killed.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="$SCRIPT_DIR/.probe_state.json"
TIMEOUT=540  # 9 minutes (leaves margin within 10-min tool limit)
RUN=1

echo "=== Rate Limit Probe Loop ==="
echo "Each run: ${TIMEOUT}s timeout, auto-resumes from saved state"
echo ""

while true; do
    echo "--- Run #${RUN} starting ---"
    timeout "$TIMEOUT" "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/rate_limit_test.py"
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 0 ]; then
        echo ""
        echo "=== Probe found the rate limit! ==="
        cat "$STATE_FILE" 2>/dev/null
        break
    elif [ $EXIT_CODE -eq 124 ]; then
        echo ""
        echo "--- Run #${RUN} timed out after ${TIMEOUT}s ---"
        if [ -f "$STATE_FILE" ]; then
            RATE=$(python3 -c "import json; print(json.load(open('$STATE_FILE'))['resume_rate'])" 2>/dev/null)
            SAFE=$(python3 -c "import json; print(json.load(open('$STATE_FILE'))['max_safe_rate'])" 2>/dev/null)
            echo "  Last tested: ${RATE} req/hr | Max safe so far: ${SAFE} req/hr"
        fi
        echo "  Restarting from saved state in 3s..."
        sleep 3
        RUN=$((RUN + 1))
    else
        echo ""
        echo "--- Run #${RUN} exited with code ${EXIT_CODE} ---"
        echo "Stopping loop."
        break
    fi
done
