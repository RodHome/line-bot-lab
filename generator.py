import requests
import pandas as pd
import json
import re
import os
import time
from datetime import datetime, timedelta, timezone
from io import StringIO
import yfinance as yf
import math   # 👈 1. 新增這行！(處理 NaN 必備)

# 🔥 雙鑰匙負載平衡系統：合併訪客與會員額度 (總計 900次/小時)
GUEST_TOKEN = "" # 訪客鑰匙 (消耗 IP 免費 300 次)
VIP_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJkYXRlIjoiMjAyNi0wMy0xOCAxOToyODoyNCIsInVzZXJfaWQiOiJyb2Q3NDEwMDEyIiwiZW1haWwiOiJyb2Q3NDEwMDFAZ21haWwuY29tIiwiaXAiOiIxMjIuMTE2LjE1OS4xMzQifQ.qmaLCfxjbwXRYo8TwFZKboTfmAADIMs0CWw-oPUJU4g"

# 👇👇👇 2. 新增這段「除毒淨水器」函數 👇👇👇
def clean_nan(data):
    if isinstance(data, list):
        return [clean_nan(item) for item in data]
    elif isinstance(data, dict):
        return {k: clean_nan(v) for k, v in data.items()}
    elif isinstance(data, float) and math.isnan(data):
        return None  # 把 NaN 變成安全的 null
    else:
        return data
# 👆👆👆 新增結束 👆👆👆

# 🔥 [新增模組] 計算 RSI
def calculate_rsi(prices, period=14):
    if len(prices) < period + 1: return 50
    gains = []; losses = []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        gains.append(max(0, change))
        losses.append(max(0, -change))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


# 🔥 [新增模組] 長線記憶融合大腦 (30天回測水庫與初始價格鎖定)
def merge_history_data(today_data, file_name, sort_key):
    history_dict = {}
    # 1. 嘗試讀取現有的舊檔案
    if os.path.exists(file_name):
        try:
            with open(file_name, 'r', encoding='utf-8') as f:
                old_data = json.load(f)
                if isinstance(old_data, list):
                    for item in old_data:
                        code = item.get('code')
                        if code: history_dict[code] = item
        except Exception as e:
            print(f"⚠️ 讀取 {file_name} 歷史資料失敗: {e}")

    # 2. 將今日新資料與歷史資料融合 (Upsert)
    today_date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    for item in today_data:
        code = item['code']
        item_date = item.get('date', today_date_str)
        
        # 鎖定初次入榜日與初次價格
        first_date = history_dict.get(code, {}).get('first_entry_date', item_date)
        first_price = history_dict.get(code, {}).get('first_entry_price', item.get('price', 0.0))
        
        new_item = item.copy()
        new_item['first_entry_date'] = first_date
        new_item['first_entry_price'] = first_price
        
        history_dict[code] = new_item

    # 3. 過濾出最近 30 個交易日的資料
    all_dates = set(v.get('date') for v in history_dict.values() if v.get('date'))
    allowed_dates = sorted(list(all_dates), reverse=True)[:30]
    
    final_list = [v for v in history_dict.values() if v.get('date') in allowed_dates]
    final_list.sort(key=lambda x: x.get(sort_key, 0), reverse=True)
    
    return final_list

def get_finmind_chips(code):
    """查詢近 5 日法人買超張數 (抗長假 30 天版)"""
    start = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    url = "https://api.finmindtrade.com/api/v4/data"
    try:
        res = requests.get(url, params={"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": code, "start_date": start, "token": GUEST_TOKEN}, timeout=10)
        data = res.json().get('data', [])
        if not data: return 0, 0
        unique_dates = sorted(list(set([d['date'] for d in data])), reverse=True)
        target_dates = unique_dates[:5]
        acc_f = 0; acc_t = 0
        for row in data:
            if row['date'] in target_dates:
                val = (row['buy'] - row['sell']) // 1000
                if row['name'] == 'Foreign_Investor': acc_f += val
                elif row['name'] == 'Investment_Trust': acc_t += val
        return acc_f, acc_t
    except: return 0, 0

def get_finmind_revenue_yoy(code):
    """查詢營收，自動對齊去年同月，並回傳開發者查核數據"""
    # 抓取過去 480 天，確保涵蓋 16 個月以便對齊去年同期
    start = (datetime.now() - timedelta(days=480)).strftime('%Y-%m-%d')
    url = "https://api.finmindtrade.com/api/v4/data"
    # 預設回傳格式 (現在改為回傳字典)
    default_res = {
        "yoy": 0.0, 
        "debug_info": {"status": "No Data", "this_rev": 0, "last_rev": 0, "this_period": "N/A", "last_period": "N/A"}
    }
    
    try:
        res = requests.get(url, params={"dataset": "TaiwanStockMonthRevenue", "data_id": code, "start_date": start, "token": GUEST_TOKEN}, timeout=10)
        data = res.json().get('data', [])
        
        if not data: return default_res
            
        # 依日期由新到舊排序 (年、月雙重排序，徹底防呆)
        data.sort(key=lambda x: (x['revenue_year'], x['revenue_month']), reverse=True)
        
        # 嘗試從最新一筆開始，往回找去年同月
        for i in range(len(data)):
            target = data[i]
            t_rev = target['revenue']
            t_y = target['revenue_year']
            t_m = target['revenue_month']
            
            # 尋找去年同月 (年份 -1 且 月份相同)
            last_year_data = next((row for row in data if row['revenue_year'] == t_y - 1 and row['revenue_month'] == t_m), None)
            
            if last_year_data:
                l_rev = last_year_data['revenue']
                if l_rev == 0: continue
                yoy = round(((t_rev - l_rev) / l_rev) * 100, 2)
                
                return {
                    "yoy": yoy,
                    "debug_info": {
                        "this_rev": t_rev,
                        "last_rev": l_rev,
                        "this_period": f"{t_y}/{t_m}",
                        "last_period": f"{t_y-1}/{t_m}",
                        "formula": f"({t_rev} - {l_rev}) / {l_rev}"
                    }
                }
        return default_res
    except Exception as e:
        default_res["debug_info"]["status"] = f"Error: {str(e)}"
        return default_res

# 🔥 [為左側雷達新增] 專門抓取近 N 日的法人買賣超陣列
def get_finmind_chips_history(code, days=3):
    start = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    url = "https://api.finmindtrade.com/api/v4/data"
    history = []
    try:
        res = requests.get(url, params={"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": code, "start_date": start, "token": GUEST_TOKEN}, timeout=10)
        data = res.json().get('data', [])
        if not data: return [0]*days
        
        unique_dates = sorted(list(set([d['date'] for d in data])), reverse=True)
        target_dates = unique_dates[:days]
        target_dates.reverse() # 把舊的排前面，新的排後面
        
        for t_date in target_dates:
            daily_net = 0
            for row in data:
                if row['date'] == t_date:
                    val = (row['buy'] - row['sell']) // 1000
                    if row['name'] in ['Foreign_Investor', 'Investment_Trust']:
                        daily_net += val
            history.append(daily_net)
        return history
    except: return [0]*days

