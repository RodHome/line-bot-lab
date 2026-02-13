import requests
import json
import re
import os
import time
import concurrent.futures
import lxml.html  # 改用輕量的 lxml 取代 pandas
from datetime import datetime, timedelta, timezone

# --- 設定區 ---
FINMIND_TOKEN = os.environ.get('FINMIND_TOKEN', '')

# --- 技術指標計算 (維持不變) ---
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

def calculate_kd(highs, lows, closes, period=9):
    if len(closes) < period: return 50, 50
    k = 50; d = 50
    try:
        highest_high = max(highs[-period:])
        lowest_low = min(lows[-period:])
        rsv = 0
        if highest_high != lowest_low:
            rsv = (closes[-1] - lowest_low) / (highest_high - lowest_low) * 100
        k = (2/3) * 50 + (1/3) * rsv
        d = (2/3) * 50 + (1/3) * k
    except: pass
    return round(k, 1), round(d, 1)

# --- 任務 1: 更新股票代號 (輕量化版 - 移除 Pandas) ---
def update_stock_list_json():
    print("🚀 [Task 1] 更新股票代號清單 (Light Mode)...")
    urls = [
        "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2", # 上市
        "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"  # 上櫃
    ]
    stock_map = {}
    
    for url in urls:
        try:
            res = requests.get(url, timeout=10)
            # 使用 lxml 解析 HTML，不依賴 pandas
            tree = lxml.html.fromstring(res.text)
            
            # 抓取所有表格列 <tr>
            rows = tree.xpath('//tr')
            
            for row in rows:
                cols = row.xpath('.//td')
                if not cols: continue
                
                # 通常第一欄是 "有價證券代號及名稱"
                # 內容格式如: "1101　台泥"
                text_content = cols[0].text_content().strip()
                
                match = re.match(r'^(\d{4})\s+(.+)', text_content)
                if match:
                    code = match.group(1)
                    name = match.group(2).strip()
                    stock_map[name] = code
                    
        except Exception as e:
            print(f"⚠️ [Task 1] 解析失敗 ({url}): {e}")
    
    # 補上熱門 ETF
    etfs = ["0050", "0056", "00878", "00929", "00919", "00940", "006208", "00713", "00939", "00679B"]
    for code in etfs: stock_map[code] = code

    with open('stock_list.json', 'w', encoding='utf-8') as f:
        json.dump(stock_map, f, ensure_ascii=False, indent=2)
    print(f"✅ [Task 1] 完成，共 {len(stock_map)} 檔。")

# --- 任務 2: 抓取詳細數據 (邏輯維持不變) ---
def fetch_stock_details(code, base_info):
    time.sleep(0.3)
    result = base_info.copy()
    
    # 預設值
    result.update({
        "eps": "N/A", 
        "yield": "N/A", 
        "chips_f": 0, "chips_t": 0, 
        "k": 50, "d": 50, "rsi": 50, 
        "ma5": 0, "ma20": 0, "ma60": 0,
        "last_close_price": 0
    })
    
    url = "https://api.finmindtrade.com/api/v4/data"
    
    try:
        # 1. 歷史股價 & 技術指標
        start = (datetime.now() - timedelta(days=150)).strftime('%Y-%m-%d')
        res = requests.get(url, params={"dataset": "TaiwanStockPrice", "data_id": code, "start_date": start, "token": FINMIND_TOKEN}, timeout=6)
        hist = res.json().get('data', [])
        
        if hist:
            closes = [d['close'] for d in hist]; highs = [d['max'] for d in hist]; lows = [d['min'] for d in hist]
            k, d_val = calculate_kd(highs, lows, closes)
            result.update({
                "k": k, "d": d_val, "rsi": calculate_rsi(closes),
                "ma5": round(sum(closes[-5:])/5, 2) if len(closes)>=5 else 0,
                "ma20": round(sum(closes[-20:])/20, 2) if len(closes)>=20 else 0,
                "ma60": round(sum(closes[-60:])/60, 2) if len(closes)>=60 else 0,
                "last_close_price": closes[-1]
            })

        # 2. 三大法人
        start_chip = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
        res_c = requests.get(url, params={"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": code, "start_date": start_chip, "token": FINMIND_TOKEN}, timeout=6)
        chips = res_c.json().get('data', [])
        if chips:
            chips = sorted(chips, key=lambda x: x['date'], reverse=True)
            latest = chips[0]['date']
            f_buy = sum([x['buy'] - x['sell'] for x in chips if x['date'] == latest and x['name'] == 'Foreign_Investor']) // 1000
            t_buy = sum([x['buy'] - x['sell'] for x in chips if x['date'] == latest and x['name'] == 'Investment_Trust']) // 1000
            result['chips_f'] = int(f_buy)
            result['chips_t'] = int(t_buy)

        # 3. EPS
        try:
            start_eps = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d')
            res_eps = requests.get(url, params={"dataset": "TaiwanStockFinancialStatements", "data_id": code, "start_date": start_eps, "token": FINMIND_TOKEN}, timeout=6)
            data_eps = res_eps.json().get('data', [])
            eps_data = [d for d in data_eps if d['type'] == 'EPS']
            
            if eps_data:
                latest_year = eps_data[-1]['date'][:4]
                vals = [d['value'] for d in eps_data if d['date'].startswith(latest_year)]
                result['eps'] = f"{latest_year}累計{round(sum(vals), 2)}元"
        except: pass

        # 4. 殖利率
        try:
            # 🔥 改回 550 天
            start_div = (datetime.now() - timedelta(days=550)).strftime('%Y-%m-%d')
            res_div = requests.get(url, params={"dataset": "TaiwanStockDividend", "data_id": code, "start_date": start_div, "token": FINMIND_TOKEN}, timeout=6)
            data_div = res_div.json().get('data', [])
            
            total_dividend = sum([float(d.get('CashEarningsDistribution', 0)) for d in data_div])
            # 🔥 加入不同欄位名稱的容錯機制
            if total_dividend == 0: 
                total_dividend = sum([float(d.get('CashDividend', 0)) for d in data_div])
                
            current_price = result['last_close_price']
            if total_dividend > 0 and current_price > 0:
                result['yield'] = f"{round((total_dividend / current_price) * 100, 2)}%"
        except: pass

    except Exception as e:
        print(f"⚠️ {code} 數據擷取異常: {e}")
    
    return result

