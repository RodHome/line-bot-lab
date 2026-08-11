import requests
import pandas as pd
import json
import re
import os
import time
from datetime import datetime, timedelta, timezone
from io import StringIO
import yfinance as yf
import math   

# 🔥 雙鑰匙負載平衡系統
GUEST_TOKEN = "" 
VIP_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJkYXRlIjoiMjAyNi0wMy0xOCAxOToyODoyNCIsInVzZXJfaWQiOiJyb2Q3NDEwMDEyIiwiZW1haWwiOiJyb2Q3NDEwMDFAZ21haWwuY29tIiwiaXAiOiIxMjIuMTE2LjE1OS4xMzQifQ.qmaLCfxjbwXRYo8TwFZKboTfmAADIMs0CWw-oPUJU4g"

def clean_nan(data):
    if isinstance(data, list):
        return [clean_nan(item) for item in data]
    elif isinstance(data, dict):
        return {k: clean_nan(v) for k, v in data.items()}
    elif isinstance(data, float) and math.isnan(data):
        return None  
    else:
        return data

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

# ==========================================
# 🆕 雙引擎資金投入優先度 (S/A/B/C) 判定邏輯
# ==========================================
def get_right_capital_rank(price, ma5, ma20, high_20d, vol_ratio, bias20, is_break_reversal=False):
    """右側動能：突破與趨勢判定"""
    if price < ma5 or is_break_reversal:
        return "C"
    
    is_trend_up = (price > ma5) and (price > ma20) and (ma5 > ma20)
    is_breakout = (price >= high_20d)
    is_near_breakout = (price >= high_20d * 0.97) 
    
    if is_trend_up and is_breakout and vol_ratio >= 1.5 and bias20 < 15.0:
        return "S" # 剛起漲、爆量突破
    elif is_trend_up and (is_breakout or is_near_breakout) and bias20 < 25.0:
        return "A" # 趨勢確立，可追擊
    elif bias20 < 35.0:
        return "B" # 強勢但乖離高，等回檔
    else:
        return "C" # 過熱或轉弱

def get_left_capital_rank(is_above_5ma, is_strong_reversal, is_anti_knife, is_breaking_low, bias60, rsi_yest, rsi_today, buy_days_5d, eps):
    """左側潛伏：防守與反轉判定"""
    if eps is not None and eps < 0 and buy_days_5d < 4:
        return "C" # 虧損且無大人照顧，危險
    
    if is_above_5ma and is_strong_reversal and (rsi_today > rsi_yest) and buy_days_5d >= 3:
        return "S" # 站上5MA、強力反轉、RSI向上、籌碼集中
    if not is_above_5ma and is_anti_knife and buy_days_5d >= 3:
        return "A" # 未過5MA，但出防守K線與籌碼進駐
    if not is_above_5ma and is_breaking_low and not is_anti_knife:
        if bias60 < -8.0: 
            return "B" # 嚴重超跌但無防守，嚴格觀望
        return "C" # 破底無支撐
    
    return "A" if is_above_5ma else "B"

def merge_history_data(today_data, file_name, sort_key):
    history_dict = {}
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

    today_date_str = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime('%Y-%m-%d')
    for item in today_data:
        code = item['code']
        item_date = item.get('date', today_date_str) 
        
        raw_first_price = history_dict.get(code, {}).get('first_entry_price')
        try:
            if raw_first_price is None or str(raw_first_price).lower() == 'null' or float(raw_first_price) <= 0:
                first_price = float(item.get('price', 0.0))
            else:
                first_price = float(raw_first_price)
        except (ValueError, TypeError):
            first_price = float(item.get('price', 0.0))
            
        first_date = history_dict.get(code, {}).get('first_entry_date', item_date)
        
        new_item = item.copy()
        new_item['first_entry_date'] = first_date
        new_item['first_entry_price'] = first_price
        
        history_dict[code] = new_item

    all_dates = set(v.get('date') for v in history_dict.values() if v.get('date'))
    allowed_dates = sorted(list(all_dates), reverse=True)[:30]
    
    final_list = [v for v in history_dict.values() if v.get('date') in allowed_dates]
    final_list.sort(key=lambda x: x.get(sort_key, 0), reverse=True)
    
    return final_list

def get_finmind_chips(code):
    start = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    url = "https://api.finmindtrade.com/api/v4/data"
    try:
        res = requests.get(url, params={"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": code, "start_date": start, "token": GUEST_TOKEN}, timeout=10)
        if res.status_code != 200: return None, None
        data = res.json().get('data', [])
        if not data: return None, None
        
        unique_dates = sorted(list(set([d['date'] for d in data])), reverse=True)
        target_dates = unique_dates[:5]
        acc_f = 0; acc_t = 0
        for row in data:
            if row['date'] in target_dates:
                val = (row['buy'] - row['sell']) // 1000
                if row['name'] == 'Foreign_Investor': acc_f += val
                elif row['name'] == 'Investment_Trust': acc_t += val
        return acc_f, acc_t
    except: return None, None

def get_finmind_revenue_yoy(code):
    start = (datetime.now() - timedelta(days=480)).strftime('%Y-%m-%d')
    url = "https://api.finmindtrade.com/api/v4/data"
    default_res = {
        "yoy": None, 
        "debug_info": {"status": "No Data", "this_rev": 0, "last_rev": 0, "this_period": "N/A", "last_period": "N/A"}
    }
    
    try:
        res = requests.get(url, params={"dataset": "TaiwanStockMonthRevenue", "data_id": code, "start_date": start, "token": GUEST_TOKEN}, timeout=10)
        if res.status_code != 200: return default_res
        data = res.json().get('data', [])
        
        if not data: return default_res
            
        data.sort(key=lambda x: (x['revenue_year'], x['revenue_month']), reverse=True)
        
        for i in range(len(data)):
            target = data[i]
            t_rev = target['revenue']
            t_y = target['revenue_year']
            t_m = target['revenue_month']
            
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

