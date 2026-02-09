import os, requests, json, time, re, threading, random
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 🟢 [版本號] v2.1 (JSON-Sniper)
BOT_VERSION = "v2.1 (JSON)"

# --- 1. 載入軍火庫 (自動更新的股票清單) ---
STOCK_MAP = {}
try:
    with open('stock_list.json', 'r', encoding='utf-8') as f:
        STOCK_MAP = json.load(f)
    print(f"✅ 成功載入 {len(STOCK_MAP)} 檔股票")
except Exception as e:
    print(f"⚠️ 讀取清單失敗: {e}")
    STOCK_MAP = {"台積電": "2330", "鴻海": "2317"} # 備用

# 反向查表 (代碼 -> 名字)
CODE_TO_NAME = {v: k for k, v in STOCK_MAP.items()}

# --- 2. 記憶體快取 (Simple Cache) ---
DATA_CACHE = {}
CACHE_LOCK = threading.Lock()

def get_cache(stock_id):
    with CACHE_LOCK:
        if stock_id in DATA_CACHE:
            entry = DATA_CACHE[stock_id]
            if time.time() < entry['expire']:
                return entry['data']
            else:
                del DATA_CACHE[stock_id]
    return None

def set_cache(stock_id, data, ttl=300):
    with CACHE_LOCK:
        DATA_CACHE[stock_id] = {"data": data, "expire": time.time() + ttl}

# --- 3. Line & Gemini 設定 ---
token = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
secret = os.environ.get('LINE_CHANNEL_SECRET')
line_bot_api = LineBotApi(token if token else 'UNKNOWN')
handler = WebhookHandler(secret if secret else 'UNKNOWN')

def call_gemini_json(prompt, system_instruction=None):
    """
    🔥 核心變革：強制 Gemini 輸出 JSON
    """
    key = os.environ.get('GEMINI_API_KEY')
    if not key: return None
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={key}"
    headers = {'Content-Type': 'application/json'}
    
    # 在提示詞中強制要求 JSON
    final_prompt = prompt + "\n\n🔴 IMPORTANT: Reply ONLY in valid JSON format. No Markdown. No explanation."
    
    payload = {
        "contents": [{"parts": [{"text": final_prompt}]}],
        "systemInstruction": {"parts": [{"text": system_instruction or "You are a stock analyzer."}]},
        "generationConfig": {
            "responseMimeType": "application/json", # 強制 JSON 模式 (Gemini 新功能)
            "maxOutputTokens": 1000,
            "temperature": 0.2
        }
    }
    
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=20)
        if res.status_code == 200:
            text = res.json()['candidates'][0]['content']['parts'][0]['text']
            # 清理可能殘留的 markdown 符號
            text = text.replace("```json", "").replace("```", "").strip()
            return json.loads(text) # 轉成 Python 字典
    except Exception as e:
        print(f"AI Error: {e}")
    return None

# --- 4. 數據抓取 (FinMind) ---
def fetch_data(stock_id):
    # 1. 查快取
    cached = get_cache(stock_id)
    if cached: return cached

    # 2. 查 API
    token = os.environ.get('FINMIND_TOKEN', '')
    url = "https://api.finmindtrade.com/api/v4/data"
    
    try:
        # 抓股價 (60天)
        start = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
        res = requests.get(url, params={
            "dataset": "TaiwanStockPrice", "data_id": stock_id, "start_date": start, "token": token
        }, timeout=5)
        data = res.json().get('data', [])
        if not data: return None
        
        latest = data[-1]
        closes = [d['close'] for d in data]
        ma5 = round(sum(closes[-5:]) / 5, 2) if len(closes) >= 5 else 0
        ma20 = round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else 0
        ma60 = round(sum(closes[-60:]) / 60, 2) if len(closes) >= 60 else 0
        
        # 抓法人 (近5日)
        start_chips = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
        res_chips = requests.get(url, params={
            "dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": stock_id, "start_date": start_chips, "token": token
        }, timeout=5)
        chips = res_chips.json().get('data', [])
        
        # 整理最近5個交易日
        dates = sorted(list(set([d['date'] for d in chips])), reverse=True)[:5]
        acc_f = sum([d['buy'] - d['sell'] for d in chips if d['date'] in dates and d['name'] == 'Foreign_Investor']) // 1000
        acc_t = sum([d['buy'] - d['sell'] for d in chips if d['date'] in dates and d['name'] == 'Investment_Trust']) // 1000

        result = {
            "code": stock_id, "close": latest['close'],
            "ma5": ma5, "ma20": ma20, "ma60": ma60,
            "acc_f": int(acc_f), "acc_t": int(acc_t)
        }
        
        # 3. 存快取
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
    
    # 1. 快速查代碼 (使用本地 stock_list.json)
    stock_id = None
    if msg.isdigit() and len(msg) == 4: stock_id = msg
    elif msg in STOCK_MAP: stock_id = STOCK_MAP[msg]
    
    if stock_id:
        # 2. 抓數據
        data = fetch_data(stock_id)
        if not data:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 查無數據"))
            return

        name = CODE_TO_NAME.get(stock_id, stock_id)
        
        # 3. 呼叫 AI (要求 JSON)
        sys_prompt = "你是專業操盤手。根據數據判斷多空。輸出 JSON 格式。"
        user_prompt = (
            f"標的: {name}({stock_id})\n"
            f"現價: {data['close']}\n"
            f"均線: MA5={data['ma5']}, MA20={data['ma20']}, MA60={data['ma60']}\n"
            f"籌碼: 外資5日{data['acc_f']}張, 投信5日{data['acc_t']}張\n\n"
            f"Output JSON format:\n"
            f"{{\n"
            f'  "trend": "多頭/空頭/盤整",\n'
            f'  "reason": "簡短理由(30字內)",\n'
            f'  "support": "xxx",\n'
            f'  "pressure": "xxx",\n'
            f'  "action": "🟢買進 / 🟡觀望 / 🔴賣出"\n'
            f"}}"
        )
        
        ai_json = call_gemini_json(user_prompt, sys_prompt)
        
        # 4. Python 排版 (這裡我們擁有 100% 控制權)
        if ai_json:
            reply = (
                f"🔥 **{name} ({stock_id})**\n"
                f"💰 現價: {data['close']}\n"
                f"------------------\n"
                f"📊 趨勢: {ai_json.get('trend', '分析中')}\n"
                f"💡 {ai_json.get('reason', '無理由')}\n"
                f"------------------\n"
                f"🎯 支撐: {ai_json.get('support')} | 壓力: {ai_json.get('pressure')}\n"
                f"⚖️ 建議: {ai_json.get('action')}\n"
                f"------------------\n"
                f"(籌碼: 外資{data['acc_f']} / 投信{data['acc_t']})\n"
                f"(系統: {BOT_VERSION})"
            )
        else:
            reply = "⚠️ AI 思考失敗，請稍後再試。"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
    
    elif msg == "DEBUG":
        # 測試一下清單有沒有載入
        reply = f"🛠️ **系統狀態**\n股票清單: {len(STOCK_MAP)} 檔\n快取數量: {len(DATA_CACHE)}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        
    else:
        # 沒對應到的指令
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"收到: {msg}\n(請輸入股票名稱測試)"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
