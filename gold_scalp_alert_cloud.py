"""
XAU/USD 30-minute scalp signal alert — GitHub Actions version.

Same logic as the Mac version: trend setup, counter-trend fade setup,
automatic open-trade tracking with THREE scaled take-profit levels
(TP1 closes 1/3 + moves stop to breakeven, TP2 closes another 1/3, TP3
closes the rest), and permanent running performance stats (win rate,
expectancy in R-multiples) in every update email. Credentials come from
environment variables (GitHub Secrets). The workflow commits
gold_alert_state.json back to the repo after each run, so trade history
and open positions survive between runs.

Requires: pip install requests
"""

import json
import os
import smtplib
import ssl
from datetime import datetime, date
from email.message import EmailMessage

import requests
TWELVE_DATA_API_KEY = os.environ["TWELVE_DATA_API_KEY"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
TO_EMAIL = os.environ["TO_EMAIL"]

STATE_FILE = os.path.join(os.path.dirname(__file__), "gold_alert_state.json")
PAUSE_FLAG = os.path.join(os.path.dirname(__file__), "in_trade")
HOLD_BARS = 6  # 6 x 5min = 30 minutes

# To pause new trade alerts while you're already in a position, create an
# empty file named "in_trade" in this same folder (e.g. type: touch in_trade).
# Delete that file (rm in_trade) when you're out, to resume getting alerts.


# ---------- Data fetch ----------
def fetch_series(outputsize=300):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": "XAU/USD",
        "interval": "5min",
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY,
    }
    resp = requests.get(url, params=params, timeout=20)
    data = resp.json()
    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError(data.get("message", "Could not fetch price data"))

    values = list(reversed(data["values"]))  # oldest first
    series = []
    for v in values:
        series.append(
            {
                "datetime": v["datetime"],
                "close": float(v["close"]),
                "high": float(v["high"]),
                "low": float(v["low"]),
            }
        )
    return series


# ---------- Indicators ----------
def ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values, period=14):
    out = [50.0] * len(values)
    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100 - 100 / (1 + avg_gain / (avg_loss or 1e-6))
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = diff if diff > 0 else 0
        loss = -diff if diff < 0 else 0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = 100 - 100 / (1 + avg_gain / (avg_loss or 1e-6))
    return out


def atr(series, period=14):
    trs = []
    for i, d in enumerate(series):
        if i == 0:
            trs.append(d["high"] - d["low"])
        else:
            prev_close = series[i - 1]["close"]
            trs.append(
                max(
                    d["high"] - d["low"],
                    abs(d["high"] - prev_close),
                    abs(d["low"] - prev_close),
                )
            )
    return ema(trs, period)


def compute_all(series):
    closes = [d["close"] for d in series]
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    rsi_vals = rsi(closes, 14)
    atr_vals = atr(series, 14)
    for i, d in enumerate(series):
        d["ema9"] = ema9[i]
        d["ema21"] = ema21[i]
        d["rsi"] = rsi_vals[i]
        d["atr"] = atr_vals[i]
    return series


# ---------- Setup logic (mirrors the dashboard) ----------
def build_setup(d, prev):
    trend_up = d["ema9"] > d["ema21"]
    trend_down = d["ema9"] < d["ema21"]
    just_crossed_up = prev and prev["ema9"] <= prev["ema21"] and d["ema9"] > d["ema21"]
    just_crossed_down = prev and prev["ema9"] >= prev["ema21"] and d["ema9"] < d["ema21"]
    ema_gap_pct = abs(d["ema9"] - d["ema21"]) / d["close"] * 100
    rsi_ok_long = 50 < d["rsi"] < 75
    rsi_ok_short = 25 < d["rsi"] < 50
    volatility_ok = d["atr"] > d["close"] * 0.0004

    bias = "No trade"
    reason = "No aligned edge right now — trend, momentum, and volatility aren't agreeing."

    if not volatility_ok:
        reason = "Volatility is too low for a clean 30-minute move right now."
    elif (trend_up or just_crossed_up) and rsi_ok_long and ema_gap_pct > 0.01:
        bias = "Long"
        reason = "Short-term trend is up and RSI confirms momentum without being overbought."
    elif (trend_down or just_crossed_down) and rsi_ok_short and ema_gap_pct > 0.01:
        bias = "Short"
        reason = "Short-term trend is down and RSI confirms momentum without being oversold."
    elif ema_gap_pct <= 0.01:
        reason = "EMA9/21 are too tangled — this reads as chop, not trend."

    entry = d["close"]
    buffer = d["atr"] * 0.15
    stop_mult = 1.5
    tp1_mult, tp2_mult, tp3_mult = 1.0, 2.25, 3.5

    setup = {
        "bias": bias,
        "reason": reason,
        "strategy": "trend",
        "entry_low": round(entry - buffer, 2),
        "entry_high": round(entry + buffer, 2),
        "stop_loss": None,
        "tp1": None,
        "tp2": None,
        "tp3": None,
    }
    if bias == "Long":
        setup["stop_loss"] = round(entry - d["atr"] * stop_mult, 2)
        setup["tp1"] = round(entry + d["atr"] * tp1_mult, 2)
        setup["tp2"] = round(entry + d["atr"] * tp2_mult, 2)
        setup["tp3"] = round(entry + d["atr"] * tp3_mult, 2)
    elif bias == "Short":
        setup["stop_loss"] = round(entry + d["atr"] * stop_mult, 2)
        setup["tp1"] = round(entry - d["atr"] * tp1_mult, 2)
        setup["tp2"] = round(entry - d["atr"] * tp2_mult, 2)
        setup["tp3"] = round(entry - d["atr"] * tp3_mult, 2)

    return setup