def get_finmind_chips_history(code, days=3):
    start = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    url = "https://api.finmindtrade.com/api/v4/data"
    history = []
    try:
        res = requests.get(url, params={"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": code, "start_date": start, "token": GUEST_TOKEN}, timeout=10)
        if res.status_code != 200: return None
        data = res.json().get('data', [])
        if not data: return None
        
        unique_dates = sorted(list(set([d['date'] for d in data])), reverse=True)
        target_dates = unique_dates[:days]
        target_dates.reverse() 
        
        for t_date in target_dates:
            daily_net = 0
            for row in data:
                if row['date'] == t_date:
                    val = (row['buy'] - row['sell']) // 1000
                    if row['name'] in ['Foreign_Investor', 'Investment_Trust']:
                        daily_net += val
            history.append(daily_net)
        return history
    except: return None

def get_finmind_fundamentals(code, current_price, fetch_yield=True):
    eps_latest = None
    yield_rate = 0.0
    annual_div = 0.0
    
    start = (datetime.now() - timedelta(days=800)).strftime('%Y-%m-%d')
    url = "https://api.finmindtrade.com/api/v4/data"
    
    try:
        res = requests.get(url, params={"dataset": "TaiwanStockFinancialStatements", "data_id": code, "start_date": start, "token": GUEST_TOKEN}, timeout=5)
        if res.status_code == 200:
            data = res.json().get('data', [])
            eps_data = [d for d in data if d['type'] == 'EPS']
            if eps_data:
                eps_data.sort(key=lambda x: x.get('date', ''))
                eps_latest = float(eps_data[-1].get('value', 0))
    except: pass
    
    if not fetch_yield:
        return eps_latest, yield_rate, annual_div

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
                    
                    annual_div = round(latest_cash * multiplier, 3)
                    if current_price > 0:
                        yield_rate = round((annual_div / current_price) * 100, 2)
    except: pass
        
    return eps_latest, yield_rate, annual_div

def get_latest_dividend_info(code, current_price):
    is_etf = str(code).startswith('00')
    tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
    today_str = tw_now.strftime('%Y-%m-%d')
    
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
        
        if raw_ex_date and raw_ex_date >= today_str:
            ex_date_for_json = raw_ex_date
            is_upcoming = True
        else:
            return yield_rate, formula, ex_date_for_json, is_upcoming

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