def generate_daily_recommendations():
    print("\n🚀 [Task 2] 篩選並計算每日熱門股...")
    utc_now = datetime.now(timezone.utc); tw_now = utc_now + timedelta(hours=8)
    if tw_now.hour < 14 or (tw_now.hour == 14 and tw_now.minute < 30): target = tw_now - timedelta(days=1)
    else: target = tw_now
    while target.weekday() > 4: target -= timedelta(days=1)
    date_str = target.strftime('%Y%m%d')
    
    print(f"📅 目標日期: {date_str}")
    url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&type=ALLBUT0999&date={date_str}"
    
    final_list = []
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        if data.get('stat') != 'OK':
            res = requests.get("https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&type=ALLBUT0999", timeout=10)
            data = res.json()
        
        candidates = []
        if 'stat' in data and data['stat'] == 'OK':
            table = next((t for t in data.get('tables', []) if '收盤價' in t.get('fields', [])), None)
            if not table and 'data9' in data: table = {'data': data['data9'], 'fields': data.get('fields9', [])}
            
            if table:
                idx_c = table['fields'].index("證券代號")
                idx_n = table['fields'].index("證券名稱")
                idx_v = table['fields'].index("成交股數")
                idx_p = table['fields'].index("收盤價")
                idx_s = table['fields'].index("漲跌(+/-)")
                
                for row in table['data']:
                    try:
                        code = row[idx_c]; vol = float(row[idx_v].replace(',', ''))
                        price_str = row[idx_p].replace(',', '')
                        if len(code) > 4 or code.startswith('91') or price_str == '--' or vol < 2000000: continue
                        price = float(price_str)
                        if price < 10: continue
                        is_up = ('+' in row[idx_s]) or ('red' in row[idx_s])
                        if is_up: candidates.append({"code": code, "name": row[idx_n], "vol": vol})
                    except: continue

                candidates.sort(key=lambda x: x['vol'], reverse=True)
                candidates = candidates[:30]
                print(f"✅ 篩選 {len(candidates)} 檔，開始並行計算詳細指標...")

                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                    futures = [executor.submit(fetch_stock_details, c['code'], c) for c in candidates]
                    for future in concurrent.futures.as_completed(futures):
                        try: final_list.append(future.result())
                        except: pass
                
                final_list.sort(key=lambda x: x['vol'], reverse=True)
                
                if final_list:
                    with open('daily_recommendations.json', 'w', encoding='utf-8') as f:
                        json.dump(final_list, f, ensure_ascii=False, indent=2)
                    print("💾 已儲存 daily_recommendations.json")
    except Exception as e:
        print(f"❌ 錯誤: {e}")

if __name__ == "__main__":
    update_stock_list_json()
    generate_daily_recommendations()
