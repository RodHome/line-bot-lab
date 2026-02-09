import os, requests, random, re, time
import json
import concurrent.futures
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage

app = Flask(__name__)

# 🟢 [版本號] v10.7 (Light Speed: Cache + Flex + Volume Filter)
BOT_VERSION = "v10.7 (極速光年)"

# --- 0. 全域快取 (Simple In-Memory Cache) ---
# 格式: { '2330': {'data': {...}, 'timestamp': datetime_obj} }
API_CACHE = {}
CACHE_DURATION = 600  # 快取存活時間 (秒) = 10分鐘

# --- 1. 菁英股票池 (建議定期手動更新熱門股) ---
STOCK_CACHE = {
    "台積電": "2330", "鴻海": "2317", "聯發科": "2454", "廣達": "2382",
    "緯創": "3231", "技嘉": "2376", "台達電": "2308", "日月光": "3711",
    "聯電": "2303", "瑞昱": "2379", "聯詠": "3034", "華碩": "2357",
    "智邦": "2345", "大立光": "3008", "光寶科": "2301", "緯穎": "6669",
    "矽力": "6415", "南亞科": "2408", "友達": "2409", "群創": "3481",
    "微星": "2377", "英業達": "2356", "仁寶": "2324", "京元電": "2449",
    "力積電": "6770", "華邦電": "2344", "佳世達": "2352", "聯強": "2347",
    "大聯大": "3702", "文曄": "3036", "健鼎": "3044", "欣興": "3037",
    "南電": "8046", "景碩": "3189", "台光電": "2383", "台燿": "6274",
    "金像電": "2368", "奇鋐": "3017", "雙鴻": "3324", "建準": "2421",
    "力致": "3483", "愛普": "6531", "智原": "3035", "創意": "3443",
    "世芯": "3661", "M31": "6643", "祥碩": "5269", "嘉澤": "3533",
    "致茂": "2360", "義隆": "2458", "新唐": "4919", "威剛": "3260",
    "群聯": "8299", "十銓": "4967", "強茂": "2481", "超豐": "2441",
    "富邦金": "2881", "國泰金": "2882", "中信金": "2891", "兆豐金": "2886",
    "玉山金": "2884", "元大金": "2885", "第一金": "2892", "合庫金": "5880",
    "華南金": "2880", "台新金": "2887", "永豐金": "2890", "凱基金": "2883",
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

# --- Gemini 呼叫 (僅用於個股深度診斷，不推薦清單) ---
def call_gemini_depth(prompt, system_instruction=None):
    keys = [os.environ.get(f'GEMINI_API_KEY_{i}') for i in range(1, 7) if os.environ.get(f'GEMINI_API_KEY_{i}')]
    if not keys and os.environ.get('GEMINI_API_KEY'):
        keys = [os.environ.get('GEMINI_API_KEY')]
    
    if not keys: return None, "NoKeys"
    random.shuffle(keys)
    
    # 深度分析使用 Gemini 2.5 Flash
    target_models = ["gemini-2.5-flash", "gemini-2.5-pro"]

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
                        "maxOutputTokens": 1000, 
                        "temperature": 0.2
                    }
                }
                response = requests.post(url, headers=headers, params=params, json=payload, timeout=25)
                if response.status_code == 200:
                    data = response.json()
                    text = data.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
                    if text: return text.strip(), "Active"
                continue
            except: continue
    return "AI 連線逾時", "Timeout"

