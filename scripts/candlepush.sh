#!/bin/bash
# Decode a base64 JSON payload from SSH_ORIGINAL_COMMAND and POST it to the
# matching FastAPI endpoint. authorized_keys uses command= to force this
# script for the candlepush@windows-mt5-bridge key -- SSH_ORIGINAL_COMMAND
# is whatever the Windows-side client tried to run, e.g.
#   "send_candles <BASE64_JSON>"                  (every 5 min, XAUUSD@ live ingest)
#   "send_rollover_check <BASE64_JSON>"            (daily, docs/instrument_rollover.md)
# Extended 2026-09-18 to dispatch on the first word instead of assuming
# send_candles always -- same restricted key, same no-pty/no-forwarding
# constraints in authorized_keys, no SSH access change needed.
set -euo pipefail
CMD=$(echo "$SSH_ORIGINAL_COMMAND" | awk '{print $1}')
ENCODED=$(echo "$SSH_ORIGINAL_COMMAND" | awk '{print $2}')
if [ -z "$ENCODED" ]; then
    echo "error: no payload" >&2
    exit 1
fi
PAYLOAD=$(echo "$ENCODED" | base64 -d)

case "$CMD" in
    send_candles)
        exec curl -s -X POST http://172.18.0.1:8000/ingest/candles \
            -H "Content-Type: application/json" \
            --data-raw "$PAYLOAD"
        ;;
    send_rollover_check)
        exec curl -s -X POST http://172.18.0.1:8000/instruments/rollover-check \
            -H "Content-Type: application/json" \
            --data-raw "$PAYLOAD"
        ;;
    *)
        echo "error: unknown command '$CMD'" >&2
        exit 1
        ;;
esac
