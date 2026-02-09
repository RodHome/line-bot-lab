import os, requests, random, re
import json
import time
import concurrent.futures
from datetime import datetime, timedelta, time as dtime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage

app = Flask(__name__)

# 🟢 [版本號] v13.0 (Commercial Grade: Smart Cache + JSON Parsing + Sector Info)
BOT_VERSION = "v13.0"

# --- 1. 全域快取與設定 ---
# AI 分析結果快取：Key=股票代碼_模式, Value={data: "...", expires: timestamp}
AI_RESPONSE_CACHE = {}

# 菁英池 (含產業標籤) - 這是給「推薦選股」用的
# 結構升級：Key=名稱, Value={code, sector}
ELITE_STOCK_DATA = {
    "台積電": {"code": "2330", "sector": "半導體"},
    "鴻海": {"code": "2317", "sector": "電子代工"},
    "聯發科": {"code": "2454", "sector": "IC設計"},
    "廣達": {"code": "2382", "sector": "AI伺服器"},
    "緯創": {"code": "3231", "sector": "AI伺服器"},
    "技嘉": {"code": "2376", "sector": "板卡/伺服器"},
    "台達電": {"code": "2308", "sector": "電源供應"},
    "日月光": {"code": "3711", "sector": "封測"},
    "聯電": {"code": "2303", "sector": "晶圓代工"},
    "瑞昱": {"code": "2379", "sector": "IC設計"},
    "長榮": {"code": "2603", "sector": "航運"},
    "陽明": {"code": "2609", "sector": "航運"},
    "萬海": {"code": "2615", "sector": "航運"},
    "富邦金": {"code": "2881", "sector": "金融"},
    "國泰金": {"code": "2882", "sector": "金融"},
    "中信金": {"code": "2891", "sector": "金融"},
    "奇鋐": {"code": "3017", "sector": "散熱"},
    "雙鴻": {"code": "3324", "sector": "散熱"},
    "華城": {"code": "1519", "sector": "重電"},
    "士電": {"code": "1503", "sector": "重電"},
    "世紀鋼": {"code": "9958", "sector": "風電/鋼鐵"}
}
# 為了相容舊程式邏輯，建立一個簡易對照表
ELITE_STOCK_POOL = {k: v["code"] for k, v in ELITE_STOCK_DATA.items()}

# 全台股名單 (個股查詢用)
ALL_STOCK_MAP = ELITE_STOCK_POOL.copy()

# 嘗試讀取 GitHub 的 stock_list.json
try:
    if os.path.exists('stock_list.json'):
        with open('stock_list.json', 'r', encoding='utf-8') as f:
            full_list = json.load(f)
            # full_list 格式若是 {"台積電": "2330"...} 直接更新
            ALL_STOCK_MAP.update(full_list)
            print(f"[System] 外部名單載入成功。總數: {len(ALL_STOCK_MAP)}")
except Exception as e:
    print(f"[System] 使用內建名單。原因: {e}")

CODE_TO_NAME = {v: k for k, v in ALL_STOCK_MAP.items()}

token = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
secret = os.environ.get('LINE_CHANNEL_SECRET')
line_bot_api = LineBotApi(token if token else 'UNKNOWN')
handler = WebhookHandler(secret if secret else 'UNKNOWN')

@app.route("/")
def health_check():
    return f"OK ({BOT_VERSION})", 200

# --- 2. 智慧快取管理器 (核心升級) ---
def get_smart_cache_ttl():
    """根據盤中/盤後決定快取存活時間 (秒)"""
    now = datetime.now().time()
    market_open = dtime(9, 0)
    market_close = dtime(13, 30)
    
    # 盤中 (09:00 - 13:30)：快取 15 分鐘 (900秒)，兼顧即時性與省錢
    if market_open <= now <= market_close:
        return 900 
    # 盤後：快取 12 小時 (43200秒)，資料已定案
    else:
        return 43200

def get_cached_ai_response(key):
    """取得快取的 AI 回覆"""
    if key in AI_RESPONSE_CACHE:
        record = AI_RESPONSE_CACHE[key]
        if time.time() < record['expires']:
            return record['data'] # 未過期，直接回傳
        else:
            del AI_RESPONSE_CACHE[key] # 過期刪除
    return None

def set_cached_ai_response(key, data):
    """寫入快取"""
    ttl = get_smart_cache_ttl()
    AI_RESPONSE_CACHE[key] = {
        'data': data,
        'expires': time.time() + ttl
    }

