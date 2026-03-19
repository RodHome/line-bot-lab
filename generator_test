import requests
import pandas as pd
import json
import re
import os
import time
from datetime import datetime, timedelta, timezone
from io import StringIO
import yfinance as yf

# 🔥 雙鑰匙負載平衡系統：合併訪客與會員額度 (總計 900次/小時)
GUEST_TOKEN = "" # 訪客鑰匙 (消耗 IP 免費 300 次)
VIP_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJkYXRlIjoiMjAyNi0wMy0xOCAxOToyODoyNCIsInVzZXJfaWQiOiJyb2Q3NDEwMDEyIiwiZW1haWwiOiJyb2Q3NDEwMDFAZ21haWwuY29tIiwiaXAiOiIxMjIuMTE2LjE1OS4xMzQifQ.qmaLCfxjbwXRYo8TwFZKboTfmAADIMs0CWw-oPUJU4g"

def get_finmind_chips(code):
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
    start = (datetime.now() - timedelta(days=480)).strftime('%Y-%m-%d')
    url = "https://api.finmindtrade.com/api/v4/data"
    default_res = {
        "yoy": 0.0, 
        "debug_info": {"status": "No Data", "this_rev": 0, "last_rev": 0, "this_period": "N/A", "last_period": "N/A"}
    }
    try:
        res = requests.get(url, params={"dataset": "TaiwanStockMonthRevenue", "data_id": code, "start_date": start, "token": GUEST_TOKEN}, timeout=10)
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
        data = res.json().get('data', [])
        if not data: return [0]*days
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
    except: return [0]*days

def get_finmind_fundamentals(code, current_price, fetch_yield=True):
    eps_latest = 0.0
    yield_rate = 0.0
    annual_div = 0.0
    start = (datetime.now() - timedelta(days=800)).strftime('%Y-%m-%d')
    url = "https://api.finmindtrade.com/api/v4/data"
    
    try:
        res = requests.get(url, params={"dataset": "TaiwanStockFinancialStatements", "data_id": code, "start_date": start, "token": GUEST_TOKEN}, timeout=5)
        if res.status_code == 200:
            data = res.json().get('data', [])
            eps_data = [d for d in data if d['type'] == 'EPS']
            if eps_data: eps_latest = float(eps_data[-1].get('value', 0))
    except: pass
    
    if not fetch_yield: return eps_latest, yield_rate, annual_div

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
                    if total > 0: valid_cash_records.append({'date': d.get('date'), 'cash': total})
                
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
                    if current_price > 0: yield_rate = round((annual_div / current_price) * 100, 2)
    except: pass
        
    return eps_latest, yield_rate, annual_div

def update_stock_list_json():
    print("🚀 [Task 1] 開始抓取所有股票代號與產業分類 (維持原版)...")
    # 測試版共用 stock_list.json，因為這只是基礎中繼資料
    # (為節省長度，實務上這段你可用你原本的 update_stock_list_json，這裡保留結構)
    pass 

