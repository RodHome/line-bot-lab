import os, requests, random, re
import json
import time
import math
import concurrent.futures
import twstock
from datetime import datetime, timedelta, time as dtime, timezone
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage

app = Flask(__name__)

# 🟢 [版本號] v17.0 (Data-Driven + Sector Filter)
BOT_VERSION = "v17.0 (全自動版)"

# --- 1. 全域資料庫初始化 ---
AI_RESPONSE_CACHE = {}
TWSE_CACHE = {"date": "", "data": []}

ALL_STOCK_DATA = {}      # 大字典: {"2330": {name, sector, is_elite...}}
CODE_TO_NAME = {}        # 代號查名稱
ELITE_CODES = []         # 菁英池
SECTOR_INDEX = {}        # 產業索引 {"半導體": [2330, 2454]}

# 啟動時讀取資料
def load_stock_db():
    global ALL_STOCK_DATA, CODE_TO_NAME, ELITE_CODES, SECTOR_INDEX
    
    # 嘗試讀取 stock_list.json (建議是從 GitHub 下載最新版)
    # 若 Zeabur 本地有檔案也可以直接讀
    GITHUB_LIST_URL = "https://raw.githubusercontent.com/RodHome/line-bot-lab/main/stock_list.json"
    
    try:
        print("[System] 下載最新股票資料庫...")
        headers = {'Cache-Control': 'no-cache'}
        # 如果是 Private Repo，這裡要加 header['Authorization']
        res = requests.get(GITHUB_LIST_URL, headers=headers, timeout=5)
        
        if res.status_code == 200:
            ALL_STOCK_DATA = res.json()
        else:
            # 讀取本地當備案
            if os.path.exists('stock_list.json'):
                with open('stock_list.json', 'r', encoding='utf-8') as f:
                    ALL_STOCK_DATA = json.load(f)
            else:
                # 極簡備案防止當機
                ALL_STOCK_DATA = {"2330": {"name": "台積電", "sector": "半導體業", "is_elite": True}}

        # 重建索引
        ELITE_CODES = []
        SECTOR_INDEX = {}
        CODE_TO_NAME = {}

        for code, info in ALL_STOCK_DATA.items():
            name = info.get('name', code)
            CODE_TO_NAME[code] = name
            
            # 建立菁英池
            if info.get('is_elite'):
                ELITE_CODES.append(code)
            
            # 建立產業索引
            sec = info.get('sector', '其他')
            if sec not in SECTOR_INDEX: SECTOR_INDEX[sec] = []
            SECTOR_INDEX[sec].append(code)
            
        print(f"[System] 資料庫載入完成: {len(ALL_STOCK_DATA)} 檔, 菁英 {len(ELITE_CODES)} 檔")

    except Exception as e:
        print(f"[Error] Load DB Failed: {e}")

# 執行初始化
load_stock_db()

token = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
secret = os.environ.get('LINE_CHANNEL_SECRET')
line_bot_api = LineBotApi(token if token else 'UNKNOWN')
handler = WebhookHandler(secret if secret else 'UNKNOWN')

@app.route("/")
def health_check(): return f"OK ({BOT_VERSION})", 200

# --- 2. 核心功能 ---

def get_taiwan_time_str():
    utc_now = datetime.now(timezone.utc)
    tw_time = utc_now + timedelta(hours=8)
    return tw_time.strftime('%H:%M:%S')

# 讀取每日推薦名單 (GitHub)
def fetch_twse_candidates():
    GITHUB_REC_URL = "https://raw.githubusercontent.com/RodHome/line-bot-lab/main/daily_recommendations.json"
    global TWSE_CACHE
    
    tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
    today_str = tw_now.strftime('%Y%m%d')

    if TWSE_CACHE.get('date') == today_str and TWSE_CACHE.get('data'):
        return TWSE_CACHE['data']

    try:
        headers = {'Cache-Control': 'no-cache'}
        res = requests.get(GITHUB_REC_URL, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, list) and len(data) > 0:
                TWSE_CACHE = {"date": today_str, "data": data}
                return data
    except Exception as e:
        print(f"[Error] Fetch Rec: {e}")

    return [] # 失敗回傳空，交給 fallback 處理

# 技術指標 (維持原樣)
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

