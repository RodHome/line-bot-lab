# -*- coding: utf-8 -*-
"""
institutional_flow.py  v1.0.0  (2026-11-20)  全市場三大法人買賣超爬蟲

【異動說明】
v1.0.0 新增。用途：每天收盤後抓「全市場」外資／投信／自營商買賣超（億元），
       輸出 institutional_flow.json，供 LINE 機器人與網頁讀取，用來觀察
       「外資何時回頭買超、賣壓何時縮小」。
       - 歷史資料：FinMind（一次呼叫抓整段）
       - 最新一天：證交所 BFI82U（官方，補上 FinMind 還沒更新的日子）
       - 兩個來源互為備援；都失敗時【不覆蓋】舊檔。
       - 資料沒變時不重寫檔案（避免無謂的 commit 與 Render 重新部署）。
       - 不影響現有的 generator.py / MarketRiskMonitor.py / market_risk.json。

【單位】一律「億元」，正數＝買超、負數＝賣超。
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

VERSION = "1.0.0"
OUT_FILE = os.environ.get("INST_FLOW_OUT", "institutional_flow.json")
KEEP_DAYS = 60            # JSON 保留最近幾個交易日
FINMIND_LOOKBACK = 110    # FinMind 往回抓的日曆天數（足夠涵蓋 60 個交易日）
TWSE_MAX_DAYS = 45        # 證交所補資料最多往回幾個日曆天
STALE_DAYS = 4            # 最新資料距今超過幾個日曆天視為過期
UA = {"User-Agent": "Mozilla/5.0 (compatible; line-bot-lab/inst-flow-1.0)"}


# ------------------------------------------------------------------ 工具
def tw_now():
    return datetime.now(timezone.utc) + timedelta(hours=8)


def to_yi(v):
    return round(v / 1e8, 2)


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return None


def log(msg):
    print(msg, flush=True)


# ------------------------------------------------------------------ 來源 A：證交所
def parse_twse(js):
    """回傳 (rec 或 None, 說明)。"""
    if not isinstance(js, dict):
        return None, "回應不是 JSON"
    if js.get("stat") != "OK":
        return None, "無資料（假日／尚未公布）"
    vals = {}
    for row in js.get("data") or []:
        if len(row) < 4:
            continue
        name = str(row[0]).strip()
        diff = _num(row[3])
        if diff is None:
            continue
        # 「外資及陸資(不含外資自營商)」名稱內也含「外資自營商」，必須先比開頭
        if name.startswith("外資自營商"):
            k = "foreign_dealer"
        elif name.startswith("外資及陸資"):
            k = "foreign"
        elif name.startswith("投信"):
            k = "trust"
        elif name.startswith("自營商"):
            k = "dealer"          # 自行買賣 + 避險 加總
        elif name.startswith("合計"):
            k = "total"
        else:
            continue
        vals[k] = vals.get(k, 0.0) + diff
    if not all(k in vals for k in ("foreign", "trust", "dealer")):
        return None, "欄位不齊"
    rec = {k: to_yi(v) for k, v in vals.items()}
    rec.setdefault("foreign_dealer", 0.0)
    rec.setdefault("total", round(rec["foreign"] + rec["foreign_dealer"] + rec["trust"] + rec["dealer"], 2))
    return rec, ""


def fetch_twse(dates):
    out = {}
    fails = 0                      # 連續連線失敗次數；連續 3 次就放棄，避免被擋時空等
    for d in dates:
        if fails >= 3:
            log("  證交所連續 3 次連線失敗，放棄剩餘日期")
            break
        js = None
        for _ in range(2):
            try:
                r = requests.get("https://www.twse.com.tw/rwd/zh/fund/BFI82U",
                                 params={"response": "json", "dayDate": d.replace("-", ""), "type": "day"},
                                 headers=UA, timeout=15)
                if r.status_code == 200:
                    js = r.json()
                    break
            except Exception as e:
                log("  證交所 %s 連線例外：%s" % (d, type(e).__name__))
            time.sleep(1.5)
        if js is None:
            fails += 1
            log("  證交所 %s 連線失敗" % d)
        else:
            fails = 0
            rec, note = parse_twse(js)
            if rec:
                out[d] = rec
            elif note != "無資料（假日／尚未公布）":
                log("  證交所 %s 解析失敗：%s" % (d, note))
        time.sleep(1.0)
    return out


# ------------------------------------------------------------------ 來源 B：FinMind
def parse_finmind(rows):
    by_date = {}
    for r in rows:
        d = str(r.get("date", ""))[:10]
        low = str(r.get("name", "")).lower()
        try:
            diff = float(r.get("buy", 0)) - float(r.get("sell", 0))
        except Exception:
            continue
        if low == "foreign_investor":
            k = "foreign"
        elif low == "foreign_dealer_self":
            k = "foreign_dealer"
        elif low == "investment_trust":
            k = "trust"
        elif low.startswith("dealer"):
            k = "dealer"          # Dealer_self + Dealer_Hedging 加總
        elif low == "total":
            k = "total"
        else:
            continue
        slot = by_date.setdefault(d, {})
        slot[k] = slot.get(k, 0.0) + diff
    out = {}
    for d, v in by_date.items():
        if not all(k in v for k in ("foreign", "trust", "dealer")):
            continue
        rec = {k: to_yi(x) for k, x in v.items()}
        rec.setdefault("foreign_dealer", 0.0)
        rec.setdefault("total", round(rec["foreign"] + rec["foreign_dealer"] + rec["trust"] + rec["dealer"], 2))
        out[d] = rec
    return out


def fetch_finmind(start):
    params = {"dataset": "TaiwanStockTotalInstitutionalInvestors", "start_date": start}
    token = os.environ.get("FINMIND_TOKEN", "")
    if token:
        params["token"] = token
    for _ in range(2):
        try:
            r = requests.get("https://api.finmindtrade.com/api/v4/data", params=params, headers=UA, timeout=25)
            rows = r.json().get("data") or []
            if rows:
                return parse_finmind(rows)
            log("  FinMind 回傳 0 筆")
        except Exception as e:
            log("  FinMind 連線例外：%s" % type(e).__name__)
        time.sleep(2)
    return {}


# ------------------------------------------------------------------ 指標計算
def streak(vals):
    """由最新一天往回數連續同方向天數：連買為正數、連賣為負數、0 表示持平。"""
    if not vals or vals[-1] == 0:
        return 0
    sign = 1 if vals[-1] > 0 else -1
    n = 0
    for v in reversed(vals):
        if v * sign > 0:
            n += 1
        else:
            break
    return n * sign


def analyze(vals):
    """vals：由舊到新的每日買賣超（億元）。回傳摘要 dict。"""
    today = vals[-1]
    prev = vals[-2] if len(vals) >= 2 else None
    sum5 = round(sum(vals[-5:]), 2)
    sum5_prev = round(sum(vals[-6:-1]), 2) if len(vals) >= 6 else None
    sum20 = round(sum(vals[-20:]), 2)
    sells = [abs(v) for v in vals[-20:] if v < 0]
    avg_sell20 = round(sum(sells) / len(sells), 2) if sells else None

    signals = []
    # ① 轉買超：今天買超，前一天是賣超
    if today > 0 and prev is not None and prev < 0:
        signals.append("轉買超")
    # ② 賣壓縮小：今天仍賣超，但金額不到近 20 日「平均賣超金額」的一半（至少要有 10 天資料）
    if today < 0 and avg_sell20 and len(vals) >= 10 and abs(today) < 0.5 * avg_sell20:
        signals.append("賣壓縮小")
    # ③ 5 日累計翻正：近 5 日累計 > 0，且昨天的 5 日累計 <= 0
    if sum5 > 0 and sum5_prev is not None and sum5_prev <= 0:
        signals.append("5日累計翻正")

    return {
        "today": today, "sum5": sum5, "sum20": sum20,
        "streak": streak(vals), "avg_sell20": avg_sell20, "signals": signals,
    }


def build_payload(hist, src):
    dates = sorted(hist)[-KEEP_DAYS:]
    days = []
    for d in dates:
        r = hist[d]
        days.append({"date": d, "foreign": r["foreign"], "trust": r["trust"], "dealer": r["dealer"],
                     "total": r["total"], "foreign_dealer": r.get("foreign_dealer", 0.0), "src": src.get(d, "")})
    latest = dates[-1]
    age = (tw_now().date() - datetime.strptime(latest, "%Y-%m-%d").date()).days
    return {
        "version": VERSION,
        "unit": "億元",
        "latest_date": latest,
        "data_quality": "ok" if age <= STALE_DAYS else "stale",
        "days": days,
        "summary": {
            "foreign": analyze([x["foreign"] for x in days]),
            "trust": analyze([x["trust"] for x in days]),
            "dealer": analyze([x["dealer"] for x in days]),
            "total": analyze([x["total"] for x in days]),
        },
    }


# ------------------------------------------------------------------ 主流程
def load_existing():
    try:
        with open(OUT_FILE, encoding="utf-8") as f:
            js = json.load(f)
        hist, src = {}, {}
        for x in js.get("days", []):
            hist[x["date"]] = {k: x[k] for k in ("foreign", "trust", "dealer", "total")}
            hist[x["date"]]["foreign_dealer"] = x.get("foreign_dealer", 0.0)
            src[x["date"]] = x.get("src", "")
        return hist, src, js
    except Exception:
        return {}, {}, None


def weekdays(start_date, end_date):
    out, cur = [], start_date
    while cur <= end_date:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


def main():
    now = tw_now()
    today = now.date()
    log("InstitutionalFlow v%s｜台灣時間 %s｜FinMind token：%s" %
        (VERSION, now.strftime("%Y-%m-%d %H:%M"), "有" if os.environ.get("FINMIND_TOKEN") else "無"))

    hist, src, old_js = load_existing()
    log("既有資料：%d 日（最新 %s）" % (len(hist), max(hist) if hist else "無"))

    # 1) FinMind：一次抓整段歷史
    fm = fetch_finmind((today - timedelta(days=FINMIND_LOOKBACK)).strftime("%Y-%m-%d"))
    log("FinMind：取得 %d 日" % len(fm))
    for d, rec in fm.items():
        hist[d] = rec
        src[d] = "finmind"

    # 2) 證交所：補 FinMind／既有資料之後的日子（含今天）
    latest = datetime.strptime(max(hist), "%Y-%m-%d").date() if hist else None
    start = (latest + timedelta(days=1)) if latest else (today - timedelta(days=TWSE_MAX_DAYS))
    start = max(start, today - timedelta(days=TWSE_MAX_DAYS))
    tw_dates = weekdays(start, today)
    tw = {}
    if tw_dates:
        tw = fetch_twse(tw_dates)
        log("證交所：查 %d 天，取得 %d 日" % (len(tw_dates), len(tw)))
        for d, rec in tw.items():
            hist[d] = rec
            src[d] = "twse"

    if len(hist) < 2:
        log("❌ 取得的資料不足，不更新檔案（保留舊檔）。")
        return 0

    payload = build_payload(hist, src)

    # 3) 與舊檔比對：沒變就不重寫，避免無謂 commit / 重新部署
    if old_js:
        a = {k: v for k, v in old_js.items() if k not in ("updated_at", "data_quality")}
        b = {k: v for k, v in payload.items() if k not in ("updated_at", "data_quality")}
        if a == b and old_js.get("data_quality") == payload["data_quality"]:
            if not fm and not tw:
                log("⚠️ 兩個來源這次都沒有取得新資料（可能連線失敗或休市），沿用舊檔（最新 %s）。" % payload["latest_date"])
            else:
                log("✅ 資料無變動（最新 %s），不重寫檔案。" % payload["latest_date"])
            return 0

    payload["updated_at"] = now.strftime("%Y-%m-%d %H:%M")
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_FILE)

    s = payload["summary"]
    log("✅ 已寫入 %s：最新 %s（%s）共 %d 日" % (OUT_FILE, payload["latest_date"], payload["data_quality"], len(payload["days"])))
    for key, label in (("foreign", "外資"), ("trust", "投信")):
        x = s[key]
        log("  %s 今日 %+.2f 億｜5日 %+.2f｜20日 %+.2f｜連%s %d 日｜訊號：%s" % (
            label, x["today"], x["sum5"], x["sum20"], "買" if x["streak"] > 0 else "賣", abs(x["streak"]),
            "、".join(x["signals"]) or "無"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
