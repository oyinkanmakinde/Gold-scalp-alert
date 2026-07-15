"""
XAU/USD 30-minute scalp signal alert — GitHub Actions version.

Same logic as the Mac version, including automatic open-trade tracking
and the breakeven/partial-profit milestone nudge. Credentials come from
environment variables (GitHub Secrets) instead of a local config file,
since a cloud runner can't see files on your Mac. The workflow that runs
this script commits gold_alert_state.json back to the repo after each
run, so the tracker's memory survives between runs.

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
    elif (trend_up or just_crossed_up) and rsi_ok_long and ema_gap_pct > 0.02:
        bias = "Long"
        reason = "Short-term trend is up and RSI confirms momentum without being overbought."
    elif (trend_down or just_crossed_down) and rsi_ok_short and ema_gap_pct > 0.02:
        bias = "Short"
        reason = "Short-term trend is down and RSI confirms momentum without being oversold."
    elif ema_gap_pct <= 0.02:
        reason = "EMA9/21 are too tangled — this reads as chop, not trend."

    entry = d["close"]
    buffer = d["atr"] * 0.15
    stop_mult, target_mult = 1.5, 2.25

    setup = {
        "bias": bias,
        "reason": reason,
        "entry_low": round(entry - buffer, 2),
        "entry_high": round(entry + buffer, 2),
        "stop_loss": None,
        "take_profit": None,
    }
    if bias == "Long":
        setup["stop_loss"] = round(entry - d["atr"] * stop_mult, 2)
        setup["take_profit"] = round(entry + d["atr"] * target_mult, 2)
    elif bias == "Short":
        setup["stop_loss"] = round(entry + d["atr"] * stop_mult, 2)
        setup["take_profit"] = round(entry - d["atr"] * target_mult, 2)

    return setup


# ---------- Open trade tracking ----------
def check_open_trade(state, current):
    """If there's an open trade logged, see if this candle resolved it
    (stop hit, target hit, or invalidated on a close back through entry),
    or if it's crossed a profit milestone worth flagging (breakeven/partial
    profit consideration) before any hard invalidation.
    Returns True if a trade is open after this check (whether just resolved,
    just flagged, or still quietly running)."""
    ot = state.get("open_trade")
    if not ot:
        return False

    outcome = None
    if ot["bias"] == "Long":
        if current["low"] <= ot["stop_loss"]:
            outcome = "Stopped out"
        elif current["high"] >= ot["take_profit"]:
            outcome = "Target hit"
        elif current["close"] < ot["entry_low"]:
            outcome = "Invalidated"
    else:  # Short
        if current["high"] >= ot["stop_loss"]:
            outcome = "Stopped out"
        elif current["low"] <= ot["take_profit"]:
            outcome = "Target hit"
        elif current["close"] > ot["entry_high"]:
            outcome = "Invalidated"

    if outcome:
        body = (
            f"Trade: {ot['bias']} (entered {ot['entry_low']}-{ot['entry_high']})\n"
            f"Outcome: {outcome}\n"
            f"Current price: {current['close']}\n"
            f"Stop loss was: {ot['stop_loss']}\n"
            f"Take profit was: {ot['take_profit']}\n"
        )
        if outcome == "Invalidated":
            body += (
                "\nPrice closed back through your entry range without reaching "
                "stop or target. The setup's own logic considers this trade's "
                "premise gone, independent of where price goes next."
            )
        send_email(f"Gold trade update: {outcome}", body)
        print(f"[{datetime.now()}] Open trade resolved: {outcome}")
        state["open_trade"] = None
        return True

    # Not resolved — check how far toward target this trade has gotten,
    # so we can flag a breakeven/partial-profit moment before any hard call.
    entry_mid = (ot["entry_low"] + ot["entry_high"]) / 2
    if ot["bias"] == "Long":
        progress = (current["close"] - entry_mid) / (ot["take_profit"] - entry_mid)
    else:
        progress = (entry_mid - current["close"]) / (entry_mid - ot["take_profit"])

    milestone_threshold = 0.4  # 40% of the way from entry to target
    if progress >= milestone_threshold and not ot.get("milestone_sent") and progress < 1:
        pct = round(progress * 100)
        body = (
            f"Trade: {ot['bias']} (entered {ot['entry_low']}-{ot['entry_high']})\n"
            f"Current price: {current['close']}\n"
            f"Roughly {pct}% of the way from entry to your take profit "
            f"({ot['take_profit']}).\n\n"
            "This is a natural point to consider two options, entirely your call:\n"
            "- Move your stop loss up to your entry price (breakeven), so a "
            "reversal can no longer turn this into a loss.\n"
            "- Take partial profit now and let the rest ride toward the "
            "original target.\n\n"
            "Not trading advice — just flagging that you're meaningfully in "
            "profit before any invalidation call would trigger."
        )
        send_email("Gold trade update: consider breakeven or partial profit", body)
        ot["milestone_sent"] = True
        state["open_trade"] = ot
        print(f"[{datetime.now()}] Sent breakeven/partial-profit nudge at {pct}% progress.")
    else:
        print(f"[{datetime.now()}] Still in open trade ({ot['bias']}), no resolution yet.")

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
    lines = [
        f"XAU/USD price: {price}",
        f"Setup: {setup['bias']}",
        f"Reason: {setup['reason']}",
    ]
    if setup["bias"] != "No trade":
        action = "BUY" if setup["bias"] == "Long" else "SELL"
        lines += [
            f"Action: {action}",
            f"Entry range: {setup['entry_low']} - {setup['entry_high']}",
            f"Stop loss: {setup['stop_loss']}",
            f"Take profit: {setup['take_profit']}",
            "Invalidation: if price closes back through the entry range without "
            "hitting stop or target, the setup's premise is considered gone.",
            "\nThis will be tracked automatically from here: you'll get an email "
            "if it moves meaningfully into profit (with a breakeven/partial-profit "
            "prompt), and another when it finally resolves. If you don't actually "
            "take this trade, just ignore those follow-ups.",
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
            f"[{datetime.now()}] New setup ({setup['bias']}) found but alerts are "
            f"paused (in_trade file present) — not emailing."
        )
    elif is_new_actionable_setup:
        send_email(
            f"Gold trade worth taking: {setup['bias']}",
            format_setup_body(setup, current["close"]),
        )
        # Automatically start tracking this as your open trade — no manual
        # step needed. If you don't actually take it, just ignore the
        # follow-up emails; the tracker will still resolve and clear itself.
        state["open_trade"] = {**setup, "opened_at": datetime.now().isoformat(), "milestone_sent": False}
        print(f"[{datetime.now()}] Sent trade alert and started auto-tracking: {setup['bias']}")

    if is_first_run_today:
        send_email(
            f"Gold signal — daily summary: {setup['bias']}",
            format_setup_body(setup, current["close"]),
        )
        state["last_digest_date"] = today_str
        print(f"[{datetime.now()}] Sent daily summary: {setup['bias']}")

    if not is_new_actionable_setup and not is_first_run_today:
        print(f"[{datetime.now()}] No new tradeable setup, no digest due. Current: {setup['bias']}")

    state["last_bias"] = setup["bias"]
    state["last_setup"] = setup
    save_state(state)


if __name__ == "__main__":
    main()
