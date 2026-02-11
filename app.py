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

# 🟢 [版本號] v16.1 (Model Fix + Prompt Upgrade)
BOT_VERSION = "v16.1 (修復版)"

# --- 1. 全域快取與設定 ---
AI_RESPONSE_CACHE = {}
# TWSE 全市場掃描快取
TWSE_CACHE = {"date": "", "data": []}

# 🔥 ETF 屬性資料庫
ETF_META = {
    "00878": {"name": "國泰永續高股息", "type": "高股息", "focus": "ESG/殖利率/填息"},
    "0056":  {"name": "元大高股息", "type": "高股息", "focus": "預測殖利率/填息"},
    "00919": {"name": "群益台灣精選高息", "type": "高股息", "focus": "殖利率/航運半導體週期"},
    "00929": {"name": "復華台灣科技優息", "type": "高股息", "focus": "月配息/科技股景氣"},
    "00713": {"name": "元大台灣高息低波", "type": "高股息", "focus": "低波動/防禦性"},
    "00940": {"name": "元大台灣價值高息", "type": "高股息", "focus": "月配息/價值投資"},
    "00939": {"name": "統一台灣高息動能", "type": "高股息", "focus": "動能指標/月底領息"},
    "0050":  {"name": "元大台灣50", "type": "市值型", "focus": "大盤乖離/台積電展望"},
    "006208":{"name": "富邦台50", "type": "市值型", "focus": "大盤乖離/台積電展望"},
    "00881": {"name": "國泰台灣5G+", "type": "科技型", "focus": "半導體/通訊供應鏈/台積電"},
    "00679B":{"name": "元大美債20年", "type": "債券型", "focus": "美債殖利率/降息預期"},
    "00687B":{"name": "國泰20年美債", "type": "債券型", "focus": "美債殖利率/降息預期"}
}

# 菁英池 (備用方案)
ELITE_STOCK_DATA = {
    "台積電": {"code": "2330", "sector": "半導體"}, "鴻海": {"code": "2317", "sector": "AI伺服器"},
    "聯發科": {"code": "2454", "sector": "IC設計"}, "廣達": {"code": "2382", "sector": "AI伺服器"},
    "緯創": {"code": "3231", "sector": "AI伺服器"}, "技嘉": {"code": "2376", "sector": "板卡"},
    "長榮": {"code": "2603", "sector": "航運"}, "陽明": {"code": "2609", "sector": "航運"},
    "華城": {"code": "1519", "sector": "重電"}, "士電": {"code": "1503", "sector": "重電"},
    "奇鋐": {"code": "3017", "sector": "散熱"}, "雙鴻": {"code": "3324", "sector": "散熱"}
}
ELITE_STOCK_POOL = {k: v["code"] for k, v in ELITE_STOCK_DATA.items()}
ALL_STOCK_MAP = ELITE_STOCK_POOL.copy()

try:
    if os.path.exists('stock_list.json'):
        with open('stock_list.json', 'r', encoding='utf-8') as f:
            full_list = json.load(f)
            ALL_STOCK_MAP.update(full_list)
except: pass

CODE_TO_NAME = {v: k for k, v in ALL_STOCK_MAP.items()}

token = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
secret = os.environ.get('LINE_CHANNEL_SECRET')
line_bot_api = LineBotApi(token if token else 'UNKNOWN')
handler = WebhookHandler(secret if secret else 'UNKNOWN')

@app.route("/")
def health_check(): return f"OK ({BOT_VERSION})", 200

# --- 2. 核心：全市場掃描與數據引擎 ---

def get_taiwan_time_str():
    utc_now = datetime.now(timezone.utc)
    tw_time = utc_now + timedelta(hours=8)
    return tw_time.strftime('%H:%M:%S')

