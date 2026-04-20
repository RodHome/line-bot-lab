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
#==========3/17==================================
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

# 🔥 [為左側/存股雷達新增] 查詢單季 EPS 與 殖利率 (動態頻率推算版)
# ==========================================
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
#==========3/17==================================
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
    
    # 🔥 [新增] 讀取剛剛產生的 stock_list.json，用來查詢名稱與產業別
    stock_meta = {}
    try:
        if os.path.exists('stock_list.json'):
            with open('stock_list.json', 'r', encoding='utf-8') as f:
                stock_meta = json.load(f)
    except Exception as e:
        print(f"⚠️ 讀取 stock_list.json 失敗: {e}")

    # 設定目標日期 (GitHub Actions 通常在 UTC 時間跑，台灣+8)
    # 策略：抓取「最新收盤日」。如果今天是週六日，API 會自動給最近的週五資料，或我們指定日期。
    # 這裡使用簡單策略：抓取當下台灣時間，如果是下午2點後抓今天，否則抓昨天
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
                        
                        # 🔥 動能濾網升級：收紅，且單日成交金額大於 3 億元 (300,000,000)
                        if is_up and turnover > 300000000: 
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
                            
                            # 條件：收紅 且 成交金額 > 3億
                            if is_up and turnover > 300000000: 
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
                    # 👇👇👇 從這裡開始替換 👇👇👇
                    if yoy > 10 and buy_value > 300000000:
                        meta_info = stock_meta.get(code, {})
                        stock_name = meta_info.get('name', '未知名稱')
                        stock_sector = meta_info.get('sector', '未知產業')
                        
                        # 取得剛剛貼上的上市/上櫃標籤，並格式化日期 (YYYY-MM-DD)
                        stock_exchange = item.get('exchange', '未知')
                        date_str = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"

                        final_list.append({
                            "date": date_str,          # ✅ 新增：資料日期
                            "code": code,
                            "name": stock_name,
                            "exchange": stock_exchange,# ✅ 新增：上市或上櫃
                            "sector": stock_sector,
                            "price": price,
                            "turnover": turnover,
                            "chips_display": f"{chips_sum}張 ({buy_value_y}億)",
                            "buy_value": buy_value,
                            "yoy": yoy,
                            "tag": "外資大買" if acc_f > acc_t else "投信作帳",
                            "debug_info": yoy_data['debug_info']
                        })
                    # 👆👆👆 替換到這裡結束 👆👆👆
                
                # 🔥 4. 將過關的菁英，依照「買超金額」由大到小排序
                final_list.sort(key=lambda x: x['buy_value'], reverse=True)
                
                # 為了避免 JSON 太大，我們只保留最強的前 15 檔給 app.py 抽樣
                final_list = final_list[:15]
                print(f"🎉 掃描結束！共 {len(final_list)} 檔符合【高潛力成長飆股】終極標準。")
            else:
                print("⚠️ [Task 2] 找不到對應的資料表")
        else:
            print("⚠️ [Task 2] API 回傳狀態非 OK")

    except Exception as e:
        print(f"❌ [Task 2] 發生錯誤: {e}")

    # 📦 [修改] 呼叫融合大腦，結合歷史 30 天記憶後存檔
    merged_list = merge_history_data(final_list, 'daily_recommendations.json', 'buy_value')
    if merged_list:
        with open('daily_recommendations.json', 'w', encoding='utf-8') as f:
            json.dump(merged_list, f, ensure_ascii=False, indent=4)
        print(f"💾 已儲存 daily_recommendations.json (包含歷史共 {len(merged_list)} 檔)")
    else:
        print("⚠️ 歷史與今日皆無資料可存。")

