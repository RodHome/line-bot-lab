import os, requests, random, re
import json
import time
import math
import concurrent.futures
from datetime import datetime, timedelta, time as dtime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage

app = Flask(__name__)

# 🟢 [版本號] v15.1 (Concise Diagnosis + Kilo Cards)
BOT_VERSION = "v15.1"

# --- 1. 全域快取與設定 ---
AI_RESPONSE_CACHE = {}

# 菁英池 (含產業標籤)
ELITE_STOCK_DATA = {
    "台積電": {"code": "2330", "sector": "半導體/晶圓代工"},
    "鴻海": {"code": "2317", "sector": "電子代工/AI伺服器"},
    "聯發科": {"code": "2454", "sector": "IC設計/AI手機"},
    "廣達": {"code": "2382", "sector": "AI伺服器"},
    "緯創": {"code": "3231", "sector": "AI伺服器"},
    "技嘉": {"code": "2376", "sector": "板卡/伺服器"},
    "台達電": {"code": "2308", "sector": "電源供應/電動車"},
    "日月光": {"code": "3711", "sector": "封測/CoWoS"},
    "聯電": {"code": "2303", "sector": "晶圓代工"},
    "瑞昱": {"code": "2379", "sector": "IC設計/網通"},
    "長榮": {"code": "2603", "sector": "航運/貨櫃"},
    "陽明": {"code": "2609", "sector": "航運/貨櫃"},
    "萬海": {"code": "2615", "sector": "航運/貨櫃"},
    "富邦金": {"code": "2881", "sector": "金融/壽險"},
    "國泰金": {"code": "2882", "sector": "金融/壽險"},
    "中信金": {"code": "2891", "sector": "金融/銀行"},
    "奇鋐": {"code": "3017", "sector": "散熱模組"},
    "雙鴻": {"code": "3324", "sector": "散熱模組"},
    "華城": {"code": "1519", "sector": "重電/綠能"},
    "士電": {"code": "1503", "sector": "重電/綠能"},
    "世紀鋼": {"code": "9958", "sector": "風電/鋼鐵"}
}
ELITE_STOCK_POOL = {k: v["code"] for k, v in ELITE_STOCK_DATA.items()}
ALL_STOCK_MAP = ELITE_STOCK_POOL.copy()

try:
    if os.path.exists('stock_list.json'):
        with open('stock_list.json', 'r', encoding='utf-8') as f:
            full_list = json.load(f)
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

# --- 2. 核心：技術指標計算引擎 ---
def calculate_rsi(prices, period=14):
    if len(prices) < period + 1: return 50
    gains = []
    losses = []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        gains.append(max(0, change))
        losses.append(max(0, -change))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

def calculate_kd(highs, lows, closes, period=9):
    if len(closes) < period: return 50, 50
    k = 50; d = 50
    try:
        highest_high = max(highs[-period:])
        lowest_low = min(lows[-period:])
        rsv = 0
        if highest_high != lowest_low:
            rsv = (closes[-1] - lowest_low) / (highest_high - lowest_low) * 100
        k = (2/3) * 50 + (1/3) * rsv
        d = (2/3) * 50 + (1/3) * k
    except: pass
    return round(k, 1), round(d, 1)

