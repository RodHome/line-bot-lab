import os, requests, json, time, re, threading
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 🟢 [版本號] v2.3 (Diagnostic)
BOT_VERSION = "v2.3 (Diagnostic)"

# --- 1. 載入軍火庫 ---
STOCK_MAP = {}
try:
    with open('stock_list.json', 'r', encoding='utf-8') as f:
        STOCK_MAP = json.load(f)
except:
    STOCK_MAP = {"台積電": "2330"} 
CODE_TO_NAME = {v: k for k, v in STOCK_MAP.items()}

# --- 2. 記憶體快取 ---
DATA_CACHE = {}
CACHE_LOCK = threading.Lock()

def get_cache(stock_id):
    with CACHE_LOCK:
        if stock_id in DATA_CACHE:
            entry = DATA_CACHE[stock_id]
            if time.time() < entry['expire']: return entry['data']
            else: del DATA_CACHE[stock_id]
    return None

def set_cache(stock_id, data, ttl=300):
    with CACHE_LOCK:
        DATA_CACHE[stock_id] = {"data": data, "expire": time.time() + ttl}

# --- 3. Line 設定 ---
token = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
secret = os.environ.get('LINE_CHANNEL_SECRET')
line_bot_api = LineBotApi(token if token else 'UNKNOWN')
handler = WebhookHandler(secret if secret else 'UNKNOWN')

# --- 4. Gemini 核心 (容錯版) ---
def call_gemini_safe(prompt):
    key = os.environ.get('GEMINI_API_KEY')
    if not key: return {"error": "Key未設定", "raw": "Env var empty"}

    # 🚨 策略：我們先試 gemini-1.5-flash，如果失敗，程式會回傳錯誤，我們再來看LOG
    # 如果您確定只有舊版，可以手動把下面這行改成 "gemini-pro"
    target_model = "gemini-1.5-flash" 
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent?key={key}"
    headers = {'Content-Type': 'application/json'}
    
    # ⚠️ 移除 responseMimeType，避免舊模型報錯 400
    payload = {
        "contents": [{"parts": [{"text": prompt + "\n(請只輸出 JSON)"}]}],
        "generationConfig": {
            "temperature": 0.2
        }
    }
    
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=20)
        if res.status_code == 200:
            # 嘗試解析 JSON
            text = res.json()['candidates'][0]['content']['parts'][0]['text']
            clean_text = text.replace("```json", "").replace("```", "").strip()
            try:
                return json.loads(clean_text)
            except:
                # 萬一 AI 沒乖乖給 JSON，至少把文字回傳
                return {"trend": "解析失敗", "reason": clean_text[:50], "action": "🟡無法判讀"}
        else:
            # 🔥 關鍵：回傳 Google 的真實錯誤訊息
            return {"error": f"HTTP {res.status_code}", "raw": res.text}
    except Exception as e:
        return {"error": "連線異常", "raw": str(e)}

def check_available_models():
    """
    🕵️‍♂️ 偵探功能：查詢這把 Key 到底能用哪些模型
    """
    key = os.environ.get('GEMINI_API_KEY')
    if not key: return "❌ Key 未設定"
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            models = res.json().get('models', [])
            # 過濾出 generateContent 類型的模型
            chat_models = [m['name'].replace('models/', '') for m in models if 'generateContent' in m['supportedGenerationMethods']]
            return "\n".join(chat_models)
        else:
            return f"❌ 查詢失敗: {res.text}"
    except Exception as e:
        return f"❌ 連線失敗: {e}"

# --- 5. 數據抓取 ---
def fetch_data(stock_id):
    # (省略重複代碼，與 v2.2 相同，只保留核心邏輯)
    cached = get_cache(stock_id)
    if cached: return cached
    token = os.environ.get('FINMIND_TOKEN', '')
    url = "https://api.finmindtrade.com/api/v4/data"
    try:
        start = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
        res = requests.get(url, params={"dataset": "TaiwanStockPrice", "data_id": stock_id, "start_date": start, "token": token}, timeout=5)
        data = res.json().get('data', [])
        if not data: return None
        latest = data[-1]
        closes = [d['close'] for d in data]
        ma5 = round(sum(closes[-5:]) / 5, 2) if len(closes) >= 5 else 0
        ma20 = round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else 0
        ma60 = round(sum(closes[-60:]) / 60, 2) if len(closes) >= 60 else 0
        start_chips = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
        res_chips = requests.get(url, params={"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": stock_id, "start_date": start_chips, "token": token}, timeout=5)
        chips = res_chips.json().get('data', [])
        dates = sorted(list(set([d['date'] for d in chips])), reverse=True)[:5]
        acc_f = sum([d['buy'] - d['sell'] for d in chips if d['date'] in dates and d['name'] == 'Foreign_Investor']) // 1000
        acc_t = sum([d['buy'] - d['sell'] for d in chips if d['date'] in dates and d['name'] == 'Investment_Trust']) // 1000
        result = {"code": stock_id, "close": latest['close'], "ma5": ma5, "ma20": ma20, "ma60": ma60, "acc_f": int(acc_f), "acc_t": int(acc_t)}
        set_cache(stock_id, result)
        return result
    except: return None

@app.route("/", methods=['GET'])
def hello(): return "OK"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except: abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip().upper()

    # 🔥 終極偵錯指令 🔥
    if msg == "DEBUG":
        key = os.environ.get('GEMINI_API_KEY', '')
        key_status = f"✅ 設定 (前4碼: {key[:4]})" if key else "❌ 未設定"
        
        # 1. 檢查模型列表
        available_models = check_available_models()
        
        # 2. 測試打一次 API (看真實錯誤)
        test_res = call_gemini_safe("Hi")
        
        report = (
            f"🕵️‍♂️ **v2.3 診斷報告**\n"
            f"----------------\n"
            f"🔑 Key狀態: {key_status}\n"
            f"📋 可用模型清單:\n{available_models}\n"
            f"----------------\n"
            f"🧪 測試結果:\n{test_res}"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=report))
        return

    # 一般查詢邏輯
    stock_id = None
    if msg.isdigit() and len(msg) == 4: stock_id = msg
    elif msg in STOCK_MAP: stock_id = STOCK_MAP[msg]

    if stock_id:
        data = fetch_data(stock_id)
        if not data:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 查無數據 | {BOT_VERSION}"))
            return
        
        name = CODE_TO_NAME.get(stock_id, stock_id)
        prompt = (
            f"標的: {name}({stock_id})\n現價: {data['close']}\n"
            f"均線: MA5={data['ma5']}, MA20={data['ma20']}\n"
            f"籌碼: 外資5日{data['acc_f']}張\n"
            f"請輸出 JSON 包含 trend, reason, action"
        )
        
        ai_json = call_gemini_safe(prompt)
        
        # 錯誤顯示
        if "error" in ai_json:
            reply = f"⚠️ AI 錯誤: {ai_json['error']}\n💬 原始訊息: {ai_json.get('raw', '')}"
        else:
            reply = (
                f"🔥 **{name} ({stock_id})**\n"
                f"💰 現價: {data['close']}\n"
                f"------------------\n"
                f"📊 {ai_json.get('trend', 'N/A')}\n"
                f"💡 {ai_json.get('reason', 'N/A')}\n"
                f"⚖️ {ai_json.get('action', 'N/A')}\n"
                f"------------------\n"
                f"(系統: {BOT_VERSION})"
            )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
    else:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"收到: {msg}\n(請輸入 DEBUG 查明真相) | {BOT_VERSION}"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