# ---------- Fade (counter-trend pullback) setup ----------
# Catches short-term overbought/oversold reversals that the trend system
# deliberately ignores, since it only fires when the broader EMA trend
# hasn't flipped. Lower probability by nature, so sized tighter.
def build_fade_setup(d, prev):
    if not prev:
        return {"bias": "No trade", "reason": "Not enough data yet.", "strategy": "fade"}

    volatility_ok = d["atr"] > d["close"] * 0.0004
    turned_down = d["close"] < prev["close"]
    turned_up = d["close"] > prev["close"]

    bias = "No trade"
    reason = "No overbought/oversold reversal candle right now."

    if not volatility_ok:
        reason = "Volatility is too low for a clean fade right now."
    elif d["rsi"] >= 68 and turned_down:
        bias = "Short"
        reason = (
            "RSI is overbought and the last candle turned down — a short-term "
            "fade against the broader trend, not a trend-following setup."
        )
    elif d["rsi"] <= 32 and turned_up:
        bias = "Long"
        reason = (
            "RSI is oversold and the last candle turned up — a short-term "
            "fade against the broader trend, not a trend-following setup."
        )

    entry = d["close"]
    buffer = d["atr"] * 0.15
    stop_mult = 1.0  # tighter than trend setups — lower-probability trades
    tp1_mult, tp2_mult, tp3_mult = 0.75, 1.5, 2.25

    setup = {
        "bias": bias,
        "reason": reason,
        "strategy": "fade",
        "entry_low": round(entry - buffer, 2),
        "entry_high": round(entry + buffer, 2),
        "stop_loss": None,
        "tp1": None,
        "tp2": None,
        "tp3": None,
    }
    if bias == "Long":
        setup["stop_loss"] = round(entry - d["atr"] * stop_mult, 2)
        setup["tp1"] = round(entry + d["atr"] * tp1_mult, 2)
        setup["tp2"] = round(entry + d["atr"] * tp2_mult, 2)
        setup["tp3"] = round(entry + d["atr"] * tp3_mult, 2)
    elif bias == "Short":
        setup["stop_loss"] = round(entry + d["atr"] * stop_mult, 2)
        setup["tp1"] = round(entry - d["atr"] * tp1_mult, 2)
        setup["tp2"] = round(entry - d["atr"] * tp2_mult, 2)
        setup["tp3"] = round(entry - d["atr"] * tp3_mult, 2)

    return setup


# ---------- Running performance stats ----------
def record_resolved_trade(state, ot, outcome, r_multiple):
    """Append this fully-closed trade to a permanent history log. r_multiple
    is the final weighted result across all partial exits, computed by the
    caller (check_open_trade), so running stats reflect real scaled-out
    outcomes rather than a single all-or-nothing exit."""
    history = state.setdefault("trade_history", [])
    history.append(
        {
            "strategy": ot.get("strategy", "trend"),
            "bias": ot["bias"],
            "outcome": outcome,
            "r_multiple": round(r_multiple, 2),
            "opened_at": ot.get("opened_at"),
            "resolved_at": datetime.now().isoformat(),
        }
    )
    return round(r_multiple, 2)


