//+------------------------------------------------------------------+
//| CandleExporter.mq5                                               |
//| Writes the last N closed M5 candles to a JSON file every 30 s.  |
//| The Python poller reads this file and SSH-pushes to Ubuntu.      |
//+------------------------------------------------------------------+
#property copyright "XAUUSD Bot"
#property version   "1.0"
#property strict

input int    LOOKBACK      = 7;                    // bars to fetch (last one is still-forming, dropped)
input string OUTPUT_FILE   = "xauusd_candles.json"; // written to MQL5\Files\ (no FILE_COMMON)
input int    TIMER_SECONDS = 30;

MqlRates g_rates[];

int OnInit() {
    ArraySetAsSeries(g_rates, true);
    EventSetTimer(TIMER_SECONDS);
    ExportCandles();
    return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) {
    EventKillTimer();
}

void OnTimer() {
    ExportCandles();
}

void ExportCandles() {
    int copied = CopyRates(_Symbol, PERIOD_M5, 0, LOOKBACK, g_rates);
    if (copied < 2) return;

    // g_rates[0] = most recent (may still be forming), g_rates[1..] = closed
    // drop g_rates[0]; send g_rates[1] through g_rates[copied-1]
    string json = "{\"symbol\":\"" + _Symbol + "\",\"tf\":\"M5\",\"candles\":[";
    for (int i = copied - 1; i >= 1; i--) {
        if (i < copied - 1) json += ",";
        // bar.time = open time in broker local time (UTC+3 summer / UTC+2 winter)
        // Python handles UTC conversion using the same DST rule as the backfill
        json += StringFormat(
            "{\"broker_ts\":%d,\"open\":%.5f,\"high\":%.5f,\"low\":%.5f,\"close\":%.5f,\"tick_volume\":%d,\"spread\":%d,\"real_volume\":%d}",
            (int)g_rates[i].time,
            g_rates[i].open, g_rates[i].high, g_rates[i].low, g_rates[i].close,
            (int)g_rates[i].tick_volume, (int)g_rates[i].spread, (int)g_rates[i].real_volume
        );
    }
    json += "]}";

    int handle = FileOpen(OUTPUT_FILE, FILE_WRITE | FILE_ANSI);
    if (handle == INVALID_HANDLE) {
        Print("CandleExporter: FileOpen failed, error ", GetLastError());
        return;
    }
    FileWriteString(handle, json);
    FileClose(handle);
}