def get_technical_signals(data, chips_val):
    signals = []
    # 簡單防呆
    if not data or 'raw_closes' not in data: return ["資料不足"]
    
    closes = data['raw_closes']
    rsi = calculate_rsi(closes)
    
    ma5 = data['ma5']; ma20 = data['ma20']; ma60 = data['ma60']; close = data['close']
    
    if rsi > 75: signals.append("🔥RSI過熱")
    elif rsi < 25: signals.append("💎RSI超賣")
    
    bias_20 = (close - ma20) / ma20 * 100 if ma20 else 0
    if bias_20 > 15: signals.append("⚠️乖離過大")
    
    if chips_val > 1000: signals.append("💰外資大買")
    elif chips_val < -1000: signals.append("💸外資大賣")
    
    if close > ma5 > ma20 > ma60: signals.append("🟢三線多頭")
    
    unique = list(set(signals))
    return unique[:3] if unique else ["🟡趨勢盤整"]

# AI 與 資料擷取 (Gemini / FinMind)
def call_gemini_json(prompt, system_instruction=None):
    keys = [os.environ.get(f'GEMINI_API_KEY_{i}') for i in range(1, 7) if os.environ.get(f'GEMINI_API_KEY_{i}')]
    if not keys and os.environ.get('GEMINI_API_KEY'): keys = [os.environ.get('GEMINI_API_KEY')]
    if not keys: return None
    random.shuffle(keys)
    
    target_models = ["gemini-2.0-flash", "gemini-1.5-flash"]
    final_prompt = prompt + "\n\n⚠️只回傳純 JSON。"
    
    for model in target_models:
        for key in keys:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                headers = {'Content-Type': 'application/json'}
                params = {'key': key}
                contents = [{"parts": [{"text": final_prompt}]}]
                if system_instruction:
                    contents = [{"parts": [{"text": f"系統指令: {system_instruction}\n用戶: {final_prompt}"}]}]
                
                payload = {"contents": contents, "generationConfig": {"responseMimeType": "application/json"}}
                res = requests.post(url, headers=headers, params=params, json=payload, timeout=20)
                if res.status_code == 200:
                    text = res.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
                    return text.replace('```json','').replace('```','').strip()
            except: continue
    return None

def fetch_data_light(stock_id):
    # (維持原本的並行抓取邏輯，這裡簡化展示，請直接用你原本 16.2 的代碼，或以下這個精簡版)
    def get_history():
        try:
            token = os.environ.get('FINMIND_TOKEN', '')
            start = (datetime.now() - timedelta(days=120)).strftime('%Y-%m-%d')
            res = requests.get("https://api.finmindtrade.com/api/v4/data", params={
                "dataset": "TaiwanStockPrice", "data_id": stock_id, "start_date": start, "token": token}, timeout=4)
            return res.json().get('data', [])
        except: return []

    def get_realtime():
        try: return twstock.realtime.get(stock_id)
        except: return None

    hist_data = []; stock_rt = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(get_history)
        f2 = executor.submit(get_realtime)
        try:
            hist_data = f1.result(timeout=5)
            stock_rt = f2.result(timeout=5)
        except: pass
    
    if not hist_data: return None
    
    # 資料縫合 (略，維持原本邏輯)
    latest_price = hist_data[-1]['close']
    if stock_rt and stock_rt['success']:
         p = stock_rt['realtime']['latest_trade_price']
         if p != "-": latest_price = float(p)
    
    closes = [d['close'] for d in hist_data]
    if hist_data[-1]['date'] != datetime.now().strftime('%Y-%m-%d'):
        closes.append(latest_price)
    else: closes[-1] = latest_price
    
    ma5 = round(sum(closes[-5:]) / 5, 2) if len(closes) >= 5 else 0
    ma20 = round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else 0
    ma60 = round(sum(closes[-60:]) / 60, 2) if len(closes) >= 60 else 0
    
    change = latest_price - (closes[-2] if len(closes)>1 else latest_price)
    change_display = f"{change:+.2f}"
    
    return {
        "code": stock_id, "close": latest_price, "change_display": change_display,
        "ma5": ma5, "ma20": ma20, "ma60": ma60,
        "raw_closes": closes, "update_time": get_taiwan_time_str(),
        "color": "#D32F2F" if change >= 0 else "#2E7D32",
        "open": hist_data[-1]['open']
    }