def sync_historical_data(file_name, today_codes, strategy_type, taiwan_50_list=None):
    updated_history = []
    print(f"🔄 正在同步 {file_name} 歷史標的最新現價與資訊...")
    
    stock_meta = {}
    try:
        if os.path.exists('stock_list.json'):
            with open('stock_list.json', 'r', encoding='utf-8') as f:
                stock_meta = json.load(f)
    except Exception as e:
        print(f"⚠️ 同步歷史資料時讀取 stock_list.json 失敗: {e}")
    
    if not os.path.exists(file_name):
        return updated_history

    try:
        with open(file_name, 'r', encoding='utf-8') as f:
            history_stocks = json.load(f)
        
        for old_s in history_stocks:
            code = old_s['code']
            if code not in today_codes:
                try:
                    exchange_type = old_s.get('exchange', '上市')
                    suffix = ".TWO" if exchange_type == '上櫃' else ".TW"
                    
                    # 🚀 左側需算 60MA (抓 3mo)，右側需算 20日高 (抓 2mo)
                    period_val = "2mo" if strategy_type == 'RIGHT' else "3mo"
                    ticker = yf.Ticker(f"{code}{suffix}")
                    hist = ticker.history(period=period_val)

                    if not hist.empty:
                        new_p = round(float(hist['Close'].iloc[-1]), 2)
                        old_s['price'] = new_p
                        
                        real_date_str = hist.index[-1].strftime('%Y-%m-%d')
                        old_s['date'] = real_date_str
                        
                        meta_info = stock_meta.get(code, {})
                        old_s['name'] = meta_info.get('name', old_s.get('name', '未知名稱'))
                        old_s['sector'] = meta_info.get('sector', old_s.get('sector', '未知產業'))
                        
                        _, _, ex_date, is_upcoming = get_latest_dividend_info(code, new_p)
                        if is_upcoming:
                            old_s['ex_dividend_date'] = ex_date
                        else:
                            old_s.pop('ex_dividend_date', None)
                            if strategy_type == 'LEFT':
                                old_s['yield_rate'] = 0.0
                                old_s['yield_formula'] = "⚠️ 已除息或尚未宣告"

                        # === 右側動能歷史同步 ===
                        if strategy_type == 'RIGHT' and taiwan_50_list:
                            old_s['cap_size'] = "大型權值股" if code in taiwan_50_list else "中小型股"
                            
                            acc_f, acc_t = get_finmind_chips(code)
                            if acc_f is not None:
                                chips_sum = acc_f + acc_t
                                buy_value = chips_sum * 1000 * new_p
                                buy_value_y = buy_value / 100000000
                                old_s['buy_value'] = buy_value
                                old_s['chips_display'] = f"{chips_sum}張 ({buy_value_y:.1f}億)"
                            
                            yoy_val = old_s.get('yoy', 0)
                            if yoy_val is None: yoy_val = 0
                            
                            current_buy_val = old_s.get('buy_value', 0)
                            buy_val_y = current_buy_val / 100000000
                            
                            m_score = (min(yoy_val, 100) * 1.5) + (min(buy_val_y, 10) * 5)
                            if old_s['cap_size'] == "中小型股":
                                m_score = m_score * 1.2
                            old_s['m_score'] = round(m_score, 2)

                            if len(hist) > 22:
                                c_price_hist = round(float(hist['Close'].iloc[-1]), 2)
                                o_price_hist = float(hist['Open'].iloc[-1])
                                h_price_hist = float(hist['High'].iloc[-1])
                                ma5_hist = hist['Close'].iloc[-5:].mean()
                                ma20_hist = hist['Close'].iloc[-20:].mean()
                                high_20d_hist = hist['Close'].iloc[-21:-1].max()
                                vol_5ma_hist = hist['Volume'].iloc[-6:-1].mean()
                                vol_ratio_hist = hist['Volume'].iloc[-1] / vol_5ma_hist if vol_5ma_hist > 0 else 0
                                bias20_hist = (c_price_hist - ma20_hist) / ma20_hist * 100
                                
                                body_hist = abs(c_price_hist - o_price_hist)
                                upper_shadow_hist = h_price_hist - max(o_price_hist, c_price_hist)
                                is_break_reversal_hist = body_hist > 0 and (upper_shadow_hist / body_hist) > 1.5
                                
                                old_s['capital_rank'] = get_right_capital_rank(c_price_hist, ma5_hist, ma20_hist, high_20d_hist, vol_ratio_hist, bias20_hist, is_break_reversal_hist)
                        
                        # === 左側價值歷史同步 ===
                        elif strategy_type == 'LEFT':
                            if len(hist) >= 60:
                                closes = hist['Close'].tolist()
                                c_price_hist = closes[-1]
                                o_price_hist = float(hist['Open'].iloc[-1])
                                h_price_hist = float(hist['High'].iloc[-1])
                                l_price_hist = float(hist['Low'].iloc[-1])
                                
                                ma5_hist = sum(closes[-5:]) / 5
                                ma60_hist = sum(closes[-60:]) / 60
                                bias60_hist = (c_price_hist - ma60_hist) / ma60_hist * 100
                                rsi_today_hist = calculate_rsi(closes)
                                rsi_yest_hist = calculate_rsi(closes[:-1])
                                
                                is_above_5ma_hist = c_price_hist > ma5_hist
                                is_breaking_low_hist = c_price_hist < min(closes[-5:-1])
                                
                                body_hist = abs(c_price_hist - o_price_hist)
                                upper_shadow_hist = h_price_hist - max(o_price_hist, c_price_hist)
                                lower_shadow_hist = min(o_price_hist, c_price_hist) - l_price_hist
                                
                                close_yest_hist = closes[-2]
                                open_yest_hist = float(hist['Open'].iloc[-2])
                                is_hammer_hist = (lower_shadow_hist > body_hist * 2.0) and (upper_shadow_hist < body_hist * 0.5)
                                is_be_hist = (close_yest_hist < open_yest_hist) and (o_price_hist < close_yest_hist) and (c_price_hist > open_yest_hist)
                                is_strong_rev_hist = is_hammer_hist or is_be_hist
                                is_anti_knife_hist = lower_shadow_hist > max(body_hist, 0.01) * 1.5

                                old_s['capital_rank'] = get_left_capital_rank(
                                    is_above_5ma_hist, is_strong_rev_hist, is_anti_knife_hist,
                                    is_breaking_low_hist, bias60_hist, rsi_yest_hist, rsi_today_hist,
                                    old_s.get('buy_days', 0), old_s.get('eps', 0)
                                )

                        updated_history.append(old_s) 
                        
                except Exception as e:
                    print(f"⚠️ 學長 {code} 更新失敗: {e}")
    except Exception as e:
        print(f"⚠️ 讀取 {file_name} 失敗: {e}")

    return updated_history

def update_stock_list_json():
    print("🚀 [Task 1] 開始抓取所有股票代號與產業分類...")
    
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

    CUSTOM_ELITE_DATA = {
        "2330": "半導體", "2317": "AI伺服器", "2454": "IC設計", "2382": "AI伺服器",
        "3231": "AI伺服器", "2376": "板卡", "2603": "航運", "2609": "航運",
        "1519": "重電", "1503": "重電", "3017": "散熱", "3324": "散熱"
    }
    
    urls = [
        "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2", 
        "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"  
    ]
    
    stock_map = {}

    if os.path.exists('stock_list.json'):
        try:
            with open('stock_list.json', 'r', encoding='utf-8') as f:
                stock_map = json.load(f)
            print(f"📥 成功載入本地備用名單，共 {len(stock_map)} 筆作為防線基底。")
        except Exception as e:
            print(f"⚠️ 讀取本地 stock_list.json 失敗: {e}")

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}

    for url in urls:
        try:
            res = requests.get(url, headers=headers, timeout=10)
            dfs = pd.read_html(StringIO(res.text))
            df = dfs[0]
            
            df.columns = df.iloc[0]
            df = df.iloc[1:]
            
            col_code_name = [c for c in df.columns if "有價證券代號" in str(c)]
            col_sector = [c for c in df.columns if "產業別" in str(c)]
            if not col_code_name: continue
            
            name_col = col_code_name[0]
            sector_col = col_sector[0] if col_sector else None
            
            for index, row in df.iterrows():
                item = str(row[name_col]).strip()
                sector_val = str(row[sector_col]).strip() if sector_col else "未知產業"
                if sector_val == 'nan': sector_val = "無"
                
                match = re.match(r'^([A-Z0-9]{4,6})\s+(.+)', item)
                if match:
                    code = match.group(1)
                    name = match.group(2).strip()
                    
                    is_normal_stock = (len(code) == 4 and code.isdigit()) 
                    is_etf = code.startswith('00')                        
                    
                    if not (is_normal_stock or is_etf):
                        continue 
                    
                    if code in CUSTOM_ELITE_DATA:
                        sector_val = CUSTOM_ELITE_DATA[code]
                        
                    stock_map[code] = {
                        "name": name,
                        "sector": sector_val,
                        "type": "股票"
                    }
        except Exception as e:
            print(f"⚠️ [Task 1] 抓取錯誤 ({url}): {e}，將自動沿用本地防線資料。")

    for code, meta in CUSTOM_ETF_META.items():
        stock_map[code] = meta

    print(f"✅ [Task 1] 完成，共收錄 {len(stock_map)} 檔純股票與ETF -> 存入 stock_list.json")

    with open('stock_list.json', 'w', encoding='utf-8') as f:
        json.dump(stock_map, f, ensure_ascii=False, indent=2)

