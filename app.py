import os, requests, random, re
import json
import time
import math
import concurrent.futures
import twstock
from datetime import datetime, timedelta, time as dtime, timezone
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage

#---restart
app = Flask(__name__)

# 🤖 [版本號] v18.1 
BOT_VERSION = "v18.1 (使用介面優化)"

# --- 1. 全域快取與設定 ---
AI_RESPONSE_CACHE = {}
TWSE_CACHE = {"date": "", "data": []}

# 🔥 新增：由外部 JSON 驅動的全域詮釋資料庫
STOCK_META = {}
ALL_STOCK_MAP = {}   # 中文名稱轉代號 (供對話比對)
CODE_TO_NAME = {}    # 代號轉中文名稱
FALLBACK_POOL = []   # 備用抽樣池 (僅限普通股票)

try:
    if os.path.exists('stock_list.json'):
        with open('stock_list.json', 'r', encoding='utf-8') as f:
            STOCK_META = json.load(f)
            
        # 動態建立查詢字典與備用池
        for code, info in STOCK_META.items():
            name = info.get('name', '')
            if name:
                ALL_STOCK_MAP[name] = code      # "台積電" -> "2330"
            ALL_STOCK_MAP[code] = code          # "2330" -> "2330" (防呆)
            CODE_TO_NAME[code] = name
            
            # 建立純股票的備用池 (排除 ETF)，供推薦選股失效時抽樣
            if info.get('type') == '股票':
                FALLBACK_POOL.append(code)
except Exception as e:
    print(f"[Warn] 載入 stock_list.json 失敗: {e}")

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

# TWSE 全市場掃描 [修改] 讓 Bot 直接讀取 GitHub 算好的資料
# --- [新增功能] 隔日沖券商讀取 ---
def get_day_trade_brokers():
    """讀取本地 JSON 檔，若檔案不存在或讀取失敗則回傳預設名單"""
    try:
        if os.path.exists('day_trade_brokers.json'):
            with open('day_trade_brokers.json', 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"[Warn] 讀取隔日沖名單失敗: {e}")
    
    # 防呆預設值 (避免檔案遺失導致報錯)
    return {
        "update_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "brokers": {
            "預設常見分點": ["凱基-台北", "元大-土城永寧", "富邦-建國", "群益-大安"]
        }
    }
def fetch_twse_candidates():
    # 🔥 這是你的 GitHub Raw 連結 (根據你提供的截圖 RodHome/line-bot-lab)
    # 如果你的檔案名稱不是 daily_recommendations.json，請修改這裡
    GITHUB_RAW_URL = "https://raw.githubusercontent.com/RodHome/line-bot-lab/main/daily_recommendations.json"
    
    # 加入簡單的快取機制 (避免短時間重複下載)
    global TWSE_CACHE
    tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
    today_str = tw_now.strftime('%Y%m%d')

    # 1. 檢查記憶體快取 (如果 Zeabur 沒重啟，直接用記憶體裡的)
    if TWSE_CACHE.get('date') == today_str and TWSE_CACHE.get('data'):
        return TWSE_CACHE['data']

    print(f"[System] 從 GitHub 下載推薦名單...")
    try:
        # 2. 去 GitHub 下載 JSON
        # 加入這行 header 避免被 GitHub 快取住舊資料
        headers = {'Cache-Control': 'no-cache'}
        res = requests.get(GITHUB_RAW_URL, headers=headers, timeout=5)
        
        if res.status_code == 200:
            stock_list = res.json()
            
            # 簡單驗證一下資料格式
            if isinstance(stock_list, list) and len(stock_list) > 0:
                # 更新快取
                TWSE_CACHE = {"date": today_str, "data": stock_list}
                print(f"[System] 成功載入 {len(stock_list)} 檔推薦股")
                return stock_list
            else:
                print("[Warn] GitHub 回傳的資料格式為空或錯誤")
        else:
            print(f"[Warn] 下載失敗，狀態碼: {res.status_code}")
            
    except Exception as e:
        print(f"[Error] GitHub Download Error: {e}")

    # 3. 如果 GitHub 掛了或還沒產出，回傳備用名單 (權值股) 防止 Bot 當機
    print("[System] 使用備用名單")
    fallback_list = ["2330", "2317", "2454", "2382", "2308"]
    return fallback_list

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
    
    if chips_val > 1000: signals.append("💰法人大買")
    elif chips_val < -1000: signals.append("💸法人大賣")
    
    if close > ma5 > ma20 > ma60: signals.append("🔴三線多頭")
    elif close < ma5 < ma20 < ma60: signals.append("🟢三線空頭")
    
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