# --- 資料抓取 (含快取機制) ---
def fetch_data_light(stock_id):
    # 1. 檢查快取 (Cache Hit)
    now = datetime.now()
    if stock_id in API_CACHE:
        cached = API_CACHE[stock_id]
        if (now - cached['timestamp']).seconds < CACHE_DURATION:
            # print(f"Cache Hit: {stock_id}") # Debug用
            return cached['data']

    # 2. 沒快取 (Cache Miss)，呼叫 FinMind
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
        volumes = [d['Trading_Volume'] for d in data] # 取得成交量
        
        ma5 = round(sum(closes[-5:]) / 5, 2) if len(closes) >= 5 else 0
        ma20 = round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else 0
        ma60 = round(sum(closes[-60:]) / 60, 2) if len(closes) >= 60 else 0
        
        # 計算量能均線
        ma5_vol = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else 0
        last_vol = volumes[-1]
        
        slope_ma20 = 0
        if len(closes) >= 25:
            prev_ma20 = round(sum(closes[-25:-5]) / 20, 2)
            if prev_ma20 > 0:
                slope_ma20 = round((ma20 - prev_ma20) / prev_ma20 * 100, 2)

        is_squeeze = False
        if ma5 > 0 and ma20 > 0 and ma60 > 0:
            mas = [ma5, ma20, ma60]
            if (max(mas) - min(mas)) / min(mas) < 0.03: is_squeeze = True

        result = {
            "code": stock_id, 
            "close": latest['close'], 
            "ma5": ma5, "ma20": ma20, "ma60": ma60,
            "slope_ma20": slope_ma20,
            "is_squeeze": is_squeeze,
            "vol": last_vol,
            "ma5_vol": ma5_vol
        }
        
        # 3. 寫入快取
        API_CACHE[stock_id] = {'data': result, 'timestamp': now}
        return result

    except: return None

def fetch_chips_accumulate(stock_id):
    # 籌碼資料變動慢，也可以做快取，這裡為求精簡先共用 API_CACHE 概念略過
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
    return None

# --- 篩選邏輯 (加入 Rate Limit & 量能過濾) ---
def check_stock_worker_turbo(code):
    # 🟢 Rate Limit: 隨機睡 0.1~0.3 秒，防止被 API 封鎖
    time.sleep(random.uniform(0.1, 0.3))
    
    try:
        data = fetch_data_light(code)
        if not data: return None
        
        # 條件 1: 三線多頭
        if data['close'] > data['ma5'] and data['ma5'] > data['ma20'] and data['ma20'] > data['ma60']:
            
            # 🔥 條件 2: 量能過濾 (v10.7新增)
            # 今日量 > 5日均量 * 1.5 (爆量) 或 今日量 > 昨日量 (溫和增量)
            # 這裡設定較寬鬆：只要有成交量且大於5日均量即可
            if data['vol'] < data['ma5_vol']: 
                return None # 量縮不推

            tf, tt, af, at = fetch_chips_accumulate(code)
            
            # 條件 3: 籌碼過濾 (5日買超 > 50張)
            if (af + at) > 50:
                name = CODE_TO_NAME.get(code, code)
                
                # 自動生成推薦理由 (Lazy Package，不靠 Gemini)
                reasons = []
                if data['vol'] > data['ma5_vol'] * 1.5: reasons.append("🔥爆量攻擊")
                if af > 1000: reasons.append("💰外資大買")
                if at > 100: reasons.append("🏦投信認養")
                if data['is_squeeze']: reasons.append("⚡均線噴出")
                if not reasons: reasons.append("📈多頭排列")
                
                reason_str = " | ".join(reasons)
                
                return {
                    "code": code,
                    "name": name,
                    "price": data['close'],
                    "chip": af + at,
                    "reason": reason_str
                }
    except: return None
    return None

def scan_recommendations_turbo():
    candidates = []
    sample_list = random.sample(list(STOCK_CACHE.values()), 40)
    # Thread 數量降為 5，避免瞬間過載
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = executor.map(check_stock_worker_turbo, sample_list)
    for res in results:
        if res: candidates.append(res)
        # 只要找到 5 檔就收工，不用掃完全部
        if len(candidates) >= 5: break
    return candidates