# TWSE 全市場掃描 (量能趨勢版)
def fetch_twse_candidates():
    global TWSE_CACHE
    tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
    if tw_now.hour < 14: 
        target_date = (tw_now - timedelta(days=1)).strftime('%Y%m%d')
    else:
        target_date = tw_now.strftime('%Y%m%d')

    if TWSE_CACHE['date'] == target_date and TWSE_CACHE['data']:
        return TWSE_CACHE['data']

    print(f"[System] 啟動 TWSE 掃描，目標: {target_date}")
    url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&type=ALLBUT0999&date={target_date}"
    
    try:
        res = requests.get(url, timeout=6)
        data = res.json()
        if data.get('stat') != 'OK': return []

        target_table = None
        if 'tables' in data:
            for table in data['tables']:
                if '每日收盤行情' in table.get('title', '') or '證券代號' in table.get('fields', []):
                    target_table = table
                    break
        elif 'data9' in data:
            target_table = {'data': data['data9'], 'fields': data.get('fields9', [])}

        if not target_table: return []

        raw_data = target_table['data']
        fields = target_table['fields']
        
        try:
            idx_code = fields.index("證券代號")
            idx_vol = fields.index("成交股數")
            idx_price = fields.index("收盤價")
            idx_sign = fields.index("漲跌(+/-)")
        except:
            idx_code, idx_vol, idx_price, idx_sign = 0, 2, 8, 9

        candidates = []
        for row in raw_data:
            try:
                code = row[idx_code]
                if code.startswith('00') or code.startswith('91'): continue
                
                vol = float(row[idx_vol].replace(',', ''))
                price_str = row[idx_price].replace(',', '')
                if price_str == '--' or vol == 0: continue
                
                price = float(price_str)
                if price < 10: continue
                
                sign = row[idx_sign]
                is_up = ('+' in sign) or ('red' in sign)
                
                if is_up and vol > 2000000: # 策略：紅盤且量大
                    candidates.append({"code": code, "vol": vol})
            except: continue
        
        candidates.sort(key=lambda x: x['vol'], reverse=True)
        final_list = [x['code'] for x in candidates[:50]]
        
        if final_list:
            TWSE_CACHE = {"date": target_date, "data": final_list}
            print(f"[System] 掃描完成，鎖定 {len(final_list)} 檔熱門股")
            return final_list

    except Exception as e:
        print(f"[Error] TWSE Scan: {e}")
    
    return []

# 技術指標
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

def calculate_cdp(high, low, close):
    cdp = (high + low + (close * 2)) / 4
    nh = (cdp * 2) - low
    nl = (cdp * 2) - high
    return int(nh), int(nl)

def get_technical_signals(data, chips_val):
    signals = []
    closes = data['raw_closes']; highs = data['raw_highs']; lows = data['raw_lows']
    volumes = data['raw_volumes']
    
    rsi = calculate_rsi(closes)
    k, d = calculate_kd(highs, lows, closes)
    ma5 = data['ma5']; ma20 = data['ma20']; ma60 = data['ma60']; close = data['close']
    
    if rsi > 75: signals.append("🔥RSI過熱")
    elif rsi < 25: signals.append("💎RSI超賣")
    
    bias_20 = (close - ma20) / ma20 * 100
    if bias_20 > 15: signals.append("⚠️乖離過大")
    
    if len(volumes) >= 6:
        avg_vol = sum(volumes[-6:-1]) / 5
        if avg_vol > 0 and volumes[-1] > avg_vol * 1.5 and close > data['open']: signals.append("🚀量增價漲")
    
    if k > 80: signals.append("📈KD高檔")
    elif k < 20: signals.append("📉KD低檔")
    
    if chips_val > 1000: signals.append("💰外資大買")
    elif chips_val < -1000: signals.append("💸外資大賣")
    
    if close > ma5 > ma20 > ma60: signals.append("🟢三線多頭")
    elif close < ma5 < ma20 < ma60: signals.append("🔴三線空頭")
    
    unique_signals = list(set(signals))
    if not unique_signals: unique_signals = ["🟡趨勢盤整"]
    return unique_signals[:3]

# --- 3. 智慧快取與 API (Gemini/FinMind) ---
def get_smart_cache_ttl():
    utc_now = datetime.now(timezone.utc)
    tw_now = utc_now + timedelta(hours=8)
    if dtime(9, 0) <= tw_now.time() <= dtime(13, 30): return 60 
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

# 🔥 [修正] 恢復您指定的模型清單，確保 AI 正常運作
def call_gemini_json(prompt, system_instruction=None):
    keys = [os.environ.get(f'GEMINI_API_KEY_{i}') for i in range(1, 7) if os.environ.get(f'GEMINI_API_KEY_{i}')]
    if not keys and os.environ.get('GEMINI_API_KEY'): keys = [os.environ.get('GEMINI_API_KEY')]
    if not keys: return None
    random.shuffle(keys)
    
    # 您的指定模型清單 (Gemini 2.0 Flash Exp 被移除)
    target_models = ["gemini-3-flash-preview", "gemini-2.5-flash", "gemini-2.5-flash-lite"]
    
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
            except: continue
    return None

