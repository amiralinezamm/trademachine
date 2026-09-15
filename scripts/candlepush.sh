#!/bin/bash
# Decode base64 candle payload from SSH_ORIGINAL_COMMAND and POST to ingest API.
# authorized_keys uses command= to force this script;
# SSH_ORIGINAL_COMMAND = what the client tried to run, e.g. "send_candles <BASE64_JSON>"
set -euo pipefail
ENCODED=$(echo "$SSH_ORIGINAL_COMMAND" | awk '{print $2}')
if [ -z "$ENCODED" ]; then
    echo "error: no payload" >&2
    exit 1
fi
PAYLOAD=$(echo "$ENCODED" | base64 -d)
exec curl -s -X POST http://172.18.0.1:8000/ingest/candles \
    -H "Content-Type: application/json" \
    --data-raw "$PAYLOAD"