def compute_stats(history, strategy=None):
    rows = [h for h in history if strategy is None or h["strategy"] == strategy]
    if not rows:
        return None
    wins = [h["r_multiple"] for h in rows if h["r_multiple"] > 0]
    losses = [h["r_multiple"] for h in rows if h["r_multiple"] <= 0]
    win_rate = round(len(wins) / len(rows) * 100, 1)
    avg_win = round(sum(wins) / len(wins), 2) if wins else 0
    avg_loss = round(sum(losses) / len(losses), 2) if losses else 0
    expectancy = round(sum(h["r_multiple"] for h in rows) / len(rows), 2)
    return {
        "count": len(rows),
        "win_rate": win_rate,
        "avg_win_r": avg_win,
        "avg_loss_r": avg_loss,
        "expectancy_r": expectancy,
    }


def format_stats_block(state):
    history = state.get("trade_history", [])
    overall = compute_stats(history)
    if not overall:
        return "\nRunning stats: no resolved trades logged yet."

    trend_stats = compute_stats(history, "trend")
    fade_stats = compute_stats(history, "fade")

    lines = [
        f"\nRunning stats ({overall['count']} resolved trades):",
        f"  Overall: {overall['win_rate']}% win rate, avg win {overall['avg_win_r']}R, "
        f"avg loss {overall['avg_loss_r']}R, expectancy {overall['expectancy_r']}R per trade",
    ]
    if trend_stats:
        lines.append(
            f"  Trend only ({trend_stats['count']}): {trend_stats['win_rate']}% win rate, "
            f"expectancy {trend_stats['expectancy_r']}R"
        )
    if fade_stats:
        lines.append(
            f"  Fade only ({fade_stats['count']}): {fade_stats['win_rate']}% win rate, "
            f"expectancy {fade_stats['expectancy_r']}R"
        )
    lines.append(
        "  (Breakeven expectancy is 0R — anything consistently above 0 after enough "
        "trades suggests real edge; below 0 suggests there isn't one yet.)"
    )
    return "\n".join(lines)