if __name__ == "__main__":
    # 執行兩個任務
    update_stock_list_json()
    generate_daily_recommendations()

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
    # 🌊 第一層：大數據降維 (流動性 5,000萬 ~ 3億)
    # 為了 100% 不干擾右側，我們在左側雷達內自己發動一次輕量級爬蟲
    # ---------------------------------------------------------
    print("🌊 [第一層] 大數據降維：尋找流動性 1000萬~3億 的潛伏股...")
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
                    code = row[idx_code]
                    if code not in stock_meta: continue
                    try:
                        turnover = float(row[idx_turnover].replace(',', ''))
                        price = float(row[idx_price].replace(',', ''))
                        # 🔥 條件：成交金額 1000萬 ~ 3億，且股價 > 10元
                        if 10000000 <= turnover <= 300000000 and price >= 10:
                            layer1_candidates.append({"code": code, "price": price, "market": "TW"})
                    except: pass
    except Exception as e:
        print(f"⚠️ TWSE 第一層抓取錯誤: {e}")

    # 2. 抓取上櫃 (TPEx) 最新交易日
    try:
        base_date = datetime.now(timezone.utc) + timedelta(hours=8)
        headers = {'User-Agent': 'Mozilla/5.0'}
        for i in range(6): # 往回找最近的交易日
            check_date = base_date - timedelta(days=i)
            roc_date = f"{check_date.year - 1911}/{check_date.strftime('%m/%d')}"
            url_otc = f"https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={roc_date}&se=EW"
            res_otc = requests.get(url_otc, headers=headers, timeout=10)
            temp_data = res_otc.json()
            if 'tables' in temp_data and temp_data['tables'] and len(temp_data['tables'][0].get('data', [])) > 0:
                table = temp_data['tables'][0]
                fields = [str(f).strip() for f in table.get('fields', [])]
                idx_code = fields.index("代號") if "代號" in fields else 0
                idx_turnover = fields.index("成交金額(元)") if "成交金額(元)" in fields else 8
                idx_price = fields.index("收盤") if "收盤" in fields else 2
                
                for row in table['data']:
                    code = str(row[idx_code]).strip()
                    if code not in stock_meta: continue
                    try:
                        price_str = str(row[idx_price]).replace(',', '').strip()
                        turnover_str = str(row[idx_turnover]).replace(',', '').strip()
                        if price_str in ['----', '--', '除息', '除權'] or turnover_str in ['--', '']: continue
                        turnover = float(turnover_str)
                        price = float(price_str)
                        # 🔥 條件：成交金額 1000萬 ~ 3億，且股價 > 10元
                        if 10000000 <= turnover <= 300000000 and price >= 10:
                            layer1_candidates.append({"code": code, "price": price, "market": "TWO"})
                    except: pass
                break
            time.sleep(0.3)
    except Exception as e:
        print(f"⚠️ TPEx 第一層抓取錯誤: {e}")

    print(f"✅ 第一層降維完畢，全市場 2000 檔中，共 {len(layer1_candidates)} 檔符合流動性門檻，進入第二層。")

    # ---------------------------------------------------------
    # 📉 第二層：位階與動能過濾 (新增量縮比例與週線運算)
    # ---------------------------------------------------------
    print("📉 [第二層] 啟動 yfinance 計算：尋找負乖離、量縮窒息、低波築底...")
    layer2_candidates = []
    
    for item in layer1_candidates:
        code = item['code']
        try:
            ticker = yf.Ticker(f"{code}.{item['market']}")
            df = ticker.history(period="6mo") 
            if df.empty or len(df) < 60: continue

            closes = df['Close'].tolist()
            lows = df['Low'].tolist()
            highs = df['High'].tolist()
            volumes = df['Volume'].tolist()

            item['real_date'] = df.index[-1].strftime('%Y-%m-%d')
            
            close_today = closes[-1]
            # 🔥 新增這行：強制用 yfinance 的精準收盤價覆寫掉第一層的粗糙價格！
            item['price'] = round(close_today, 2)
            ma60 = sum(closes[-60:]) / 60
            ma24 = sum(closes[-24:]) / 24 # 🔥 新增月線
            ma6 = sum(closes[-6:]) / 6    # 🔥 新增週線
            
            bias60 = (close_today - ma60) / ma60
            bias24 = (close_today - ma24) / ma24
            bias6 = (close_today - ma6) / ma6

            if bias60 >= -0.03: continue
            
            vol_today = volumes[-1]
            ma20_vol = sum(volumes[-20:]) / 20
            vol_ratio = vol_today / ma20_vol # 🔥 記錄量縮比例，用來算分數
            
            if vol_ratio >= 0.8: continue
            
            recent_10_high = max(highs[-10:])
            recent_10_low = min(lows[-10:])
            amplitude = (recent_10_high - recent_10_low) / recent_10_low
            
            if amplitude >= 0.12: continue
            if (close_today - closes[-5]) / closes[-5] >= 0.05: continue

            # 通過第二層考驗，把數據打包給第三層算分
            item['bias60'] = bias60
            item['bias24'] = bias24 # 傳遞給第三層
            item['bias6'] = bias6   # 傳遞給第三層
            item['vol_ratio'] = vol_ratio
            item['amplitude'] = amplitude
            item['ma60'] = ma60
            layer2_candidates.append(item)
            
        except Exception: pass
        time.sleep(0.1)

    print(f"✅ 第二層過濾完畢，剩餘 {len(layer2_candidates)} 檔進入終極基本面與評分查核。")

    # ---------------------------------------------------------
    # 🏦 第三層：聰明錢定錨與 🌟信心評分系統 (Scoring Model)
    # ---------------------------------------------------------
    print("🏦 [第三層] 啟動 FinMind 查核與評分：法人連買、EPS、黃金交叉探測...")
    final_list = []
    
    for item in layer2_candidates:
        code = item['code']
        # 🔥 加上 , _ 接住第三個變數
        eps, yield_rate, _ = get_finmind_fundamentals(code, item['price'], fetch_yield=False)

        if eps <= 0: continue # 🔴 淘汰虧損股
        
        yoy_data = get_finmind_revenue_yoy(code)
        yoy = yoy_data['yoy']
        
        chips_history = get_finmind_chips_history(code, days=5)
        buy_days = sum(1 for x in chips_history if x > 0)
        
        # 門檻：法人至少買 3 天 (或轉機股特例)
        if buy_days >= 4 or (buy_days == 3 and yoy > -15.0):
            
            # 🎯 啟動計分模型 (Base: 50)
            score = 50 
            
            # 1. 籌碼權重 (Max 30)
            if buy_days == 5: score += 30
            elif buy_days == 4: score += 20
            elif buy_days == 3: score += 10
            
            # 2. 量縮權重 (Max 10)
            if item['vol_ratio'] < 0.5: score += 10
            elif item['vol_ratio'] < 0.6: score += 8
            elif item['vol_ratio'] < 0.7: score += 5
            
            # 3. 乖離權重 (Max 10)
            bias_pct = item['bias60'] * 100
            if -8.0 <= bias_pct <= -5.0: score += 10
            elif bias_pct < -8.0: score += 8
            elif -5.0 < bias_pct <= -3.0: score += 5

            # 🎯 判斷趨勢狀態 (Trend Status)
            if item['bias6'] > 0:
                trend_status = "⭐ 底部起漲 (乖離6已翻正)"
            else:
                trend_status = "⏳ 築底量縮中 (乖離6仍為負)"
                
            # 計算建議進場價 (取今日收盤與季線的折衷，或是保守取今日收盤往下抓 1%)
            entry_price = round(item['price'] * 0.99, 2)

            # 🏆 封裝入庫
            final_list.append({
                "date": item['real_date'],
                "code": code,
                "name": stock_meta[code]['name'],
                "price": item['price'],
                "score": score,
                "trend_status": trend_status,
                "entry_price": entry_price,
                "bias60": f"{bias_pct:.1f}%",
                "bias24": f"{item['bias24']*100:.1f}%", # 🔥 新增
                "bias6": f"{item['bias6']*100:.1f}%",   # 🔥 新增
                "vol_ratio": f"{item['vol_ratio']*100:.1f}%",
                "eps": eps,
                "yield_rate": yield_rate,
                "buy_days": buy_days,
                "tag": "左側黃金坑"
            })
            print(f"   🏆 入選: {code} | 分數: {score} | 狀態: {trend_status}")

    # ---------------------------------------------------------
    # 📦 結算與存檔 (融入 30 天歷史大水庫)
    # ---------------------------------------------------------
    if final_list:
        print(f"✅ 今日掃描共 {len(final_list)} 檔無敵黃金坑達標。")
    else:
        print("⚠️ 今日掃描無股票通過三層漏斗。")

    merged_list = merge_history_data(final_list, 'left_side_value.json', 'score')
    
    with open('left_side_value.json', 'w', encoding='utf-8') as f:
        json.dump(merged_list, f, ensure_ascii=False, indent=4)
        print(f"💾 已強制更新 left_side_value.json (包含歷史共 {len(merged_list)} 檔)")
  
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
        
        with open('deposit_stocks.json', 'w', encoding='utf-8') as f:
            json.dump(deposit_list, f, ensure_ascii=False, indent=4)
        print(f"💾 任務完成！已儲存 deposit_stocks.json (共分析 {len(deposit_list)} 檔存股)")

# ========================================================
# 最後，記得在你的 __main__ 區塊把這支程式加上去執行！
if __name__ == "__main__":
    update_stock_list_json()
    generate_daily_recommendations()  # 右側產線
    generate_left_side_value()        # 左側產線
    generate_deposit_stocks()         # 🏦 存股產線 (新增這行！)
