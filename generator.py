import requests
import pandas as pd
import json
import re
import os

def fetch_tw_stocks():
    print("🚀 Github Action: 開始抓取最新股票清單...")
    
    # 1. 抓取上市與上櫃
    urls = [
        "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2", # 上市
        "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"  # 上櫃
    ]
    
    stock_map = {}

    for url in urls:
        try:
            res = requests.get(url)
            # 使用 pandas 讀取 HTML 表格
            dfs = pd.read_html(res.text)
            df = dfs[0]
            
            # 整理欄位 (第一列通常是標題)
            df.columns = df.iloc[0]
            df = df.iloc[1:]
            
            # 找到代號欄位
            col_name = [c for c in df.columns if "有價證券代號" in str(c)][0]
            
            for item in df[col_name]:
                item = str(item).strip()
                # 抓出 "2330 台積電" 這種格式
                match = re.match(r'^(\d{4})\s+(.+)', item)
                if match:
                    code = match.group(1)
                    name = match.group(2).strip()
                    stock_map[name] = code
        except Exception as e:
            print(f"⚠️ 抓取錯誤: {e}")

    # 2. 補上熱門 ETF (手動清單，確保這些一定要有)
    etfs = ["0050", "0056", "00878", "00929", "00919", "00940", "006208", "00713", "00939", "00679B"]
    for code in etfs:
        stock_map[code] = code

    print(f"✅ 成功抓取 {len(stock_map)} 檔股票")

    # 3. 存檔
    with open('stock_list.json', 'w', encoding='utf-8') as f:
        json.dump(stock_map, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    fetch_tw_stocks()
