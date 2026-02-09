import os, requests, json, time, re, threading, random, concurrent.futures
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 🟢 [版本號] v14.2 (Real-Time Final)
BOT_VERSION = "v14.2 (RT-Final)"

# --- 1. 載入清單 ---
STOCK_MAP = {}
try:
    if os.path.exists('stock_list.json'):
        with open('stock_list.json', 'r', encoding='utf-8') as f:
            STOCK_MAP = json.load(f)
except: pass

if not STOCK_MAP:
    STOCK_MAP = {"台積電": "2330", "鴻海": "2317", "南電": "8046"}
CODE_TO_NAME = {v: k for k, v in STOCK_MAP.items()}

# --- 2. 快取 ---
DATA_CACHE = {}
CACHE_LOCK = threading.Lock()

def get_cache(stock_id):
    with CACHE_LOCK:
        if stock_id in DATA_CACHE:
            entry = DATA_CACHE[stock_id]
            if time.time() < entry['expire']: return entry['data']
            else: del DATA_CACHE[stock_id]
    return None

def set_cache(stock_id, data, ttl=120): # 縮短快取時間確保即時性
    with CACHE_LOCK:
        DATA_CACHE[stock_id] = {"data": data, "expire": time.time() + ttl}

# --- 3. Line 設定 ---
token = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
secret = os.environ.get('LINE_CHANNEL_SECRET')
line_bot_api = LineBotApi(token if token else 'UNKNOWN')
handler = WebhookHandler(secret if secret else 'UNKNOWN')

# --- 4. 核心：官方即時股價抓取 (免 twstock) ---
def get_realtime_price_official(stock_id):
    """直接對接證交所/上櫃即時 API"""
    # 試試上市
    try:
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw"
        res = requests.get(url, timeout=5).json()
        if res.get('msgArray'):
            info = res['msgArray'][0]
            # z 為成交價, y 為昨收
            p = info.get('z', info.get('y'))
            if p == '-': p = info.get('y')
            return float(p)
    except: pass
    # 試試上櫃
    try:
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=otc_{stock_id}.tw"
        res = requests.get(url, timeout=5).json()
        if res.get('msgArray'):
            info = res['msgArray'][0]
            p = info.get('z', info.get('y'))
            if p == '-': p = info.get('y')
            return float(p)
    except: pass
    return None

# --- 5. Gemini 核心 (強化解析與還原 v10.4 格式) ---
def call_gemini_v14(prompt, mode="NORMAL"):
    keys = [os.environ.get(f'GEMINI_API_KEY_{i}') for i in range(1, 7) if os.environ.get(f'GEMINI_API_KEY_{i}')]
    if not keys and os.environ.get('GEMINI_API_KEY'): keys = [os.environ.get('GEMINI_API_KEY')]
    if not keys: return {"error": "No Keys"}
    random.shuffle(keys)
    
    # 模式 Prompt 優化：還原 v10.4 的「文字建議」格式
    if mode == "COST":
        final_prompt = prompt + "\n🔴 JSON ONLY. Keys: 'diagnosis' (續抱/加碼/減碼/停損/停利), 'reason' (max 30 words), 'target_text' (e.g. 停利:400元/防守:340元)."
    else:
        final_prompt = prompt + "\n🔴 JSON ONLY. Keys: 'trend' (e.g. 趨勢向上), 'reason' (max 50 words), 'action' (買進/觀望/賣出), 'advice_text' (e.g. 支撐340元，壓力410元)."

    for model in ["gemini-1.5-flash", "gemini-2.0-flash"]:
        for key in keys:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
                payload = {"contents": [{"parts": [{"text": final_prompt}]}], "generationConfig": {"temperature": 0.2}}
                res = requests.post(url, json=payload, timeout=20)
                if res.status_code == 200:
                    t = res.json()['candidates'][0]['content']['parts'][0]['text']
                    clean = re.sub(r'```json|```', '', t).strip()
                    return json.loads(clean)
            except: continue
    return {"error": "AI Busy"}