def get_technical_signals(data, chips_val):
    signals = []
    closes = data['raw_closes']
    highs = data['raw_highs']
    lows = data['raw_lows']
    volumes = data['raw_volumes']
    
    rsi = calculate_rsi(closes)
    k, d = calculate_kd(highs, lows, closes)
    ma5 = data['ma5']; ma20 = data['ma20']; ma60 = data['ma60']; close = data['close']
    
    if rsi > 80: signals.append("🔥RSI過熱")
    elif rsi < 20: signals.append("💎RSI超賣")
    
    bias_20 = (close - ma20) / ma20 * 100
    if bias_20 > 15: signals.append("⚠️乖離過大")
    
    if len(volumes) >= 6:
        avg_vol = sum(volumes[-6:-1]) / 5
        if avg_vol > 0 and volumes[-1] > avg_vol * 2 and close > data['open']:
            signals.append("🚀爆量長紅")
            
    if (close - data['open']) / data['open'] > 0.05: signals.append("🧱長紅棒")
    elif (min(data['open'], close) - data['low']) > (abs(close - data['open']) * 2): signals.append("📌長下影線")
        
    if k > 80: signals.append("📈KD高檔")
    elif k < 20: signals.append("📉KD低檔")
    
    if chips_val > 2000: signals.append("💰外資大買")
    elif chips_val > 50: signals.append("💰法人買超")
    elif chips_val < -2000: signals.append("💸外資倒貨")
    elif chips_val < -50: signals.append("💸法人賣超")
    
    if close > ma5 > ma20 > ma60: signals.append("🟢三線多頭")
    elif close < ma5 < ma20 < ma60: signals.append("🔴三線空頭")
    
    unique_signals = []
    [unique_signals.append(x) for x in signals if x not in unique_signals]
    if not unique_signals: unique_signals = ["🟡趨勢盤整"]
    return unique_signals[:3]

# --- 3. 智慧快取與 API ---
def get_smart_cache_ttl():
    now = datetime.now().time()
    if dtime(9, 0) <= now <= dtime(13, 30): return 900 
    else: return 43200

def get_cached_ai_response(key):
    if key in AI_RESPONSE_CACHE:
        record = AI_RESPONSE_CACHE[key]
        if time.time() < record['expires']: return record['data']
        else: del AI_RESPONSE_CACHE[key]
    return None

def set_cached_ai_response(key, data):
    AI_RESPONSE_CACHE[key] = {'data': data, 'expires': time.time() + get_smart_cache_ttl()}

def clean_json_string(text):
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    return text.strip()

def call_gemini_json(prompt, system_instruction=None):
    keys = [os.environ.get(f'GEMINI_API_KEY_{i}') for i in range(1, 7) if os.environ.get(f'GEMINI_API_KEY_{i}')]
    if not keys and os.environ.get('GEMINI_API_KEY'): keys = [os.environ.get('GEMINI_API_KEY')]
    if not keys: return None
    random.shuffle(keys)
    target_models = ["gemini-2.5-flash", "gemini-1.5-flash"] 
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
                    "generationConfig": {"maxOutputTokens": 2000, "temperature": 0.3, "responseMimeType": "application/json"}
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
        highs = [d['max'] for d in data]
        lows = [d['min'] for d in data]
        volumes = [d['Trading_Volume'] for d in data]
        
        ma5 = round(sum(closes[-5:]) / 5, 2) if len(closes) >= 5 else 0
        ma20 = round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else 0
        ma60 = round(sum(closes[-60:]) / 60, 2) if len(closes) >= 60 else 0
        
        prev_close = data[-2]['close'] if len(data) >= 2 else latest['close']
        change = latest['close'] - prev_close
        change_pct = round(change / prev_close * 100, 2) if prev_close > 0 else 0
        
        sign = "+" if change > 0 else ""
        formatted_change = f"{sign}{round(change, 2)}"
        formatted_pct = f"{sign}{change_pct}%"
        change_display = f"({formatted_change}, {formatted_pct})"
        color = "#D32F2F" if change >= 0 else "#2E7D32"

        return {
            "code": stock_id, "close": latest['close'], "open": latest['open'], "low": latest['min'],
            "ma5": ma5, "ma20": ma20, "ma60": ma60,
            "change": change, "change_display": change_display, "color": color,
            "raw_closes": closes, "raw_highs": highs, "raw_lows": lows, "raw_volumes": volumes
        }
    except: return None