# ==========================================
# 區塊：FinMind 基本面與殖利率查詢 (雙軌分流版)
# 用途：EPS 扣訪客額度，殖利率扣 VIP 額度，極大化 API 使用率
# ==========================================
def get_finmind_fundamentals(code, current_price, fetch_yield=True):
    eps_latest = 0.0
    yield_rate = 0.0
    annual_div = 0.0
    
    start = (datetime.now() - timedelta(days=800)).strftime('%Y-%m-%d')
    url = "https://api.finmindtrade.com/api/v4/data"
    
    # 1. 抓取最新 EPS (🔥 分流：消耗 GUEST_TOKEN 免費額度)
    try:
        res = requests.get(url, params={"dataset": "TaiwanStockFinancialStatements", "data_id": code, "start_date": start, "token": GUEST_TOKEN}, timeout=5)
        if res.status_code == 200:
            data = res.json().get('data', [])
            eps_data = [d for d in data if d['type'] == 'EPS']
            if eps_data: eps_latest = float(eps_data[-1].get('value', 0))
    except: pass
    
    # 🌟 額度防護機制：Task 3 到此為止，回傳 3 個變數
    if not fetch_yield:
        return eps_latest, yield_rate, annual_div

    # 2. 抓取殖利率 (🔥 分流：只有 Task 4 會走到這，消耗 VIP_TOKEN 額度)
    try:
        res_div = requests.get(url, params={"dataset": "TaiwanStockDividend", "data_id": code, "start_date": start, "token": VIP_TOKEN}, timeout=10)
        if res_div.status_code == 200:
            data_div = res_div.json().get('data', [])
            if data_div:
                data_div = sorted(data_div, key=lambda x: x.get('date', ''))
                valid_cash_records = []
                for d in data_div:
                    v1 = d.get('CashEarningsDistribution') or 0
                    v2 = d.get('CashStatutorySurplus') or 0
                    v3 = d.get('CashCapitalReserve') or 0
                    total = float(v1) + float(v2) + float(v3)
                    if total > 0:
                        valid_cash_records.append({'date': d.get('date'), 'cash': total})
                
                if valid_cash_records:
                    valid_cash_records = sorted(valid_cash_records, key=lambda x: x['date'], reverse=True)
                    latest_cash = valid_cash_records[0]['cash']
                    multiplier = 1
                    if len(valid_cash_records) >= 2:
                        d_new = datetime.strptime(valid_cash_records[0]['date'], '%Y-%m-%d')
                        d_old = datetime.strptime(valid_cash_records[1]['date'], '%Y-%m-%d')
                        days_diff = (d_new - d_old).days
                        if days_diff <= 45: multiplier = 12
                        elif days_diff <= 120: multiplier = 4
                        elif days_diff <= 240: multiplier = 2
                    
                    # 🔥 計算分子並回傳
                    annual_div = round(latest_cash * multiplier, 3)
                    if current_price > 0:
                        yield_rate = round((annual_div / current_price) * 100, 2)
    except: pass
        
    return eps_latest, yield_rate, annual_div

# ==========================================
# 🔥 [終極重構] 殖利率與除息日一體化運算引擎 (自己的殖利率自己算)
# ==========================================
def get_latest_dividend_info(code, current_price):
    is_etf = str(code).startswith('00')
    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    
    # 預設狀態：直接套用你的「過期遮蔽歸零」邏輯
    yield_rate = 0.0
    formula = "⚠️ 已除息或尚未宣告" 
    ex_date_for_json = None
    is_upcoming = False
    
    start_date = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d')
    url = "https://api.finmindtrade.com/api/v4/data"
    
    try:
        res = requests.get(url, params={"dataset": "TaiwanStockDividend", "data_id": code, "start_date": start_date, "token": VIP_TOKEN}, timeout=10)
        data = res.json().get('data', [])
        if not data:
            return yield_rate, formula, ex_date_for_json, is_upcoming
            
        data.sort(key=lambda x: x.get('date', ''), reverse=True)
        latest_record = data[0]
        raw_ex_date = latest_record.get('CashExDividendTradingDate') or latest_record.get('StockExDividendTradingDate')
        
        # 🎯 任務 1：檢查除息日。如果有未來除息日，才放行
        if raw_ex_date and raw_ex_date >= today_str:
            ex_date_for_json = raw_ex_date
            is_upcoming = True
        else:
            # 💡 [保留你的神邏輯]：除息日過期，直接回傳預設的 0.0 與遮蔽文字
            return yield_rate, formula, ex_date_for_json, is_upcoming

        # 🎯 任務 2：只有在即將除息 (is_upcoming=True) 時，才計算真實最新殖利率
        if current_price > 0:
            if is_etf:
                total_cash = sum([float(d.get('CashEarningsDistribution', 0)) for d in data])
                if total_cash > 0:
                    yield_rate = round((total_cash / current_price) * 100, 2)
                    formula = f"ETF推算(近一年): {round(total_cash, 3)} / 現價 {current_price}"
            else:
                target_year = latest_record.get('year')
                if target_year:
                    total_cash = sum([
                        float(d.get('CashEarningsDistribution') or 0) + 
                        float(d.get('CashStatutorySurplus') or 0) + 
                        float(d.get('CashCapitalReserve') or 0)
                        for d in data if str(d.get('year', '')) == str(target_year)
                    ])
                    if total_cash > 0:
                        yield_rate = round((total_cash / current_price) * 100, 2)
                        formula = f"最新宣告({target_year}): {round(total_cash, 3)} / 現價 {current_price}"

        return yield_rate, formula, ex_date_for_json, is_upcoming

    except Exception as e:
        return 0.0, f"股利運算錯誤: {e}", None, False

# ==========================================
# 🔥 [新增模組] 歷史標的共用同步大腦 (DRY 原則)
# ==========================================
def sync_historical_data(file_name, today_codes, strategy_type, taiwan_50_list=None):
    """
    統一處理左側與右側老標的之股價同步、除息日更新與動能重新計分
    """
    updated_history = []
    print(f"🔄 正在同步 {file_name} 歷史標的最新現價與除息資訊...")
    
    if not os.path.exists(file_name):
        return updated_history

    try:
        with open(file_name, 'r', encoding='utf-8') as f:
            history_stocks = json.load(f)
        
        for old_s in history_stocks:
            code = old_s['code']
            # 若今日未被掃到，則啟動歷史標的更新機制
            if code not in today_codes:
                try:
                    # 1. 統一判斷上市櫃並更新股價
                    exchange_type = old_s.get('exchange', '上市')
                    suffix = ".TWO" if exchange_type == '上櫃' else ".TW"
                    ticker = yf.Ticker(f"{code}{suffix}")
                    hist = ticker.history(period="1d")

                    if not hist.empty:
                        # 更新最新收盤價與資料日期
                        new_p = round(float(hist['Close'].iloc[-1]), 2)
                        old_s['price'] = new_p
                        old_s['date'] = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                        
                        # 2. 檢查並更新除息日狀態
                        _, _, ex_date, is_upcoming = get_latest_dividend_info(code, new_p)
                        if is_upcoming:
                            old_s['ex_dividend_date'] = ex_date
                        else:
                            # 拔除過期日期，並遮蔽左側殖利率
                            old_s.pop('ex_dividend_date', None)
                            if strategy_type == 'LEFT':
                                old_s['yield_rate'] = 0.0
                                old_s['yield_formula'] = "⚠️ 已除息或尚未宣告"

                  

                        # 3. 若為右側標的，重新計算動能分數 (m_score)
                        if strategy_type == 'RIGHT' and taiwan_50_list:
                            old_s['cap_size'] = "大型權值股" if code in taiwan_50_list else "中小型股"
                            yoy_val = old_s.get('yoy', 0)
                            buy_val = old_s.get('buy_value', 0)
                            buy_val_y = buy_val / 100000000
                            m_score = (min(yoy_val, 100) * 1.5) + (min(buy_val_y, 10) * 5)
                            if old_s['cap_size'] == "中小型股":
                                m_score = m_score * 1.2
                            old_s['m_score'] = round(m_score, 2)
                        
                        updated_history.append(old_s) 
                        
                except Exception as e:
                    print(f"⚠️ 學長 {code} 更新股價失敗: {e}")
    except Exception as e:
        print(f"⚠️ 讀取 {file_name} 失敗: {e}")

    return updated_history