def call_gemini_json(prompt, system_instruction=None):
    keys = [os.environ.get(f'GEMINI_API_KEY_{i}') for i in range(1, 7) if os.environ.get(f'GEMINI_API_KEY_{i}')]
    if not keys and os.environ.get('GEMINI_API_KEY'): keys = [os.environ.get('GEMINI_API_KEY')]
    if not keys: return None
    random.shuffle(keys)
    
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

# --- 🔥 優化版：數據並行擷取 (Safe Mode) ---
def fetch_data_light(stock_id):
    # 定義內部子任務
    def get_history():
        token = os.environ.get('FINMIND_TOKEN', '')
        url_hist = "https://api.finmindtrade.com/api/v4/data"
        try:
            start = (datetime.now() - timedelta(days=120)).strftime('%Y-%m-%d')
            res = requests.get(url_hist, params={
                "dataset": "TaiwanStockPrice", "data_id": stock_id, "start_date": start, "token": token
            }, timeout=4)
            return res.json().get('data', [])
        except: return []

    def get_realtime():
        try:
            return twstock.realtime.get(stock_id)
        except: return None

    # 並行執行
    hist_data = []
    stock_rt = None
    try:
        # max_workers=2 為 Zeabur 安全值
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_hist = executor.submit(get_history)
            future_rt = executor.submit(get_realtime)
            
            hist_data = future_hist.result(timeout=5)
            stock_rt = future_rt.result(timeout=5)
    except Exception as e:
        print(f"[Warn] 並行擷取失敗，改為序列執行: {e}")
        hist_data = get_history()
        stock_rt = get_realtime()

    if not hist_data: return None

    # 數據縫合
    latest_price = 0
    source_name = "歷史"
    update_time = get_taiwan_time_str()
    
    try:
        if stock_rt and stock_rt['success']:
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

def check_stock_worker_turbo(item):
    # 支援新版字典結構或舊版字串
    if isinstance(item, dict):
        code = item.get('code')
        item_data = item
    else:
        code = str(item)
        item_data = {}

    try:
        # 1. 抓取「即時」股價與均線 (計算依然在 fetch_data_light 裡運作)
        data = fetch_data_light(code)
        if not data: return None
        
        # 🔥 補回技術面護城河：就算基本面再好，跌破月線 (20日均線) 就無情淘汰！
        if data['close'] < data['ma20']: 
            return None 

        name = CODE_TO_NAME.get(code, code)
        sector = STOCK_META.get(code, {}).get('sector', '熱門股')
        
        # 2. 提取後台算好的強大數據
        chips_display = item_data.get('chips_display', 'N/A')
        buy_value = item_data.get('buy_value', 0)
        yoy = item_data.get('yoy', 'N/A')
        tag = item_data.get('tag', '強勢股')
        
        # 3. 取得技術指標
        signals = get_technical_signals(data, 1001 if buy_value > 0 else 0)
        signal_str = " | ".join(signals)

        # 格式化 YoY 顯示字串
        yoy_display = f"+{yoy}%" if isinstance(yoy, (int, float)) and yoy > 0 else f"{yoy}%"

        return {
            "code": code, "name": name, "sector": sector,
            "close": data['close'], "change_display": data['change_display'], "color": data['color'],
            "chips": chips_display, 
            "buy_value": buy_value,
            "yoy_display": yoy_display, 
            "signal_str": signal_str,
            "tag": tag
        }
    except Exception as e: 
        print(f"Worker Error: {e}")
        return None

