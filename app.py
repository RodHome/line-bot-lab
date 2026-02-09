import os, requests, random, re
import json
import concurrent.futures
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage

app = Flask(__name__)

# 🟢 [版本號] v11.1 (Analyst Mode + Giga UI)
BOT_VERSION = "v11.1"

# --- 1. 菁英股票池 ---
STOCK_CACHE = {
    "台積電": "2330", "鴻海": "2317", "聯發科": "2454", "廣達": "2382",
    "緯創": "3231", "技嘉": "2376", "台達電": "2308", "日月光": "3711",
    "聯電": "2303", "瑞昱": "2379", "聯詠": "3034", "華碩": "2357",
    "研華": "2395", "智邦": "2345", "大立光": "3008", "光寶科": "2301",
    "緯穎": "6669", "矽力": "6415", "南亞科": "2408", "友達": "2409",
    "群創": "3481", "微星": "2377", "英業達": "2356", "仁寶": "2324",
    "京元電": "2449", "力積電": "6770", "華邦電": "2344", "佳世達": "2352",
    "聯強": "2347", "大聯大": "3702", "文曄": "3036", "健鼎": "3044",
    "欣興": "3037", "南電": "8046", "景碩": "3189", "台光電": "2383",
    "台燿": "6274", "金像電": "2368", "奇鋐": "3017", "雙鴻": "3324",
    "建準": "2421", "力致": "3483", "愛普": "6531", "智原": "3035",
    "創意": "3443", "世芯": "3661", "M31": "6643", "祥碩": "5269",
    "嘉澤": "3533", "致茂": "2360", "義隆": "2458", "新唐": "4919",
    "威剛": "3260", "群聯": "8299", "十銓": "4967", 
    "強茂": "2481", "超豐": "2441",
    "富邦金": "2881", "國泰金": "2882", "中信金": "2891", "兆豐金": "2886",
    "玉山金": "2884", "元大金": "2885", "第一金": "2892", "合庫金": "5880",
    "華南金": "2880", "台新金": "2887", "永豐金": "2890", "凱基金": "2883",
    "台泥": "1101", "亞泥": "1102", "台塑": "1301", "南亞": "1303",
    "台化": "1326", "台塑化": "6505", "遠東新": "1402", "中鋼": "2002",
    "統一": "1216", "統一超": "2912", "和泰車": "2207", "裕隆": "2201", 
    "長榮": "2603", "陽明": "2609", "萬海": "2615", "長榮航": "2618",
    "華航": "2610", "慧洋": "2637", "裕民": "2606", "華城": "1519",
    "士電": "1503", "中興電": "1513", "東元": "1504", "亞力": "1514",
    "世紀鋼": "9958", "上緯": "3708"
}

CODE_TO_NAME = {v: k for k, v in STOCK_CACHE.items()}

token = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
secret = os.environ.get('LINE_CHANNEL_SECRET')
line_bot_api = LineBotApi(token if token else 'UNKNOWN')
handler = WebhookHandler(secret if secret else 'UNKNOWN')

@app.route("/")
def health_check():
    return "OK", 200

def call_gemini_fast(prompt, system_instruction=None):
    keys = [os.environ.get(f'GEMINI_API_KEY_{i}') for i in range(1, 7) if os.environ.get(f'GEMINI_API_KEY_{i}')]
    if not keys and os.environ.get('GEMINI_API_KEY'):
        keys = [os.environ.get('GEMINI_API_KEY')]
    
    if not keys: return None, "NoKeys"
    random.shuffle(keys)
    
    target_models = ["gemini-2.5-flash", "gemini-2.5-flash-lite"] 

    for model in target_models:
        for key in keys:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                headers = {'Content-Type': 'application/json'}
                params = {'key': key}
                contents = [{"parts": [{"text": prompt}]}]
                if system_instruction:
                    full_prompt = f"【系統指令】：{system_instruction}\n\n【用戶請求】：{prompt}"
                    contents = [{"parts": [{"text": full_prompt}]}]

                payload = {
                    "contents": contents,
                    "generationConfig": {
                        "maxOutputTokens": 3000, 
                        "temperature": 0.4
                    }
                }
                response = requests.post(url, headers=headers, params=params, json=payload, timeout=35)
                if response.status_code == 200:
                    data = response.json()
                    text = data.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
                    if text: return text.strip(), "Active"
                continue
            except: continue
    return "AI 連線逾時", "Timeout"

