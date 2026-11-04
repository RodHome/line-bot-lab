import yfinance as yf
import pandas as pd
import numpy as np
import requests
import json
import datetime
from pathlib import Path

# ==========================================
# 1. 參數與常數設定
# ==========================================
START_DATE = "2018-01-01" # 確保有足夠暖機期計算 250 日滾動與 60 日均線
OUTPUT_FILE = "market_risk.json"

# 74 檔廣度大型樣本股 (已排除 3529)
TICKERS = [
    "2330","2317","2454","2308","2412","2882","2881","2891","2886","1301","1303","2002","3711","2303","2884",
    "2885","2892","5880","2207","1216","2357","3034","2327","2379","4938","2395","3008","2382","2345","1101","5871",
    "2603","2609","2615","3045","2890","2883","1326","2301","2408","3037","3231","2376","6669","2368","3017","2474",
    "2049","1590","2105","9904","2912","1102","2801","2887","5876","6415","3443","3661","3533","2356","2353","2324",
    "2344","2449","6770","8046","2409","3481","2618","2610","2006","1402","1476"
]

LV_NAMES = ["正常", "注意", "警戒", "危險"]

# ==========================================
# 2. 核心運算函數
# ==========================================
def below_state(close, ma, b=0.01, n=2):
    """均線狀態機：跌破 MA*(1-b) 連續 n 天進入，站回 MA*(1+b) 連續 n 天解除"""
    below = False; dn = 0; up = 0; out = []
    for c, m in zip(close, ma):
        if pd.isna(m):
            out.append(False); continue
        if not below:
            dn = dn + 1 if c < m*(1-b) else 0
            if dn >= n: below = True; up = 0
        else:
            up = up + 1 if c > m*(1+b) else 0
            if up >= n: below = False; dn = 0
        out.append(below)
    return pd.Series(out, index=close.index)

def keep(s, w=5):
    """事件發生後保留 w 個交易日"""
    return s.fillna(False).astype(float).rolling(w, min_periods=1).max().astype(bool)

def buffered_level(raw_series, n=3):
    """降級緩衝：調升即時，調降需連續 n 天維持較低"""
    raw = list(raw_series); out = []; cur = 0
    for i, r in enumerate(raw):
        if r >= cur:
            cur = r
        else:
            m = max(raw[max(0, i-n+1): i+1])
            if m < cur: cur = m
        out.append(cur)
    return pd.Series(out, index=raw_series.index)

