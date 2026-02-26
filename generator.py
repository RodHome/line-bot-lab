import requests
import pandas as pd
import json
import re
import os
import time
from datetime import datetime, timedelta, timezone
from io import StringIO

# ================= 新增：FinMind 查詢區域 =================
FINMIND_TOKEN = os.environ.get('FINMIND_TOKEN', '')

def get_finmind_chips(code):
    """查詢近 5 日法人買超張數 (抗長假 30 天版)"""
    start = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    url = "https://api.finmindtrade.com/api/v4/data"
    try:
        res = requests.get(url, params={"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": code, "start_date": start, "token": FINMIND_TOKEN}, timeout=10)
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
        res = requests.get(url, params={"dataset": "TaiwanStockMonthRevenue", "data_id": code, "start_date": start, "token": FINMIND_TOKEN}, timeout=10)
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
                            candidates.append({"code": code, "turnover": turnover, "price": price})
                    except: continue
                
               # 🔥 1. 依「成交金額 (turnover)」排序，取前 50 檔母體
                candidates.sort(key=lambda x: x['turnover'], reverse=True)
                top_50 = candidates[:50]
                
                print(f"✅ [Task 2] 第一階段篩選完成，取得 50 檔強勢資金股。啟動 FinMind 深度掃描...")
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
                        final_list.append({
                            "code": code,
                            "price": price,
                            "turnover": turnover,
                            "chips_display": f"{chips_sum}張 ({buy_value_y}億)",
                            "buy_value": buy_value,
                            "yoy": yoy,
                            "tag": "外資大買" if acc_f > acc_t else "投信作帳",
                            # ⚠️ 新增：將開發者查帳資訊存入 JSON
                            "debug_info": yoy_data['debug_info']
                        })
                
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

    # 存檔 2
    # 就算沒抓到(空陣列)，也要存檔，避免 LineBot 讀舊檔
    # 或是你可以選擇：若空的就不存，沿用昨天的 (看你需求，這裡預設是覆蓋)
    if final_list:
        with open('daily_recommendations.json', 'w', encoding='utf-8') as f:
            # 🔥 加上 indent=4，讓 JSON 產生 4 個空白鍵的漂亮縮排
            json.dump(final_list, f, ensure_ascii=False, indent=4)
            print("💾 已儲存 daily_recommendations.json")
    else:
        print("⚠️ 本次未產出新名單，未覆蓋檔案。")

if __name__ == "__main__":
    # 執行兩個任務
    update_stock_list_json()
    generate_daily_recommendations()