# ========================================================
# 🔥 [測試版] 功能 2: 右側推薦 (五日累積機制)
# ========================================================
def generate_daily_recommendations_test():
    print("\n🚀 [Task 2 TEST] 開始分析每日熱門飆股 (五日累積測試版)...")
    
    stock_meta = {}
    try:
        if os.path.exists('stock_list.json'):
            with open('stock_list.json', 'r', encoding='utf-8') as f:
                stock_meta = json.load(f)
    except Exception as e:
        print(f"⚠️ 讀取 stock_list.json 失敗: {e}")

    # 1. 讀取「測試版歷史名單」，建立去重字典
    history_dict = {}
    test_file_name = 'daily_recommendations_test.json'
    try:
        if os.path.exists(test_file_name):
            with open(test_file_name, 'r', encoding='utf-8') as f:
                old_data = json.load(f)
                if isinstance(old_data, list):
                    for item in old_data:
                        code = item.get('code')
                        if code: history_dict[code] = item
    except Exception as e:
        print(f"⚠️ 讀取 {test_file_name} 失敗 (第一次執行可能無此檔): {e}")

    utc_now = datetime.now(timezone.utc)
    tw_now = utc_now + timedelta(hours=8)
    target_date = (tw_now - timedelta(days=1)).strftime('%Y%m%d') if tw_now.hour < 14 else tw_now.strftime('%Y%m%d')

    print(f"📅 目標日期: {target_date}")
    
    url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&type=ALLBUT0999&date={target_date}"
    final_list = []
    
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        
        if data.get('stat') != 'OK':
            print("🔄 嘗試抓取最新交易日資料...")
            url_latest = "https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&type=ALLBUT0999"
            res = requests.get(url_latest, timeout=10)
            data = res.json()
        
        if data.get('stat') == 'OK':
            target_table = None
            if 'tables' in data:
                for table in data['tables']:
                    if '證券代號' in table.get('fields', []) and '收盤價' in table.get('fields', []):
                        target_table = table; break
            elif 'data9' in data:
                target_table = {'data': data['data9'], 'fields': data.get('fields9', [])}

            if target_table:
                raw_data = target_table['data']
                fields = target_table['fields']
                
                try:
                    idx_code, idx_vol, idx_turnover, idx_price, idx_sign = fields.index("證券代號"), fields.index("成交股數"), fields.index("成交金額"), fields.index("收盤價"), fields.index("漲跌(+/-)")
                except:
                    idx_code, idx_vol, idx_turnover, idx_price, idx_sign = 0, 2, 4, 8, 9

                candidates = []
                for row in raw_data:
                    try:
                        code = row[idx_code]
                        if len(code) > 4 or code.startswith('91') or code.startswith('00'): continue 
                        price_str, turnover_str = row[idx_price].replace(',', ''), row[idx_turnover].replace(',', '')
                        if price_str == '--' or turnover_str == '--': continue
                        price, turnover = float(price_str), float(turnover_str)
                        if price < 10: continue
                        
                        is_up = ('+' in row[idx_sign]) or ('red' in row[idx_sign])
                        if is_up and turnover > 300000000: 
                            candidates.append({"code": code, "turnover": turnover, "price": price, "exchange": "上市"})
                    except: continue

                print(f"🔄 正在尋找最新上櫃 (TPEx) 行情...")
                base_date = datetime.strptime(target_date, '%Y%m%d')
                headers = {'User-Agent': 'Mozilla/5.0'}
                data_otc = None
                
                for i in range(6):
                    check_date = base_date - timedelta(days=i)
                    roc_date = f"{check_date.year - 1911}/{check_date.strftime('%m/%d')}"
                    url_otc = f"https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={roc_date}&se=EW"
                    try:
                        res_otc = requests.get(url_otc, headers=headers, timeout=10)
                        temp_data = res_otc.json()
                        if 'tables' in temp_data and temp_data['tables'] and 'data' in temp_data['tables'][0] and len(temp_data['tables'][0]['data']) > 0:
                            data_otc = temp_data
                            break
                    except: pass
                    time.sleep(0.5)

                if data_otc and 'tables' in data_otc and data_otc['tables']:
                    table = data_otc['tables'][0]
                    fields = [str(f).strip() for f in table.get('fields', [])]
                    raw_data = table.get('data', [])
                    try: idx_code, idx_price, idx_turnover, idx_sign = fields.index("代號"), fields.index("收盤"), fields.index("成交金額(元)"), fields.index("漲跌")
                    except: idx_code, idx_price, idx_turnover, idx_sign = 0, 2, 8, 3
                    
                    for row in raw_data:
                        try:
                            code = str(row[idx_code]).strip()
                            if len(code) > 4 or code.startswith('91') or code.startswith('00'): continue 
                            price_str, turnover_str = str(row[idx_price]).replace(',', '').strip(), str(row[idx_turnover]).replace(',', '').strip() 
                            if price_str in ['----', '--', '', '除息', '除權'] or turnover_str in ['--', '', '0']: continue
                            price, turnover = float(price_str), float(turnover_str)
                            if price < 10: continue
                            
                            raw_sign = str(row[idx_sign]).replace(',', '').strip()
                            is_up = '+' in raw_sign or 'red' in raw_sign or (re.sub(r'[^\d.-]', '', raw_sign) and float(re.sub(r'[^\d.-]', '', raw_sign)) > 0)
                            if is_up and turnover > 300000000: 
                                candidates.append({"code": code, "turnover": turnover, "price": price, "exchange": "上櫃"})
                        except: continue
                
                candidates.sort(key=lambda x: x['turnover'], reverse=True)
                top_50 = candidates[:50]
                
                today_date_str = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"
                
                for item in top_50:
                    code, turnover, price = item['code'], item['turnover'], item['price']
                    acc_f, acc_t = get_finmind_chips(code)
                    yoy_data = get_finmind_revenue_yoy(code) 
                    yoy = yoy_data['yoy']
                    
                    buy_value = (acc_f + acc_t) * 1000 * price
                    time.sleep(0.5) 
                    
                    if yoy > 10 and buy_value > 300000000:
                        # 2. 獲取舊的初次入榜日 (若無則用今天)
                        first_date = history_dict.get(code, {}).get('first_entry_date', today_date_str)

                        # 3. 覆寫或新增進歷史字典 (Upsert)
                        history_dict[code] = {
                            "date": today_date_str, 
                            "first_entry_date": first_date,
                            "code": code,
                            "name": stock_meta.get(code, {}).get('name', '未知名稱'),
                            "exchange": item.get('exchange', '未知'),
                            "sector": stock_meta.get(code, {}).get('sector', '未知產業'),
                            "price": price,
                            "turnover": turnover,
                            "chips_display": f"{acc_f + acc_t}張 ({round(buy_value / 100000000, 1)}億)",
                            "buy_value": buy_value,
                            "yoy": yoy,
                            "tag": "外資大買" if acc_f > acc_t else "投信作帳",
                            "debug_info": yoy_data['debug_info']
                        }
                
                # 4. 五日存活過濾 (獨立交易日保留法)
                all_dates = set(item.get('date') for item in history_dict.values() if item.get('date'))
                allowed_dates = sorted(list(all_dates), reverse=True)[:5]
                
                final_list = [item for item in history_dict.values() if item.get('date') in allowed_dates]
                final_list.sort(key=lambda x: x.get('buy_value', 0), reverse=True)
            else:
                print("⚠️ [Task 2] 找不到對應的資料表")
        else:
            print("⚠️ [Task 2] API 回傳狀態非 OK")

    except Exception as e:
        print(f"❌ [Task 2] 發生錯誤: {e}")

    # 寫入測試用 JSON
    with open(test_file_name, 'w', encoding='utf-8') as f:
        json.dump(final_list, f, ensure_ascii=False, indent=4)
        print(f"💾 已儲存 {test_file_name}")