def check_open_trade(state, current):
    """If there's an open trade logged, walk it through up to three scaled
    take-profit levels:
      - TP1 hit: close 1/3, move the effective stop to breakeven for the rest.
      - TP2 hit: close another 1/3, stop stays at breakeven.
      - TP3 hit: close the final 1/3 — trade fully resolved.
      - Stop hit before TP1: full loss, resolved.
      - Stop (now at breakeven) hit after TP1 but before TP2/TP3: resolved
        with a partial win already locked in from TP1.
      - Invalidated: only checked before TP1, since once the stop is at
        breakeven the "invalidation" concept is already covered by that.
    Returns True if a trade is open after this check (whether just resolved,
    just partially closed, or still quietly running)."""
    ot = state.get("open_trade")
    if not ot:
        return False

    tp1_hit = ot.get("tp1_hit", False)
    tp2_hit = ot.get("tp2_hit", False)
    realized_r = ot.get("realized_r", 0.0)
    entry_mid = (ot["entry_low"] + ot["entry_high"]) / 2
    risk = abs(entry_mid - ot["stop_loss"])
    is_long = ot["bias"] == "Long"

    # Effective stop: original stop until TP1 hits, then breakeven (entry).
    effective_stop = entry_mid if tp1_hit else ot["stop_loss"]

    def r_at(price):
        move = (price - entry_mid) if is_long else (entry_mid - price)
        return move / risk if risk else 0

    stopped = (current["low"] <= effective_stop) if is_long else (current["high"] >= effective_stop)
    hit_tp1 = (not tp1_hit) and ((current["high"] >= ot["tp1"]) if is_long else (current["low"] <= ot["tp1"]))
    hit_tp2 = tp1_hit and (not tp2_hit) and ((current["high"] >= ot["tp2"]) if is_long else (current["low"] <= ot["tp2"]))
    hit_tp3 = tp1_hit and tp2_hit and ((current["high"] >= ot["tp3"]) if is_long else (current["low"] <= ot["tp3"]))
    invalidated = (not tp1_hit) and (
        (current["close"] < ot["entry_low"]) if is_long else (current["close"] > ot["entry_high"])
    )

    # Stop / breakeven-stop hit — trade fully closes here.
    if stopped:
        this_leg_r = r_at(effective_stop)
        remaining_share = 1.0 if not tp1_hit else (2 / 3 if not tp2_hit else 1 / 3)
        final_r = realized_r + remaining_share * this_leg_r
        outcome = "Stopped out" if not tp1_hit else "Remainder stopped at breakeven (TP1 already secured)"
        record_resolved_trade(state, ot, outcome, final_r)
        body = (
            f"Trade: {ot['bias']} (entered {ot['entry_low']}-{ot['entry_high']})\n"
            f"Outcome: {outcome} ({final_r:+.2f}R)\n"
            f"Current price: {current['close']}\n"
        )
        body += format_stats_block(state)
        send_email(f"Gold trade update: {outcome}", body)
        print(f"[{datetime.now()}] Trade resolved: {outcome} ({final_r:+.2f}R)")
        state["open_trade"] = None
        return True

    if invalidated:
        this_leg_r = r_at(current["close"])
        record_resolved_trade(state, ot, "Invalidated", this_leg_r)
        body = (
            f"Trade: {ot['bias']} (entered {ot['entry_low']}-{ot['entry_high']})\n"
            f"Outcome: Invalidated ({this_leg_r:+.2f}R)\n"
            f"Current price: {current['close']}\n\n"
            "Price closed back through your entry range without reaching TP1 or "
            "stop. The setup's own logic considers this trade's premise gone."
        )
        body += format_stats_block(state)
        send_email("Gold trade update: Invalidated", body)
        print(f"[{datetime.now()}] Trade resolved: Invalidated ({this_leg_r:+.2f}R)")
        state["open_trade"] = None
        return True

    if hit_tp3:
        this_leg_r = r_at(ot["tp3"])
        final_r = realized_r + (1 / 3) * this_leg_r
        record_resolved_trade(state, ot, "Fully closed at TP3", final_r)
        body = (
            f"Trade: {ot['bias']} (entered {ot['entry_low']}-{ot['entry_high']})\n"
            f"Outcome: Fully closed at TP3 ({final_r:+.2f}R)\n"
            f"Current price: {current['close']}\n\n"
            "Final third closed at TP3 — full target range achieved."
        )
        body += format_stats_block(state)
        send_email("Gold trade update: TP3 hit, fully closed", body)
        print(f"[{datetime.now()}] Trade resolved: TP3 ({final_r:+.2f}R)")
        state["open_trade"] = None
        return True

    if hit_tp2:
        this_leg_r = r_at(ot["tp2"])
        ot["realized_r"] = realized_r + (1 / 3) * this_leg_r
        ot["tp2_hit"] = True
        state["open_trade"] = ot
        send_email(
            "Gold trade update: TP2 hit, take another 1/3",
            f"Trade: {ot['bias']} (entered {ot['entry_low']}-{ot['entry_high']})\n"
            f"TP2 hit at {ot['tp2']}. Take another 1/3 profit here.\n"
            f"Stop stays at breakeven ({entry_mid:.2f}) for the final 1/3, "
            f"which now rides toward TP3 ({ot['tp3']}).",
        )
        print(f"[{datetime.now()}] TP2 hit, took another 1/3.")
        return True

    if hit_tp1:
        this_leg_r = r_at(ot["tp1"])
        ot["realized_r"] = realized_r + (1 / 3) * this_leg_r
        ot["tp1_hit"] = True
        state["open_trade"] = ot
        send_email(
            "Gold trade update: TP1 hit, take 1/3 + move to breakeven",
            f"Trade: {ot['bias']} (entered {ot['entry_low']}-{ot['entry_high']})\n"
            f"TP1 hit at {ot['tp1']}. Take 1/3 profit here.\n"
            f"Move your stop on the remaining 2/3 to breakeven ({entry_mid:.2f}) — "
            f"from this point, this trade can no longer become a net loss.\n"
            f"Remaining targets: TP2 {ot['tp2']}, TP3 {ot['tp3']}.",
        )
        print(f"[{datetime.now()}] TP1 hit, took 1/3, moved stop to breakeven.")
        return True

    print(f"[{datetime.now()}] Still in open trade ({ot['bias']}), no level hit yet.")
    return True