# --- 6. 數據整合 ---
def fetch_all_data(stock_id):
    cached = get_cache(stock_id)
    if cached: return cached

    token = os.environ.get('FINMIND_TOKEN', '')
    fin_url = "https://api.finmindtrade.com/api/v4/data"
    
    try:
        # A. 即時價格 (絕對精準)
        rt_price = get_realtime_price_official(stock_id)
        
        # B. 歷史數據 (均線與籌碼)
        res = requests.get(fin_url, params={"dataset": "TaiwanStockPrice", "data_id": stock_id, "start_date": (datetime.now()-timedelta(days=90)).strftime('%Y-%m-%d'), "token": token}, timeout=5).json()
        hist = res.get('data', [])
        if not hist: return None
        
        # 若即時抓不到，才用昨日收盤
        curr_p = rt_price if rt_price else hist[-1]['close']
        
        closes = [d['close'] for d in hist]
        ma5 = round(sum(closes[-5:]) / 5, 2)
        ma20 = round(sum(closes[-20:]) / 20, 2)
        ma60 = round(sum(closes[-60:]) / 60, 2)
        
        # C. 籌碼 (格式還原 v10.4)
        c_res = requests.get(fin_url, params={"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": stock_id, "start_date": (datetime.now()-timedelta(days=12)).strftime('%Y-%m-%d'), "token": token}, timeout=5).json()
        chips = c_res.get('data', [])
        dates = sorted(list(set([d['date'] for d in chips])), reverse=True)
        recent_5 = dates[:5]
        f_lat = sum([d['buy']-d['sell'] for d in chips if d['date']==dates[0] and d['name']=='Foreign_Investor']) // 1000
        f_sum5 = sum([d['buy']-d['sell'] for d in chips if d['date'] in recent_5 and d['name']=='Foreign_Investor']) // 1000
        t_lat = sum([d['buy']-d['sell'] for d in chips if d['date']==dates[0] and d['name']=='Investment_Trust']) // 1000
        t_sum5 = sum([d['buy']-d['sell'] for d in chips if d['date'] in recent_5 and d['name']=='Investment_Trust']) // 1000
        
        # D. EPS
        e_res = requests.get(fin_url, params={"dataset": "TaiwanStockFinancialStatements", "data_id": stock_id, "start_date": "2024-01-01", "token": token}, timeout=5).json()
        eps_data = [d for d in e_res.get('data', []) if d['type']=='EPS']
        eps_val = eps_data[-1]['value'] if eps_data else "N/A"

        # E. 訊號快篩
        sigs = []
        if curr_p > ma20 and ma20 > ma60: sigs.append("📈**月線翻揚** (趨勢向上)")
        elif curr_p > ma20: sigs.append("📈**站上月線** (短線轉強)")
        elif curr_p < ma20: sigs.append("📉**跌破月線** (趨勢轉弱)")
        
        bias = ((curr_p - ma20) / ma20) * 100
        if bias > 5: sigs.append("🔥**乖離過大** (防回檔)")
        
        if f_sum5 > 0 and t_sum5 > 0: sigs.append("💰**籌碼集中** (波段偏多)")

        result = {
            "id": stock_id, "close": curr_p, "ma5": ma5, "ma20": ma20, "ma60": ma60,
            "f_lat": f_lat, "f_sum5": f_sum5, "t_lat": t_lat, "t_sum5": t_sum5,
            "eps": eps_val, "sigs": sigs
        }
        set_cache(stock_id, result)
        return result
    except: return None

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
    cost_m = re.match(r'^([A-Z0-9\u4e00-\u9fa5]+)\s*成本\s*(\d+(?:\.\d+)?)$', msg)
    
    # 找 ID
    raw_n = cost_m.group(1) if cost_m else msg
    sid = next((k for k, v in STOCK_MAP.items() if v == raw_n or k == raw_n), None)
    if not sid: sid = raw_n if raw_n.isdigit() and len(raw_n)==4 else STOCK_MAP.get(raw_n)
    
    if not sid: return

    data = fetch_all_data(sid)
    if not data:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 查無數據"))
        return
    
    name = CODE_TO_NAME.get(sid, sid)

    if cost_m:
        # === 🌟 診斷模式 (復刻 v10.4) ===
        cost = float(cost_match.group(2)) if 'cost_match' in locals() else float(cost_m.group(2))
        p_pct = round(((data['close']-cost)/cost)*100, 2)
        status = "獲利" if p_pct>0 else "虧損"
        icon = "🔴" if p_pct>0 else "🟢"
        prompt = f"持有{name}({sid}),成本{cost},現價{data['close']}({status}{p_pct}%)。分析續抱或停損。"
        ai = call_gemini_v14(prompt, mode="COST")
        reply = (
            f"🩺 **持股診斷: {name}({sid})**\n"
            f"💰 帳面: {status} {p_pct}% (現價 {data['close']})\n"
            f"------------------\n"
            f"【診斷】 {icon}{ai.get('diagnosis', '續抱')} - {ai.get('reason', '...')}\n"
            f"【策略】 {ai.get('target_text', '-')}\n"
            f"------------------\n(系統: {BOT_VERSION})"
        )
    else:
        # === 🌟 一般模式 (復刻 v10.4) ===
        prompt = f"標的{name}({sid}),現價{data['close']},均線{data['ma5']}/{data['ma20']}/{data['ma60']},籌碼外資{data['f_lat']},投信{data['t_lat']}。分析趨勢。"
        ai = call_gemini_v14(prompt, mode="NORMAL")
        sigs = "\n".join([f"  {s}" for s in data['sigs']]) if data['sigs'] else "  (無顯著訊號)"
        act_icon = "🔴" if "買" in ai.get('action','') else "🟢" if "賣" in ai.get('action','') else "🟡"
        
        reply = (
            f"📊 **{name}({sid})**\n"
            f"💰 現價: {data['close']}\n"
            f"⚡週: {data['ma5']} | 月: {data['ma20']} | 季: {data['ma60']}\n"
            f"🤝外資: {data['f_lat']} (5日: {data['f_sum5']})\n"
            f"🏦投信: {data['t_lat']} (5日: {data['t_sum5']})\n"
            f"💎 2025累計EPS {data['eps']}元\n"
            f"------------------\n"
            f"🚩 **訊號快篩**:\n{sigs}\n"
            f"------------------\n"
            f"【AI總結】 {act_icon}{ai.get('action', '觀望')}\n"
            f"【分析】 {ai.get('reason', '...')}\n"
            f"【建議】 {ai.get('advice_text', '-')}\n"
            f"------------------\n"
            f"(系統: {BOT_VERSION})\n💡 輸入『{name}成本xxx』AI 幫你算！"
        )
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))
