#!/bin/bash
BASE="http://172.18.0.1:8000"
AS_OF=$(python3 -c "import datetime, urllib.parse; print(urllib.parse.quote(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')))")
SYMBOL="XAUUSD%40"  # XAUUSD@ URL-encoded

# lookback_bars: each module gets only the recent window it actually needs.
# None (omitted) would load all 213k candles — avoided here to prevent OOM.
# replay.py and full-rebuild scripts call the Python API directly with
# lookback_bars=None (full history); HTTP callers here always pass a number.
curl -s -X POST "${BASE}/levels/compute?symbol=${SYMBOL}&tf=M5&as_of=${AS_OF}&lookback_bars=30000" \
    --max-time 60 --silent --output /dev/null &

curl -s -X POST "${BASE}/patterns/compute?symbol=${SYMBOL}&tf=M5&as_of=${AS_OF}&lookback_bars=1000" \
    --max-time 30 --silent --output /dev/null &

curl -s -X POST "${BASE}/round_numbers/compute?symbol=${SYMBOL}&tf=M5&as_of=${AS_OF}&lookback_bars=500" \
    --max-time 30 --silent --output /dev/null &

curl -s -X POST "${BASE}/gaps/compute?symbol=${SYMBOL}&tf=M5&as_of=${AS_OF}&lookback_bars=3000" \
    --max-time 30 --silent --output /dev/null &

curl -s -X POST "${BASE}/fibonacci/compute?symbol=${SYMBOL}&tf=M5&as_of=${AS_OF}&lookback_bars=500" \
    --max-time 30 --silent --output /dev/null &

# regime/incremental: O(1025 bars) — safe to run every 5 min
curl -s -X POST "${BASE}/regime/compute/incremental?symbol=${SYMBOL}&tf=M5&as_of=${AS_OF}" \
    --max-time 60 --silent --output /dev/null &

wait
