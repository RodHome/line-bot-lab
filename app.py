import os, requests, json, time, re, threading, random
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 🟢 [版本號] v11.0 (Lab Test)
BOT_VERSION = "v11.0 (Lab Test)"

# --- 1. 載入自動更新的股票清單 (優先讀取 json) ---
STOCK_MAP = {}
try:
    if os.path.exists('stock_list.json'):
        with open('stock_list.json', 'r', encoding='utf-8') as f:
            STOCK_MAP = json.load(f)
        print(f"✅ [v11.0] 成功載入 stock_list.json: {len(STOCK_MAP)} 檔")
except Exception as e:
    print(f"⚠️ 讀取清單失敗: {e}")

# 備援名單 (防止完全讀不到檔案時掛掉)
if not STOCK_MAP:
    STOCK_MAP = {"台積電": "2330", "鴻海": "2317", "聯發科": "2454", "廣達": "2382", "緯創": "3231"}

CODE_TO_NAME = {v: k for k, v in STOCK_MAP.items()}

# --- 2. 記憶體快取 (Simple Cache) ---
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

# --- 4. Gemini 核心 (v10.5 多 Key 輪詢 + 強制 JSON) ---
def call_gemini_v11(prompt):
    # 讀取環境變數中的 Key (支援多組)
    keys = [os.environ.get(f'GEMINI_API_KEY_{i}') for i in range(1, 7) if os.environ.get(f'GEMINI_API_KEY_{i}')]
    # 如果沒有多組 Key，嘗試讀取單一 Key
    if not keys and os.environ.get('GEMINI_API_KEY'):
        keys = [os.environ.get('GEMINI_API_KEY')]
    
    if not keys: return {"error": "No Keys Found"}
    random.shuffle(keys)

    # 沿用 v10.5 驗證過可用的模型清單 (避開 1.5)
    target_models = ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-flash-latest"]
    
    # 強制 JSON 格式提示詞 (Prompt Engineering)
    final_prompt = prompt + "\n\n🔴 IMPORTANT: Reply ONLY in valid JSON format (no markdown code blocks). Keys: trend, reason, support, pressure, action."

    for model in target_models:
        for key in keys:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                headers = {'Content-Type': 'application/json'}
                params = {'key': key}
                
                # 不使用 responseMimeType，避免舊模型報錯，改用 Prompt 強制
                payload = {
                    "contents": [{"parts": [{"text": final_prompt}]}],
                    "generationConfig": {
                        "temperature": 0.2,
                        "maxOutputTokens": 800
                    }
                }
                
                res = requests.post(url, headers=headers, params=params, json=payload, timeout=25)
                if res.status_code == 200:
                    text = res.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
                    # 清理 Markdown 符號
                    clean_text = text.replace("```json", "").replace("```", "").strip()
                    try:
                        return json.loads(clean_text)
                    except:
                        # 萬一 AI 沒給 JSON，回傳原始文字做備援
                        return {"trend": "格式異常", "reason": clean_text[:50], "action": "🟡人工判讀"}
            except: continue
            
    return {"error": "AI 忙碌中 (All Fail)"}

# --- 5. 數據抓取 (FinMind + Cache) ---
def fetch_data(stock_id):
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

# --- 6. 主程式邏輯 ---
def get_stock_id_v11(text):
    text = text.strip().upper()
    if text.isdigit() and len(text) == 4: return text
    if text in STOCK_MAP: return STOCK_MAP[text]
    return None

@app.route("/", methods=['GET'])
def hello(): return f"OK {BOT_VERSION}"

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
    
    # 📌 1. Debug 指令
    if msg == "DEBUG":
        reply = f"🛠️ **{BOT_VERSION} 診斷**\n清單: {len(STOCK_MAP)} 檔\n快取: {len(DATA_CACHE)}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    stock_id = get_stock_id_v11(msg)
    
    if stock_id:
        data = fetch_data(stock_id)
        # 📌 2. 查無資料
        if not data:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 查無數據 ({stock_id}) | {BOT_VERSION}"))
            return
            
        name = CODE_TO_NAME.get(stock_id, stock_id)
        
        # 使用精簡 Prompt，要求 JSON
        prompt = (
            f"標的: {name}({stock_id})\n現價: {data['close']}\n"
            f"均線: MA5={data['ma5']}, MA20={data['ma20']}, MA60={data['ma60']}\n"
            f"籌碼: 外資5日{data['acc_f']}張, 投信5日{data['acc_t']}張\n"
            f"判斷多空，並給出操作建議。"
        )
        
        ai_json = call_gemini_v11(prompt)
        
        # 📌 3. AI 異常
        if "error" in ai_json:
             reply = f"⚠️ AI 分析異常\n({ai_json['error']})\n系統: {BOT_VERSION}"
        else:
            # 📌 4. 成功回覆
            reply = (
                f"🔥 **{name} ({stock_id})**\n"
                f"💰 現價: {data['close']}\n"
                f"------------------\n"
                f"📊 {ai_json.get('trend', '分析中')}\n"
                f"💡 {ai_json.get('reason', '資料解讀中')}\n"
                f"------------------\n"
                f"🎯 支撐: {ai_json.get('support', '-')} | 壓力: {ai_json.get('pressure', '-')}\n"
                f"⚖️ {ai_json.get('action', '觀望')}\n"
                f"------------------\n"
                f"(籌碼: 外資{data['acc_f']} / 投信{data['acc_t']})\n"
                f"(系統: {BOT_VERSION})"
            )
        
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
    
    # 📌 5. 非股票指令 (可選)
    else:
       line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"收到: {msg} | {BOT_VERSION}"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
