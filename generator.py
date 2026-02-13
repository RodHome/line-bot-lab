import requests
import pandas as pd
import json
import re
import os
import time
from datetime import datetime, timedelta, timezone

# --- 功能 1: 抓取所有股票代號 (建立通訊錄) ---
def update_stock_list_json():
    print("🚀 [Task 1] 開始抓取所有股票代號對照表...")
    
    urls = [
        "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2", # 上市
        "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"  # 上櫃
    ]
    
    stock_map = {}

    for url in urls:
        try:
            res = requests.get(url, timeout=10)
            # 使用 pandas 讀取 HTML 表格
            dfs = pd.read_html(res.text)
            df = dfs[0]
            
            # 整理欄位 (第一列通常是標題)
            df.columns = df.iloc[0]
            df = df.iloc[1:]
            
            # 找到代號欄位
            col_matches = [c for c in df.columns if "有價證券代號" in str(c)]
            if not col_matches: continue
            col_name = col_matches[0]
            
            for item in df[col_name]:
                item = str(item).strip()
                # 抓出 "2330 台積電" 這種格式
                match = re.match(r'^(\d{4})\s+(.+)', item)
                if match:
                    code = match.group(1)
                    name = match.group(2).strip()
                    stock_map[name] = code
        except Exception as e:
            print(f"⚠️ [Task 1] 抓取錯誤 ({url}): {e}")

    # 補上熱門 ETF
    etfs = ["0050", "0056", "00878", "00929", "00919", "00940", "006208", "00713", "00939", "00679B"]
    for code in etfs:
        stock_map[code] = code  # ETF 有時候代號與名稱相同或需特殊處理，這邊簡化

    print(f"✅ [Task 1] 完成，共抓取 {len(stock_map)} 檔股票 -> 存入 stock_list.json")

    # 存檔 1
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
                    idx_price = fields.index("收盤價")
                    idx_sign = fields.index("漲跌(+/-)")
                except:
                    idx_code, idx_vol, idx_price, idx_sign = 0, 2, 8, 9 # 預設值

                candidates = []
                for row in raw_data:
                    try:
                        code = row[idx_code]
                        # 過濾權證、ETF(00開頭)、DR股(91開頭) -> 若你想保留 ETF，可移除 00 判斷
                        if len(code) > 4 or code.startswith('91') or code.startswith('00'): continue 
                        vol = float(row[idx_vol].replace(',', ''))
                        price_str = row[idx_price].replace(',', '')
                        
                        if price_str == '--' or vol == 0: continue
                        price = float(price_str)
                        
                        # 🔥 選股邏輯：價格 > 10元，且 量大 (這裡設 2000 張 = 2,000,000 股)
                        if price < 10: continue
                        
                        sign = row[idx_sign]
                        is_up = ('+' in sign) or ('red' in sign) # 簡單判斷漲勢
                        
                        if is_up and vol > 2000000: 
                            candidates.append({"code": code, "vol": vol})
                    except: continue
                
                # 依成交量排序，取前 50 檔
                candidates.sort(key=lambda x: x['vol'], reverse=True)
                final_list = [x['code'] for x in candidates[:50]]
                
                print(f"✅ [Task 2] 篩選完成，共 {len(final_list)} 檔熱門股")
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
            json.dump(final_list, f, ensure_ascii=False)
            print("💾 已儲存 daily_recommendations.json")
    else:
        print("⚠️ 本次未產出新名單，未覆蓋檔案。")

if __name__ == "__main__":
    # 執行兩個任務
    update_stock_list_json()
    generate_daily_recommendations()
