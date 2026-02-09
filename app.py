import os, requests, json, time, re, threading, random, concurrent.futures
import twstock # 🟢 新增：即時股價套件
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 🟢 [版本號] v14.1 (Real-Time)
BOT_VERSION = "v14.1 (Real-Time)"

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

def set_cache(stock_id, data, ttl=300):
    with CACHE_LOCK:
        DATA_CACHE[stock_id] = {"data": data, "expire": time.time() + ttl}

# --- 3. Line 設定 ---
token = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
secret = os.environ.get('LINE_CHANNEL_SECRET')
line_bot_api = LineBotApi(token if token else 'UNKNOWN')
handler = WebhookHandler(secret if secret else 'UNKNOWN')

# --- 4. Gemini 核心 (增強解析) ---
def call_gemini_v14(prompt, mode="NORMAL"):
    keys = [os.environ.get(f'GEMINI_API_KEY_{i}') for i in range(1, 7) if os.environ.get(f'GEMINI_API_KEY_{i}')]
    if not keys and os.environ.get('GEMINI_API_KEY'): keys = [os.environ.get('GEMINI_API_KEY')]
    if not keys: return {"error": "No Keys"}
    random.shuffle(keys)

    target_models = ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-flash-latest"]
    
    # Prompt 優化：要求更嚴格的 JSON
    if mode == "COST":
        final_prompt = prompt + """
        🔴 Output strict JSON. No Markdown.
        Keys: "diagnosis" (續抱/加碼/減碼/停損/停利), "reason" (max 30 words), "target_price", "stop_loss".
        """
    else:
        final_prompt = prompt + """
        🔴 Output strict JSON. No Markdown.
        Keys: "trend" (e.g. 盤整偏多), "reason" (max 50 words), "action" (買進/觀望/賣出), "target_price", "stop_loss".
        """

    for model in target_models:
        for key in keys:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                headers = {'Content-Type': 'application/json'}
                params = {'key': key}
                payload = {
                    "contents": [{"parts": [{"text": final_prompt}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1000}
                }
                res = requests.post(url, headers=headers, params=params, json=payload, timeout=25)
                if res.status_code == 200:
                    text = res.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
                    # 1. 嘗試標準 JSON 解析
                    try:
                        clean = text.replace("```json", "").replace("```", "").strip()
                        return json.loads(clean)
                    except:
                        # 2. Regex 暴力解析 (增加 DOTALL 支援換行)
                        if mode == "COST":
                            d = re.search(r'"diagnosis"\s*:\s*"(.*?)"', text, re.DOTALL)
                            r = re.search(r'"reason"\s*:\s*"(.*?)"', text, re.DOTALL)
                            t = re.search(r'"target_price"\s*:\s*"(.*?)"', text)
                            s = re.search(r'"stop_loss"\s*:\s*"(.*?)"', text)
                            if d: return {"diagnosis": d.group(1), "reason": r.group(1) if r else "...", "target_price": t.group(1) if t else "-", "stop_loss": s.group(1) if s else "-"}
                        else:
                            t = re.search(r'"trend"\s*:\s*"(.*?)"', text, re.DOTALL)
                            r = re.search(r'"reason"\s*:\s*"(.*?)"', text, re.DOTALL)
                            a = re.search(r'"action"\s*:\s*"(.*?)"', text, re.DOTALL)
                            tp = re.search(r'"target_price"\s*:\s*"(.*?)"', text)
                            sl = re.search(r'"stop_loss"\s*:\s*"(.*?)"', text)
                            if t: return {"trend": t.group(1), "reason": r.group(1) if r else "...", "action": a.group(1) if a else "觀望", "target_price": tp.group(1) if tp else "-", "stop_loss": sl.group(1) if sl else "-"}
            except: continue
    return {"error": "AI Busy"}

# --- 5. 數據抓取 (FinMind + RealTime) ---
def fetch_data_v14(stock_id):
    cached = get_cache(stock_id)
    if cached: return cached

    token = os.environ.get('FINMIND_TOKEN', '')
    url = "https://api.finmindtrade.com/api/v4/data"
    
    try:
        # A. 歷史股價 (用來算均線)
        start = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
        res = requests.get(url, params={"dataset": "TaiwanStockPrice", "data_id": stock_id, "start_date": start, "token": token}, timeout=5)
        data = res.json().get('data', [])
        
        # 🟢 [關鍵修改] 抓即時股價 (Real-Time)
        current_price = 0
        try:
            real = twstock.realtime.get(stock_id)
            if real['success']:
                current_price = float(real['realtime']['latest_trade_price'])
                # 如果即時價格是 "-", 代表還沒開盤或錯誤，沿用 FinMind 最新收盤
                if current_price == 0 and data: current_price = data[-1]['close']
            elif data:
                current_price = data[-1]['close']
        except:
            if data: current_price = data[-1]['close']

        if not data: return None
        
        # 計算均線 (使用最新的歷史收盤數據)
        closes = [d['close'] for d in data]
        ma5 = round(sum(closes[-5:]) / 5, 2) if len(closes) >= 5 else 0
        ma20 = round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else 0
        ma60 = round(sum(closes[-60:]) / 60, 2) if len(closes) >= 60 else 0
        
        # B. 籌碼
        start_chips = (datetime.now() - timedelta(days=12)).strftime('%Y-%m-%d')
        res_chips = requests.get(url, params={"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": stock_id, "start_date": start_chips, "token": token}, timeout=5)
        chips = res_chips.json().get('data', [])
        dates = sorted(list(set([d['date'] for d in chips])), reverse=True)
        latest_date = dates[0] if dates else ""
        recent_5_dates = dates[:5]
        
        f_lat = sum([d['buy'] - d['sell'] for d in chips if d['date'] == latest_date and d['name'] == 'Foreign_Investor']) // 1000
        f_sum5 = sum([d['buy'] - d['sell'] for d in chips if d['date'] in recent_5_dates and d['name'] == 'Foreign_Investor']) // 1000
        t_lat = sum([d['buy'] - d['sell'] for d in chips if d['date'] == latest_date and d['name'] == 'Investment_Trust']) // 1000
        t_sum5 = sum([d['buy'] - d['sell'] for d in chips if d['date'] in recent_5_dates and d['name'] == 'Investment_Trust']) // 1000
        
        # C. EPS
        start_eps = (datetime.now() - timedelta(days=200)).strftime('%Y-%m-%d')
        res_eps = requests.get(url, params={"dataset": "TaiwanStockFinancialStatements", "data_id": stock_id, "start_date": start_eps, "token": token}, timeout=5)
        eps_data = res_eps.json().get('data', [])
        eps_val = "N/A"
        eps_year = ""
        if eps_data:
             eps_list = [d for d in eps_data if d['type'] == 'EPS']
             if eps_list:
                 latest_eps = eps_list[-1]
                 eps_val = latest_eps['value']
                 eps_year = latest_eps['date'][:4]

        # D. 訊號快篩 (使用 current_price 即時價來判斷)
        signals = []
        if current_price > ma20 and ma20 > ma60: signals.append("📈 **多頭排列** (趨勢強)")
        elif current_price > ma20: signals.append("📈 **站上月線** (轉強)")
        elif current_price < ma20: signals.append("📉 **跌破月線** (轉弱)")
        
        bias = ((current_price - ma20) / ma20) * 100
        if bias > 5: signals.append("🔥 **乖離過大** (防回檔)")
        elif bias < -5: signals.append("❄️ **乖離過大** (醞釀反彈)")
        
        if f_sum5 > 0 and t_sum5 > 0: signals.append("💰 **土洋合買** (籌碼佳)")
        elif f_sum5 < 0 and t_sum5 < 0: signals.append("💸 **土洋棄守** (籌碼爛)")

        result = {
            "code": stock_id, "close": current_price, # 這裡是即時價
            "ma5": ma5, "ma20": ma20, "ma60": ma60,
            "f_lat": f_lat, "f_sum5": f_sum5,
            "t_lat": t_lat, "t_sum5": t_sum5,
            "eps": eps_val, "eps_year": eps_year,
            "signals": signals
        }
        set_cache(stock_id, result)
        return result
    except: return None

# --- 6. 推薦 ---
def get_lucky_picks():
    candidates = random.sample(list(STOCK_MAP.values()), min(8, len(STOCK_MAP)))
    results = []
    def check(sid):
        d = fetch_data_v14(sid)
        if d: return (sid, CODE_TO_NAME.get(sid, sid), d['close'], d['f_lat']+d['t_lat'])
        return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(check, s) for s in candidates]
        for f in concurrent.futures.as_completed(futures):
            r = f.result()
            if r: results.append(r)
    results.sort(key=lambda x: x[3], reverse=True)
    return results[:3]

# --- 7. 主程式 ---
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
    
    # 模式判斷
    cost_match = re.match(r'^([A-Z0-9\u4e00-\u9fa5]+)\s*成本\s*(\d+(?:\.\d+)?)$', msg)
    
    if msg == "推薦":
        picks = get_lucky_picks()
        reply = "🕵️‍♂️ **今日精選 (法人買超)**\n------------------"
        for p in picks: reply += f"\n🔥 **{p[1]} ({p[0]})** | 現價:{p[2]}"
        reply += "\n------------------\n💡 輸入`股票`查看詳情"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    stock_id = None
    cost = None
    
    if cost_match:
        raw_name = cost_match.group(1)
        cost = float(cost_match.group(2))
    else:
        raw_name = msg

    if raw_name.isdigit() and len(raw_name) == 4: stock_id = raw_name
    elif raw_name in STOCK_MAP: stock_id = STOCK_MAP[raw_name]
    
    if not stock_id:
        if "成本" in msg: line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 請輸入: 股票名稱 成本 價格"))
        else: line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"收到: {msg}"))
        return

    data = fetch_data_v14(stock_id)
    if not data:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 查無數據"))
        return
    
    name = CODE_TO_NAME.get(stock_id, stock_id)

    # === 🌟 成本模式 ===
    if cost:
        profit_pct = round(((data['close'] - cost) / cost) * 100, 2)
        status_text = "獲利" if profit_pct > 0 else "虧損"
        status_icon = "🔴" if profit_pct > 0 else "🟢"
        
        prompt = (
            f"持有{name}, 成本{cost}, 現價{data['close']} ({status_text}{profit_pct}%)\n"
            f"技術: MA20={data['ma20']}, MA60={data['ma60']}\n"
            f"籌碼: 外資{data['f_lat']}, 投信{data['t_lat']}\n"
            f"請給出診斷(續抱/停損/停利)與理由。"
        )
        ai = call_gemini_v14(prompt, mode="COST")
        
        reply = (
            f"🩺 **持股診斷: {name} ({stock_id})**\n"
            f"💰 帳面: {status_text} {profit_pct}% (現價 {data['close']})\n"
            f"------------------\n"
            f"【診斷】 {status_icon} {ai.get('diagnosis', '續抱')}\n"
            f"📝 {ai.get('reason', '...')}\n"
            f"------------------\n"
            f"【策略】 停利: {ai.get('target_price')} / 防守: {ai.get('stop_loss')}\n"
            f"(系統: {BOT_VERSION})"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # === 🌟 一般模式 ===
    prompt = (
        f"標的{name}, 現價{data['close']}\n"
        f"技術: MA5={data['ma5']}, MA20={data['ma20']}, MA60={data['ma60']}\n"
        f"籌碼: 外資{data['f_lat']}, 投信{data['t_lat']}\n"
        f"訊號: {', '.join(data['signals'])}\n"
        f"請給出趨勢分析與操作建議。"
    )
    ai = call_gemini_v14(prompt, mode="NORMAL")
    
    signals_str = "\n".join([f"  {s}" for s in data['signals']]) if data['signals'] else "  (無特殊訊號)"
    act = ai.get('action', '觀望')
    if "買" in act: icon = "🔴"
    elif "賣" in act: icon = "🟢"
    else: icon = "🟡"

    reply = (
        f"📊 **{name} ({stock_id})**\n"
        f"💰 現價: {data['close']}\n"
        f"⚡週: {data['ma5']} | 月: {data['ma20']} | 季: {data['ma60']}\n"
        f"🤝外資: {data['f_lat']} (5日:{data['f_sum5']})\n"
        f"🏦投信: {data['t_lat']} (5日:{data['t_sum5']})\n"
        f"💎 {data['eps_year']}累計EPS {data['eps']}元\n"
        f"------------------\n"
        f"🚩 **訊號快篩**:\n{signals_str}\n"
        f"------------------\n"
        f"【AI總結】 {icon} {act}\n"
        f"【分析】 {ai.get('reason', '...')}\n"
        f"【建議】 目標:{ai.get('target_price')} / 停損:{ai.get('stop_loss')}\n"
        f"------------------\n"
        f"(系統: {BOT_VERSION})\n"
        f"💡 輸入『{name} 成本 xxx』\nAI 幫你算停利停損點！"
    )
    
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