def generate_daily_recommendations():
    print("\n🚀 [Task 2] 開始分析每日熱門飆股...")
    
    TAIWAN_50 = [
        "2330", "2317", "2454", "2382", "2308", "2881", "2412", "2882", "2891", "2886", 
        "1303", "2884", "1216", "2892", "2002", "2885", "3231", "2303", "2890", "2880", 
        "2883", "5880", "1301", "2345", "3711", "2887", "1101", "2324", "2357", "3045", 
        "2395", "1326", "2603", "3008", "3036", "6669", "3661", "2408", "2207", "4904", 
        "1519", "1590", "9904", "2353", "6505", "2368", "7769", "2449", "3037", "3653"
    ]
    
    stock_meta = {}
    try:
        if os.path.exists('stock_list.json'):
            with open('stock_list.json', 'r', encoding='utf-8') as f:
                stock_meta = json.load(f)
    except Exception as e:
        print(f"⚠️ 讀取 stock_list.json 失敗: {e}")

    tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
    
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
            print("🔄 嘗試抓取最新交易日資料...")
            url_latest = "https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&type=ALLBUT0999"
            res = requests.get(url_latest, timeout=10)
            data = res.json()
        
        if data.get('stat') == 'OK':
            target_table = None
            if 'tables' in data:
                for table in data['tables']:
                    if '證券代號' in table.get('fields', []) and '收盤價' in table.get('fields', []):
                        target_table = table
                        break
            elif 'data9' in data:
                target_table = {'data': data['data9'], 'fields': data.get('fields9', [])}

            if target_table:
                raw_data = target_table['data']
                fields = target_table['fields']
                
                try:
                    idx_code = fields.index("證券代號")
                    idx_vol = fields.index("成交股數")
                    idx_turnover = fields.index("成交金額") 
                    idx_price = fields.index("收盤價")
                    idx_sign = fields.index("漲跌(+/-)")
                except:
                    idx_code, idx_vol, idx_turnover, idx_price, idx_sign = 0, 2, 4, 8, 9 

                candidates = []
                for row in raw_data:
                    try:
                        code = row[idx_code]
                        if len(code) > 4 or code.startswith('91') or code.startswith('00'): continue 
                        
                        price_str = row[idx_price].replace(',', '')
                        turnover_str = row[idx_turnover].replace(',', '')
                        
                        if price_str == '--' or turnover_str == '--': continue
                        price = float(price_str)
                        turnover = float(turnover_str)
                        
                        if price < 10: continue
                        
                        sign = row[idx_sign]
                        is_up = ('+' in sign) or ('red' in sign) 
                        
                        if is_up and turnover > 100000000: 
                            candidates.append({"code": code, "turnover": turnover, "price": price, "exchange": "上市"})
                    except: continue

                print(f"🔄 正在尋找最新上櫃 (TPEx) 行情...")
                
                data_otc = None
                valid_roc_date = None
                base_date = datetime.strptime(target_date, '%Y%m%d')
                
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                
                for i in range(6):
                    check_date = base_date - timedelta(days=i)
                    roc_year = check_date.year - 1911
                    roc_date = f"{roc_year}/{check_date.strftime('%m/%d')}"
                    
                    url_otc = f"https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={roc_date}&se=EW"
                    try:
                        res_otc = requests.get(url_otc, headers=headers, timeout=10)
                        temp_data = res_otc.json()
                        
                        if 'tables' in temp_data and temp_data['tables']:
                            if 'data' in temp_data['tables'][0] and len(temp_data['tables'][0]['data']) > 0:
                                data_otc = temp_data
                                valid_roc_date = roc_date
                                print(f"✅ 成功取得上櫃資料，實際資料日期: {valid_roc_date}")
                                break
                    except Exception as e:
                        print(f"⚠️ {roc_date} 抓取失敗，嘗試前一天... ({e})")
                    
                    time.sleep(0.5)

                tpex_count = 0  

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
                            
                            if is_up and turnover > 100000000: 
                                candidates.append({"code": code, "turnover": turnover, "price": price, "exchange": "上櫃"})
                                tpex_count += 1
                        except: continue
                    print(f"✅ 上櫃 (TPEx) 飆股已成功合併至候選池！(共 {tpex_count} 檔通過 3 億門檻)")
                else:
                    print("❌ 仍無法取得上櫃資料，請檢查 API 狀態。")
                            
                candidates.sort(key=lambda x: x['turnover'], reverse=True)
                top_50 = candidates[:50]
                
                tw_count = sum(1 for x in top_50 if x.get('exchange') == '上市')
                otc_count = sum(1 for x in top_50 if x.get('exchange') == '上櫃')
                
                print(f"✅ [Task 2] 第一階段篩選完成，取得 50 檔強勢資金股 (上市: {tw_count} 檔 / 上櫃: {otc_count} 檔)。")
                print("啟動 FinMind 深度掃描...")
                final_list = []
                
                for item in top_50:
                    code = item['code']
                    turnover = item['turnover']
                    price = item['price']
                    
                    acc_f, acc_t = get_finmind_chips(code)
                    if acc_f is None: 
                        continue
                        
                    yoy_data = get_finmind_revenue_yoy(code) 
                    yoy = yoy_data['yoy']
                    if yoy is None:
                        continue
                    
                    chips_sum = acc_f + acc_t
                    buy_value = chips_sum * 1000 * price
                    buy_value_y = round(buy_value / 100000000, 1)
                    
                    print(f"掃描 {code}: YoY={yoy}%, 法人買超={buy_value_y}億")
                    time.sleep(0.5) 
                    
                    if yoy > 10 and buy_value > 300000000:
                        meta_info = stock_meta.get(code, {})
                        stock_name = meta_info.get('name', '未知名稱')
                        stock_sector = meta_info.get('sector', '未知產業')
                        stock_exchange = item.get('exchange', '未知')
                        capital_rank = "C"

                        try:
                            suffix = ".TWO" if stock_exchange == '上櫃' else ".TW"
                            hist = yf.Ticker(f"{code}{suffix}").history(period="2mo")
                            if not hist.empty and len(hist) > 22:
                                closes = hist['Close']
                                volumes = hist['Volume']
                                
                                latest_k = hist.iloc[-1]
                                c_price = latest_k['Close']
                                o_price = latest_k['Open']
                                h_price = latest_k['High']
                                
                                ma20 = closes.iloc[-20:].mean()
                                ma5 = closes.iloc[-5:].mean()
                                bias20 = (c_price - ma20) / ma20 * 100
                                
                                vol_today = volumes.iloc[-1]
                                vol_5ma = volumes.iloc[-6:-1].mean()
                                vol_ratio = vol_today / vol_5ma if vol_5ma > 0 else 0
                                
                                high_20d = closes.iloc[-21:-1].max()
                                
                                upper_shadow = h_price - max(o_price, c_price)
                                body = abs(c_price - o_price)
                                is_break_reversal = body > 0 and (upper_shadow / body) > 1.5
                                if is_break_reversal:
                                    print(f"⚠️ {code} 出現長上影線避雷針，防禦假突破，淘汰！")
                                    continue
                                
                                capital_rank = get_right_capital_rank(c_price, ma5, ma20, high_20d, vol_ratio, bias20)
                        except Exception as e:
                            pass
                        
                        stock_cap_size = "大型權值股" if code in TAIWAN_50 else "中小型股"
                        score_yoy = min(yoy, 100) * 1.5
                        buy_value_y = buy_value / 100000000
                        capped_buy = min(buy_value_y, 10)
                        score_chips = capped_buy * 5
                        m_score = score_yoy + score_chips
                        if stock_cap_size == "中小型股":
                            m_score = m_score * 1.2
                            
                        final_tag = "外資大買" if acc_f > acc_t else "投信作帳"
                            
                        date_str = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"
                        _, _, ex_date, _ = get_latest_dividend_info(code, price)

                        final_list.append({
                            "date": date_str,
                            "code": code,
                            "name": stock_name,
                            "exchange": stock_exchange,
                            "sector": stock_sector,
                            "cap_size": stock_cap_size, 
                            "m_score": round(m_score, 2), 
                            "capital_rank": capital_rank,
                            "ex_dividend_date": ex_date, 
                            "price": price,
                            "turnover": turnover,
                            "chips_display": f"{chips_sum}張 ({buy_value_y:.1f}億)",
                            "buy_value": buy_value,
                            "yoy": yoy,
                            "tag": final_tag,
                            "debug_info": yoy_data['debug_info']
                        })
                
                # 第一階段排序，保留 m_score 最高的 15 檔
                final_list.sort(key=lambda x: x['m_score'], reverse=True)
                final_list = final_list[:15]
                print(f"🎉 掃描結束！共 {len(final_list)} 檔符合【高潛力成長飆股】終極標準。")
            else:
                print("⚠️ [Task 2] 找不到對應的資料表")
        else:
            print("⚠️ [Task 2] API 回傳狀態非 OK")

    except Exception as e:
        print(f"❌ [Task 2] 發生錯誤: {e}")

    today_codes = {s['code'] for s in final_list}
    updated_history = sync_historical_data('daily_recommendations.json', today_codes, 'RIGHT', TAIWAN_50)
    final_list.extend(updated_history)

    merged_list = merge_history_data(final_list, 'daily_recommendations.json', 'm_score')
    
    filtered_momentum = []
    today_dt = datetime.now(timezone.utc) + timedelta(hours=8)
    
    if merged_list:
        for item in merged_list:
            try:
                first_date_str = item.get('first_entry_date', item.get('date'))
                first_date = datetime.strptime(first_date_str, '%Y-%m-%d').replace(tzinfo=timezone(timedelta(hours=8)))
                days_diff = (today_dt - first_date).days
            except:
                days_diff = 0
                
            if days_diff > 30:
                continue
                
            raw_fp = item.get('first_entry_price')
            if raw_fp is None: raw_fp = item.get('price')
            if raw_fp is None: raw_fp = 1
            first_price = float(raw_fp)
            
            raw_cp = item.get('price')
            if raw_cp is None: raw_cp = 1
            current_price = float(raw_cp)
            
            roi = (current_price - first_price) / first_price if first_price > 0 else 0
            
            if roi <= -0.08:
                continue
                
            filtered_momentum.append(item)

        # 🚀 最終極雙重排序：S/A/B/C 級別優先，其次為 m_score 動能總分
        rank_order = {"S": 0, "A": 1, "B": 2, "C": 3}
        filtered_momentum.sort(key=lambda x: (rank_order.get(x.get('capital_rank', 'C'), 3), -x.get('m_score', 0)))

        clean_filtered_momentum = clean_nan(filtered_momentum) 
        with open('daily_recommendations.json', 'w', encoding='utf-8') as f:
            json.dump(clean_filtered_momentum, f, ensure_ascii=False, indent=4, allow_nan=False) 
        print(f"💾 已儲存 daily_recommendations.json (保留 {len(filtered_momentum)} 檔)")
    else:
        print("⚠️ 歷史與今日皆無資料可存。")
   