def fetch_chips_accumulate(stock_id):
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
        if data['ma5'] > data['ma20']:
            tf, tt, af, at = fetch_chips_accumulate(code)
            threshold = 50 if data['close'] > 100 else 200
            if (af + at) > threshold:
                name = CODE_TO_NAME.get(code, code)
                sector = "熱門股"
                if name in ELITE_STOCK_DATA: sector = ELITE_STOCK_DATA[name]['sector']
                
                signals = get_technical_signals(data, af+at)
                signal_str = " | ".join(signals)
                
                return {
                    "code": code, "name": name, "sector": sector,
                    "close": data['close'], "change_display": data['change_display'], "color": data['color'],
                    "chips": f"{af+at}張", "signal_str": signal_str,
                    "tag": "外資大買" if af > at else "投信認養"
                }
    except: return None
    return None

def scan_recommendations_turbo(target_sector=None):
    candidates = []
    if target_sector:
        pool = [v['code'] for k, v in ELITE_STOCK_DATA.items() if target_sector in v['sector']]
        if not pool: return []
        sample_list = pool
    else:
        elite_codes = [v['code'] for v in ELITE_STOCK_DATA.values()]
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
    
    # 🔥 [功能 1] 推薦選股 (Kilo Cards)
    msg_parts = msg.split()
    if msg_parts[0] in ["推薦", "選股"]:
        target_sector = msg_parts[1] if len(msg_parts) > 1 else None
        
        good_stocks = scan_recommendations_turbo(target_sector)
        if not good_stocks:
            sector_msg = f"「{target_sector}」" if target_sector else "菁英池"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 掃描{sector_msg}後，暫無符合標的。"))
            return
            
        stocks_payload = [{"name": s['name'], "sector": s['sector']} for s in good_stocks]
        sys_prompt = (
            "你是專業操盤手。請針對下列股票回傳 JSON。Array屬性: name, reason。\n"
            "⚠️ reason 撰寫規則(50字內)：\n"
            "1. 必須結合『產業題材』(如AI、運價、CoWoS)。\n"
            "2. 禁止只寫技術面廢話。"
        )
        ai_json_str = call_gemini_json(f"股票清單: {json.dumps(stocks_payload, ensure_ascii=False)}", system_instruction=sys_prompt)
        
        reasons_map = {}
        if ai_json_str:
            try:
                ai_data = json.loads(ai_json_str)
                items = ai_data if isinstance(ai_data, list) else ai_data.get('stocks', [])
                for item in items: reasons_map[item.get('name')] = item.get('reason', '產業趨勢向上。')
            except: pass

        bubbles = []
        for stock in good_stocks:
            reason = reasons_map.get(stock['name'], f"受惠{stock['sector']}需求，籌碼集中。")
            bubble = {
                "type": "bubble", "size": "kilo", # 🔥 改為 Kilo (260px)
                "header": {
                    "type": "box", "layout": "vertical", 
                    "contents": [
                        {"type": "text", "text": f"{stock['name']} ({stock['sector']})", "weight": "bold", "size": "lg", "color": "#ffffff"}, # 字體微調適應窄卡片
                        {"type": "text", "text": f"{stock['code']} | {stock['signal_str']}", "size": "xxs", "color": "#eeeeee"}
                    ], "backgroundColor": stock['color']
                },
                "body": {"type": "box", "layout": "vertical", "contents": [
                    {"type": "text", "text": str(stock['close']), "weight": "bold", "size": "3xl", "color": stock['color'], "align": "center"},
                    {"type": "text", "text": stock['change_display'], "size": "xs", "color": stock['color'], "align": "center"},
                    {"type": "text", "text": f"💰{stock['tag']}", "size": "xs", "color": "#555555", "align": "center", "margin": "md"},
                    {"type": "separator", "margin": "md"},
                    {"type": "text", "text": reason, "size": "xs", "color": "#333333", "wrap": True, "margin": "md"},
                    {"type": "button", "action": {"type": "message", "label": "詳細診斷", "text": stock['code']}, "style": "link", "margin": "md"}
                ]}
            }
            bubbles.append(bubble)
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="AI 精選強勢股", contents={"type": "carousel", "contents": bubbles}))
        return

    # [功能 2] 個股診斷 / 持股診斷 (Concise Cost Mode)
    stock_id = get_stock_id(msg)
    user_cost = None
    cost_match = re.search(r'(成本|cost)[:\s]*(\d+\.?\d*)', msg, re.IGNORECASE)
    if cost_match: user_cost = float(cost_match.group(2))

    if stock_id:
        name = CODE_TO_NAME.get(stock_id, stock_id)
        data = fetch_data_light(stock_id) 
        if not data: return
        
        # 如果是問成本，就不抓籌碼與 EPS，加速回應並保持簡潔
        if user_cost:
            profit_pct = round((data['close'] - user_cost) / user_cost * 100, 1)
            profit_status = "獲利" if profit_pct > 0 else "虧損"
            profit_icon = "💰" if profit_pct > 0 else "💸"
            
            # 專用的簡潔 Prompt
            sys_prompt = "你是嚴格的操盤手。使用者持有股票。請回傳JSON。屬性: analysis(30字內簡述籌碼/技術現況), action(建議:🔴續抱/🟡減碼/⚫停損), strategy(明確的停利價與停損價)。"
            user_prompt = f"標的:{name}, 現價:{data['close']}, 成本:{user_cost}"
            
            json_str = call_gemini_json(user_prompt, system_instruction=sys_prompt)
            try:
                res = json.loads(json_str)
                # 🔥 極簡回覆格式
                reply = (
                    f"🩺 **持股診斷：{name}({stock_id})**\n"
                    f"{profit_icon} 帳面：{profit_status} {profit_pct}% (現價 {data['close']})\n"
                    f"------------------\n"
                    f"【診斷】{res['action']}\n"
                    f"【分析】{res['analysis']}\n"
                    f"【策略】{res['strategy']}\n"
                    f"------------------\n"
                    f"(系統: {BOT_VERSION})"
                )
            except: reply = "AI 數據解析失敗。"
            
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        # 如果沒問成本 (一般查詢)，顯示完整 Dashboard
        tf, tt, af, at = fetch_chips_accumulate(stock_id)
        eps = fetch_eps(stock_id)
        signals = get_technical_signals(data, af+at)
        signal_str = " | ".join(signals)
        
        cache_key = f"{stock_id}_query"
        ai_reply_text = get_cached_ai_response(cache_key)
        
        if not ai_reply_text:
            sys_prompt = "你是股市判官。請回傳 JSON。屬性: analysis (100字內), advice (🔴進場 / 🟡觀望 / ⚫不可進場), target_price, stop_loss。"
            user_prompt = f"標的:{name}, 現價:{data['close']}, 訊號:{signal_str}, 外資:{af}張"
            json_str = call_gemini_json(user_prompt, system_instruction=sys_prompt)
            try:
                res = json.loads(json_str)
                advice_str = f"【建議】{res['advice']}"
                if "進場" in res['advice']:
                    advice_str += f"\n🎯停利：{res.get('target_price','N/A')} | 🛑停損：{res.get('stop_loss','N/A')}"
                ai_reply_text = f"【分析】{res['analysis']}\n{advice_str}"
            except: ai_reply_text = "AI 數據解析失敗。"
            if "解析失敗" not in ai_reply_text: set_cached_ai_response(cache_key, ai_reply_text)

        data_dashboard = f"💰 現價：{data['close']} {data['change_display']}\n📊 週: {data['ma5']} | 月: {data['ma20']}\n🏦 外資: {af} | 投信: {at}\n💎 EPS: {eps}"
        cta = f"💡 輸入『{name}成本xxx』AI 幫你算！"
        reply = f"📈 **{name}({stock_id})**\n{data_dashboard}\n------------------\n🚩 **指標快篩** :\n{signal_str}\n------------------\n{ai_reply_text}\n------------------\n{cta}\n(系統: {BOT_VERSION})"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