# ========================================================
# 🔥 [測試版] 功能 3: 左側黃金坑 (五日累積機制)
# ========================================================
def generate_left_side_value_test():
    print("\n🛡️ [Task 3 TEST] 啟動左側交易 (五日累積測試版)...")
    
    stock_meta = {}
    try:
        with open('stock_list.json', 'r', encoding='utf-8') as f:
            stock_meta = {k: v for k, v in json.load(f).items() if v.get('type') == '股票'}
    except: return

    # 1. 讀取「測試版歷史名單」
    history_dict = {}
    test_file_name = 'left_side_value_test.json'
    try:
        if os.path.exists(test_file_name):
            with open(test_file_name, 'r', encoding='utf-8') as f:
                old_data = json.load(f)
                if isinstance(old_data, list):
                    for item in old_data:
                        code = item.get('code')
                        if code: history_dict[code] = item
    except: pass

    # 🌊 第一層/第二層 (移植自原程式碼，確保過濾標準一致)
    layer1_candidates = []
    try:
        res = requests.get("https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&type=ALLBUT0999", timeout=10)
        data = res.json()
        if data.get('stat') == 'OK':
            target_table = next((t for t in data.get('tables', []) if '證券代號' in t.get('fields', [])), None)
            if not target_table and 'data9' in data: target_table = {'data': data['data9'], 'fields': data.get('fields9', [])}
            if target_table:
                fields = target_table['fields']
                idx_code, idx_turnover, idx_price = fields.index("證券代號") if "證券代號" in fields else 0, fields.index("成交金額") if "成交金額" in fields else 4, fields.index("收盤價") if "收盤價" in fields else 8
                for row in target_table['data']:
                    code = row[idx_code]
                    if code not in stock_meta: continue
                    try:
                        turnover, price = float(row[idx_turnover].replace(',', '')), float(row[idx_price].replace(',', ''))
                        if 10000000 <= turnover <= 300000000 and price >= 10: layer1_candidates.append({"code": code, "price": price, "market": "TW"})
                    except: pass
    except: pass

    try:
        base_date = datetime.now(timezone.utc) + timedelta(hours=8)
        headers = {'User-Agent': 'Mozilla/5.0'}
        for i in range(6): 
            check_date = base_date - timedelta(days=i)
            roc_date = f"{check_date.year - 1911}/{check_date.strftime('%m/%d')}"
            url_otc = f"https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={roc_date}&se=EW"
            res_otc = requests.get(url_otc, headers=headers, timeout=10)
            temp_data = res_otc.json()
            if 'tables' in temp_data and temp_data['tables'] and len(temp_data['tables'][0].get('data', [])) > 0:
                table = temp_data['tables'][0]
                fields = [str(f).strip() for f in table.get('fields', [])]
                idx_code, idx_turnover, idx_price = fields.index("代號") if "代號" in fields else 0, fields.index("成交金額(元)") if "成交金額(元)" in fields else 8, fields.index("收盤") if "收盤" in fields else 2
                for row in table['data']:
                    code = str(row[idx_code]).strip()
                    if code not in stock_meta: continue
                    try:
                        price_str, turnover_str = str(row[idx_price]).replace(',', '').strip(), str(row[idx_turnover]).replace(',', '').strip()
                        if price_str in ['----', '--', '除息', '除權'] or turnover_str in ['--', '']: continue
                        turnover, price = float(turnover_str), float(price_str)
                        if 10000000 <= turnover <= 300000000 and price >= 10: layer1_candidates.append({"code": code, "price": price, "market": "TWO"})
                    except: pass
                break
            time.sleep(0.3)
    except: pass

    layer2_candidates = []
    for item in layer1_candidates:
        code = item['code']
        try:
            ticker = yf.Ticker(f"{code}.{item['market']}")
            df = ticker.history(period="6mo") 
            if df.empty or len(df) < 60: continue
            closes, lows, highs, volumes = df['Close'].tolist(), df['Low'].tolist(), df['High'].tolist(), df['Volume'].tolist()
            close_today, ma60, ma24, ma6 = closes[-1], sum(closes[-60:]) / 60, sum(closes[-24:]) / 24, sum(closes[-6:]) / 6    
            bias60, bias24, bias6 = (close_today - ma60) / ma60, (close_today - ma24) / ma24, (close_today - ma6) / ma6

            if bias60 >= -0.03: continue
            vol_today, ma20_vol = volumes[-1], sum(volumes[-20:]) / 20
            vol_ratio = vol_today / ma20_vol 
            
            if vol_ratio >= 0.8: continue
            recent_10_high, recent_10_low = max(highs[-10:]), min(lows[-10:])
            amplitude = (recent_10_high - recent_10_low) / recent_10_low
            
            if amplitude >= 0.12 or (close_today - closes[-5]) / closes[-5] >= 0.05: continue

            item.update({'bias60': bias60, 'bias24': bias24, 'bias6': bias6, 'vol_ratio': vol_ratio, 'amplitude': amplitude, 'ma60': ma60})
            layer2_candidates.append(item)
        except: pass
        time.sleep(0.1)

    today_date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    for item in layer2_candidates:
        code = item['code']
        eps, yield_rate, _ = get_finmind_fundamentals(code, item['price'], fetch_yield=False)
        if eps <= 0: continue 
        
        yoy_data = get_finmind_revenue_yoy(code)
        yoy = yoy_data['yoy']
        buy_days = sum(1 for x in get_finmind_chips_history(code, days=5) if x > 0)
        
        if buy_days >= 4 or (buy_days == 3 and yoy > -15.0):
            score = 50 
            if buy_days == 5: score += 30
            elif buy_days == 4: score += 20
            elif buy_days == 3: score += 10
            
            if item['vol_ratio'] < 0.5: score += 10
            elif item['vol_ratio'] < 0.6: score += 8
            elif item['vol_ratio'] < 0.7: score += 5
            
            bias_pct = item['bias60'] * 100
            if -8.0 <= bias_pct <= -5.0: score += 10
            elif bias_pct < -8.0: score += 8
            elif -5.0 < bias_pct <= -3.0: score += 5

            trend_status = "⭐ 底部起漲 (乖離6已翻正)" if item['bias6'] > 0 else "⏳ 築底量縮中 (乖離6仍為負)"
            entry_price = round(item['price'] * 0.99, 2)

            # 2. 抓取初次入榜日
            first_date = history_dict.get(code, {}).get('first_entry_date', today_date_str)

            # 3. Upsert
            history_dict[code] = {
                "date": today_date_str,
                "first_entry_date": first_date,
                "code": code,
                "name": stock_meta[code]['name'],
                "price": item['price'],
                "score": score,
                "trend_status": trend_status,
                "entry_price": entry_price,
                "bias60": f"{bias_pct:.1f}%",
                "bias24": f"{item['bias24']*100:.1f}%", 
                "bias6": f"{item['bias6']*100:.1f}%",   
                "vol_ratio": f"{item['vol_ratio']*100:.1f}%",
                "eps": eps,
                "yield_rate": yield_rate,
                "buy_days": buy_days,
                "tag": "左側黃金坑"
            }

    # 4. 五日過濾
    all_dates = set(item.get('date') for item in history_dict.values() if item.get('date'))
    allowed_dates = sorted(list(all_dates), reverse=True)[:5]
    final_list = [item for item in history_dict.values() if item.get('date') in allowed_dates]

    if final_list: final_list.sort(key=lambda x: x.get('score', 0), reverse=True)

    with open(test_file_name, 'w', encoding='utf-8') as f:
        json.dump(final_list, f, ensure_ascii=False, indent=4)
        print(f"💾 已強制更新 {test_file_name}")

if __name__ == "__main__":
    # update_stock_list_json()  # 測試階段可以省略這行，直接吃正式版的 stock_list.json
    generate_daily_recommendations_test()
    generate_left_side_value_test()
    # generate_deposit_stocks() # 存股與本次測試無關，可省略
