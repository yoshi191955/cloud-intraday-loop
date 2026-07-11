"""
intraday_loop.py — クラウド毎時ループ（機械的モメンタム・2時間horizon）。

GitHub Actions から毎時（米国市場時間）起動される想定。PC非依存。
  1) 15分足を取得（yfinance・無料・APIキー不要）
  2) ウォッチリスト各銘柄に機械的モメンタムで up/down/flat を予測（新規行を追記）
  3) 保有時間(2時間)が満了した過去予測を、実現した15分足で採点
     コストモデル: 往復0.5% + 利益のみ25%課税（既存ログと同一）
  4) 既存ログと同一スキーマの CSV を docs/predictions_intraday.csv に保存

秘密情報は一切持たない。冪等（同じ hour×ticker は二度予測しない）。
"""
from __future__ import annotations
import csv, os, math, datetime as dt
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "docs", "predictions_intraday.csv")
WATCH = os.path.join(HERE, "watchlist.csv")

# 既存「Claude予測ログ」と同一の23列スキーマ（末尾に空列は付けない）
COLS = ["pred_id","predict_date_jst","session_date","market","ticker","strategy_tag",
        "direction","entry_ref","target_pct","stop_pct","horizon","confidence",
        "catalyst_type","regime","thesis","status","actual_open","actual_close",
        "return_pct","net_return_pct","hit","exit_reason","notes"]

# ── 設定 ──
HORIZON = dt.timedelta(hours=2)      # 採点の保有時間（コスト0.5%で黒字が残る安全側）
ONE_WAY_COST = 0.5                   # 片道%（往復で0.5*2? ここでは往復0.5%運用に合わせ0.5を1回）
ROUND_TRIP_COST = 0.5                # 既存ログの前提: 往復0.5%
TAX = 0.25
TARGET_PCT, STOP_PCT = 1.0, -0.8
FAST, SLOW = 4, 16                   # 15分足のSMA本数（1h / 4h）
THRESH = 0.0015                      # フラット閾値（±0.15%）
JST = ZoneInfo("Asia/Tokyo")
ET = ZoneInfo("America/New_York")


def load_watchlist():
    with open(WATCH) as f:
        return [r["ticker"] for r in csv.DictReader(f)]


def read_log():
    if not os.path.exists(LOG):
        return []
    with open(LOG, newline="") as f:
        return list(csv.DictReader(f))


def write_log(rows):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLS})


def fetch_bars(tickers):
    """{ticker: DataFrame(15m, tz=UTC, columns incl 'Close')}。失敗銘柄はスキップ。"""
    out = {}
    data = yf.download(tickers, period="5d", interval="15m", progress=False,
                       auto_adjust=True, group_by="ticker", threads=True)
    for t in tickers:
        try:
            df = data[t] if isinstance(data.columns, pd.MultiIndex) else data
            df = df.dropna(subset=["Close"]).copy()
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            else:
                df.index = df.index.tz_convert("UTC")
            if len(df) >= SLOW + 1:
                out[t] = df
        except Exception:
            continue
    return out


def cost_net(return_pct: float) -> float:
    after = return_pct - ROUND_TRIP_COST
    return after * (1 - TAX) if after > 0 else after


def predict_one(df: pd.Series):
    """(direction, confidence, thesis)。t までの情報のみ。"""
    close = df["Close"]
    fast = close.tail(FAST).mean()
    slow = close.tail(SLOW).mean()
    rel = (fast - slow) / slow if slow else 0.0
    if rel > THRESH:
        conf = 3 if rel > 3 * THRESH else 2
        return "up", conf, f"15分足モメンタム上向き(fast/slow乖離{rel*100:+.2f}%)"
    if rel < -THRESH:
        conf = 3 if rel < -3 * THRESH else 2
        return "down", conf, f"15分足モメンタム下向き(fast/slow乖離{rel*100:+.2f}%)"
    return "flat", 1, f"モメンタム中立(乖離{rel*100:+.2f}%)→見送り"


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def market_open(now: dt.datetime) -> bool:
    e = now.astimezone(ET)
    if e.weekday() >= 5:
        return False
    o = e.replace(hour=9, minute=30, second=0, microsecond=0)
    c = e.replace(hour=16, minute=0, second=0, microsecond=0)
    return o <= e <= c


def main():
    now = now_utc()
    tickers = load_watchlist()
    rows = read_log()
    seen = {r["pred_id"] for r in rows}
    bars = fetch_bars(tickers)

    # ── 2) 予測（市場時間かつ新鮮なバーがある時のみ） ──
    if market_open(now):
        hour_key = now.strftime("%Y%m%d%H")
        for t in tickers:
            df = bars.get(t)
            if df is None:
                continue
            last_ts = df.index[-1]
            if (now - last_ts) > dt.timedelta(minutes=45):
                continue  # 新鮮なバーが無い（データ遅延）→ 予測しない
            pid = f"I-{hour_key}-{t}"
            if pid in seen:
                continue
            direction, conf, thesis = predict_one(df)
            entry = float(df["Close"].iloc[-1])
            rows.append({
                "pred_id": pid,
                "predict_date_jst": now.astimezone(JST).strftime("%Y-%m-%d %H:%M"),
                "session_date": now.astimezone(ET).strftime("%Y-%m-%d"),
                "market": "US", "ticker": t, "strategy_tag": "momentum_15m",
                "direction": direction, "entry_ref": "bar_close",
                "target_pct": f"{TARGET_PCT:+.1f}", "stop_pct": f"{STOP_PCT:+.1f}",
                "horizon": "2h", "confidence": conf,
                "catalyst_type": "mechanical", "regime": "intraday",
                "thesis": thesis, "status": "open",
                "actual_open": f"{entry:.4f}", "actual_close": "",
                "return_pct": "", "net_return_pct": "", "hit": "",
                "exit_reason": "", "notes": f"predict_ts_utc={now.isoformat()}",
            })
            seen.add(pid)

    # ── 3) 採点（horizon 満了・directional・open のみ） ──
    for r in rows:
        if r["status"] != "open" or r["direction"] not in ("up", "down"):
            continue
        try:
            ptxt = r["notes"].split("predict_ts_utc=")[-1]
            pts = dt.datetime.fromisoformat(ptxt)
        except Exception:
            continue
        due = pts + HORIZON
        if now < due:
            continue  # まだ満了していない
        df = bars.get(r["ticker"])
        if df is None:
            continue
        after = df[df.index >= due]
        if after.empty:
            # 満了時刻以降のバーがまだ無い（当日引け後など）。次回持ち越し。
            continue
        entry = float(r["actual_open"])
        exitp = float(after["Close"].iloc[0])
        raw = (exitp - entry) / entry * 100.0
        ret = raw if r["direction"] == "up" else -raw
        net = cost_net(ret)
        r["actual_close"] = f"{exitp:.4f}"
        r["return_pct"] = f"{ret:+.2f}"
        r["net_return_pct"] = f"{net:+.2f}"
        r["hit"] = "TRUE" if ret > 0 else "FALSE"
        r["status"] = "closed"
        r["exit_reason"] = "2h_horizon" + ("(コスト負け)" if (ret > 0 and net < 0) else "")

    write_log(rows)
    closed = sum(1 for r in rows if r["status"] == "closed")
    print(f"[intraday_loop] rows={len(rows)} closed={closed} "
          f"open={sum(1 for r in rows if r['status']=='open')} "
          f"market_open={market_open(now)} bars={len(bars)}")


if __name__ == "__main__":
    main()