# 數據縫合 (Data Stitching)
def fetch_data_light(stock_id):
    token = os.environ.get('FINMIND_TOKEN', '')
    url_hist = "https://api.finmindtrade.com/api/v4/data"
    
    try:
        start = (datetime.now() - timedelta(days=120)).strftime('%Y-%m-%d')
        res = requests.get(url_hist, params={
            "dataset": "TaiwanStockPrice", "data_id": stock_id, "start_date": start, "token": token
        }, timeout=5)
        hist_data = res.json().get('data', [])
    except: hist_data = []

    if not hist_data: return None

    latest_price = 0
    source_name = "歷史"
    update_time = get_taiwan_time_str()
    
    try:
        stock_rt = twstock.realtime.get(stock_id)
        if stock_rt['success']:
            real_price = stock_rt['realtime']['latest_trade_price']
            rt_time = stock_rt['realtime'].get('latest_trade_time', '')
            if rt_time: update_time = rt_time 
            
            if real_price and real_price != "-":
                latest_price = float(real_price)
                source_name = "TWSE"
            else:
                bid = stock_rt['realtime']['best_bid_price'][0]
                ask = stock_rt['realtime']['best_ask_price'][0]
                if bid and ask and bid != "-" and ask != "-":
                    latest_price = round((float(bid) + float(ask)) / 2, 2)
                    source_name = "TWSE(試)"
    except: pass

    if latest_price == 0:
        latest_price = hist_data[-1]['close']

    closes = [d['close'] for d in hist_data]
    highs = [d['max'] for d in hist_data]
    lows = [d['min'] for d in hist_data]
    volumes = [d['Trading_Volume'] for d in hist_data]

    today_str = datetime.now().strftime('%Y-%m-%d')
    hist_last_date = hist_data[-1]['date']

    if hist_last_date != today_str:
        closes.append(latest_price)
        highs.append(latest_price)
        lows.append(latest_price)
        volumes.append(0)
    else:
        closes[-1] = latest_price

    ma5 = round(sum(closes[-5:]) / 5, 2) if len(closes) >= 5 else 0
    ma20 = round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else 0
    ma60 = round(sum(closes[-60:]) / 60, 2) if len(closes) >= 60 else 0

    prev_close = closes[-2] if len(closes) > 1 else latest_price
    change = latest_price - prev_close
    change_pct = round(change / prev_close * 100, 2) if prev_close > 0 else 0
    sign = "+" if change > 0 else ""
    color = "#D32F2F" if change >= 0 else "#2E7D32"

    last_day = hist_data[-1]
    res_price, sup_price = calculate_cdp(last_day['max'], last_day['min'], last_day['close'])

    return {
        "code": stock_id, 
        "close": latest_price, 
        "update_time": f"{update_time} ({source_name})",
        "resistance": res_price, "support": sup_price,
        "ma5": ma5, "ma20": ma20, "ma60": ma60,
        "change_display": f"({sign}{round(change, 2)}, {sign}{change_pct}%)", 
        "color": color,
        "raw_closes": closes, "raw_highs": highs, "raw_lows": lows, "raw_volumes": volumes,
        "open": hist_data[-1]['open']
    }

def fetch_chips_accumulate(stock_id):
    token = os.environ.get('FINMIND_TOKEN', '')
    url = "https://api.finmindtrade.com/api/v4/data"
    try:
        start = (datetime.now() - timedelta(days=15)).strftime('%Y-%m-%d')
        res = requests.get(url, params={"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": stock_id, "start_date": start, "token": token}, timeout=5)
        data = res.json().get('data', [])
        if not data: return "0 (5日: 0)", "0 (5日: 0)", 0, 0
        unique_dates = sorted(list(set([d['date'] for d in data])), reverse=True)
        latest_date = unique_dates[0] if unique_dates else ""
        target_dates = unique_dates[:5]
        today_f = 0; acc_f = 0; today_t = 0; acc_t = 0
        for row in data:
            if row['date'] in target_dates:
                val = (row['buy'] - row['sell']) // 1000
                if row['name'] == 'Foreign_Investor':
                    acc_f += val
                    if row['date'] == latest_date: today_f = val
                elif row['name'] == 'Investment_Trust':
                    acc_t += val
                    if row['date'] == latest_date: today_t = val
        return f"{today_f} (5日: {acc_f})", f"{today_t} (5日: {acc_t})", acc_f, acc_t
    except: return "N/A", "N/A", 0, 0

def fetch_dividend_yield(stock_id, current_price):
    token = os.environ.get('FINMIND_TOKEN', '')
    try:
        start = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
        res = requests.get("https://api.finmindtrade.com/api/v4/data", params={"dataset": "TaiwanStockDividend", "data_id": stock_id, "start_date": start, "token": token}, timeout=5)
        data = res.json().get('data', [])
        total_dividend = sum([float(d.get('CashEarningsDistribution', 0)) for d in data])
        if total_dividend > 0 and current_price > 0:
            return f"{round((total_dividend / current_price) * 100, 2)}%"
        else: return "N/A"
    except: return "N/A"