def fetch_data_light(stock_id):
    token = os.environ.get('FINMIND_TOKEN', '')
    url = "https://api.finmindtrade.com/api/v4/data"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        start = (datetime.now() - timedelta(days=150)).strftime('%Y-%m-%d')
        res = requests.get(url, params={"dataset": "TaiwanStockPrice", "data_id": stock_id, "start_date": start, "token": token}, headers=headers, timeout=5)
        data = res.json().get('data', [])
        if not data: return None
        
        latest = data[-1]
        closes = [d['close'] for d in data]
        highs = [d['max'] for d in data]
        
        ma5 = round(sum(closes[-5:]) / 5, 2) if len(closes) >= 5 else 0
        ma20 = round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else 0
        ma60 = round(sum(closes[-60:]) / 60, 2) if len(closes) >= 60 else 0
        
        slope_ma20 = 0
        if len(closes) >= 25:
            prev_ma20 = round(sum(closes[-25:-5]) / 20, 2)
            if prev_ma20 > 0:
                slope_ma20 = round((ma20 - prev_ma20) / prev_ma20 * 100, 2)

        high_60 = max(highs[-60:]) if len(highs) >= 60 else max(highs)
        bias_60 = 0
        if ma60 > 0: bias_60 = round((latest['close'] - ma60) / ma60 * 100, 1)

        is_squeeze = False
        if ma5 > 0 and ma20 > 0 and ma60 > 0:
            mas = [ma5, ma20, ma60]
            if (max(mas) - min(mas)) / min(mas) < 0.03: is_squeeze = True

        return {
            "code": stock_id, 
            "close": latest['close'], 
            "ma5": ma5, "ma20": ma20, "ma60": ma60,
            "slope_ma20": slope_ma20,
            "high_60": high_60,
            "bias_60": bias_60,
            "is_squeeze": is_squeeze
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
        if not eps_data: return "N/A"
        latest_year = eps_data[-1]['date'][:4]
        vals = [d['value'] for d in eps_data if d['date'].startswith(latest_year)]
        return f"{latest_year}累計{round(sum(vals), 2)}元"
    except: return "逾時"

def fetch_full_data(stock_id):
    basic = fetch_data_light(stock_id)
    if not basic: return None
    tf, tt, af, at = fetch_chips_accumulate(stock_id)
    basic.update({'foreign': tf, 'trust': tt, 'acc_foreign': af, 'acc_trust': at})
    return basic

def get_stock_id(text):
    text = text.strip()
    clean_text = re.sub(r'(成本|cost).*', '', text, flags=re.IGNORECASE).strip()
    if clean_text in STOCK_CACHE: return STOCK_CACHE[clean_text]
    if clean_text.isdigit() and len(clean_text) >= 4: return clean_text
    if len(clean_text) > 6: return None
    return None

def check_stock_worker_turbo(code):
    try:
        data = fetch_data_light(code)
        if not data: return None
        if data['close'] > data['ma5'] and data['ma5'] > data['ma20'] and data['ma20'] > data['ma60']:
            tf, tt, af, at = fetch_chips_accumulate(code)
            if (af + at) > 50:
                name = CODE_TO_NAME.get(code, code)
                return {
                    "code": code, "name": name, 
                    "close": data['close'], 
                    "chips": f"{af+at}張",
                    "tag": "外資大買" if af > at else "投信認養"
                }
    except: return None
    return None

def scan_recommendations_turbo():
    candidates = []
    sample_list = random.sample(list(STOCK_CACHE.values()), 40)
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
    
    # 🔥 [推薦選股] - Flex Message (設定為 Giga 寬版)
    if msg in ["推薦", "選股"]:
        good_stocks = scan_recommendations_turbo()
        if not good_stocks:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 今日市場偏弱，掃描後無符合「強勢多頭+籌碼集中」之標的，建議觀望。"))
            return

        bubbles = []
        for stock in good_stocks:
            bubble = {
                "type": "bubble",
                "size": "giga",  # 🔥 這裡加入了 Giga 寬度設定
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": stock['name'], "weight": "bold", "size": "xl", "color": "#ffffff"},
                        {"type": "text", "text": stock['code'], "size": "xs", "color": "#eeeeee"}
                    ],
                    "backgroundColor": "#D32F2F"
                },
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": str(stock['close']), "weight": "bold", "size": "3xl", "color": "#D32F2F", "align": "center"},
                        {"type": "text", "text": f"💰{stock['tag']} | 🏦籌碼:{stock['chips']}", "size": "xs", "color": "#555555", "align": "center", "margin": "md"},
                        {"type": "button", "action": {"type": "message", "label": "詳細診斷", "text": stock['code']}, "style": "link", "margin": "md"}
                    ]
                }
            }
            bubbles.append(bubble)
        
        flex_msg = FlexSendMessage(
            alt_text="AI 精選強勢股",
            contents={"type": "carousel", "contents": bubbles}
        )
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    # [Debug]
    if msg.lower() == "debug":
        token_chk = os.environ.get('FINMIND_TOKEN', '')
        ai_res, ai_stat = call_gemini_fast("Hi")
        reply = f"🛠️ **v11.1 診斷**\nToken: {'✅' if token_chk else '❌'}\nAI: {ai_stat}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # 1. 解析成本
    user_cost = None
    cost_match = re.search(r'(成本|cost)[:\s]*(\d+\.?\d*)', msg, re.IGNORECASE)
    if cost_match:
        try: user_cost = float(cost_match.group(2))
        except: pass

    # 2. 取得股票代碼
    stock_id = get_stock_id(msg)
    if not stock_id: return

    # 3. 抓完整資料
    name = CODE_TO_NAME.get(stock_id, stock_id)
    data = fetch_full_data(stock_id)
    if not data:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 無法讀取 {stock_id} 數據"))
        return

    # 4. 回覆邏輯 (雙軌制)
    if user_cost:
        # 持股診斷 (Analyst Mode)
        profit_pct = round((data['close'] - user_cost) / user_cost * 100, 1)
        profit_status = "獲利" if profit_pct > 0 else "虧損"
        profit_icon = "💰" if profit_pct > 0 else "💸"
        
        sys_prompt = (
            "你是果斷的操盤手。請根據成本與現價，結合籌碼技術面，給出具體且有邏輯的操作建議。"
            "字數限制：150字以內。"
            "回答格式：\n"
            "【診斷】(🟢續抱/🟡減碼/🔴停損) 理由說明\n"
            "【策略】(具體的停利點與防守點數值)"
        )
        user_prompt = (
            f"標的：{stock_id} {name}\n"
            f"現價：{data['close']} (成本：{user_cost}，{profit_status} {profit_pct}%)\n"
            f"MA20={data['ma20']}, 籌碼5日={data['acc_foreign']+data['acc_trust']}張\n"
            f"任務：分析目前是該跑還是該抱？理由是什麼？"
        )
        ai_ans, status = call_gemini_fast(user_prompt, system_instruction=sys_prompt)
        
        reply = (
            f"🩺 **{name} 診斷**\n"
            f"{profit_icon} 帳面：{profit_status} {profit_pct}%\n"
            f"------------------\n"
            f"{ai_ans}\n"
            f"------------------\n"
            f"系統版本：{BOT_VERSION}"
        )
    else:
        # [個股健檢] - 恢復分析師靈魂 + 數據儀表板
        eps = fetch_eps(stock_id)
        
        data_dashboard = (
            f"💰 現價：{data['close']}\n"
            f"📊 週: {data['ma5']} | 月: {data['ma20']} | 季: {data['ma60']}\n"
            f"🏦 外資: {data['foreign']} (5日: {data['acc_foreign']})\n"
            f"🏦 投信: {data['trust']} (5日: {data['acc_trust']})\n"
            f"💎 EPS: {eps}"
        )

        sys_prompt = (
            "你是專業且犀利的股市分析師。請針對提供的數據進行深度解讀，而非單純覆述數據。"
            "分析重點：外資與投信的動向對股價的影響、目前股價在均線的位置意義。"
            "字數限制：150字以內。"
            "回答格式：\n"
            "【分析】(解析技術面與籌碼面的多空力道)\n"
            "【建議】(給出具體的觀察重點或操作區間)"
        )
        user_prompt = (
            f"標的：{stock_id} {name}\n"
            f"數據：現價{data['close']} (MA20={data['ma20']})\n"
            f"籌碼：外資{data['acc_foreign']}張, 投信{data['acc_trust']}張\n"
        )
        ai_ans, status = call_gemini_fast(user_prompt, system_instruction=sys_prompt)

        signals = []
        if data['close'] > data['ma5'] > data['ma20'] > data['ma60']: signals.append("🟢三線多頭")
        if data['acc_foreign'] + data['acc_trust'] > 50: signals.append("💰法人進場")
        elif data['acc_foreign'] + data['acc_trust'] < -50: signals.append("💸法人提款")
        signal_str = " | ".join(signals) if signals else "🟡觀望"

        reply = (
            f"📈 **{name}({stock_id})**\n"
            f"{data_dashboard}\n"
            f"------------------\n"
            f"🚩 {signal_str}\n"
            f"------------------\n"
            f"{ai_ans}\n"
            f"------------------\n"
            f"系統版本：{BOT_VERSION}"
        )

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