# ========================================================
# --- 功能 1: 抓取所有股票代號與產業分類 (精準過濾版) ---
def update_stock_list_json():
    print("🚀 [Task 1] 開始抓取所有股票代號與產業分類...")
    
    # 🔥 將原本 app.py 裡的自訂標籤移到這裡，作為「覆寫規則」
    CUSTOM_ETF_META = {
        "00878": {"name": "國泰永續高股息", "type": "高股息ETF", "sector": "ESG/殖利率/填息"},
        "0056":  {"name": "元大高股息", "type": "高股息ETF", "sector": "預測殖利率/填息"},
        "00919": {"name": "群益台灣精選高息", "type": "高股息ETF", "sector": "殖利率/航運半導體週期"},
        "00929": {"name": "復華台灣科技優息", "type": "高股息ETF", "sector": "月配息/科技股景氣"},
        "00713": {"name": "元大台灣高息低波", "type": "高股息ETF", "sector": "低波動/防禦性"},
        "00940": {"name": "元大台灣價值高息", "type": "高股息ETF", "sector": "月配息/價值投資"},
        "00939": {"name": "統一台灣高息動能", "type": "高股息ETF", "sector": "動能指標/月底領息"},
        "0050":  {"name": "元大台灣50", "type": "市值型ETF", "sector": "大盤乖離/台積電展望"},
        "006208":{"name": "富邦台50", "type": "市值型ETF", "sector": "大盤乖離/台積電展望"},
        "00881": {"name": "國泰台灣5G+", "type": "科技型ETF", "sector": "半導體/通訊供應鏈/台積電"},
        "00679B":{"name": "元大美債20年", "type": "債券型ETF", "sector": "美債殖利率/降息預期"},
        "00687B":{"name": "國泰20年美債", "type": "債券型ETF", "sector": "美債殖利率/降息預期"}
    }

    # 菁英股的熱門產業標籤
    CUSTOM_ELITE_DATA = {
        "2330": "半導體", "2317": "AI伺服器", "2454": "IC設計", "2382": "AI伺服器",
        "3231": "AI伺服器", "2376": "板卡", "2603": "航運", "2609": "航運",
        "1519": "重電", "1503": "重電", "3017": "散熱", "3324": "散熱"
    }
    
    urls = [
        "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2", # 上市
        "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"  # 上櫃
    ]
    
    stock_map = {}

    for url in urls:
        try:
            res = requests.get(url, timeout=10)
            # 🔥 修復 Pandas 警告，使用 StringIO 包裝 HTML 內容
            dfs = pd.read_html(StringIO(res.text))
            df = dfs[0]
            
            df.columns = df.iloc[0]
            df = df.iloc[1:]
            
            # 找出欄位名稱
            col_code_name = [c for c in df.columns if "有價證券代號" in str(c)]
            col_sector = [c for c in df.columns if "產業別" in str(c)]
            if not col_code_name: continue
            
            name_col = col_code_name[0]
            sector_col = col_sector[0] if col_sector else None
            
            for index, row in df.iterrows():
                item = str(row[name_col]).strip()
                sector_val = str(row[sector_col]).strip() if sector_col else "未知產業"
                if sector_val == 'nan': sector_val = "無"
                
                # 抓出代號與名稱
                match = re.match(r'^([A-Z0-9]{4,6})\s+(.+)', item)
                if match:
                    code = match.group(1)
                    name = match.group(2).strip()
                    
                    # 🛡️ 【關鍵過濾器】：排除四萬檔權證與可轉債
                    is_normal_stock = (len(code) == 4 and code.isdigit()) # 條件 1: 四碼純數字 (一般股票)
                    is_etf = code.startswith('00')                        # 條件 2: 00 開頭 (ETF)
                    
                    if not (is_normal_stock or is_etf):
                        continue # 不是一般股票也不是 ETF，直接跳過不收錄
                    
                    # 套用覆寫規則：若是菁英股，替換為我們自訂的熱門標籤
                    if code in CUSTOM_ELITE_DATA:
                        sector_val = CUSTOM_ELITE_DATA[code]
                        
                    stock_map[code] = {
                        "name": name,
                        "sector": sector_val,
                        "type": "股票"
                    }
        except Exception as e:
            print(f"⚠️ [Task 1] 抓取錯誤 ({url}): {e}")

    # 將 ETF 專屬資訊合併進去 (覆蓋掉爬蟲抓的生硬分類)
    for code, meta in CUSTOM_ETF_META.items():
        stock_map[code] = meta

    print(f"✅ [Task 1] 完成，共過濾出 {len(stock_map)} 檔純股票與ETF -> 存入 stock_list.json")

    # 存檔 1 (新版結構)
    with open('stock_list.json', 'w', encoding='utf-8') as f:
        json.dump(stock_map, f, ensure_ascii=False, indent=2)