# ---------- State (so we only alert on genuine changes) ----------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_bias": None, "last_digest_date": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ---------- Email ----------
def send_email(subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = TO_EMAIL
    msg.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(msg)


def format_setup_body(setup, price):
    strategy_label = "Counter-trend fade (lower probability, tighter target)" if setup.get("strategy") == "fade" else "Trend continuation"
    lines = [
        f"XAU/USD price: {price}",
        f"Strategy: {strategy_label}",
        f"Setup: {setup['bias']}",
        f"Reason: {setup['reason']}",
    ]
    if setup["bias"] != "No trade":
        action = "BUY" if setup["bias"] == "Long" else "SELL"
        lines += [
            f"Action: {action}",
            f"Entry range: {setup['entry_low']} - {setup['entry_high']}",
            f"Stop loss: {setup['stop_loss']}",
            f"TP1: {setup['tp1']}  (close 1/3, move stop to breakeven)",
            f"TP2: {setup['tp2']}  (close another 1/3, stop stays at breakeven)",
            f"TP3: {setup['tp3']}  (close the final 1/3 — trade fully done)",
            "Invalidation: if price closes back through the entry range before "
            "TP1 is reached, the setup's premise is considered gone.",
            "\nThis will be tracked automatically from here: you'll get an email "
            "at each TP as it's hit (with what to do), and a final one when the "
            "trade fully resolves. If you don't actually take this trade, just "
            "ignore those follow-ups.",
        ]
    lines.append(
        "\nNot trading advice — a rule-based read sized to recent volatility (ATR). "
        "Your actual fills depend on your broker's spread and slippage."
    )
    return "\n".join(lines)


# ---------- Main ----------
def main():
    series = compute_all(fetch_series(300))
    prev = series[-2] if len(series) > 1 else None
    current = series[-1]
    setup = build_setup(current, prev)

    state = load_state()
    today_str = date.today().isoformat()

    # If you're already in a tracked trade, check whether it resolved this run,
    # and skip firing a brand-new signal alert while it's still open.
    trade_was_open = check_open_trade(state, current)

    changed = setup["bias"] != state.get("last_bias")
    is_first_run_today = state.get("last_digest_date") != today_str

    # Only alert immediately when something is actually tradeable, it's new,
    # and you're not already tracking an open trade.
    is_new_actionable_setup = changed and setup["bias"] in ("Long", "Short") and not trade_was_open
    is_paused = os.path.exists(PAUSE_FLAG)

    if is_new_actionable_setup and is_paused:
        print(
            f"[{datetime.now()}] New trend setup ({setup['bias']}) found but alerts are "
            f"paused (in_trade file present) — not emailing."
        )
    elif is_new_actionable_setup:
        send_email(
            f"Gold trade worth taking: {setup['bias']} (trend)",
            format_setup_body(setup, current["close"]),
        )
        # Automatically start tracking this as your open trade — no manual
        # step needed. If you don't actually take it, just ignore the
        # follow-up emails; the tracker will still resolve and clear itself.
        state["open_trade"] = {**setup, "opened_at": datetime.now().isoformat(), "tp1_hit": False, "tp2_hit": False, "realized_r": 0.0}
        print(f"[{datetime.now()}] Sent trend trade alert and started auto-tracking: {setup['bias']}")

    # Fade (counter-trend) check: only look for a pullback opportunity when
    # the trend system itself found nothing, and nothing else is being tracked.
    # This keeps the two strategies from ever firing on the same candle.
    fade_setup = None
    is_new_fade_setup = False
    if setup["bias"] == "No trade" and not trade_was_open:
        fade_setup = build_fade_setup(current, prev)
        fade_changed = fade_setup["bias"] != state.get("last_fade_bias")
        is_new_fade_setup = fade_changed and fade_setup["bias"] in ("Long", "Short")

        if is_new_fade_setup and is_paused:
            print(
                f"[{datetime.now()}] New fade setup ({fade_setup['bias']}) found but "
                f"alerts are paused — not emailing."
            )
        elif is_new_fade_setup:
            send_email(
                f"Gold trade worth taking: {fade_setup['bias']} (fade)",
                format_setup_body(fade_setup, current["close"]),
            )
            state["open_trade"] = {**fade_setup, "opened_at": datetime.now().isoformat(), "tp1_hit": False, "tp2_hit": False, "realized_r": 0.0}
            print(f"[{datetime.now()}] Sent fade trade alert and started auto-tracking: {fade_setup['bias']}")

        state["last_fade_bias"] = fade_setup["bias"]

    if is_first_run_today:
        send_email(
            f"Gold signal — daily summary: {setup['bias']}",
            format_setup_body(setup, current["close"]) + format_stats_block(state),
        )
        state["last_digest_date"] = today_str
        print(f"[{datetime.now()}] Sent daily summary: {setup['bias']}")

    if not is_new_actionable_setup and not is_new_fade_setup and not is_first_run_today:
        print(f"[{datetime.now()}] No new tradeable setup, no digest due. Current: {setup['bias']}")

    state["last_bias"] = setup["bias"]
    state["last_setup"] = setup
    save_state(state)


if __name__ == "__main__":
    main()