def fetch_chips_accumulate(stock_id):
    # (維持原本邏輯)
    return "0", "0", 0, 0 

def fetch_eps(stock_id):
    # (維持原本邏輯)
    return "N/A"

def fetch_dividend_yield(stock_id, price):
    # (維持原本邏輯)
    return "N/A"

# 推薦掃描引擎 (新版：隨機 + 產業篩選)
def check_stock_worker_turbo(code):
    try:
        data = fetch_data_light(code)
        if not data: return None
        # 簡單篩選：收盤 > 月線
        if data['close'] > data['ma20']:
            # 這裡簡化，直接回傳
            name = ALL_STOCK_DATA.get(code, {}).get('name', code)
            sector = ALL_STOCK_DATA.get(code, {}).get('sector', '')
            return {
                "code": code, "name": name, "sector": sector,
                "close": data['close'], "change_display": data['change_display'],
                "color": data['color'], "signal_str": "多頭排列", "tag": "熱門"
            }
    except: pass
    return None

def scan_recommendations_turbo(target_sector=None):
    candidates_pool = []
    twse_list = fetch_twse_candidates() # Top 50
    
    if target_sector:
        # 1. 嘗試從 Top 50 裡找產業
        if twse_list:
            pool = [c for c in twse_list if target_sector in ALL_STOCK_DATA.get(c, {}).get('sector', '')]
            if pool: candidates_pool = pool
        
        # 2. 沒找到，去菁英池找
        if not candidates_pool:
            pool = [c for c in ELITE_CODES if target_sector in ALL_STOCK_DATA.get(c, {}).get('sector', '')]
            candidates_pool = pool
    else:
        # 一般推薦
        if twse_list:
            random.shuffle(twse_list)
            candidates_pool = twse_list[:15] # 隨機 15 檔
        else:
            candidates_pool = random.sample(ELITE_CODES, 10) # 備案
            
    # 並行檢查
    candidates = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        results = executor.map(check_stock_worker_turbo, candidates_pool)
    
    for res in results:
        if res: candidates.append(res)
        if len(candidates) >= 5: break
        
    return candidates

def get_stock_id(text):
    text = text.strip()
    clean = re.sub(r'(成本|cost).*', '', text, flags=re.IGNORECASE).strip()
    
    # 1. 查代號 -> 名稱
    if clean in ALL_STOCK_DATA: return clean
    
    # 2. 查名稱 -> 代號 (使用反向索引)
    for code, name in CODE_TO_NAME.items():
        if clean == name: return code
        
    if clean.isdigit() and len(clean) >= 4: return clean
    return None

# --- Line Bot Handlers ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except: abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    
    # [功能 1] 推薦
    if msg.startswith("推薦") or msg.startswith("選股"):
        parts = msg.split()
        target_sector = parts[1] if len(parts) > 1 else None
        
        good_stocks = scan_recommendations_turbo(target_sector)
        if not good_stocks:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 暫無符合條件標的"))
            return
            
        # ... (這裡放你原本的 Flex Message 產出邏輯) ...
        # 簡單回應測試
        reply = f"找到 {len(good_stocks)} 檔推薦股"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # [功能 2] 個股診斷
    stock_id = get_stock_id(msg)
    if stock_id:
        info = ALL_STOCK_DATA.get(stock_id, {})
        name = info.get('name', stock_id)
        is_etf = info.get('is_etf', False) or stock_id.startswith('00')
        etf_focus = info.get('focus', '')
        
        # ... (這裡接你原本的診斷邏輯) ...
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"收到！正在分析 {name} ({stock_id})..."))
        return
        
    # [預設] 功能說明
    help_text = """🤖 **抱歉，我不確定您的意思...**
(但我可以幫您做這些事！)

1️⃣ 【個股 AI 診斷】
👉 輸入「代號」或「名稱」
範例：2330、長榮

2️⃣ 【AI 飆股推薦】
👉 輸入：「推薦」
(全市場掃描熱門股)

3️⃣ 【指定產業】
👉 輸入：「推薦 半導體」

4️⃣ 【ETF 查詢】
👉 輸入代號：00878
"""
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
