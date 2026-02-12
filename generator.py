import requests
import pandas as pd
import json
import re
import os

# --- 1. 人工維護區 (原本寫在 app.py 的資料搬來這裡) ---
# 這些是爬蟲爬不到的「特色描述」，我們在這裡維護，讓 app.py 保持乾淨
CUSTOM_META = {
    # --- 熱門 ETF ---
    "00878": {"type": "高股息", "focus": "ESG/殖利率/填息", "is_etf": True},
    "0056":  {"type": "高股息", "focus": "預測殖利率/填息", "is_etf": True},
    "00919": {"type": "高股息", "focus": "殖利率/航運半導體週期", "is_etf": True},
    "00929": {"type": "高股息", "focus": "月配息/科技股景氣", "is_etf": True},
    "00713": {"type": "高股息", "focus": "低波動/防禦性", "is_etf": True},
    "00940": {"type": "高股息", "focus": "月配息/價值投資", "is_etf": True},
    "00939": {"type": "高股息", "focus": "動能指標/月底領息", "is_etf": True},
    "0050":  {"type": "市值型", "focus": "大盤乖離/台積電展望", "is_etf": True},
    "006208":{"type": "市值型", "focus": "大盤乖離/台積電展望", "is_etf": True},
    "00881": {"type": "科技型", "focus": "半導體/通訊供應鏈", "is_etf": True},
    "00679B":{"type": "債券型", "focus": "美債殖利率/降息預期", "is_etf": True},
    
    # --- 產業龍頭 (菁英池) ---
    # 我們在這裡標記 is_elite: True，這樣 app.py 就可以識別誰是備用名單
    "2330": {"is_elite": True, "sector": "半導體業"}, # 強制覆蓋產業名稱
    "2317": {"is_elite": True, "sector": "電腦及週邊設備業"}, 
    "2454": {"is_elite": True, "sector": "半導體業"},
    "2382": {"is_elite": True, "sector": "電腦及週邊設備業"},
    "3231": {"is_elite": True, "sector": "電腦及週邊設備業"},
    "2376": {"is_elite": True, "sector": "電腦及週邊設備業"},
    "2603": {"is_elite": True, "sector": "航運業"},
    "2609": {"is_elite": True, "sector": "航運業"},
    "1519": {"is_elite": True, "sector": "電機機械"},
    "1503": {"is_elite": True, "sector": "電機機械"},
    "3017": {"is_elite": True, "sector": "電子零組件業"},
    "3324": {"is_elite": True, "sector": "電子零組件業"}
}

def update_stock_list_json():
    print("🚀 [Generator] 開始建立全方位股票資料庫...")
    
    urls = [
        "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2", # 上市
        "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"  # 上櫃
    ]
    
    stock_db = {} # 最終資料庫

    # 1. 爬蟲抓取代號、名稱、標準產業
    for url in urls:
        try:
            res = requests.get(url, timeout=10)
            dfs = pd.read_html(res.text)
            df = dfs[0]
            df.columns = df.iloc[0]
            df = df.iloc[1:]
            
            col_code = [c for c in df.columns if "有價證券代號" in str(c)][0]
            col_sector = [c for c in df.columns if "產業別" in str(c)][0]
            
            for index, row in df.iterrows():
                item = str(row[col_code]).strip()
                raw_sector = str(row[col_sector]).strip()
                
                match = re.match(r'^(\d{4})\s+(.+)', item)
                if match:
                    code = match.group(1)
                    name = match.group(2).strip()
                    
                    if raw_sector == 'nan' or not raw_sector: raw_sector = "其他"
                    
                    # 建立基本資料
                    stock_db[code] = {
                        "name": name,
                        "sector": raw_sector,
                        "is_etf": False,     # 預設非 ETF
                        "is_elite": False    # 預設非菁英
                    }
        except Exception as e:
            print(f"⚠️ 爬蟲部分失敗 ({url}): {e}")

    # 2. 融合人工維護資料 (CUSTOM_META)
    # 這一步最重要！把我們手動寫的 focus 和 elite 標籤打上去
    for code, meta in CUSTOM_META.items():
        if code in stock_db:
            # 如果爬蟲有抓到這檔，就更新它的資料
            stock_db[code].update(meta)
        else:
            # 如果爬蟲沒抓到 (例如剛上市)，就強制補進去
            # 這裡需要補上 name，因為 CUSTOM_META 裡我沒寫 name，假設爬蟲通常抓得到
            # 如果是純手動新增的 ETF，建議在 CUSTOM_META 裡也補上 "name"
            if "name" not in meta:
                 # 簡單防呆，如果是 ETF 列表裡的
                 pass 
            else:
                 stock_db[code] = meta

    print(f"✅ 資料庫建立完成，共 {len(stock_db)} 檔 (含產業與ETF屬性)")

    # 存檔
    with open('stock_list.json', 'w', encoding='utf-8') as f:
        json.dump(stock_db, f, ensure_ascii=False, indent=2)

# --- 每日推薦名單 (維持原本邏輯，只抓代號) ---
def generate_daily_recommendations():
    # ... (這部分邏輯不用變，維持您原本的爬蟲即可) ...
    # 為了節省篇幅，這裡省略，請保留原本的 generate_daily_recommendations 函式
    pass

if __name__ == "__main__":
    update_stock_list_json()
    # generate_daily_recommendations() # 記得打開這行
