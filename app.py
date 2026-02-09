import os, requests, json, time, re, threading
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 🟢 [版本號] v2.0 (Alpha: Local Map + Cache)
BOT_VERSION = "v2.0 (Alpha)"

# --- 1. 初始化設定 & 讀取本地代碼表 ---
STOCK_MAP = {}
try:
    with open('stock_list.json', 'r', encoding='utf-8') as f:
        STOCK_MAP = json.load(f)
    # 建立反向對照表 (代碼 -> 名字)
    CODE_TO_NAME = {v: k for k, v in STOCK_MAP.items()}
    print(f"✅ 成功載入 {len(STOCK_MAP)} 檔股票代碼")
except Exception as e:
    print(f"⚠️ 無法讀取 stock_list.json: {e}")
    # 萬一讀不到檔案，至少留幾個基本的
    STOCK_MAP = {"台積電": "2330", "鴻海": "2317"} 
    CODE_TO_NAME = {"2330": "台積電", "2317": "鴻海"}

# --- 2. 實作記憶體快取 (Simple Cache) ---
# 結構: { "2330": {"data": {...}, "expire": 1700000000.0} }
DATA_CACHE = {}
CACHE_LOCK = threading.Lock() # 確保多執行緒安全

def get_cache(stock_id):
    """嘗試從快取拿資料，過期或沒有則回傳 None"""
    with CACHE_LOCK:
        if stock_id in DATA_CACHE:
            entry = DATA_CACHE[stock_id]
            if time.time() < entry['expire']:
                print(f"🚀 [Hit Cache] {stock_id}")
                return entry['data']
            else:
                del DATA_CACHE[stock_id] # 刪除過期資料
    return None

def set_cache(stock_id, data, ttl=300):
    """寫入快取 (預設存活 300秒 = 5分鐘)"""
    with CACHE_LOCK:
        DATA_CACHE[stock_id] = {
            "data": data,
            "expire": time.time() + ttl
        }

# --- 3. 基礎功能 (Line Bot 設定) ---
token = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
secret = os.environ.get('LINE_CHANNEL_SECRET')
line_bot_api = LineBotApi(token if token else 'UNKNOWN')
handler = WebhookHandler(secret if secret else 'UNKNOWN')

@app.route("/")
def health_check():
    return f"🟢 {BOT_VERSION} is Running!", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except: abort(400)
    return 'OK'

# --- 4. 數據抓取層 (FinMind + Cache) ---
def fetch_data_v2(stock_id):
    # [Step 1] 先查快取
    cached = get_cache(stock_id)
    if cached: return cached

    # [Step 2] 快取沒資料，才去問 API
    token = os.environ.get('FINMIND_TOKEN', '')
    url = "https://api.finmindtrade.com/api/v4/data"
    print(f"🐢 [Fetching API] {stock_id}...") # 方便看 Log 追蹤

    try:
        # 抓股價
        start = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
        res = requests.get(url, params={
            "dataset": "TaiwanStockPrice",
            "data_id": stock_id,
            "start_date": start,
            "token": token
        }, timeout=5)
        data = res.json().get('data', [])
        
        if not data: return None
        latest = data[-1]
        closes = [d['close'] for d in data]
        
        # 簡單計算 MA (均線)
        ma5 = round(sum(closes[-5:]) / 5, 2) if len(closes) >= 5 else 0
        ma20 = round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else 0
        ma60 = round(sum(closes[-60:]) / 60, 2) if len(closes) >= 60 else 0

        # 抓法人 (只要最新的)
        start_chips = (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d')
        res_chips = requests.get(url, params={
            "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
            "data_id": stock_id,
            "start_date": start_chips,
            "token": token
        }, timeout=5)
        chips_data = res_chips.json().get('data', [])
        
        # 簡單統計近5日累積
        acc_foreign = sum([d['buy'] - d['sell'] for d in chips_data if d['name'] == 'Foreign_Investor']) // 1000
        acc_trust = sum([d['buy'] - d['sell'] for d in chips_data if d['name'] == 'Investment_Trust']) // 1000

        result = {
            "code": stock_id,
            "close": latest['close'],
            "ma5": ma5, "ma20": ma20, "ma60": ma60,
            "acc_foreign": int(acc_foreign),
            "acc_trust": int(acc_trust)
        }

        # [Step 3] 寫入快取 (存 5 分鐘)
        set_cache(stock_id, result, ttl=300)
        return result

    except Exception as e:
        print(f"❌ Fetch Error: {e}")
        return None

# --- 5. 核心邏輯層 (Controller) ---
def get_stock_id_v2(text):
    text = text.strip().upper() # 轉大寫以防萬一
    # 1. 檢查是不是數字 (2330)
    if text.isdigit() and len(text) == 4:
        return text
    # 2. 檢查是不是中文名 (台積電) -> 讀取 STOCK_MAP
    if text in STOCK_MAP:
        return STOCK_MAP[text]
    
    return None

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    
    # [快速查代碼]
    stock_id = get_stock_id_v2(msg)

    if stock_id:
        # 有代碼 -> 抓資料
        data = fetch_data_v2(stock_id)
        if data:
            name = CODE_TO_NAME.get(stock_id, stock_id)
            # 暫時用簡單文字回覆，測試數據層是否正常
            reply = (
                f"📊 {name} ({stock_id})\n"
                f"💰 現價: {data['close']}\n"
                f"----------------\n"
                f"MA5: {data['ma5']} | MA20: {data['ma20']}\n"
                f"外資5日: {data['acc_foreign']} 張\n"
                f"投信5日: {data['acc_trust']} 張\n"
                f"----------------\n"
                f"(來源: {'🚀快取' if get_cache(stock_id) else '🐢API'} | {BOT_VERSION})"
            )
        else:
            reply = f"❌ 找不到 {stock_id} 的資料 (或 API 異常)"
    else:
        # 沒代碼 -> Echo 測試 (之後接 AI)
        reply = f"Bot 收到: {msg}\n(請輸入 '2330' 或 '鴻海' 測試數據層)"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