# --- 3. 工具函式 ---
def clean_json_string(text):
    """清洗 AI 回傳的 JSON 字串 (移除 markdown 標記)"""
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    return text.strip()

def call_gemini_json(prompt, system_instruction=None):
    """強制 AI 回傳 JSON 格式"""
    keys = [os.environ.get(f'GEMINI_API_KEY_{i}') for i in range(1, 7) if os.environ.get(f'GEMINI_API_KEY_{i}')]
    if not keys and os.environ.get('GEMINI_API_KEY'): keys = [os.environ.get('GEMINI_API_KEY')]
    if not keys: return None
    
    random.shuffle(keys)
    target_models = ["gemini-2.5-flash", "gemini-1.5-flash"] 

    # 在 prompt 後面強制加上 JSON 要求
    final_prompt = prompt + "\n\n⚠️請務必只回傳純 JSON 格式，不要有任何其他文字。"

    for model in target_models:
        for key in keys:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                headers = {'Content-Type': 'application/json'}
                params = {'key': key}
                contents = [{"parts": [{"text": final_prompt}]}]
                if system_instruction:
                    contents = [{"parts": [{"text": f"系統指令: {system_instruction}\n用戶: {final_prompt}"}]}]

                payload = {
                    "contents": contents,
                    "generationConfig": {
                        "maxOutputTokens": 2000, 
                        "temperature": 0.2,
                        "responseMimeType": "application/json" # v13.0: 啟用 JSON 模式
                    }
                }
                response = requests.post(url, headers=headers, params=params, json=payload, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    text = data.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
                    if text: return clean_json_string(text)
                continue
            except: continue
    return None

def fetch_data_light(stock_id):
    """抓取股價 (絕對不快取，保證即時)"""
    token = os.environ.get('FINMIND_TOKEN', '')
    url = "https://api.finmindtrade.com/api/v4/data"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        start = (datetime.now() - timedelta(days=120)).strftime('%Y-%m-%d')
        res = requests.get(url, params={"dataset": "TaiwanStockPrice", "data_id": stock_id, "start_date": start, "token": token}, headers=headers, timeout=5)
        data = res.json().get('data', [])
        if not data: return None
        
        latest = data[-1]
        closes = [d['close'] for d in data]
        ma5 = round(sum(closes[-5:]) / 5, 2) if len(closes) >= 5 else 0
        ma20 = round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else 0
        ma60 = round(sum(closes[-60:]) / 60, 2) if len(closes) >= 60 else 0
        
        # 計算漲跌幅 (用於紅綠燈)
        prev_close = data[-2]['close'] if len(data) >= 2 else latest['close']
        change = latest['close'] - prev_close
        change_pct = round(change / prev_close * 100, 2) if prev_close > 0 else 0
        
        color = "#D32F2F" if change >= 0 else "#2E7D32" # 紅漲綠跌

        return {
            "code": stock_id, "close": latest['close'], 
            "ma5": ma5, "ma20": ma20, "ma60": ma60,
            "change": change, "change_pct": change_pct, "color": color,
            "high_60": max([d['max'] for d in data[-60:]])
        }
    except: return None

def fetch_chips_accumulate(stock_id):
    # (此函式邏輯不變，略過重複代碼以節省篇幅，請保留原有的 fetch_chips_accumulate)
    # ... (請將 v12.2 的 fetch_chips_accumulate 完整複製過來) ...
    token = os.environ.get('FINMIND_TOKEN', '')
    url = "https://api.finmindtrade.com/api/v4/data"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        start = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
        res = requests.get(url, params={"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": stock_id, "start_date": start, "token": token}, headers=headers, timeout=5)
        data = res.json().get('data', [])
        if not data: return 0, 0, 0, 0
        
        latest_date = data[-1]['date']
        today_f = 0; today_t = 0
        unique_dates = sorted(list(set([d['date'] for d in data])), reverse=True)[:5]
        acc_f = 0; acc_t = 0
        
        for row in data:
            if row['date'] in unique_dates:
                val = row['buy'] - row['sell']
                if row['name'] == 'Foreign_Investor':
                    acc_f += val
                    if row['date'] == latest_date: today_f = val
                elif row['name'] == 'Investment_Trust':
                    acc_t += val
                    if row['date'] == latest_date: today_t = val
        return int(today_f/1000), int(today_t/1000), int(acc_f/1000), int(acc_t/1000)
    except: return 0, 0, 0, 0

def fetch_eps(stock_id):
    # ... (請將 v12.2 的 fetch_eps 完整複製過來) ...
    if stock_id.startswith("00"): return "ETF"
    token = os.environ.get('FINMIND_TOKEN', '')
    start = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d')
    try:
        res = requests.get("https://api.finmindtrade.com/api/v4/data", params={"dataset": "TaiwanStockFinancialStatements", "data_id": stock_id, "start_date": start, "token": token}, timeout=5)
        data = res.json().get('data', [])
        if not data: return "N/A"
        eps_data = [d for d in data if d['type'] == 'EPS']
        latest_year = eps_data[-1]['date'][:4]
        vals = [d['value'] for d in eps_data if d['date'].startswith(latest_year)]
        return f"{latest_year}累計{round(sum(vals), 2)}元"
    except: return "逾時"

def get_stock_id(text):
    text = text.strip()
    clean = re.sub(r'(成本|cost).*', '', text, flags=re.IGNORECASE).strip()
    if clean in ALL_STOCK_MAP: return ALL_STOCK_MAP[clean]
    if clean.isdigit() and len(clean) >= 4: return clean
    return None

def check_stock_worker_turbo(code):
    try:
        data = fetch_data_light(code)
        if not data: return None
        
        # 條件：三線多頭
        if data['close'] > data['ma5'] and data['ma5'] > data['ma20'] and data['ma20'] > data['ma60']:
            tf, tt, af, at = fetch_chips_accumulate(code)
            
            # v13.0 動態籌碼門檻
            threshold = 50 if data['close'] > 100 else 200
            
            if (af + at) > threshold:
                name = CODE_TO_NAME.get(code, code)
                # 嘗試取得產業標籤
                sector = "熱門股"
                if name in ELITE_STOCK_DATA: sector = ELITE_STOCK_DATA[name]['sector']
                
                return {
                    "code": code, "name": name, "sector": sector,
                    "close": data['close'], "color": data['color'],
                    "chips": f"{af+at}張", 
                    "tag": "外資大買" if af > at else "投信認養"
                }
    except: return None
    return None

def scan_recommendations_turbo():
    candidates = []
    # 降低抽樣數至 25 檔，提升回應速度
    elite_codes = [v['code'] for v in ELITE_STOCK_DATA.values()]
    # 如果菁英池不夠多，就全掃
    sample_list = random.sample(elite_codes, 25) if len(elite_codes) > 25 else elite_codes
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = executor.map(check_stock_worker_turbo, sample_list)
    for res in results:
        if res: candidates.append(res)
        if len(candidates) >= 3: break
    return candidates

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
    
    # 🔥 [功能 1] 推薦選股
    if msg in ["推薦", "選股"]:
        good_stocks = scan_recommendations_turbo()
        if not good_stocks:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 掃描菁英池後，暫無符合「強勢多頭+籌碼集中」之標的，建議觀望。"))
            return
            
        # 準備 AI 資料 (使用 JSON 模式)
        stocks_payload = []
        for s in good_stocks:
            stocks_payload.append({"name": s['name'], "code": s['code'], "sector": s['sector']})
            
        sys_prompt = "你是專業操盤手。請針對下列股票回傳 JSON 格式推薦。Array中包含每個股票的物件，屬性有: name, suggestion(進場/觀望), reason(50字內，結合產業面)。"
        ai_json_str = call_gemini_json(f"股票清單: {json.dumps(stocks_payload, ensure_ascii=False)}", system_instruction=sys_prompt)
        
        reasons_map = {}
        if ai_json_str:
            try:
                ai_data = json.loads(ai_json_str)
                # 相容回傳可能是 list 或 dict 的情況
                items = ai_data if isinstance(ai_data, list) else ai_data.get('stocks', [])
                for item in items:
                    reasons_map[item.get('name')] = item.get('reason', '趨勢偏多')
            except: pass

        bubbles = []
        for stock in good_stocks:
            reason = reasons_map.get(stock['name'], "技術面強勢，籌碼集中。")
            bubble = {
                "type": "bubble",
                "size": "giga", 
                "header": {
                    "type": "box", "layout": "vertical", 
                    "contents": [
                        {"type": "text", "text": f"{stock['name']} ({stock['sector']})", "weight": "bold", "size": "xl", "color": "#ffffff"},
                        {"type": "text", "text": stock['code'], "size": "xs", "color": "#eeeeee"}
                    ], 
                    "backgroundColor": stock['color'] # 🔥 紅漲綠跌
                },
                "body": {"type": "box", "layout": "vertical", "contents": [
                    {"type": "text", "text": str(stock['close']), "weight": "bold", "size": "3xl", "color": stock['color'], "align": "center"},
                    {"type": "text", "text": f"💰{stock['tag']} | 🏦籌碼:{stock['chips']}", "size": "xs", "color": "#555555", "align": "center", "margin": "md"},
                    {"type": "separator", "margin": "md"},
                    {"type": "text", "text": reason, "size": "sm", "color": "#333333", "wrap": True, "margin": "md"},
                    {"type": "button", "action": {"type": "message", "label": "詳細診斷", "text": stock['code']}, "style": "link", "margin": "md"}
                ]}
            }
            bubbles.append(bubble)
            
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="AI 精選強勢股", contents={"type": "carousel", "contents": bubbles}))
        return

    # [功能 2] 個股診斷 (含 Cache 機制)
    stock_id = get_stock_id(msg)
    user_cost = None
    cost_match = re.search(r'(成本|cost)[:\s]*(\d+\.?\d*)', msg, re.IGNORECASE)
    if cost_match: user_cost = float(cost_match.group(2))

    if stock_id:
        name = CODE_TO_NAME.get(stock_id, stock_id)
        # 1. 股價絕對即時
        data = fetch_data_light(stock_id) 
        if not data: return
        
        # 2. 籌碼與EPS
        tf, tt, af, at = fetch_chips_accumulate(stock_id)
        eps = fetch_eps(stock_id)
        
        # 3. AI 分析 (使用快取)
        cache_key = f"{stock_id}_{'cost' if user_cost else 'query'}"
        ai_reply_text = get_cached_ai_response(cache_key)
        
        if not ai_reply_text:
            # 快取過期或不存在，呼叫 AI
            if user_cost:
                profit_pct = round((data['close'] - user_cost) / user_cost * 100, 1)
                profit_status = "獲利" if profit_pct > 0 else "虧損"
                sys_prompt = "你是專業分析師。請回傳 JSON。屬性: analysis(分析), action(建議:進場/減碼/停損), strategy(停利停損價)。"
                user_prompt = f"標的:{name}, 現價:{data['close']}, 成本:{user_cost}"
                
                json_str = call_gemini_json(user_prompt, system_instruction=sys_prompt)
                try:
                    res = json.loads(json_str)
                    ai_reply_text = f"【診斷】{res['action']}\n{res['analysis']}\n【策略】{res['strategy']}"
                except: ai_reply_text = "AI 數據解析失敗，請重試。"
                
            else:
                sys_prompt = "你是股市判官。請回傳 JSON。屬性: analysis(市場面與籌碼分析,100字), advice(建議:進場/觀望/不可入場)。"
                user_prompt = f"標的:{name}, 現價:{data['close']}, MA20:{data['ma20']}, 外資:{af}張, 投信:{at}張"
                
                json_str = call_gemini_json(user_prompt, system_instruction=sys_prompt)
                try:
                    res = json.loads(json_str)
                    ai_reply_text = f"【分析】{res['analysis']}\n【建議】{res['advice']}"
                except: ai_reply_text = "AI 數據解析失敗，請重試。"
            
            # 寫入快取
            if "解析失敗" not in ai_reply_text:
                set_cached_ai_response(cache_key, ai_reply_text)

        # 4. 組裝最終訊息
        data_dashboard = f"💰 現價：{data['close']} ({data['change_pct']}%)\n📊 週: {data['ma5']} | 月: {data['ma20']}\n🏦 外資: {af} | 投信: {at}\n💎 EPS: {eps}"
        
        signals = []
        if data['close'] > data['ma5'] > data['ma20']: signals.append("🟢多頭排列")
        if (af + at) > 50: signals.append("💰法人買超")
        signal_str = " | ".join(signals) if signals else "🟡趨勢不明"
        
        cta = f"💡 輸入『{name}成本xxx』AI 幫你算！"
        reply = f"📈 **{name}({stock_id})**\n{data_dashboard}\n------------------\n🚩 {signal_str}\n------------------\n{ai_reply_text}\n------------------\n{cta}\n(系統: {BOT_VERSION})"
        
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