def fetch_eps(stock_id):
    if stock_id.startswith("00"): return "ETF"
    token = os.environ.get('FINMIND_TOKEN', '')
    start = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d')
    try:
        res = requests.get("https://api.finmindtrade.com/api/v4/data", params={"dataset": "TaiwanStockFinancialStatements", "data_id": stock_id, "start_date": start, "token": token}, timeout=5)
        data = res.json().get('data', [])
        eps_data = [d for d in data if d['type'] == 'EPS']
        if not eps_data: return "N/A"
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
        if data['close'] > data['ma20']:
            f_str, t_str, af_val, at_val = fetch_chips_accumulate(code) 
            chips_sum = af_val + at_val
            
            is_hot = chips_sum > 50 or (data['close'] > data['ma5'] and data['close'] > data['ma60'])
            
            if is_hot:
                name = CODE_TO_NAME.get(code, code)
                sector = ELITE_STOCK_DATA.get(name, {}).get('sector', '熱門股')
                
                signals = get_technical_signals(data, chips_sum)
                signal_str = " | ".join(signals)
                
                return {
                    "code": code, "name": name, "sector": sector,
                    "close": data['close'], "change_display": data['change_display'], "color": data['color'],
                    "chips": f"{chips_sum}張", "signal_str": signal_str,
                    "tag": "外資大買" if af_val > at_val else "主力控盤"
                }
    except: return None
    return None