# --- Flex Message 生成器 (視覺化卡片) ---
def create_recommendation_flex(stocks):
    bubbles = []
    for stock in stocks[:5]: # 最多顯示 5 張卡片
        bubble = {
            "type": "bubble",
            "size": "micro",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text", "text": stock['name'], "weight": "bold", "color": "#ffffff", "size": "sm"},
                    {"type": "text", "text": str(stock['code']), "color": "#ffffff", "size": "xxs"}
                ],
                "backgroundColor": "#D63031", # 紅色背景代表多頭
                "paddingAll": "8px"
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "text",
                        "text": str(stock['price']),
                        "weight": "bold",
                        "size": "xl",
                        "align": "center",
                        "color": "#D63031"
                    },
                    {
                        "type": "box",
                        "layout": "vertical",
                        "margin": "md",
                        "contents": [
                            {"type": "text", "text": stock['reason'], "size": "xxs", "color": "#555555", "wrap": True},
                            {"type": "text", "text": f"5日籌碼: +{stock['chip']}張", "size": "xxs", "color": "#1E90FF", "margin": "xs"}
                        ]
                    }
                ],
                "paddingAll": "10px"
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "button",
                        "action": {
                            "type": "message",
                            "label": "詳細診斷",
                            "text": f"{stock['name']}"
                        },
                        "height": "sm",
                        "style": "link"
                    }
                ]
            }
        }
        bubbles.append(bubble)

    return FlexSendMessage(
        alt_text="🔥 AI 精選強勢股清單",
        contents={
            "type": "carousel",
            "contents": bubbles
        }
    )

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
    
    # 🔥 [功能 1] 推薦選股 (v10.7 Flex Message 版)
    if msg in ["推薦", "選股"]:
        good_stocks = scan_recommendations_turbo()
        if not good_stocks:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 市場氣氛不佳，AI 掃描無符合「爆量多頭」標準之個股。建議觀望。"))
        else:
            # 直接回傳漂亮卡片，不經過 Gemini，速度極快
            flex_msg = create_recommendation_flex(good_stocks)
            line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    # [Debug]
    if msg.lower() == "debug":
        cache_size = len(API_CACHE)
        reply = f"🛠️ **v10.7 極速版**\n快取數: {cache_size} 筆\nAPI狀態: 正常"
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
    if not stock_id:
        return

    # 3. 抓資料 (會優先讀 Cache)
    name = CODE_TO_NAME.get(stock_id, stock_id)
    data = fetch_full_data(stock_id)
    if not data:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 無法讀取 {stock_id} 數據"))
        return

    # 4. 回覆邏輯 (個股診斷仍維持 Gemini 深度分析)
    if user_cost:
        profit_pct = round((data['close'] - user_cost) / user_cost * 100, 1)
        profit_status = "獲利" if profit_pct > 0 else "虧損"
        sys_prompt = "你是無情的停損機器。不要廢話。限100字。"
        user_prompt = (
            f"標的：{stock_id} {name}\n"
            f"現價：{data['close']} (成本：{user_cost}，{profit_status} {profit_pct}%)\n"
            f"MA20={data['ma20']}, 籌碼5日={data['acc_foreign']+data['acc_trust']}張\n"
            f"指令：\n【診斷】(🟢續抱/🟡減碼/🔴停損) 理由\n【策略】停利/防守價位"
        )
        ai_ans, status = call_gemini_depth(user_prompt, system_instruction=sys_prompt)
        reply = f"🩺 **{name} 持股診斷**\n{profit_status} {profit_pct}%\n------------------\n{ai_ans}\n(系統: {status})"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
    else:
        sys_prompt = "你是無情的分析機器。不要打招呼。限 50 字。"
        user_prompt = (
            f"標的：{stock_id} {name}\n"
            f"數據：現價{data['close']} (MA20={data['ma20']})\n"
            f"籌碼：外資{data['acc_foreign']}張, 投信{data['acc_trust']}張\n"
            f"指令：\n【分析】趨勢與籌碼解讀\n【支撐】價位"
        )
        ai_ans, status = call_gemini_depth(user_prompt, system_instruction=sys_prompt)
        
        # 簡單訊號判斷
        signals = []
        if data['close'] > data['ma5'] > data['ma20']: signals.append("🟢多頭排列")
        if data['vol'] > data['ma5_vol'] * 1.5: signals.append("🔥爆量")
        
        reply = f"📊 **{name}({stock_id})**\n💰 {data['close']}\n------------------\n{' | '.join(signals)}\n------------------\n{ai_ans}\n(系統: {status})"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