def generate_left_side_value():
    print("\n🛡️ [Task 3] 啟動左側交易：重裝價值雷達 (三層漏斗過濾)...")
    
    stock_meta = {}
    try:
        with open('stock_list.json', 'r', encoding='utf-8') as f:
            stock_meta = {k: v for k, v in json.load(f).items() if v.get('type') == '股票'}
    except Exception as e:
        print(f"⚠️ 讀取 stock_list.json 失敗，左側雷達中止: {e}")
        return

    print("🌊 [第一層] 大數據降維：尋找流動性 1000萬~5億 的潛伏股...")
    layer1_candidates = []
    
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
                    if code.startswith('00'): continue 
                    try:
                        turnover = float(row[idx_turnover].replace(',', ''))
                        price = float(row[idx_price].replace(',', ''))
                        if 10000000 <= turnover <= 500000000 and price >= 10:
                            layer1_candidates.append({"code": code, "price": price, "market": "TW"})
                    except: pass
    except Exception as e:
        print(f"⚠️ TWSE 第一層抓取錯誤: {e}")

    print(f"🔄 正在尋找最新上櫃 (TPEx) 行情...")
    try:
        base_date = datetime.now(timezone.utc) + timedelta(hours=8)
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
                            if code.startswith('00'): continue 
                            try:
                                price_str = str(row[idx_price]).replace(',', '').strip()
                                turnover_str = str(row[idx_turnover]).replace(',', '').strip()
                                if price_str in ['----', '--', '除息', '除權'] or turnover_str in ['--', '']: continue
                                turnover = float(turnover_str)
                                price = float(price_str)
                                if 10000000 <= turnover <= 500000000 and price >= 10:
                                    layer1_candidates.append({"code": code, "price": price, "market": "TWO"})
                            except: pass
                        break
            except Exception: pass
            time.sleep(0.5)
    except Exception as e:
        print(f"⚠️ TPEx 第一層抓取錯誤: {e}")

    print(f"✅ 第一層降維完畢，進入第二層。")

    print(f"📉 [第二層] 啟動 yfinance 計算 (預計處理 {len(layer1_candidates)} 檔)...")
    layer2_candidates = []
    
    for i, item in enumerate(layer1_candidates):
        if i > 0 and i % 50 == 0: 
            print(f"   ... 已處理 {i}/{len(layer1_candidates)} 檔")
            
        code = item['code']
        try:
            ticker = yf.Ticker(f"{code}.{item['market']}")
            df = ticker.history(period="6mo") 
            if df.empty or len(df) < 60: continue

            closes = df['Close'].tolist()
            lows = df['Low'].tolist()
            highs = df['High'].tolist()
            opens = df['Open'].tolist() 
            volumes = df['Volume'].tolist()

            item['real_date'] = df.index[-1].strftime('%Y-%m-%d')
            
            close_today = closes[-1]
            open_today = opens[-1]
            low_today = lows[-1]
            
            item['price'] = round(close_today, 2)
            ma60 = sum(closes[-60:]) / 60
            ma24 = sum(closes[-24:]) / 24 
            ma6 = sum(closes[-6:]) / 6    
            ma5 = sum(closes[-5:]) / 5     
            item['is_above_5ma'] = bool(close_today > ma5) 

            item['rsi_today'] = calculate_rsi(closes)
            item['rsi_yest'] = calculate_rsi(closes[:-1])
            
            bias60 = (close_today - ma60) / ma60
            bias24 = (close_today - ma24) / ma24
            bias6 = (close_today - ma6) / ma6

            if bias60 >= 0: continue
            
            vol_today = volumes[-1]
            ma20_vol = sum(volumes[-20:]) / 20

            if ma20_vol < 500000: continue 

            vol_ratio = vol_today / ma20_vol if ma20_vol > 0 else 1
            
            if (max(highs[-10:]) - min(lows[-10:])) / min(lows[-10:]) >= 0.15: continue
            
            if len(closes) >= 6 and (close_today - closes[-6]) / closes[-6] >= 0.08: continue

            item['is_breaking_low'] = bool(close_today < min(closes[-5:-1]))
                
            is_red_candle = close_today > open_today
            lower_shadow = min(open_today, close_today) - low_today
            upper_shadow = highs[-1] - max(open_today, close_today)
            body = abs(close_today - open_today)
            
            close_yest = closes[-2] if len(closes) > 1 else close_today
            open_yest = opens[-2] if len(opens) > 1 else open_today

            is_hammer = (lower_shadow > body * 2.0) and (upper_shadow < body * 0.5)
            is_bullish_engulfing = (close_yest < open_yest) and (open_today < close_yest) and (close_today > open_yest)
            
            item['is_strong_reversal'] = bool(is_hammer or is_bullish_engulfing)
            
            item['is_anti_knife'] = bool(lower_shadow > max(body, 0.01) * 1.5)

            item['bias60'] = bias60
            item['bias24'] = bias24 
            item['bias6'] = bias6   
            item['vol_ratio'] = vol_ratio
            item['vol_5d'] = sum(volumes[-5:]) 
            layer2_candidates.append(item)
            
        except Exception: pass
        time.sleep(0.1)

    print(f"✅ 第二層過濾完畢，剩餘 {len(layer2_candidates)} 檔進入終極基本面與評分查核。")

    print("🏦 [第三層] 啟動 FinMind 查核與動態評分 (API 節流模式啟動)...")
    final_list = []
    
    for item in layer2_candidates:
        code = item['code']
        print(f"   🔍 查核 {code}...", end=" ")
        
        chips_history = get_finmind_chips_history(code, days=5)
        if chips_history is None:
            print("❌ 籌碼API異常")
            continue
            
        buy_days_5d = sum(1 for x in chips_history if x > 0)
        if buy_days_5d < 1:
            print("❌ 籌碼掛零")
            continue
            
        net_buy_vol_5d = sum(chips_history)
        total_vol_5d = item['vol_5d'] / 1000 
        buy_ratio = (net_buy_vol_5d / total_vol_5d) * 100 if total_vol_5d > 0 else 0
        net_buy_amount_10k = (net_buy_vol_5d * item['price']) / 10
        
        if not ((net_buy_vol_5d > 100 or net_buy_amount_10k > 500) and buy_ratio > 2.0):
            print("❌ 佔比/金額不足")
            continue
        
        eps, _, _ = get_finmind_fundamentals(code, item['price'], fetch_yield=False)
        if eps is None:
            print("❌ EPS API異常")
            continue
            
        yoy_data = get_finmind_revenue_yoy(code)
        yoy = yoy_data['yoy']
        if yoy is None:
            print("❌ YoY API異常")
            continue
        
        yield_rate, yield_formula, ex_date, is_upcoming = get_latest_dividend_info(code, item['price'])
        
        score = 40 
        
        if eps < 0:
            if item.get('is_breaking_low') and not item.get('is_strong_reversal'):
                print("❌ 虧損且破底無防守，淘汰")
                continue
            if buy_days_5d < 4 and buy_ratio < 5.0:
                print("❌ 虧損且籌碼集中度不足，淘汰")
                continue
            if yoy <= 0:
                print("❌ 虧損且營收未反轉，淘汰")
                continue
            score -= 5
            print("   ⚠️ 虧損轉機股通關，扣 5 分")
            
        elif item.get('is_breaking_low'):
            score -= 10
            
        if item.get('is_strong_reversal'): 
            score += 15
            print(f"   ⭐ 偵測到強力底部反轉型態！")
        elif item.get('is_anti_knife'): 
            score += 5
        
        if eps > 0: score += 10
        if yoy > 10.0: score += 10
        if yield_rate >= 4.0: score += 10
        
        if is_upcoming:
            score += 10
            print(f"   💰 具備即將除息優勢 ({ex_date})，額外加 10 分！")
        
        if buy_ratio > 5.0: score += 10
        if buy_days_5d == 5: score += 30
        elif buy_days_5d == 4: score += 20
        elif buy_days_5d == 3: score += 10
        
        if item['vol_ratio'] < 0.5: score += 10
        elif item['vol_ratio'] < 0.6: score += 8
        elif item['vol_ratio'] < 0.7: score += 5
        
        bias_pct = item['bias60'] * 100
        if bias_pct < -8.0: score += 15
        elif bias_pct < -5.0: score += 10
        elif bias_pct < -3.0: score += 5

        if item['rsi_yest'] < 35 and item['rsi_today'] > item['rsi_yest']:
           score += 15
           print(f"   🚀 RSI超賣區勾頭向上 ({item['rsi_yest']} -> {item['rsi_today']})，加 15 分！")

        if item['is_above_5ma']:
            trend_status = "🔥 L1_左側起漲"
            is_qualified = (score >= 60)  
        else:
            trend_status = "⏳ L0_左側築底"
            is_qualified = (score >= 55)

        if not is_qualified:
            print(f"❌ 資格不符 ({trend_status} 但分數僅 {score} 分)")
            continue
            
        entry_price = round(item['price'] * 0.99, 2)
        
        # 🚀 賦予左側資金優先級別
        capital_rank = get_left_capital_rank(
            item['is_above_5ma'], item['is_strong_reversal'], item['is_anti_knife'],
            item['is_breaking_low'], bias_pct, item['rsi_yest'], item['rsi_today'],
            buy_days_5d, eps
        )

        print(f"✅ 最終清單入選 | 級別: {capital_rank} | 分數: {score} | {trend_status}")

        final_list.append({
            "date": item['real_date'],
            "code": code,
            "name": stock_meta[code]['name'],
            "price": item['price'],
            "exchange": "上市" if item.get('market') == 'TW' else "上櫃",
            "score": score,
            "capital_rank": capital_rank,
            "trend_status": trend_status,
            "entry_price": entry_price,
            "ex_dividend_date": ex_date,  
            "bias60": f"{bias_pct:.1f}%",
            "bias24": f"{item['bias24']*100:.1f}%", 
            "bias6": f"{item['bias6']*100:.1f}%",   
            "vol_ratio": f"{item['vol_ratio']*100:.1f}%",
            "eps": eps,
            "yield_rate": yield_rate,
            "yield_formula": yield_formula,  
            "buy_days": buy_days_5d,
            "tag": "左側黃金坑"
        })

    if final_list:
        final_list.sort(key=lambda x: x['score'], reverse=True)
        final_list = final_list[:15]
        print(f"✅ 今日掃描共 {len(final_list)} 檔無敵黃金坑達標。")
    else:
        print("⚠️ 今日掃描無股票通過三層漏斗。")

    today_codes = {s['code'] for s in final_list}
    updated_history = sync_historical_data('left_side_value.json', today_codes, 'LEFT')
    final_list.extend(updated_history)

    merged_list = merge_history_data(final_list, 'left_side_value.json', 'score')
    
    filtered_list = []
    today_dt = datetime.now(timezone.utc) + timedelta(hours=8)
    
    if merged_list:
        for item in merged_list:
            try:
                first_date_str = item.get('first_entry_date', item.get('date'))
                first_date = datetime.strptime(first_date_str, '%Y-%m-%d').replace(tzinfo=timezone(timedelta(hours=8)))
                days_diff = (today_dt - first_date).days
            except:
                days_diff = 0
                
            if days_diff > 30:
                continue
                
            raw_fp = item.get('first_entry_price')
            if raw_fp is None: raw_fp = item.get('price')
            if raw_fp is None: raw_fp = 1
            first_price = float(raw_fp)
            
            raw_cp = item.get('price')
            if raw_cp is None: raw_cp = 1
            current_price = float(raw_cp)
            
            roi = (current_price - first_price) / first_price if first_price > 0 else 0
            
            if roi <= -0.10:
                continue
                
            filtered_list.append(item)

        # 🚀 最終極雙重排序：左側同享資金優先級別排序機制
        rank_order = {"S": 0, "A": 1, "B": 2, "C": 3}
        filtered_list.sort(key=lambda x: (rank_order.get(x.get('capital_rank', 'C'), 3), -x.get('score', 0)))

        clean_filtered_list = clean_nan(filtered_list) 
        with open('left_side_value.json', 'w', encoding='utf-8') as f:
            json.dump(clean_filtered_list, f, ensure_ascii=False, indent=4, allow_nan=False) 
        print(f"💾 已強制更新 left_side_value.json (保留 {len(filtered_list)} 檔)")
    else:
        print("⚠️ 歷史與今日皆無資料可存。")
  