# --- 功能 2: 抓取每日熱門飆股 (建立推薦菜單) ---
def generate_daily_recommendations():
    print("\n🚀 [Task 2] 開始分析每日熱門飆股...")
    
    # 👇 新增：台灣 50 權值股名單
    TAIWAN_50 = [
        "2330", "2317", "2454", "2382", "2308", "2881", "2412", "2882", "2891", "2886", 
        "1303", "2884", "1216", "2892", "2002", "2885", "3231", "2303", "2890", "2880", 
        "2883", "5880", "1301", "2345", "3711", "2887", "1101", "2324", "2357", "3045", 
        "2395", "1326", "2603", "3008", "3036", "6669", "3661", "2408", "2207", "4904", 
        "1519", "1590", "9904", "2353", "6505", "2368", "7769", "2449", "3037", "3653"
    ]
    
    # 🔥 [新增] 讀取剛剛產生的 stock_list.json，用來查詢名稱與產業別
    stock_meta = {}
    try:
        if os.path.exists('stock_list.json'):
            with open('stock_list.json', 'r', encoding='utf-8') as f:
                stock_meta = json.load(f)
    except Exception as e:
        print(f"⚠️ 讀取 stock_list.json 失敗: {e}")

    # 設定目標日期 (GitHub Actions 通常在 UTC 時間跑，台灣+8)
    utc_now = datetime.now(timezone.utc)
    tw_now = utc_now + timedelta(hours=8)
    
    if tw_now.hour < 14: 
        target_date = (tw_now - timedelta(days=1)).strftime('%Y%m%d')
    else:
        target_date = tw_now.strftime('%Y%m%d')

    print(f"📅 目標日期: {target_date}")
    
    url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&type=ALLBUT0999&date={target_date}"
    
    final_list = []
    
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        
        if data.get('stat') != 'OK':
            print(f"⚠️ [Task 2] 今日 ({target_date}) 無資料或休市: {data.get('stat')}")
            # 若無資料(例如假日)，嘗試不帶日期參數，抓取「最新交易日」
            print("🔄 嘗試抓取最新交易日資料...")
            url_latest = "https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&type=ALLBUT0999"
            res = requests.get(url_latest, timeout=10)
            data = res.json()
        
        if data.get('stat') == 'OK':
            # 解析資料表
            target_table = None
            # 尋找包含股價的表格 (通常是 data9 或 title 含 '每日收盤行情')
            if 'tables' in data:
                for table in data['tables']:
                    if '證券代號' in table.get('fields', []) and '收盤價' in table.get('fields', []):
                        target_table = table
                        break
            # 舊版 API 相容
            elif 'data9' in data:
                target_table = {'data': data['data9'], 'fields': data.get('fields9', [])}

            if target_table:
                raw_data = target_table['data']
                fields = target_table['fields']
                
                # 動態找索引位置
                try:
                    idx_code = fields.index("證券代號")
                    idx_vol = fields.index("成交股數")
                    idx_turnover = fields.index("成交金額") # 🔥 新增成交金額
                    idx_price = fields.index("收盤價")
                    idx_sign = fields.index("漲跌(+/-)")
                except:
                    idx_code, idx_vol, idx_turnover, idx_price, idx_sign = 0, 2, 4, 8, 9 # 預設值

                candidates = []
                for row in raw_data:
                    try:
                        code = row[idx_code]
                        # 過濾權證、ETF(00開頭)、DR股(91開頭) -> 若你想保留 ETF，可移除 00 判斷
                        if len(code) > 4 or code.startswith('91') or code.startswith('00'): continue 
                        
                        price_str = row[idx_price].replace(',', '')
                        turnover_str = row[idx_turnover].replace(',', '')
                        
                        if price_str == '--' or turnover_str == '--': continue
                        price = float(price_str)
                        turnover = float(turnover_str)
                        
                        # 🔥 選股邏輯：價格 > 10元
                        if price < 10: continue
                        
                        sign = row[idx_sign]
                        is_up = ('+' in sign) or ('red' in sign) # 簡單判斷漲勢
                        
                        # 🔥 動能濾網升級：收紅，且單日成交金額大於 1 億元 (100,000,000)
                        if is_up and turnover > 100000000: 
                            # ⚠️ 這裡一定要把 price 存進來，FinMind 才能算金額！
                            candidates.append({"code": code, "turnover": turnover, "price": price, "exchange": "上市"})
                    except: continue

                # 👇👇👇 從這裡開始替換【上櫃 (TPEx) 爬蟲】 👇👇👇
                print(f"🔄 正在尋找最新上櫃 (TPEx) 行情...")
                
                data_otc = None
                valid_roc_date = None
                base_date = datetime.strptime(target_date, '%Y%m%d')
                
                # 🛡️ 加上 User-Agent 偽裝成瀏覽器，避免被櫃買中心阻擋
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                
                # 🔥 主動往回找最近的交易日 (最多找 6 天)
                for i in range(6):
                    check_date = base_date - timedelta(days=i)
                    roc_year = check_date.year - 1911
                    roc_date = f"{roc_year}/{check_date.strftime('%m/%d')}"
                    
                    url_otc = f"https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={roc_date}&se=EW"
                    try:
                        res_otc = requests.get(url_otc, headers=headers, timeout=10)
                        temp_data = res_otc.json()
                        
                        # 🌟 適應 TPEx 新版 API 結構 (tables)
                        if 'tables' in temp_data and temp_data['tables']:
                            if 'data' in temp_data['tables'][0] and len(temp_data['tables'][0]['data']) > 0:
                                data_otc = temp_data
                                valid_roc_date = roc_date
                                print(f"✅ 成功取得上櫃資料，實際資料日期: {valid_roc_date}")
                                break
                    except Exception as e:
                        print(f"⚠️ {roc_date} 抓取失敗，嘗試前一天... ({e})")
                    
                    time.sleep(0.5)

                tpex_count = 0  # 📊 [新增] 用來統計有幾檔上櫃股通過 3 億門檻

                # 開始解析新版上櫃資料
                if data_otc and 'tables' in data_otc and data_otc['tables']:
                    table = data_otc['tables'][0]
                    fields = [str(f).strip() for f in table.get('fields', [])]
                    raw_data = table.get('data', [])
                    
                    try:
                        idx_code = fields.index("代號")
                        idx_price = fields.index("收盤")
                        idx_turnover = fields.index("成交金額(元)")
                        idx_sign = fields.index("漲跌")
                    except:
                        idx_code, idx_price, idx_turnover, idx_sign = 0, 2, 8, 3
                    
                    for row in raw_data:
                        try:
                            code = str(row[idx_code]).strip()
                            if len(code) > 4 or code.startswith('91') or code.startswith('00'): continue 
                            
                            price_str = str(row[idx_price]).replace(',', '').strip()
                            turnover_str = str(row[idx_turnover]).replace(',', '').strip() 
                            
                            if price_str in ['----', '--', '', '除息', '除權'] or turnover_str in ['--', '', '0']: continue
                            
                            price = float(price_str)
                            turnover = float(turnover_str)
                            if price < 10: continue
                            
                            # 🔥 強化版漲跌判斷：處理沒有加號的隱藏紅K
                            raw_sign = str(row[idx_sign]).replace(',', '').strip()
                            is_up = False
                            if '+' in raw_sign or 'red' in raw_sign:
                                is_up = True
                            else:
                                try:
                                    clean_sign = re.sub(r'[^\d.-]', '', raw_sign)
                                    if clean_sign and float(clean_sign) > 0:
                                        is_up = True
                                except: pass
                            
                            # 條件：收紅 且 成交金額 > 1億
                            if is_up and turnover > 100000000: 
                                candidates.append({"code": code, "turnover": turnover, "price": price, "exchange": "上櫃"})
                                tpex_count += 1
                        except: continue
                    print(f"✅ 上櫃 (TPEx) 飆股已成功合併至候選池！(共 {tpex_count} 檔通過 3 億門檻)")
                else:
                    print("❌ 仍無法取得上櫃資料，請檢查 API 狀態。")
                # 👆👆👆 替換結束 👆👆👆
                            
                # 🔥 1. 依「成交金額 (turnover)」排序，取前 50 檔母體
                candidates.sort(key=lambda x: x['turnover'], reverse=True)
                top_50 = candidates[:50]
                
                # 📊 [新增] 統計 Top 50 的板塊分佈
                tw_count = sum(1 for x in top_50 if x.get('exchange') == '上市')
                otc_count = sum(1 for x in top_50 if x.get('exchange') == '上櫃')
                
                print(f"✅ [Task 2] 第一階段篩選完成，取得 50 檔強勢資金股 (上市: {tw_count} 檔 / 上櫃: {otc_count} 檔)。")
                print("啟動 FinMind 深度掃描...")
                final_list = []
                
                # 🔥 2. 針對 50 檔逐一調查基本面與籌碼
                for item in top_50:
                    code = item['code']
                    turnover = item['turnover']
                    price = item['price']
                    
                    acc_f, acc_t = get_finmind_chips(code)
                    
                    # ⚠️ 這裡接收剛剛寫好的新版字典
                    yoy_data = get_finmind_revenue_yoy(code) 
                    yoy = yoy_data['yoy']
                    
                    chips_sum = acc_f + acc_t
                    buy_value = chips_sum * 1000 * price
                    buy_value_y = round(buy_value / 100000000, 1)
                    
                    print(f"掃描 {code}: YoY={yoy}%, 法人買超={buy_value_y}億")
                    time.sleep(0.5) # 避免被 API 封鎖
                    
                    # 🔥 3. 分析師終極濾網：營收 YoY > 10% 且 法人買超金額 > 3億
                    if yoy > 10 and buy_value > 300000000:
                        meta_info = stock_meta.get(code, {})
                        stock_name = meta_info.get('name', '未知名稱')
                        stock_sector = meta_info.get('sector', '未知產業')
                        stock_exchange = item.get('exchange', '未知')
                        
                        # =========================================================
                        # 🛡️ 升級濾網：乖離率天花板 + K線避雷針 (防追高與假突破)
                        # =========================================================
                        try:
                            suffix = ".TWO" if stock_exchange == '上櫃' else ".TW"
                            hist = yf.Ticker(f"{code}{suffix}").history(period="1mo")
                            if not hist.empty and len(hist) > 0:
                                latest_k = hist.iloc[-1]
                                c_price = latest_k['Close']
                                o_price = latest_k['Open']
                                h_price = latest_k['High']
                                
                                is_overheated = False
                                # 1. 乖離過大警示 (改為不淘汰，後續給予懲罰扣分與專屬標籤)
                                ma20 = hist['Close'].mean()
                                bias20 = (c_price - ma20) / ma20 * 100
                                if bias20 > 15.0:
                                    print(f"⚠️ {code} 月線乖離過大({bias20:.1f}%)，動能極強但風險高，標記為過熱！")
                                    is_overheated = True
                                
                                # 2. 長上影線避雷針淘汰 (防主力出貨，這個還是要留著防假突破)
                                upper_shadow = h_price - max(o_price, c_price)
                                body = abs(c_price - o_price)
                                
                                # 上影線 > 實體 1.5 倍，且非一字線，即淘汰
                                if body > 0 and (upper_shadow / body) > 1.5:
                                    print(f"⚠️ {code} 出現長上影線避雷針，防禦假突破，淘汰！")
                                    continue
                        except Exception as e:
                            pass
                        # =========================================================
                        
                        # 👇 新增：判斷市值與計算動能分數
                        stock_cap_size = "大型權值股" if code in TAIWAN_50 else "中小型股"
                        score_yoy = min(yoy, 100) * 1.5
                        buy_value_y = buy_value / 100000000
                        capped_buy = min(buy_value_y, 10)
                        score_chips = capped_buy * 5
                        m_score = score_yoy + score_chips
                        if stock_cap_size == "中小型股":
                            m_score = m_score * 1.2
                            
                        # 🔥 針對過熱妖股的處理：扣減 20% 分數避免擠掉剛起漲的安全股，並給予專屬標籤
                        final_tag = "外資大買" if acc_f > acc_t else "投信作帳"
                        if is_overheated:
                            m_score = m_score * 0.8 
                            final_tag = "🔥過熱妖股"
                            
                        date_str = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"
                        # 👉 呼叫新引擎取得除息日 (只取第三個變數)
                        _, _, ex_date, _ = get_latest_dividend_info(code, price)

                        final_list.append({
                            "date": date_str,
                            "code": code,
                            "name": stock_name,
                            "exchange": stock_exchange,
                            "sector": stock_sector,
                            "cap_size": stock_cap_size, # 👈 寫入標籤
                            "m_score": round(m_score, 2), # 👈 寫入分數
                            "ex_dividend_date": ex_date, # 👈 寫入除息日
                            "price": price,
                            "turnover": turnover,
                            "chips_display": f"{chips_sum}張 ({buy_value_y:.1f}億)",
                            "buy_value": buy_value,
                            "yoy": yoy,
                            "tag": final_tag,
                            "debug_info": yoy_data['debug_info']
                        })
                
                # 🔥 4. 將過關的菁英，改為依照「動能總分 (m_score)」由大到小排序
                final_list.sort(key=lambda x: x['m_score'], reverse=True)
                              
                
                # 為了避免 JSON 太大，我們只保留最強的前 15 檔給 app.py 抽樣
                final_list = final_list[:15]
                print(f"🎉 掃描結束！共 {len(final_list)} 檔符合【高潛力成長飆股】終極標準。")
            else:
                print("⚠️ [Task 2] 找不到對應的資料表")
        else:
            print("⚠️ [Task 2] API 回傳狀態非 OK")

    except Exception as e:
        print(f"❌ [Task 2] 發生錯誤: {e}")

    # =========================================================
    # 🔥 [重構] 右側歷史標的同步模組 (支援除息日檢查)
    # =========================================================
    today_codes = {s['code'] for s in final_list}
    updated_history = sync_historical_data('daily_recommendations.json', today_codes, 'RIGHT', TAIWAN_50)
    final_list.extend(updated_history)

    # 📦 呼叫融合大腦，結合歷史記憶
    merged_list = merge_history_data(final_list, 'daily_recommendations.json', 'm_score')
    
    # =========================================================
    # 🔥 [新增] 右側無痕濾網 (15天期限 + 入榜理由消失，不留多餘欄位)
    # =========================================================
    filtered_momentum = []
    today_dt = datetime.now()
    
    if merged_list:
        for item in merged_list:
            # 1. 隱形計算天數
            try:
                first_date_str = item.get('first_entry_date', item.get('date'))
                first_date = datetime.strptime(first_date_str, '%Y-%m-%d')
                days_diff = (today_dt - first_date).days
            except:
                days_diff = 0
                
            # ⏳ 天數濾網：動能股不拖泥帶水，超過 30 天剔除
            if days_diff > 30:
                continue
                
           # 2. 隱形計算漲跌幅 (判斷入榜理由)
            raw_fp = item.get('first_entry_price')
            if raw_fp is None: raw_fp = item.get('price')
            if raw_fp is None: raw_fp = 1
            first_price = float(raw_fp)
            
            raw_cp = item.get('price')
            if raw_cp is None: raw_cp = 1
            current_price = float(raw_cp)
            
            roi = (current_price - first_price) / first_price if first_price > 0 else 0
            
            # 📈 邏輯濾網：暴漲 20% (超漲過熱) 或 跌破動能 -8% (假突破) 直接剔除
            if roi >= 0.20 or roi <= -0.08:
                continue
                
            filtered_momentum.append(item)

        # 📦 最終結算存檔 (儲存乾淨的 filtered_momentum)
        clean_filtered_momentum = clean_nan(filtered_momentum) # 👈 啟動淨水器
        with open('daily_recommendations.json', 'w', encoding='utf-8') as f:
            json.dump(clean_filtered_momentum, f, ensure_ascii=False, indent=4, allow_nan=False) # 👈 加入 allow_nan=False 嚴格防呆
        print(f"💾 已儲存 daily_recommendations.json (經無痕濾網淘汰後保留 {len(filtered_momentum)} 檔)")
    else:
        print("⚠️ 歷史與今日皆無資料可存。")
   

