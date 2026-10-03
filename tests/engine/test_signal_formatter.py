"""Pure tests for src/engine/signal_formatter.py — no DB, no network.

Covers the 2026-10-03 message-formatting revamp: fixed section order,
TP/SL isolated in a quote block, the price at signal time (and, when
known, the current price) reported explicitly.
"""
from src.engine.signal_formatter import (
    ALERT_DISCLAIMER,
    build_reversal_advisory_text,
    build_signal_alert_text,
)


def _signal(**overrides):
    base = {
        "direction": "BUY",
        "entry": 2350.123,
        "stop_loss": 2340.5,
        "take_profit": 2365.75,
        "ts_utc": "2026-10-03T10:05:00+00:00",
        "rule_version": "level_reversion_v1",
        "components": {
            "symbol": "XAUUSD@",
            "level_kind": "support",
            "level_price_low": 2340.0,
            "level_price_high": 2345.0,
            "level_strength": 1.75,
            "atr": 8.5,
            "net_votes": 3,
            "sl_tp": {"rr": 2.3, "relaxed": False},
            "market_structure": "bullish",
        },
    }
    base.update(overrides)
    return base


def test_section_order_is_price_then_quoted_sl_tp_then_context():
    msg = build_signal_alert_text(_signal())
    price_i = msg.index("قیمت در لحظه‌ی صدور")
    quote_i = msg.index("<blockquote>")
    tp_i = msg.index("حد سود")
    sl_i = msg.index("حد ضرر")
    rr_i = msg.index("نسبت سود:ضرر")
    close_quote_i = msg.index("</blockquote>")
    level_i = msg.index("سطح برخوردی")
    assert price_i < quote_i < tp_i < sl_i < rr_i < close_quote_i < level_i


def test_sl_tp_are_inside_the_quote_block():
    msg = build_signal_alert_text(_signal())
    quote_body = msg[msg.index("<blockquote>") : msg.index("</blockquote>")]
    assert "2340.500" in quote_body  # stop_loss
    assert "2365.750" in quote_body  # take_profit


def test_buy_direction_label_and_arrow():
    msg = build_signal_alert_text(_signal())
    assert "🟢" in msg
    assert "خرید" in msg


def test_sell_direction_label_and_arrow():
    msg = build_signal_alert_text(_signal(direction="SELL"))
    assert "🔴" in msg
    assert "فروش" in msg


def test_relaxed_rr_gets_the_note():
    sig = _signal()
    sig["components"]["sl_tp"]["relaxed"] = True
    msg = build_signal_alert_text(sig)
    assert "اطمینان رأی‌گیری بالا" in msg


def test_missing_fields_degrade_to_placeholder_not_crash():
    msg = build_signal_alert_text({"direction": "BUY", "entry": 2350.0,
                                    "components": {}})
    assert "?" in msg
    assert "خرید" in msg  # direction label still resolved from a bare dict


def test_current_price_shown_with_floating_move_for_open_signal():
    sig = _signal(current_price=2355.0)  # +4.877 for a BUY from 2350.123
    msg = build_signal_alert_text(sig)
    assert "قیمت فعلی" in msg
    assert "+4.88" in msg


def test_current_price_omitted_once_signal_has_an_outcome():
    """A closed signal's 'current price' is stale/meaningless -- showing a
    floating-move line next to a TP/SL outcome would be misleading."""
    sig = _signal(current_price=2355.0, outcome="tp")
    msg = build_signal_alert_text(sig)
    assert "قیمت فعلی" not in msg
    assert "TP" in msg


def test_disclaimer_present_and_last():
    msg = build_signal_alert_text(_signal())
    assert msg.rstrip().endswith(ALERT_DISCLAIMER)


def test_rule_version_and_tehran_time_present():
    msg = build_signal_alert_text(_signal())
    assert "level_reversion_v1" in msg
    assert "ساعت" in msg


# ---------------------------------------------------------------------------
# build_reversal_advisory_text
# ---------------------------------------------------------------------------


def _advisory(**overrides):
    base = {
        "signal_id": 1, "direction": "BUY", "entry": 2350.0,
        "ts_utc": "2026-10-03T09:00:00+00:00", "structure": "bearish",
        "message": "توقف اجباری: ...",
    }
    base.update(overrides)
    return base


def test_reversal_advisory_reports_entry_and_current_price_with_sign():
    msg = build_reversal_advisory_text(_advisory(), current_price=2345.0)
    assert "قیمت ورود" in msg
    assert "قیمت فعلی" in msg
    assert "-5.00" in msg  # BUY, price dropped 5 -> negative move


def test_reversal_advisory_sell_direction_move_sign_mirrors():
    adv = _advisory(direction="SELL", entry=2350.0)
    msg = build_reversal_advisory_text(adv, current_price=2345.0)
    assert "+5.00" in msg  # SELL benefits from price dropping


def test_reversal_advisory_without_current_price_still_renders():
    msg = build_reversal_advisory_text(_advisory())
    assert "قیمت ورود" in msg
    assert "قیمت فعلی" not in msg


def test_reversal_advisory_has_manual_close_instruction_and_disclaimer():
    msg = build_reversal_advisory_text(_advisory())
    assert "دستی ببندید" in msg
    assert msg.rstrip().endswith(ALERT_DISCLAIMER)