def generate_deposit_stocks():
    print("\n🏦 [Task 4] 啟動存股打折加碼雷達 (均線乖離策略)...")
  
    DEPOSIT_WATCHLIST = [
    "2886", "2892", "5880", "2880", "2881", "2882", "2883", "2884", "2891", "2890", 
    "2330", "2317", "0050", "0056", "00878", "00713", "00919", "00881", "006208", "0052", "00929"
    ]

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
            ticker_tw = yf.Ticker(f"{code}.TW")
            df = ticker_tw.history(period="6mo") 
            if df.empty:
                ticker_two = yf.Ticker(f"{code}.TWO")
                df = ticker_two.history(period="6mo")

            if not df.empty:
                df = df.dropna(subset=['Close'])
            
            if len(df) < 60: 
                print("資料不足，跳過。")
                continue

            closes = df['Close'].tolist()
            close_today = closes[-1]

            data_date_str = df.index[-1].strftime('%Y-%m-%d')
            
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

            eps, yield_rate, annual_div = get_finmind_fundamentals(code, close_today)

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
            else: 
                signal = "🚨 重壓"
                action = "【大舉進場】市場恐慌超跌，長線買點浮現！"

            if bias_20 < -2.0 and bias_5 < 0:
                anti_knife_warning = " ⚠️ (跌勢未止，請分批慢接)"
            elif bias_20 < -2.0 and bias_5 > 0:
                anti_knife_warning = " ⭐ (週線翻正，跌勢止穩，建議加碼！)"

            action += anti_knife_warning
            
            meta_info = stock_meta.get(code, {})
            deposit_list.append({
                "date": data_date_str,  
                "code": code,
                "name": meta_info.get('name', '未知名稱'),
                "price": round(close_today, 2),
                "bias_6": round(bias_6, 2),   
                "bias_12": round(bias_12, 2), 
                "bias_24": round(bias_24, 2), 
                "bias_20": round(bias_20, 2),
                "bias_60": round(bias_60, 2), 
                "yield_rate": yield_rate if yield_rate is not None else 0.0,
                "yield_formula": f"預估配息 {annual_div if annual_div is not None else 0.0:.3f} / 股價 {close_today:.2f}",
                "signal": signal,
                "action": action
            })
            print(f"完成 (乖離 {bias_20:.2f}%, 殖利率 {yield_rate}%)")

        except Exception as e:
            print(f"錯誤: {e}")
        time.sleep(0.1)

    if deposit_list:
        deposit_list.sort(key=lambda x: x['bias_20'])
        
        clean_deposit_list = clean_nan(deposit_list) 
        with open('deposit_stocks.json', 'w', encoding='utf-8') as f:
            json.dump(clean_deposit_list, f, ensure_ascii=False, indent=4, allow_nan=False) 
        print(f"💾 任務完成！已儲存 deposit_stocks.json (共分析 {len(deposit_list)} 檔存股)")

if __name__ == "__main__":
    update_stock_list_json()
    generate_daily_recommendations()  
    generate_left_side_value()        
    generate_deposit_stocks()