#----------3/13增加左側交易-------------
# ========================================================
# 🔥 新增功能 3: 【左側交易：三層漏斗價值雷達】(100% 獨立產線)
# ========================================================
def generate_left_side_value():
    print("\n🛡️ [Task 3] 啟動左側交易：重裝價值雷達 (三層漏斗過濾)...")
    
    # 讀取基礎股票池
    stock_meta = {}
    try:
        with open('stock_list.json', 'r', encoding='utf-8') as f:
            stock_meta = {k: v for k, v in json.load(f).items() if v.get('type') == '股票'}
    except Exception as e:
        print(f"⚠️ 讀取 stock_list.json 失敗，左側雷達中止: {e}")
        return

    # ---------------------------------------------------------
    # 🌊 第一層：大數據降維 (流動性 1000萬 ~ 5億，排除 ETF)
    # ---------------------------------------------------------
    print("🌊 [第一層] 大數據降維：尋找流動性 1000萬~5億 的潛伏股...")
    layer1_candidates = []
    
    # 1. 抓取上市 (TWSE) 最新交易日
    try:
        res = requests.get("https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&type=ALLBUT0999", timeout=10)
        data = res.json()
        if data.get('stat') == 'OK':
            target_table = next((t for t in data.get('tables', []) if '證券代號' in t.get('fields', [])), None)
            if not target_table and 'data9' in data:
                target_table = {'data': data['data9'], 'fields': data.get('fields9', [])}

            if target_table:
                fields = target_table['fields']
                idx_code = fields.index("證券代號") if "證券代號" in fields else 0
                idx_turnover = fields.index("成交金額") if "成交金額" in fields else 4
                idx_price = fields.index("收盤價") if "收盤價" in fields else 8

                for row in target_table['data']:
                    code = str(row[idx_code]).strip()
                    if code not in stock_meta: continue
                    if code.startswith('00'): continue # 🛡️ 防線：拆穿偽裝成股票的 ETF
                    try:
                        turnover = float(row[idx_turnover].replace(',', ''))
                        price = float(row[idx_price].replace(',', ''))
                        # 🔥 條件：放寬上限至 5 億
                        if 10000000 <= turnover <= 500000000 and price >= 10:
                            layer1_candidates.append({"code": code, "price": price, "market": "TW"})
                    except: pass
    except Exception as e:
        print(f"⚠️ TWSE 第一層抓取錯誤: {e}")

    # 2. 抓取上櫃 (TPEx) 最新交易日
    print(f"🔄 正在尋找最新上櫃 (TPEx) 行情...")
    try:
        base_date = datetime.now(timezone.utc) + timedelta(hours=8)
        # 🛡️ 升級：高級偽裝與延遲，防擋 IP
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        time.sleep(1) 
        
        for i in range(6): 
            check_date = base_date - timedelta(days=i)
            roc_date = f"{check_date.year - 1911}/{check_date.strftime('%m/%d')}"
            url_otc = f"https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={roc_date}&se=EW"
            
            try:
                res_otc = requests.get(url_otc, headers=headers, timeout=10)
                if res_otc.status_code == 200:
                    try:
                        temp_data = res_otc.json()
                    except json.JSONDecodeError:
                        continue 

                    if 'tables' in temp_data and temp_data['tables'] and len(temp_data['tables'][0].get('data', [])) > 0:
                        table = temp_data['tables'][0]
                        fields = [str(f).strip() for f in table.get('fields', [])]
                        idx_code = fields.index("代號") if "代號" in fields else 0
                        idx_turnover = fields.index("成交金額(元)") if "成交金額(元)" in fields else 8
                        idx_price = fields.index("收盤") if "收盤" in fields else 2
                        
                        for row in table['data']:
                            code = str(row[idx_code]).strip()
                            if code not in stock_meta: continue
                            if code.startswith('00'): continue # 🛡️ 防線：拆穿偽裝成股票的 ETF
                            try:
                                price_str = str(row[idx_price]).replace(',', '').strip()
                                turnover_str = str(row[idx_turnover]).replace(',', '').strip()
                                if price_str in ['----', '--', '除息', '除權'] or turnover_str in ['--', '']: continue
                                turnover = float(turnover_str)
                                price = float(price_str)
                                # 🔥 條件：放寬上限至 5 億
                                if 10000000 <= turnover <= 500000000 and price >= 10:
                                    layer1_candidates.append({"code": code, "price": price, "market": "TWO"})
                            except: pass
                        break
            except Exception: pass
            time.sleep(0.5)
    except Exception as e:
        print(f"⚠️ TPEx 第一層抓取錯誤: {e}")

    print(f"✅ 第一層降維完畢，全市場 2000 檔中，共 {len(layer1_candidates)} 檔符合流動性門檻，進入第二層。")

    # ---------------------------------------------------------
    # 📉 第二層：位階與動能過濾 (不接破底刀)
    # ---------------------------------------------------------
    print(f"📉 [第二層] 啟動 yfinance 計算 (預計處理 {len(layer1_candidates)} 檔)...")
    layer2_candidates = []
    
    for i, item in enumerate(layer1_candidates):
        # 👁️ 加上進度條
        if i > 0 and i % 50 == 0: 
            print(f"   ... yfinance 已處理 {i}/{len(layer1_candidates)} 檔")
            
        code = item['code']
        try:
            ticker = yf.Ticker(f"{code}.{item['market']}")
            df = ticker.history(period="6mo") 
            if df.empty or len(df) < 60: continue

            closes = df['Close'].tolist()
            lows = df['Low'].tolist()
            highs = df['High'].tolist()
            opens = df['Open'].tolist() # 🔥 抓開盤價算 K 線
            volumes = df['Volume'].tolist()

            item['real_date'] = df.index[-1].strftime('%Y-%m-%d')
            
            close_today = closes[-1]
            open_today = opens[-1]
            low_today = lows[-1]
            
            item['price'] = round(close_today, 2)
            ma60 = sum(closes[-60:]) / 60
            ma24 = sum(closes[-24:]) / 24 
            ma6 = sum(closes[-6:]) / 6    
            ma5 = sum(closes[-5:]) / 5     # 👈 新增：計算 5MA
            item['is_above_5ma'] = bool(close_today > ma5) # 👈 新增：判斷是否站上 5MA

            # 👇👇👇 請在這裡補上這兩行，把 RSI 算出來並存入 item 👇👇👇
            item['rsi_today'] = calculate_rsi(closes)
            item['rsi_yest'] = calculate_rsi(closes[:-1])
            # 👆👆👆 補上結束 👆👆👆
            
            bias60 = (close_today - ma60) / ma60
            bias24 = (close_today - ma24) / ma24
            bias6 = (close_today - ma6) / ma6

            # 🔥 放寬：只要跌破季線就不淘汰
            if bias60 >= 0: continue
            
            vol_today = volumes[-1]
            ma20_vol = sum(volumes[-20:]) / 20

            # 🛡️ 流動性硬門檻：20日均量低於 500 張 (500,000股) 直接淘汰
            if ma20_vol < 500000: continue 

            vol_ratio = vol_today / ma20_vol if ma20_vol > 0 else 1
            
            # 🔥 放寬：量縮 0.9 以內、振幅 15% 以內、5日漲幅 8% 以內
            if vol_ratio >= 0.9: continue
            if (max(highs[-10:]) - min(lows[-10:])) / min(lows[-10:]) >= 0.15: continue
            if (close_today - closes[-5]) / closes[-5] >= 0.08: continue

            # 🛡️ 紀錄是否破底 (不直接淘汰，交給第三層扣分)
            item['is_breaking_low'] = bool(close_today < min(closes[-5:-1]))
                
            # 判斷有無防守紅K或下影線 (存起來當第三層加分項)
            is_red_candle = close_today > open_today
            lower_shadow = min(open_today, close_today) - low_today
            upper_shadow = highs[-1] - max(open_today, close_today)
            body = abs(close_today - open_today)
            
            # 取得昨日開收盤價 (防呆確保資料長度 > 1)
            close_yest = closes[-2] if len(closes) > 1 else close_today
            open_yest = opens[-2] if len(opens) > 1 else open_today

            # 1. 槌子線：下影線 > 實體 2 倍，且上影線 < 實體 0.5 倍
            is_hammer = (lower_shadow > body * 2.0) and (upper_shadow < body * 0.5)
            # 2. 吞噬紅K (破底翻)：昨收黑，今開低收高且實體包覆昨日實體
            is_bullish_engulfing = (close_yest < open_yest) and (open_today < close_yest) and (close_today > open_yest)
            
            item['is_strong_reversal'] = bool(is_hammer or is_bullish_engulfing)
            item['is_anti_knife'] = bool(is_red_candle or (lower_shadow > max(body, 0.01) * 1.5))

            item['bias60'] = bias60
            item['bias24'] = bias24 
            item['bias6'] = bias6   
            item['vol_ratio'] = vol_ratio
            item['vol_5d'] = sum(volumes[-5:]) 
            layer2_candidates.append(item)
            
        except Exception: pass
        time.sleep(0.1)

    print(f"✅ 第二層過濾完畢，剩餘 {len(layer2_candidates)} 檔進入終極基本面與評分查核。")

    # ---------------------------------------------------------
    # 🏦 第三層：聰明錢定錨與 🌟動態評分系統
    # ---------------------------------------------------------
    print("🏦 [第三層] 啟動 FinMind 查核與動態評分 (API 節流模式啟動)...")
    final_list = []
    
    for item in layer2_candidates:
        code = item['code']
        print(f"   🔍 查核 {code}...", end=" ")
        
        # 1. 查籌碼
        chips_history = get_finmind_chips_history(code, days=5)
        buy_days_5d = sum(1 for x in chips_history if x > 0)
        
        if buy_days_5d < 1:
            print("❌ 籌碼掛零")
            continue
            
        net_buy_vol_5d = sum(chips_history)
        total_vol_5d = item['vol_5d'] / 1000 
        buy_ratio = (net_buy_vol_5d / total_vol_5d) * 100 if total_vol_5d > 0 else 0
        net_buy_amount_10k = (net_buy_vol_5d * item['price']) / 10
        
        # 🛡️ 隱蔽吃貨防線：買超大於 100張 或 500萬，且佔比 > 2.0% 
        if not ((net_buy_vol_5d > 100 or net_buy_amount_10k > 500) and buy_ratio > 2.0):
            print("❌ 佔比/金額不足")
            continue
        
        # 2. 查基本面與殖利率
        eps, _, _ = get_finmind_fundamentals(code, item['price'], fetch_yield=False)
        yoy_data = get_finmind_revenue_yoy(code)
        yoy = yoy_data['yoy']
        
        # 🔥 [一鍵呼叫新引擎]：同時取得精準殖利率、公式、乾淨的除息日、與是否加分
        yield_rate, yield_formula, ex_date, is_upcoming = get_latest_dividend_info(code, item['price'])
        
        # 🎯 啟動計分模型 (Base: 40)
        score = 40 
        
        # 🛡️ 智能虧損股漏斗：嚴格檢視 EPS < 0 標的
        if eps < 0:
            # 考驗1：破底且無主力防守反轉，淘汰
            if item.get('is_breaking_low') and not item.get('is_strong_reversal'):
                print("❌ 虧損且破底無防守，淘汰")
                continue
            # 考驗2：法人買盤不夠異常集中，淘汰
            if buy_days_5d < 4 and buy_ratio < 5.0:
                print("❌ 虧損且籌碼集中度不足，淘汰")
                continue
            # 考驗3：營收未見底回升，淘汰
            if yoy <= 0:
                print("❌ 虧損且營收未反轉，淘汰")
                continue
            # 通過嚴格考驗的轉機股，扣減基本面分數
            score -= 5
            print("   ⚠️ 虧損轉機股通關，扣 5 分")
            
        # 🛡️ 破底扣分：獲利正常但仍在破底，扣 10 分
        elif item.get('is_breaking_low'):
            score -= 10
            
        # ⭐ 底部型態加分：大資金防守重磅加分
        if item.get('is_strong_reversal'): 
            score += 15
            print(f"   ⭐ 偵測到強力底部反轉型態！")
        elif item.get('is_anti_knife'): 
            score += 5
        
       # 3. 獲利與成長加分 (補上之前漏掉的)
        if eps > 0: score += 10
        if yoy > 10.0: score += 10
        if yield_rate >= 4.0: score += 10
        
        # 🔥 [新增] 即將除息額外加分！
        if is_upcoming:
            score += 10
            print(f"   💰 具備即將除息優勢 ({ex_date})，額外加 10 分！")
        
        # 籌碼加分
        if buy_ratio > 5.0: score += 10
        if buy_days_5d == 5: score += 30
        elif buy_days_5d == 4: score += 20
        elif buy_days_5d == 3: score += 10
        
        # 量縮加分
        if item['vol_ratio'] < 0.5: score += 10
        elif item['vol_ratio'] < 0.6: score += 8
        elif item['vol_ratio'] < 0.7: score += 5
        
        # 🔥 修正邏輯：乖離加分 (跌越深，加越多分)
        bias_pct = item['bias60'] * 100
        if bias_pct < -8.0: score += 15
        elif bias_pct < -5.0: score += 10
        elif bias_pct < -3.0: score += 5

        # ⭐ 底部型態加分：大資金防守重磅加分
        if item.get('is_strong_reversal'): 
           score += 15
           print(f"   ⭐ 偵測到強力底部反轉型態！")
        elif item.get('is_anti_knife'): 
           score += 5

        # 👇 [新增這段] RSI 落底反轉加分
        if item['rsi_yest'] < 35 and item['rsi_today'] > item['rsi_yest']:
           score += 15
           print(f"   🚀 RSI超賣區勾頭向上 ({item['rsi_yest']} -> {item['rsi_today']})，加 15 分！")

        # ---------------------------------------------------------
        # 🛡️ 資格層與輸出層 (決定誰有資格活下來)
        # ---------------------------------------------------------
        if item['is_above_5ma']:
            trend_status = "🔥 L1_左側起漲"
            is_qualified = (score >= 60)  
        else:
            trend_status = "⏳ L0_左側築底"
            is_qualified = (score >= 55)

        # ❌ 不及格的直接淘汰，絕對不放進 final_list 佔用注意力
        if not is_qualified:
            print(f"❌ 資格不符 ({trend_status} 但分數僅 {score} 分)")
            continue
            
        entry_price = round(item['price'] * 0.99, 2)
        print(f"✅ 最終清單入選 | 分數: {score} | {trend_status}")

        

        final_list.append({
            "date": item['real_date'],
            "code": code,
            "name": stock_meta[code]['name'],
            "price": item['price'],
            # 👇 新增這行！把第一層貼好的標籤傳承下來，並統一叫 exchange
            "exchange": "上市" if item.get('market') == 'TW' else "上櫃",
            "score": score,
            "trend_status": trend_status,
            "entry_price": entry_price,
            "ex_dividend_date": ex_date,  # 👈 寫入除息
            "bias60": f"{bias_pct:.1f}%",
            "bias24": f"{item['bias24']*100:.1f}%", 
            "bias6": f"{item['bias6']*100:.1f}%",   
            "vol_ratio": f"{item['vol_ratio']*100:.1f}%",
            "eps": eps,
            "yield_rate": yield_rate,
            "yield_formula": yield_formula,  # 👈 新增這行：把公式印出來！
            "buy_days": buy_days_5d,
            "tag": "左側黃金坑"
        })

    # ---------------------------------------------------------
    # 📦 結算與存檔
    # ---------------------------------------------------------
    if final_list:
        # 🔥 數量控制：依照分數高低排序，每天最多只取前 15 檔極品
        final_list.sort(key=lambda x: x['score'], reverse=True)
        final_list = final_list[:15]
        print(f"✅ 今日掃描共 {len(final_list)} 檔無敵黃金坑達標。")
    else:
        print("⚠️ 今日掃描無股票通過三層漏斗。")
    # =========================================================
    # 🔥 [重構] 左側歷史標的同步模組 (支援除息日檢查)
    # =========================================================
    today_codes = {s['code'] for s in final_list}
    updated_history = sync_historical_data('left_side_value.json', today_codes, 'LEFT')
    final_list.extend(updated_history)

    # 執行融合與除名機制
    merged_list = merge_history_data(final_list, 'left_side_value.json', 'score')
    
    # =========================================================
    # 🔥 [新增] 左側無痕濾網 (30天期限 + 入榜理由消失，不留多餘欄位)
    # =========================================================
    filtered_list = []
    today_dt = datetime.now()
    
    if merged_list:
        for item in merged_list:
            # 1. 隱形計算天數
            try:
                first_date_str = item.get('first_entry_date', item.get('date'))
                first_date = datetime.strptime(first_date_str, '%Y-%m-%d')
                days_diff = (today_dt - first_date).days
            except:
                days_diff = 0
                
            # ⏳ 天數濾網：超過 30 天直接剔除
            if days_diff > 30:
                continue
                
            # 2. 隱形計算漲跌幅 (判斷入榜理由)
            raw_fp = item.get('first_entry_price')
            if raw_fp is None: raw_fp = item.get('price')
            if raw_fp is None: raw_fp = 1
            first_price = float(raw_fp)
            
            raw_cp = item.get('price')
            if raw_cp is None: raw_cp = 1
            current_price = float(raw_cp)
            
            roi = (current_price - first_price) / first_price if first_price > 0 else 0
            
            # 📉 邏輯濾網：強彈 15% (坑填平) 或 破底跌 10% (接刀失敗) 直接剔除
            if roi >= 0.15 or roi <= -0.10:
                continue
                
            filtered_list.append(item)

        # 📦 最終結算存檔 (儲存乾淨的 filtered_list)
        clean_filtered_list = clean_nan(filtered_list) # 👈 啟動淨水器
        with open('left_side_value.json', 'w', encoding='utf-8') as f:
            json.dump(clean_filtered_list, f, ensure_ascii=False, indent=4, allow_nan=False) # 👈 加入 allow_nan=False
        print(f"💾 已強制更新 left_side_value.json (經無痕濾網淘汰後保留 {len(filtered_list)} 檔)")
    else:
        print("⚠️ 歷史與今日皆無資料可存。")
  
