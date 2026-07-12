"""
backtest_intraday.py — 過去60日の15分足で、クラウドループと同一ロジックを再現検証。

目的:
  ライブループを数ヶ月待たずに、「機械的モメンタム(2h horizon)に edge があるか」を今すぐ判定する。
  Yahooの15分足は直近60日まで取得可能 → 52社 × 約60営業日 × 5回/日 で数千〜1万件超の予測が得られる。

設計（intraday_loop.py と完全に同じ条件）:
  * シグナル      : predict_one()（15分足のfast/slow SMA乖離）をそのまま流用
  * 保有時間      : 2時間（HORIZON）
  * コスト        : 往復0.5% + 利益のみ25%課税（cost_net()を流用）
  * 予測タイミング: 引けまで2時間以上ある時刻のみ（ET 09:30/10:30/11:30/12:30/13:30 相当）

ルックアヘッド防止（最重要）:
  * 時点 t のシグナルは「t までのバーだけ」で計算する（df.loc[:t]）
  * 決済は「t + 2時間 以降の最初のバー」。ただし**同一営業日内**に限る
    （日を跨ぐとオーバーナイトのギャップを測ることになり、2時間の値動きではなくなる）
  * 未来のバーは一切参照しない

ベースライン比較（ここが肝）:
  * AlwaysUp : 同じ時刻に、シグナルを無視して常に "up" で入る
    → これに勝てないなら、モメンタムシグナルには価値が無い（単に相場の上昇に乗っただけ）
  * AlwaysFlat: 何もしない（= 0%）

実行:  python backtest_intraday.py
"""
from __future__ import annotations

import datetime as dt
import math
import statistics as st
from collections import defaultdict
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

from intraday_loop import (predict_one, cost_net, HORIZON, ET,
                           ROUND_TRIP_COST, TAX, load_watchlist, SLOW)

DECISION_TIMES = [(9, 30), (10, 30), (11, 30), (12, 30), (13, 30)]  # ET。引けまで2h以上
CLOSE_ET = (16, 0)


def fetch(tickers):
    print(f"15分足を取得中（直近60日 / {len(tickers)}銘柄）…")
    data = yf.download(tickers, period="60d", interval="15m", progress=False,
                       auto_adjust=True, group_by="ticker", threads=True)
    out = {}
    for t in tickers:
        try:
            df = data[t] if isinstance(data.columns, pd.MultiIndex) else data
            df = df.dropna(subset=["Close"]).copy()
            df.index = (df.index.tz_localize("UTC") if df.index.tz is None
                        else df.index.tz_convert("UTC"))
            if len(df) > SLOW + 10:
                out[t] = df
        except Exception:
            continue
    print(f"取得成功: {len(out)}/{len(tickers)} 銘柄\n")
    return out


def backtest_one(ticker, df):
    """1銘柄の全予測を返す。未来情報は一切使わない。"""
    et_idx = df.index.tz_convert(ET)
    trades = []
    for day, day_mask in df.groupby(et_idx.date).groups.items():
        day_df = df.loc[day_mask]
        if len(day_df) < 3:
            continue
        day_et = day_df.index.tz_convert(ET)
        close_dt = pd.Timestamp(dt.datetime.combine(day, dt.time(*CLOSE_ET)),
                                tz=ET)
        for hh, mm in DECISION_TIMES:
            target = pd.Timestamp(dt.datetime.combine(day, dt.time(hh, mm)), tz=ET)
            # その時刻「以下」の最後のバー（= t までの情報のみ）
            avail = day_df[day_et <= target]
            if avail.empty:
                continue
            t = avail.index[-1]
            # シグナルは「t までの全履歴」で計算（過去情報のみ）
            hist = df.loc[:t]
            if len(hist) < SLOW + 1:
                continue
            direction, conf, _ = predict_one(hist)
            if direction == "flat":
                trades.append({"ticker": ticker, "t": t, "direction": "flat",
                               "conf": conf, "ret": None, "net": None})
                continue

            entry = float(hist["Close"].iloc[-1])
            due = t + HORIZON
            # 決済は「満期以降の最初のバー」かつ**同一営業日内**（日跨ぎは除外）
            after = day_df[(day_df.index >= due)]
            if after.empty:
                continue  # その日のうちに満期が来ない → 採用しない
            exitp = float(after["Close"].iloc[0])

            raw = (exitp - entry) / entry * 100.0
            ret = raw if direction == "up" else -raw
            net = cost_net(ret)
            # ベースライン: 同じ時刻に常に up で入った場合
            base_ret = raw
            base_net = cost_net(base_ret)
            trades.append({"ticker": ticker, "t": t, "direction": direction,
                           "conf": conf, "ret": ret, "net": net,
                           "base_ret": base_ret, "base_net": base_net})
    return trades