def scan_recommendations_turbo(target_sector=None):
    candidates_pool = []
    
    if target_sector:
        pool = [v['code'] for k, v in ELITE_STOCK_DATA.items() if target_sector in v['sector']]
        if pool: candidates_pool = pool
    else:
        twse_list = fetch_twse_candidates()
        if twse_list:
            candidates_pool = twse_list[:20]
        else:
            elite_codes = [v['code'] for v in ELITE_STOCK_DATA.values()]
            candidates_pool = random.sample(elite_codes, 20) if len(elite_codes) > 20 else elite_codes
    
    candidates = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = executor.map(check_stock_worker_turbo, candidates_pool)
    
    for res in results:
        if res: candidates.append(res)
        if len(candidates) >= 5: break
        
    return candidates

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
    
    # [功能 1] 推薦選股 (修正：AI 推薦理由)
    if msg.startswith("推薦") or msg.startswith("選股"):
        parts = msg.split()
        target_sector = parts[1] if len(parts) > 1 else None
        
        good_stocks = scan_recommendations_turbo(target_sector)
        
        if not good_stocks:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 市場震盪，暫無符合強勢條件的標的。"))
            return
            
        # 🔥 [修正] AI 潤飾理由 Prompt，確保 JSON key 匹配
        stocks_payload = [{"code": s['code'], "name": s['name'], "signal": s['signal_str'], "sector": s['sector']} for s in good_stocks]
        
        sys_prompt = (
            "你是資深股市分析師。請分析清單中的股票。"
            "回傳 JSON 格式：[{'code': '股票代號', 'reason': '20字內短評'}]。"
            "規則：必須結合『產業趨勢』或『技術突破』，語氣專業，不要只寫籌碼集中。"
            "例如：AI伺服器需求爆發，量價齊揚突破前高。"
        )
        ai_json_str = call_gemini_json(f"清單: {json.dumps(stocks_payload, ensure_ascii=False)}", system_instruction=sys_prompt)
        
        reasons_map = {}
        try:
            ai_data = json.loads(ai_json_str)
            items = ai_data if isinstance(ai_data, list) else ai_data.get('stocks', [])
            for item in items: 
                # 確保用 code 對應，避免名稱不一致
                reasons_map[item.get('code')] = item.get('reason', '動能強勁。')
        except: pass

        bubbles = []
        for stock in good_stocks:
            # 🔥 [修正] 若 AI 失敗，備案改為更豐富的技術描述
            default_reason = f"主力控盤，{stock['signal_str']}，多頭排列。"
            reason = reasons_map.get(stock['code'], default_reason)
            
            bubble = {
                "type": "bubble", "size": "kilo",
                "header": {
                    "type": "box", "layout": "vertical", 
                    "contents": [
                        {"type": "text", "text": f"{stock['name']} ({stock['code']})", "weight": "bold", "size": "lg", "color": "#ffffff"},
                        {"type": "text", "text": f"{stock['sector']} | {stock['tag']}", "size": "xxs", "color": "#eeeeee"}
                    ], "backgroundColor": stock['color']
                },
                "body": {"type": "box", "layout": "vertical", "contents": [
                    {"type": "text", "text": str(stock['close']), "weight": "bold", "size": "3xl", "color": stock['color'], "align": "center"},
                    {"type": "text", "text": stock['change_display'], "size": "xs", "color": stock['color'], "align": "center"},
                    {"type": "separator", "margin": "md"},
                    {"type": "text", "text": reason, "size": "xs", "color": "#333333", "wrap": True, "margin": "md"},
                    {"type": "button", "action": {"type": "message", "label": "詳細診斷", "text": stock['code']}, "style": "link", "margin": "md"}
                ]}
            }
            bubbles.append(bubble)
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="AI 精選飆股", contents={"type": "carousel", "contents": bubbles}))
        return

    # [功能 2] 個股/ETF 診斷 (AI 正常化)
    stock_id = get_stock_id(msg)
    user_cost = None
    cost_match = re.search(r'(成本|cost)[:\s]*(\d+\.?\d*)', msg, re.IGNORECASE)
    if cost_match: user_cost = float(cost_match.group(2))

    if stock_id:
        name = CODE_TO_NAME.get(stock_id, stock_id)
        if stock_id in ETF_META: name = ETF_META[stock_id]['name']

        data = fetch_data_light(stock_id) 
        if not data: return
        
        is_etf = stock_id.startswith("00")
        
        if user_cost:
            profit_pct = round((data['close'] - user_cost) / user_cost * 100, 1)
            sys_prompt = "你是操盤手。回傳JSON: analysis(30字內), action(🔴續抱/🟡減碼/⚫停損), strategy(操作建議)。"
            user_prompt = f"標的:{name}, 現價:{data['close']}, 成本:{user_cost}, 均線:{data['ma5']}/{data['ma60']}"
            json_str = call_gemini_json(user_prompt, system_instruction=sys_prompt)
            try:
                res = json.loads(json_str)
                reply = f"🩺 **{name}診斷**\n💰 帳面: {profit_pct}%\n【建議】{res['action']}\n【分析】{res['analysis']}\n【策略】{res['strategy']}"
            except: reply = "AI 數據解析失敗 (請檢查 Key)。"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        f_str, t_str, af_val, at_val = fetch_chips_accumulate(stock_id) 
        eps = fetch_eps(stock_id)
        yield_rate = fetch_dividend_yield(stock_id, data['close'])
        signals = get_technical_signals(data, af_val + at_val)
        signal_str = " | ".join(signals)
        
        cache_key = f"{stock_id}_query"
        ai_reply_text = get_cached_ai_response(cache_key)
        
        if not ai_reply_text:
            sys_prompt = (
                "你是資深操盤手。請回傳 JSON: analysis (100字內), advice (🔴進場 / 🟡觀望 / ⚫避開), target_price, stop_loss。"
                "規則：1. 若現價站上 MA5 與 MA20，視為強勢。2. 若外資大賣且破線，請示警。"
            )
            user_prompt = f"標的:{name}, 現價:{data['close']}, MA5:{data['ma5']}, MA20:{data['ma20']}, 訊號:{signal_str}, 外資:{f_str}"
            json_str = call_gemini_json(user_prompt, system_instruction=sys_prompt)
            try:
                res = json.loads(json_str)
                advice_str = f"【建議】{res['advice']}\n🎯目標：{res.get('target_price','N/A')} | 🛑防守：{res.get('stop_loss','N/A')}"
                ai_reply_text = f"【分析】{res['analysis']}\n{advice_str}"
            except: ai_reply_text = "AI 數據解析失敗 (連線異常)。"
            if "解析失敗" not in ai_reply_text: set_cached_ai_response(cache_key, ai_reply_text)

        indicator_line = f"💎 殖利率: {yield_rate}" if is_etf else f"💎 EPS: {eps}"
        
        data_dashboard = (
            f"💰 現價:{data['close']} {data['change_display']} 🕒{data['update_time']}\n"
            f"📊 均線: 週:{data['ma5']} | 月:{data['ma20']} | 季:{data['ma60']}\n" 
            f"✈️ 外資: {f_str}\n"
            f"🤝 投信: {t_str}\n"
            f"{indicator_line}"
        )
        
        reply = f"📈 **{name}({stock_id})**\n{data_dashboard}\n------------------\n🚩 **指標快篩** :\n{signal_str}\n------------------\n{ai_reply_text}\n------------------\n💡 輸入『推薦』查看今日熱門飆股！\n(系統: {BOT_VERSION})"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