# ==========================================
# 3. 資料獲取與主程式
# ==========================================
def generate_market_risk_report():
    print("啟動大盤風險掃描 (Market Risk Monitor)...")
    
    # [資料 1] 加權指數
    raw_twii = yf.download("^TWII", start=START_DATE, auto_adjust=True, progress=False)
    if isinstance(raw_twii.columns, pd.MultiIndex):
        raw_twii.columns = raw_twii.columns.get_level_values(0)
    df = raw_twii[["Close"]].dropna().copy()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None) # 移除時區差異
    df["MA20"] = df["Close"].rolling(20).mean()
    df["MA60"] = df["Close"].rolling(60).mean()
    hi60 = df["Close"].rolling(60).max()
    
    # 盤中執行防護：丟棄當天未收盤資料
    now = datetime.datetime.now()
    if now.hour < 13 or (now.hour == 13 and now.minute < 30):
        df = df.iloc[:-1] 

    # [資料 2] 廣度指標
    print(f"下載 {len(TICKERS)} 檔廣度樣本股...")
    px = yf.download([t+".TW" for t in TICKERS], start=START_DATE, auto_adjust=True, progress=False)["Close"]
    if isinstance(px.columns, pd.MultiIndex):
         px.columns = px.columns.get_level_values(0)
    if px.index.tz is not None:
        px.index = px.index.tz_localize(None) # 移除時區差異
        
    px = px.dropna(axis=1, how="all").reindex(df.index).ffill(limit=3)
    breadth_stock_count = px.shape[1]
    
    ma20s = px.rolling(20).mean()
    v20 = ma20s.notna()
    b20 = ((px > ma20s) & v20).sum(axis=1) / v20.sum(axis=1).replace(0, np.nan)
    b20 = b20.reindex(df.index) # 強制索引完全對齊大盤 df.index
    
    # [資料 3] 外資期貨淨空單
    print("抓取 FinMind 外資期貨籌碼...")
    res = requests.get(
        "https://api.finmindtrade.com/api/v4/data", 
        params={"dataset": "TaiwanFuturesInstitutionalInvestors", "data_id": "TX", "start_date": START_DATE},
        timeout=30
    ).json()
    
    fut = pd.DataFrame(res.get("data", []))
    if not fut.empty:
        f = fut[fut["institutional_investors"].str.contains("外資|Foreign", regex=True)].copy()
        f["net_oi"] = f["long_open_interest_balance_volume"] - f["short_open_interest_balance_volume"]
        fnet = f.groupby("date")["net_oi"].sum()
        fnet.index = pd.to_datetime(fnet.index)
    else:
        fnet = pd.Series(dtype=float)

    # 組合資料對齊大盤交易日
    d = df.join(fnet.rename("fnet")).ffill()
    d["chg5"] = d["fnet"].diff(5)
    thr_roll = d["chg5"].rolling(250, min_periods=120).quantile(0.10).shift(1) # shift(1) 避免前視偏誤
    chip_surge = d["chg5"] <= thr_roll

    # ==========================================
    # 4. 判斷六大跡象與計分
    # ==========================================
    sig = pd.DataFrame(index=df.index)
    sig["指數破MA20"]   = below_state(df["Close"], df["MA20"], 0.01, 2)
    sig["指數破MA60"]   = below_state(df["Close"], df["MA60"], 0.01, 2)
    sig["廣度<50%"]     = (b20 < 0.50).fillna(False)
    sig["廣度急降"]     = keep(b20.diff(3) <= -0.20)
    sig["高檔背離"]     = keep((df["Close"] >= hi60*0.98) & (b20 < 0.60))
    sig["外資空單急增"] = keep(chip_surge.reindex(df.index))
    
    # ==========================================
    # 5. 資料品質校驗與狀態決策
    # ==========================================
    last_date = df.index[-1]
    status = "OK"
    missing_fields = []
    note = ""
    
    foreign_last_date = fnet.index[-1] if not fnet.empty else None
    if foreign_last_date is None or foreign_last_date < last_date:
        status = "PARTIAL_DATA"
        missing_fields.append("foreign_futures")
        note = "外資期貨資料未更新，今日分數排除空單跡象。"
        sig.loc[last_date, "外資空單急增"] = False 
        
    if breadth_stock_count < 60:
        status = "DATA_ERROR"
        note = "廣度樣本股數量不足，請檢查 yfinance 連線。"

    # 計算分數與緩衝等級
    # 規則：0-1(正常:0), 2(注意:1), 3(警戒:2), 4-6(危險:3)
    score = sig.sum(axis=1)
    raw_lv = pd.Series(np.select([score >= 4, score == 3, score == 2], [3, 2, 1], 0), index=df.index)
    buf_lv = buffered_level(raw_lv, n=3)
    
    # 取今日最終狀態
    today_score = int(score.loc[last_date])
    today_lv = int(buf_lv.loc[last_date])
    today_raw_lv = int(raw_lv.loc[last_date])
    lit_signals = [c for c in sig.columns if bool(sig.loc[last_date, c])]
    
    # ==========================================
    # 6. 產出 JSON
    # ==========================================
    output = {
        "date": last_date.strftime("%Y-%m-%d"),
        "close": int(df.loc[last_date, "Close"]),
        "level": today_lv,
        "level_name": LV_NAMES[today_lv],
        "raw_level": today_raw_lv,
        "score": today_score,
        "lit_signals": lit_signals,
        "signals": sig.loc[last_date].astype(bool).to_dict(),
        "indicators": {
            "b20": round(float(b20.loc[last_date]), 4) if pd.notna(b20.loc[last_date]) else None,
            "foreign_net_oi": int(d.loc[last_date, "fnet"]) if pd.notna(d.loc[last_date, "fnet"]) else None,
            "foreign_chg5": int(d.loc[last_date, "chg5"]) if pd.notna(d.loc[last_date, "chg5"]) else None,
        },
        "data_quality": {
            "status": status,
            "index_last_date": last_date.strftime("%Y-%m-%d"),
            "foreign_last_date": foreign_last_date.strftime("%Y-%m-%d") if foreign_last_date else None,
            "breadth_stock_count": breadth_stock_count,
            "missing_fields": missing_fields,
            "note": note
        },
        "version": {"model": "1.0.0", "threshold": "B-rolling250-buf3"}
    }
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
        
    print(f"風險掃描完成！狀態：{LV_NAMES[today_lv]}，已儲存至 {OUTPUT_FILE}")

if __name__ == "__main__":
    generate_market_risk_report()