def stats(vals):
    if not vals:
        return dict(n=0, mean=0, tstat=0, pos=0)
    n = len(vals)
    m = st.mean(vals)
    sd = st.stdev(vals) if n > 1 else 0
    se = sd / math.sqrt(n) if sd else 0
    return dict(n=n, mean=m, tstat=(m / se if se else 0),
                pos=sum(1 for v in vals if v > 0) / n)


def main():
    tickers = load_watchlist()
    bars = fetch(tickers)
    if not bars:
        print("データ取得に失敗しました。"); return

    all_tr = []
    for t, df in bars.items():
        all_tr += backtest_one(t, df)

    directional = [x for x in all_tr if x["direction"] in ("up", "down") and x["net"] is not None]
    flats = [x for x in all_tr if x["direction"] == "flat"]

    net = [x["net"] for x in directional]
    ret = [x["ret"] for x in directional]
    base_net = [x["base_net"] for x in directional]
    hit = [1 for x in directional if x["ret"] > 0]

    s_net, s_base = stats(net), stats(base_net)
    sessions = len({x["t"].tz_convert(ET).date() for x in directional})

    print("=" * 68)
    print(f"■ バックテスト結果（過去60日・15分足・2h horizon・コスト計上済み）")
    print("=" * 68)
    print(f"  総予測行     : {len(all_tr):,}  （うち flat=見送り {len(flats):,}）")
    print(f"  実トレード   : {len(directional):,}  / 独立セッション {sessions} 日")
    print()
    print(f"  方向的中率      : {len(hit)/max(len(directional),1):.1%}")
    print(f"  コスト後プラス率: {s_net['pos']:.1%}   ← 本当の勝率")
    print(f"  値幅(コスト前)  : 平均 {st.mean(ret):+.3f}%")
    print(f"  純益(コスト後)  : 平均 {s_net['mean']:+.4f}%  t={s_net['tstat']:+.2f}")
    print()
    print("  --- ベースライン比較（シグナルに価値があるか） ---")
    print(f"  AlwaysUp(常に買い) 純益平均 {s_base['mean']:+.4f}%  t={s_base['tstat']:+.2f}  勝率 {s_base['pos']:.1%}")
    print(f"  AlwaysFlat(何もしない)     +0.0000%")
    edge = s_net["mean"] - s_base["mean"]
    print(f"  → シグナルの付加価値: {edge:+.4f}%/トレード")
    print()
    if s_net["mean"] > 0 and s_net["tstat"] > 2 and edge > 0:
        print("  判定: ★コスト後プラス かつ ベースライン超え。芽の可能性あり（要追試）")
    elif s_net["mean"] <= 0:
        print("  判定: ×コスト後マイナス → この戦略に edge は無い（正直な結果）")
    else:
        print("  判定: △プラスだが有意性/付加価値が不足 → 採用しない")
    print("=" * 68)

    # 銘柄別
    byt = defaultdict(list)
    for x in directional:
        byt[x["ticker"]].append(x["net"])
    rows = sorted(((k, len(v), st.mean(v), sum(v)) for k, v in byt.items()),
                  key=lambda r: -r[3])
    print("\n■ 銘柄別（net合計 上位5 / 下位5）")
    print(f"{'ticker':<8}{'n':>6}{'net平均':>10}{'net合計':>10}")
    for r in rows[:5] + [("…", 0, 0, 0)] + rows[-5:]:
        if r[0] == "…":
            print("  …"); continue
        print(f"{r[0]:<8}{r[1]:>6}{r[2]:>9.3f}%{r[3]:>9.1f}%")

    # 期間前後半で頑健性
    ts = sorted(x["t"] for x in directional)
    mid = ts[len(ts) // 2]
    h1 = [x["net"] for x in directional if x["t"] <= mid]
    h2 = [x["net"] for x in directional if x["t"] > mid]
    print(f"\n■ 頑健性（期間を前後半に分割）")
    print(f"  前半: n={len(h1):,}  net平均 {st.mean(h1):+.4f}%")
    print(f"  後半: n={len(h2):,}  net平均 {st.mean(h2):+.4f}%")
    if st.mean(h1) > 0 and st.mean(h2) > 0:
        print("  → 両期間でプラス（頑健）")
    else:
        print("  → 片方または両方がマイナス（再現性なし）")


if __name__ == "__main__":
    main()