def scan_recommendations_turbo(target_sector=None):
    candidates_pool = []
    
    # 1. 先取得今日的推薦母池 (由 generator 算好的 GitHub 嚴格名單)
    twse_list = fetch_twse_candidates()
    
    # 確認名單是新版結構 (有 yoy 等資料)
    if twse_list and isinstance(twse_list[0], dict) and 'yoy' in twse_list[0]:
        pool_source = twse_list
    else:
        # 若 API 失效，使用備用池 (這裡也要組裝成 dict 格式讓 worker 吃)
        pool_source = [{"code": c} for c in FALLBACK_POOL]
        
    # 2. 進行產業過濾 或 全量抽樣
    if target_sector:
        # 🔥 修正點：只在「嚴格過濾後的推薦母池」中，比對 STOCK_META 裡的產業標籤
        for item in pool_source:
            code = item.get('code')
            sector = STOCK_META.get(code, {}).get('sector', '')
            if target_sector in sector:
                candidates_pool.append(item)
                
        # 如果今天的飆股池裡面，剛好沒有這個產業，直接回傳空陣列
        if not candidates_pool:
            return []
    else:
        # 如果沒有指定產業
        if pool_source == twse_list:
            # 🔥 升級盲抽機制：從 50 檔強勢母池中，每次隨機抽出 8 檔候選！
            candidates_pool = random.sample(twse_list, min(8, len(twse_list)))
        else:
            # 備用池隨機抽 8 檔
            candidates_pool = random.sample(pool_source, min(8, len(pool_source)))
    
    valid_candidates = []
    
    # 3. 交給 worker 進行最後的現價與均線確認
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        results = executor.map(check_stock_worker_turbo, candidates_pool)
    
    for res in results:
        if res: valid_candidates.append(res)
        
    # 4. 確保依照籌碼買超金額排序
    if valid_candidates:
        valid_candidates.sort(key=lambda x: x.get('buy_value', 0), reverse=True)
        
    return valid_candidates[:5]

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
    #🔥 [效能優化] 零成本引導教學攔截
    if msg in ["如何評估", "如何診斷"]:
        guide_text = (
            "💡 【個股與持股診斷教學】\n"
            "請直接在對話框輸入您的目標，系統將自動啟動 AI 運算：\n\n"
            "🔎 單純評估個股：\n"
            "請輸入「股票代號」或「名稱」。\n"
            "👉 例如：2330 或 台積電\n\n"
            "📊 帶有成本的持股健檢：\n"
            "請輸入「代號」加上「成本 XX」。\n"
            "👉 例如：2330 成本 800\n\n"
            "⚠️ 注意：每次深度診斷約需 8-15 秒，請耐心等候喔！"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=guide_text))
        return
        
    # 🔥 [新增功能] 選股邏輯說明
    if msg in ["選股邏輯", "推薦說明", "篩選條件","右側邏輯"]:
        logic_text = (
            "🤖【AI 選股雷達：篩選邏輯說明】\n"
            "—— 結合「大數據動能」與「基本面趨勢」的雙重防線 ——\n"
            "為避免選到流動性差或基本面不佳的個股，每日盤後將進行「金流與業績」地毯式雙重掃描：\n\n"
            "1️⃣ 第一關：價量濾網 (剔除冷門與低價股)\n"
            " ‧ 剔除雜質：排除 ETF、權證與 DR 股\n"
            " ‧ 剔除低價股：股價必須 > 10 元，遠離低價投機與財務預警風險。\n"
            " ‧ 資金熱區：單日成交金額必須 > 3 億元且當日收紅\n\n"
            "2️⃣ 第二關：基本面與大戶籌碼 (勝率核心)\n"
            " ‧ 營收真成長：營收 YoY (年增率) 必須 > 10% (確保業績真成長)\n"
            " ‧ 大戶共識：近 5 日「外資+投信」買超合計 > 3 億元 (確保有大人照顧)\n\n"
            "────────────────\n"
            "💡 常見問答：為何強勢股偶爾會「漏網」？\n"
            "這不是系統的疏漏，而是對風險的堅持！\n"
            "1. 結構性風險： 系統僅選取「上市櫃」優質標的，自動過濾流動性較低、波動劇烈且資訊透明度較差的興櫃標的。\n"
            "2. 純題材炒作： 股價雖漲，但最新月營收年增率未達 10%，顯示上漲缺乏業績支撐，極易出現「假突破、真倒貨」。\n"
            "3. 缺乏法人背書： 漲勢若由短線主力或散戶衝動推升（法人買超未達 3 億），籌碼結構相對鬆散，不符合我們「穩中求噴」的選股精神。\n\n"
            "📌 我們的鐵律：只推薦有「基本面」與「大資金」雙重背書的優質標的！"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=logic_text))
        return
    # 🔥 [新增功能] 左側黃金坑邏輯說明
    if msg in ["左側邏輯", "左側說明", "左側條件", "黃金坑邏輯"]:
        left_logic_text = (
            "🤖【AI 左側雷達：黃金坑篩選邏輯】\n"
            "—— 嚴守「不接刀、只撿鑽石」的逆勢價值投資 ——\n"
            "每日盤後從全市場尋找「被錯殺、量縮打底、法人偷吃貨」的潛伏股：\n\n"
            "1️⃣ 第一關：流動性降維 (尋找無人問津區)\n"
            " ‧ 股價 > 10 元，剔除仙股風險。\n"
            " ‧ 成交額 1000萬~3億：避開當沖熱門，鎖定冷門潛伏區。\n\n"
            "2️⃣ 第二關：技術面尋底 (確認賣壓竭盡)\n"
            " ‧ 跌深委屈：季線負乖離達 -3% 以下 (均線引力空間大)。\n"
            " ‧ 量縮窒息：今日成交量低於 20日均量 80% (浮額清洗完畢)。\n"
            " ‧ 低波築底：近 10 日振幅 < 12% (底部橫盤不再劇烈下殺)。\n"
            " ‧ 尚未起漲：近 5 日漲幅 < 5% (買在安全起漲點前)。\n\n"
            "3️⃣ 第三關：籌碼與基本面定錨 (終極防飛刀)\n"
            " ‧ 獲利底線：最新單季 EPS > 0 (公司必須賺錢，拒絕價值陷阱)。\n"
            " ‧ 聰明錢進駐：近 5 日內「外資或投信」買超 >= 3 天。\n"
            " ‧ 轉機特例：法人連買 4 天，無視短期營收衰退，視為強勢轉機股。\n\n"
            "🎯【獨家信心評分系統】\n"
            "符合上述條件後，系統會依據「法人連買天數」、「量縮窒息程度 (<50%)」、「負乖離深度」給予 1~100 分綜合評分，分數越高勝率越大！"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=left_logic_text))
        return
        
    # [功能 1] 推薦選股
    if msg.startswith("推薦") or msg.startswith("選股"):
        parts = msg.split()
        target_sector = parts[1] if len(parts) > 1 else None
        
        good_stocks = scan_recommendations_turbo(target_sector)
        
       # 🔥 優化：更精確的回報找不到標的之原因
        if not good_stocks:
            if target_sector:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 今日的嚴選飆股池中，暫無符合條件的「{target_sector}」相關個股。"))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 市場震盪，暫無符合強勢條件的標的。"))
            return
            
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
                reasons_map[item.get('code')] = item.get('reason', '動能強勁。')
        except: pass

        bubbles = []
        for stock in good_stocks:
            default_reason = f"主力控盤，{stock['signal_str']}，多頭排列。"
            reason = reasons_map.get(stock['code'], default_reason)

            # 🔥 [修改處 1] 被動防禦提醒 (推薦卡片)：若帶量突破，短評後方附加警語
            if "量增價漲" in stock['signal_str'] or "RSI過熱" in stock['signal_str']:
                reason += "\n🚨 留意隔日沖倒貨風險"
            
            bubble = {
                "type": "bubble", "size": "hecto",
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

                    # 🔥 籌碼金額 (從 JSON 讀取)
                    {"type": "text", "text": f"💰 近5日法人: {stock.get('chips', 'N/A')}", "size": "sm", "weight": "bold", "color": "#D84315", "align": "center", "margin": "md"},
                    
                    # 🔥 營收 YoY (從 JSON 讀取的新武器！)
                    {"type": "text", "text": f"📈 營收 YoY: {stock.get('yoy_display', 'N/A')}", "size": "sm", "weight": "bold", "color": "#1976D2", "align": "center", "margin": "sm"},
                    
                    {"type": "separator", "margin": "md"},
                    {"type": "text", "text": reason, "size": "xs", "color": "#333333", "wrap": True, "margin": "md"},
                    {"type": "button", "action": {"type": "message", "label": "詳細診斷", "text": stock['code']}, "style": "link", "margin": "md"}
                ]}
            }
            bubbles.append(bubble)
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="AI 精選飆股", contents={"type": "carousel", "contents": bubbles}))
        return
    
    # 🔥 [修改處 2] 隔日沖主動查詢 (版面美化版)
    if msg in ["隔日沖", "主力", "主力分點"]:
        dt_data = get_day_trade_brokers() 
        
        reply_text = (
            f"🚨 【常見隔日沖券商清單】 🚨\n"
            f"📅 更新日期：{dt_data.get('update_date', '未知')}\n"
            f"────────────────\n"
            f"發現股票爆量長紅？盤後請務必檢查是否有以下分點大量買超：\n\n"
        )       
        
        # 歷遍所有分類
        for category, brokers in dt_data.get('brokers', {}).items():
            reply_text += f"🎯 【{category}】\n"
            
            # 將券商名單每 3 個一組強制作斷行，並用「中點」分隔，視覺更乾淨
            for i in range(0, len(brokers), 3):
                chunk = brokers[i:i+3]
                reply_text += " ‧ ".join(chunk) + "\n"
            reply_text += "\n"
            
        reply_text += (
            f"────────────────\n"
            f"💡 實戰技巧：\n"
            f"若上述名單買超合計佔當日總成交量 > 10%~15%，隔天早盤 9:00~9:30 切勿盲目追高！"
        )
        
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return
    # ==========================================
    # 🌟 新增功能 3：召喚【左側黃金坑】(加入時間卡片)
    # ==========================================
    if msg == "左側":
        try:
            # 🔥 修改點：定義 GitHub 遠端連結
            LEFT_SIDE_URL = "https://raw.githubusercontent.com/RodHome/line-bot-lab/main/left_side_value.json"
            
            # 從遠端下載資料
            headers = {'Cache-Control': 'no-cache'} # 確保抓到最新，不被快取
            res = requests.get(LEFT_SIDE_URL, headers=headers, timeout=10)
            
            if res.status_code != 200:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 無法連線至 GitHub 取得左側資料。"))
                return

            left_data = res.json()
            update_str = "最新資料" # 因為是遠端讀取，若要精確時間需從 JSON 內部讀取，或保留原本邏輯
            
            if not left_data:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🛡️ 報告！今日大盤強勢，無符合嚴格超跌標準之錯殺股，請保留資金，耐心等待黃金坑出現！"))
                return # 🔥 必須加上這行：回覆後立刻中斷函式
            else:
                bubbles = []
                # 🛡️ 首張導覽與時間卡片
                info_bubble = {
                    "type": "bubble", "size": "hecto",
                    "body": {
                        "type": "box", "layout": "vertical", "spacing": "sm", "alignItems": "center", "justifyContent": "center",
                        "contents": [
                            {"type": "text", "text": "🛡️ 左側黃金坑", "weight": "bold", "size": "xl", "color": "#1E88E5", "align": "center"},
                            {"type": "text", "text": f"雷達掃描時間\n{update_str}", "size": "xs", "color": "#888888", "align": "center", "wrap": True, "margin": "md"},
                            {"type": "separator", "margin": "lg"},
                            {"type": "text", "text": "👉 向右滑動查看標的", "size": "sm", "color": "#FF8F00", "weight": "bold", "margin": "lg", "align": "center"}
                        ]
                    }
                }
                bubbles.append(info_bubble)

                for item in left_data[:5]:
                    score = int(item.get('score', 50))
                    header_color = "#D32F2F" if score >= 80 else "#00897B"
                    
                    bubble = {
                        "type": "bubble", "size": "hecto",
                        "header": {
                            "type": "box", "layout": "vertical", "contents": [
                                {"type": "text", "text": f"{item['name']} ({item['code']})", "weight": "bold", "size": "lg", "color": "#ffffff"},
                                {"type": "text", "text": f"🏆 信心評分: {score} 分", "size": "sm", "color": "#FFD54F", "weight": "bold"}
                            ], "backgroundColor": header_color
                        },
                        "body": {
                            "type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                                {"type": "text", "text": "📉 均線乖離率", "size": "xs", "color": "#888888", "weight": "bold"},
                                {"type": "text", "text": f"季 {item.get('bias60', 'N/A')} | 月 {item.get('bias24', 'N/A')} | 週 {item.get('bias6', 'N/A')}", "size": "sm", "color": "#333333"},
                                {"type": "separator", "margin": "md"},
                                {"type": "text", "text": "📊 籌碼與動能", "size": "xs", "color": "#888888", "weight": "bold", "margin": "md"},
                                {"type": "text", "text": f"法人連買 {item.get('buy_days', 'N/A')} 天", "size": "sm", "color": "#D84315", "weight": "bold"},
                                {"type": "text", "text": f"量縮至均量 {item.get('vol_ratio', 'N/A')}", "size": "sm", "color": "#1976D2"},
                                {"type": "separator", "margin": "md"},
                                {"type": "text", "text": "💡 實戰策略", "size": "xs", "color": "#888888", "weight": "bold", "margin": "md"},
                                {"type": "text", "text": f"狀態：{item.get('trend_status', '築底中')}", "size": "xs", "color": "#333333", "wrap": True},
                                {"type": "text", "text": f"進場：{item.get('entry_price', 'N/A')} 元分批試單", "size": "xs", "color": "#2E7D32", "weight": "bold"},
                                {"type": "text", "text": "停損：波段最低價跌破 3%", "size": "xs", "color": "#C62828", "weight": "bold"}
                            ]
                        }
                    }
                    bubbles.append(bubble)
                    
                line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="左側黃金坑報告", contents={"type": "carousel", "contents": bubbles}))
                return # 🔥 必須加上這行：確保程式在此停住，不會觸發二次回覆報錯
                
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 左側資料讀取失敗，請確認今日爬蟲是否已執行。"))
        return

   # ==========================================
    # 🌟 新增功能 4：召喚【存股雷達】(分類大選單)
    # ==========================================
    if msg == "存股":
        stock_menu_flex = {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical", "spacing": "md",
                "contents": [
                    {"type": "text", "text": "🏦 存股打折加碼雷達", "weight": "bold", "size": "lg", "color": "#1E88E5"},
                    {"type": "text", "text": "這是開發者的存股名單\n點選下方類別👇", "wrap": True, "size": "sm", "color": "#666666"},
                    {"type": "separator", "margin": "md"},
                    {"type": "button", "style": "primary", "color": "#1E88E5", "action": {"type": "message", "label": "🏦 金融控股", "text": "金融股"}, "margin": "md"},
                    {"type": "button", "style": "primary", "color": "#00897B", "action": {"type": "message", "label": "📈 國民 ETF", "text": "存股 ETF"}, "margin": "sm"},
                    {"type": "button", "style": "primary", "color": "#8E24AA", "action": {"type": "message", "label": "🚀 權值龍頭", "text": "存股 龍頭"}, "margin": "sm"}
                ]
            }
        }
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="存股分類選單", contents=stock_menu_flex))
        return
   
    # ==========================================
    # 🌟 [終極整合版] 存股子分類：遠端讀取 + 雙欄排版 + 加碼區完整邏輯
    # ==========================================
    if msg in ["金融股", "存股 ETF", "存股 龍頭"]:
        try:
            # 1. 改為 GitHub 遠端連結讀取
            DEPOSIT_URL = "https://raw.githubusercontent.com/RodHome/line-bot-lab/main/deposit_stocks.json"
            headers = {'Cache-Control': 'no-cache'}
            res = requests.get(DEPOSIT_URL, headers=headers, timeout=10)
            
            if res.status_code != 200:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 無法從 GitHub 取得存股資料，請稍後再試。"))
                return

            deposit_data = res.json()
            
            # 2. 修正時間語法 (解決當機問題)
            tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
            update_str = tw_now.strftime('%Y-%m-%d')
            
            # 3. 分類邏輯
            category_name = ""
            buy_list = []; hold_list = []; warn_list = []
            for item in deposit_data:
                code = item['code']
                match = False
                if msg == "金融股" and (code.startswith('28') or code.startswith('58')): category_name = "金融控股"; match = True
                elif msg == "存股 ETF" and code.startswith('00'): category_name = "國民 ETF"; match = True
                elif msg == "存股 龍頭" and not code.startswith('00') and not (code.startswith('28') or code.startswith('58')): category_name = "權值龍頭"; match = True
                
                if match:
                    signal_str = item.get('signal', '')
                    if '加碼' in signal_str or '重壓' in signal_str: buy_list.append(item)
                    elif '警示' in signal_str: warn_list.append(item)
                    else: hold_list.append(item)

            # 4. 建立雙欄排版輔助函式 (💡加入 wrap: True 防止長檔名被切斷)
            def build_two_columns_grid(stock_list):
                grid_contents = []
                for i in range(0, len(stock_list), 2):
                    row_contents = []
                    for j in range(2):
                        if i + j < len(stock_list):
                            item = stock_list[i + j]
                            val = item.get('bias_24', item.get('bias_20', 'N/A'))
                            row_contents.append({
                                "type": "text", 
                                "text": f"▪️ {item['name']}({val}%)", 
                                "size": "xxs", 
                                "color": "#666666", 
                                "flex": 1,
                                "wrap": True
                            })
                        else:
                            row_contents.append({"type": "text", "text": " ", "size": "xxs", "flex": 1})
                    grid_contents.append({"type": "box", "layout": "horizontal", "contents": row_contents, "margin": "sm"})
                return grid_contents

            # --- 組裝 Flex Message ---
            flex_contents = [
                {"type": "text", "text": f"🏦 存股雷達：{category_name}", "weight": "bold", "size": "lg", "color": "#1E88E5", "align": "center"},
                {
                    "type": "box", "layout": "horizontal", "margin": "sm", "alignItems": "center",
                    "contents": [
                        {"type": "text", "text": f"資料日期：{update_str}", "size": "xxs", "color": "#9E9E9E", "flex": 0},
                        {"type": "box", "layout": "vertical", "flex": 1}, # 透明推桿
                        {
                            "type": "box", "layout": "vertical", "width": "110px", # 固定寬度紅框
                            "borderColor": "#E57373", "borderWidth": "1px", "cornerRadius": "4px", "paddingAll": "1px",
                            "contents": [{"type": "text", "text": "*(括弧為月線乖離率)*", "size": "xxs", "color": "#D32F2F", "align": "center"}]
                        }
                    ]
                },
                {"type": "separator", "margin": "md"}
            ]

            # --- 1. 🛒 打折加碼區 (🔥 還原遺落的迴圈代碼) ---
            if buy_list:
                flex_contents.append({"type": "text", "text": "🛒 📉 【打折加碼區】", "weight": "bold", "size": "sm", "color": "#D32F2F", "margin": "md"})
                grouped_buys = {}
                common_main_action = ""
                
                for item in buy_list:
                    action_text = item.get('action', '')
                    main_match = re.match(r'^(.*?)(?:\(|（)', action_text)
                    if main_match and not common_main_action:
                        common_main_action = main_match.group(1).strip()

                    sig_match = re.search(r'[\(（](.*?)[）\)]', action_text)
                    sig = sig_match.group(1).strip() if sig_match else "分批建倉"
                    
                    if sig not in grouped_buys: grouped_buys[sig] = []
                    grouped_buys[sig].append(item)

                if common_main_action:
                    flex_contents.append({"type": "text", "text": common_main_action, "size": "xs", "color": "#FF8F00", "wrap": True, "weight": "bold"})

                def sort_groups(k):
                    if "翻正" in k: return 0
                    if "跌勢未止" in k: return 1
                    return 2
                sorted_sigs = sorted(grouped_buys.keys(), key=sort_groups)

                for sig in sorted_sigs:
                    stocks = grouped_buys[sig]
                    sig_color = "#2E7D32" if "翻正" in sig else ("#C62828" if "跌勢" in sig else "#FF8F00")
                    
                    group_box = {
                        "type": "box", "layout": "vertical", "margin": "sm", "spacing": "xs",
                        "contents": [{"type": "text", "text": f"🌟 {sig}" if "翻正" in sig else f"⚠️ {sig}", "weight": "bold", "size": "xs", "color": sig_color, "wrap": True}]
                    }
                    
                    for item in stocks:
                        b6 = item.get('bias_6', 'N/A')
                        b24 = item.get('bias_24', 'N/A')
                        yld = item.get('yield_rate', 'N/A')
                        
                        stats_str = f"月{b24}% | 週{b6}%"
                        
                        group_box["contents"].append({
                            "type": "box", "layout": "vertical", "margin": "xs",
                            "contents": [
                                {
                                    "type": "box", "layout": "horizontal",
                                    "contents": [
                                        {"type": "text", "text": f"▪️ {item['name']}({item['code']})", "weight": "bold", "size": "sm", "color": "#333333", "flex": 5},
                                        {"type": "text", "text": f"殖利率≒{yld}%", "size": "xxs", "color": "#E65100", "align": "end", "flex": 3},
                                        {"type": "text", "text": str(item['price']), "weight": "bold", "size": "sm", "color": "#D32F2F", "align": "end", "flex": 2}
                                    ]
                                },
                                {"type": "text", "text": stats_str, "size": "xxs", "color": "#1976D2", "align": "start"}
                            ]
                        })
                    flex_contents.append(group_box)

            # --- 2. 🟢 平穩定額區 (雙欄顯示) ---
            # 💡 將分隔線與標題全部縮進 if hold_list: 條件內
            if hold_list:
                flex_contents.append({"type": "separator", "margin": "lg"})
                flex_contents.append({"type": "text", "text": "🟢 ⚖️ 【平穩定額區】(維持紀律)", "weight": "bold", "size": "sm", "color": "#2E7D32", "margin": "md"})
                flex_contents.extend(build_two_columns_grid(hold_list))
            # 💡 刪除原本的 else 區塊

            # --- 3. 🚨 過熱觀察區 (雙欄顯示 + 建議) ---
            # 💡 將分隔線與標題全部縮進 if warn_list: 條件內
            if warn_list:
                flex_contents.append({"type": "separator", "margin": "md"})
                flex_contents.append({"type": "text", "text": "🚨 🔥 【過熱觀察區】(調節賺價差)", "weight": "bold", "size": "sm", "color": "#C62828", "margin": "md"})
                flex_contents.append({"type": "text", "text": "*(建議分批獲利了結，待回穩再接回)*", "size": "xxs", "color": "#D32F2F", "wrap": True, "margin": "xs"})
                flex_contents.extend(build_two_columns_grid(warn_list))
            # 💡 刪除原本的 else 區塊 (這樣沒資料就不會印出任何東西)

            final_flex = {"type": "bubble", "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": flex_contents}}
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"存股雷達：{category_name}", contents=final_flex))
            return

        except Exception as e:
            print(f"Error in GitHub deposit fetch: {e}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 存股資料讀取失敗。"))
            return
    
    #=================3/17==========================
    # [功能 2] 個股/ETF 診斷 (優化版)
    stock_id = get_stock_id(msg)
    user_cost = None
    cost_match = re.search(r'(成本|cost)[:\s]*(\d+\.?\d*)', msg, re.IGNORECASE)
    if cost_match: user_cost = float(cost_match.group(2))

    # 🔥 [修改處 3] 防呆引導：攔截無效輸入，回傳 Flex 導覽選單
    # 🔥 防呆引導：攔截無效輸入，回傳 Flex 導覽選單
    if not stock_id:
        welcome_flex = {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical", "spacing": "md",
                "contents": [
                    {"type": "text", "text": "⚠️ 找不到您輸入的代號或指令喔！", "weight": "bold", "color": "#D32F2F", "wrap": True},
                    {"type": "text", "text": "💡 【程式高手 Bot 使用指南】\n請直接輸入股票名稱/代號，或點擊下方按鈕探索三大策略：", "wrap": True, "size": "sm", "color": "#666666"},
                    
                    # --- 三大主力策略區 (用顏色區分) ---
                    {"type": "button", "style": "primary", "color": "#1E88E5", "action": {"type": "message", "label": "🚀 右側動能：今日推薦", "text": "推薦"}, "margin": "md"},
                    {"type": "button", "style": "primary", "color": "#00897B", "action": {"type": "message", "label": "🛡️ 左側價值：超跌黃金坑", "text": "左側"}, "margin": "sm"},
                    {"type": "button", "style": "primary", "color": "#8E24AA", "action": {"type": "message", "label": "🏦 存股雷達：打折加碼區", "text": "存股"}, "margin": "sm"},
                    
                    # --- 個股與進階查詢區 (灰色次要按鈕) ---
                    {"type": "separator", "margin": "lg"},
                    {"type": "button", "style": "secondary", "action": {"type": "message", "label": "🔎 個股評估 (輸入代號)", "text": "如何評估"}, "margin": "md"},
                    {"type": "button", "style": "secondary", "action": {"type": "message", "label": "📊 持股診斷 (帶成本)", "text": "如何診斷"}, "margin": "sm"},
                    {"type": "button", "style": "secondary", "action": {"type": "message", "label": "🚨 隔日沖券商名單", "text": "隔日沖"}, "margin": "sm"},

                    # --- 說明區 (雙按鈕並排) ---
                    {"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "md", "contents": [
                        {"type": "button", "style": "secondary", "color": "#1E88E5", "action": {"type": "message", "label": "🧠 右側邏輯", "text": "右側邏輯"}},
                        {"type": "button", "style": "secondary", "color": "#00897B", "action": {"type": "message", "label": "🧠 左側邏輯", "text": "左側邏輯"}}
                    ]}
                ]
            }
        }
        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="使用導覽", contents=welcome_flex))
        return
    
    if stock_id:
        name = STOCK_META.get(stock_id, {}).get('name', CODE_TO_NAME.get(stock_id, stock_id))

        # 🔥 並行抓取開始
        data = None
        chips_res = ("0 (5日: 0)", "0 (5日: 0)", 0, 0)
        eps = "N/A"
        yield_rate = "N/A"
        
        try:
            # Zeabur 安全設置 max_workers=3
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                future_data = executor.submit(fetch_data_light, stock_id)
                future_chips = executor.submit(fetch_chips_accumulate, stock_id)
                future_eps = executor.submit(fetch_eps, stock_id)
                
                # 必須先等到 data
                data = future_data.result(timeout=8)
                
                if data:
                    future_yield = executor.submit(fetch_dividend_yield, stock_id, data['close'])
                    yield_rate = future_yield.result(timeout=3)
                
                chips_res = future_chips.result(timeout=5)
                eps = future_eps.result(timeout=5)

        except Exception as e:
            print(f"並行錯誤: {e}")
            if not data: data = fetch_data_light(stock_id) # 補救
            if not data: return
        
        f_str, t_str, af_val, at_val = chips_res
        is_etf = stock_id.startswith("00")

        signals = get_technical_signals(data, af_val + at_val)
        signal_str = " | ".join(signals)

        # 🔥 [修改處 4-1] 產生被動防禦字串
        warning_block = ""
        if "🚀量增價漲" in signal_str or "🔥RSI過熱" in signal_str:
            warning_block = "🚨【籌碼防禦】本檔爆量強勢，請留意是否隔日沖分點進駐，嚴防洗盤！\n------------------\n"
        
        if user_cost:
            profit_pct = round((data['close'] - user_cost) / user_cost * 100, 1)
            sys_prompt = "你是操盤手。回傳JSON: analysis(30字內), action(🔴續抱/🟡減碼/⚫停損), strategy(操作建議)。"
            "【規則】：請嚴格檢查數字邏輯。若給出防守價，『大於成本』才可稱為停利，『小於成本』必須稱為停損。"
            user_prompt = f"標的:{name}, 現價:{data['close']}, 成本:{user_cost}, 均線:{data['ma5']}/{data['ma60']}"
            json_str = call_gemini_json(user_prompt, system_instruction=sys_prompt)
            try:
                res = json.loads(json_str)
                # 🔥 [修改處 4-2] 字串尾端加上 warning_block
                reply = f"🩺 **{name}診斷**\n💰 帳面: {profit_pct}%\n【建議】{res['action']}\n【分析】{res['analysis']}\n【策略】{res['strategy']}\n------------------\n{warning_block.strip()}"
            except: reply = "AI 數據解析失敗 (請檢查 Key)。"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return    
                
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
        
        reply = (
        f"📈 **{name}({stock_id})**\n"
        f"{data_dashboard}\n"
        f"------------------\n"
        f"🚩 **指標快篩** :\n"
        f"{signal_str}\n"
        f"------------------\n"
        f"{ai_reply_text}\n"
        f"------------------\n"    
        f"{warning_block}"  # 🔥 [修改處 4-3] 插入警示區塊變數
        f"(版本: {BOT_VERSION})"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