# ========================================================
# 🔥 新增功能 4: 【金剛不壞：存股打折加碼雷達】(獨立產線)
# ========================================================
def generate_deposit_stocks():
    print("\n🏦 [Task 4] 啟動存股打折加碼雷達 (均線乖離策略)...")
  
    # 📝 你專屬的存股口袋名單 (未來要新增/刪除，只需改這行！)
    DEPOSIT_WATCHLIST = [
    # --- 官股金控 (獲利穩健，存股首選) ---
    "2886",  # 兆豐金
    "2892",  # 第一金
    "5880",  # 合庫金
    "2880",  # 華南金

    # --- 民營金控 (績效領先，股息亮眼) ---
    "2881",  # 富邦金
    "2882",  # 國泰金
    "2883",  # 凱基金 (原開發金)
    "2884",  # 玉山金
    "2891",  # 中信金
    "2890",  # 永豐金

    # --- 電子龍頭 (產業趨勢，增值潛力) ---
    "2330",  # 台積電
    "2317",  # 鴻海

    # --- 國民 ETF (分散風險，被動投資) ---
    "0050",  # 元大台灣50
    "0056",  # 元大高股息
    "00878", # 國泰永續高股息
    "00713", # 元大台灣高息低波
    "00919", # 群益台灣精選高息
    "00881", # 國泰台灣5G+
    "006208",# 富邦台50
    "0052",  # 富邦台灣科技
    "00929"  # 復華台灣科技優息
    ]

    # 讀取對照表來抓中文名稱
    stock_meta = {}
    try:
        with open('stock_list.json', 'r', encoding='utf-8') as f:
            stock_meta = json.load(f)
    except Exception as e:
        print(f"⚠️ 讀取 stock_list.json 失敗: {e}")

    deposit_list = []

    for code in DEPOSIT_WATCHLIST:
        print(f"🔍 分析存股標的: {code} ...", end=" ")
        try:
            # 判斷上市或上櫃 (ETF通常是上市 TW)
            ticker_tw = yf.Ticker(f"{code}.TW")
            df = ticker_tw.history(period="6mo") # 🔥 改為 6mo 以涵蓋季線
            if df.empty:
                ticker_two = yf.Ticker(f"{code}.TWO")
                df = ticker_two.history(period="6mo")

            # 🌟 [新增防呆機制] 清除 Yahoo Finance 的異常空值(NaN)
            if not df.empty:
                df = df.dropna(subset=['Close'])
            
            if len(df) < 60: # 🔥 確保資料夠算 60MA
                print("資料不足，跳過。")
                continue

            closes = df['Close'].tolist()
            close_today = closes[-1]

            # 👇 確保有加這行，挖出真實 K 線最後交易日
            data_date_str = df.index[-1].strftime('%Y-%m-%d')
            
            # 🔥 計算多週期均線與乖離率
            ma60 = sum(closes[-60:]) / 60
            ma24 = sum(closes[-24:]) / 24
            ma20 = sum(closes[-20:]) / 20
            ma12 = sum(closes[-12:]) / 12
            ma6 = sum(closes[-6:]) / 6
            ma5 = sum(closes[-5:]) / 5
            
            bias_60 = (close_today - ma60) / ma60 * 100
            bias_24 = (close_today - ma24) / ma24 * 100
            bias_20 = (close_today - ma20) / ma20 * 100
            bias_12 = (close_today - ma12) / ma12 * 100
            bias_6 = (close_today - ma6) / ma6 * 100
            bias_5 = (close_today - ma5) / ma5 * 100

            # 🔥 抓取殖利率 (利用你寫好的函式)
            eps, yield_rate, annual_div = get_finmind_fundamentals(code, close_today)

            # 🧠 核心大腦：5 段式燈號與防飛刀邏輯
            signal = ""
            action = ""
            anti_knife_warning = ""

            if bias_20 > 8.0:
                signal = "🔴 警示"
                action = "【停扣 / 獲利了結】短線過熱，可先獲利了結，回檔後再接。"
            elif 3.0 < bias_20 <= 8.0:
                signal = "🟡 觀望"
                action = "【維持現狀】穩定上漲中。"
            elif -2.0 <= bias_20 <= 3.0:
                signal = "🟢 平穩"
                action = "【定期定額】價值平衡。"
            elif -8.0 <= bias_20 < -2.0:
                signal = "🛒 加碼"
                action = "【小幅加碼】股價委屈，預估殖利率上升，可撿便宜。"
            else: # bias_20 < -8.0
                signal = "🚨 重壓"
                action = "【大舉進場】市場恐慌超跌，長線買點浮現！"

            # 🛡️ 防飛刀濾網 (當月線大跌，但週線還在跌，代表還沒見底)
            if bias_20 < -2.0 and bias_5 < 0:
                anti_knife_warning = " ⚠️ (跌勢未止，請分批慢接)"
            elif bias_20 < -2.0 and bias_5 > 0:
                anti_knife_warning = " ⭐ (週線翻正，跌勢止穩，建議加碼！)"

            action += anti_knife_warning
            
            meta_info = stock_meta.get(code, {})
            deposit_list.append({
                "date": data_date_str,  # 👈 2. 新增這行！把剛剛取得的日期塞進每一檔股票裡
                "code": code,
                "name": meta_info.get('name', '未知名稱'),
                "price": round(close_today, 2),
                "bias_6": round(bias_6, 2),   
                "bias_12": round(bias_12, 2), 
                "bias_24": round(bias_24, 2), 
                "bias_20": round(bias_20, 2),
                "bias_60": round(bias_60, 2), 
                "yield_rate": yield_rate,
                # 🔥 新增這行：將分子(預估配息)與分母(今日收盤價)寫入 JSON，方便核對
                "yield_formula": f"預估配息 {annual_div:.3f} / 股價 {close_today:.2f}",
                "signal": signal,
                "action": action
            })
            print(f"完成 (乖離 {bias_20:.2f}%, 殖利率 {yield_rate}%)")

        except Exception as e:
            print(f"錯誤: {e}")
        time.sleep(0.1)

    # 📦 結算與存檔
    if deposit_list:
        # 依照乖離率由低到高排序 (越便宜、跌越多的排越上面)
        deposit_list.sort(key=lambda x: x['bias_20'])
        
        clean_deposit_list = clean_nan(deposit_list) # 👈 啟動淨水器
        with open('deposit_stocks.json', 'w', encoding='utf-8') as f:
            json.dump(clean_deposit_list, f, ensure_ascii=False, indent=4, allow_nan=False) # 👈 加入 allow_nan=False
        print(f"💾 任務完成！已儲存 deposit_stocks.json (共分析 {len(deposit_list)} 檔存股)")

# ========================================================
# 最後，記得在你的 __main__ 區塊把這支程式加上去執行！
if __name__ == "__main__":
    update_stock_list_json()
    generate_daily_recommendations()  # 右側產線
    generate_left_side_value()        # 左側產線
    generate_deposit_stocks()         # 🏦 存股產線 (新增這行！)
