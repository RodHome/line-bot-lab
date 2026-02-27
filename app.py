import os, requests, random, re
import json
import time
import math
import concurrent.futures
# import twstock
from datetime import datetime, timedelta, time as dtime, timezone
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage

app = Flask(__name__)

# 🤖 [版本號] v17.2 
BOT_VERSION = "v17.2 (程式碼優化)"

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
# --- 🔥 優化版：數據擷取 (移除了會卡死的 twstock，改用極速 API 與防呆機制) ---
def fetch_data_light(stock_id):
    token = os.environ.get('FINMIND_TOKEN', '')
    url_hist = "https://api.finmindtrade.com/api/v4/data"
    hist_data = []
    
    # 1. 抓取歷史資料 (帶 4 秒極限 timeout)
    try:
        start = (datetime.now() - timedelta(days=120)).strftime('%Y-%m-%d')
        res = requests.get(url_hist, params={
            "dataset": "TaiwanStockPrice", "data_id": stock_id, "start_date": start, "token": token
        }, timeout=4)
        hist_data = res.json().get('data', [])
    except Exception as e:
        print(f"[Warn] FinMind 歷史股價抓取失敗: {e}")
        
    if not hist_data: return None

    # 2. 抓取即時股價 (替換掉 twstock，改用極速 Yahoo API)
    latest_price = 0
    source_name = "歷史"
    update_time = get_taiwan_time_str()
    
    try:
        # 簡單判斷上市上櫃後綴 (.TW 或 .TWO)
        suffix = ".TW" if len(stock_id) == 4 and stock_id.startswith(('1', '2', '3', '4', '5', '9')) else ".TWO"
        if stock_id.startswith('00'): suffix = ".TW" # ETF 預設給上市
        
        y_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock_id}{suffix}"
        y_res = requests.get(y_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=2) # 只要 2 秒拿不到就果斷放棄，絕不卡死
        if y_res.status_code == 200:
            y_data = y_res.json()
            meta = y_data['chart']['result'][0]['meta']
            if 'regularMarketPrice' in meta:
                latest_price = float(meta['regularMarketPrice'])
                source_name = "即時"
    except Exception as e:
        pass # Yahoo 失敗就算了，我們還有歷史收盤價當底線

    if latest_price == 0:
        latest_price = hist_data[-1]['close']

    closes = [d['close'] for d in hist_data]
    highs = [d['max'] for d in hist_data]
    lows = [d['min'] for d in hist_data]
    volumes = [d['Trading_Volume'] for d in hist_data]

    today_str = datetime.now().strftime('%Y-%m-%d')
    hist_last_date = hist_data[-1]['date']

    if hist_last_date != today_str and source_name == "即時":
        closes.append(latest_price)
        highs.append(latest_price)
        lows.append(latest_price)
        volumes.append(0)
    elif hist_last_date == today_str and source_name == "即時":
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
            # 直接取推薦池算好、最強的前 8 檔
            candidates_pool = twse_list[:8] 
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

    # 🔥 [新增功能] 選股邏輯說明
    if msg in ["選股邏輯", "推薦說明", "篩選條件"]:
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
            "1. 結構性風險： 系統優先選取「上市」優質標的，自動過濾流動性較低、波動劇烈且資訊透明度較差的興櫃標的。\n"
            "2. 純題材炒作： 股價雖漲，但最新月營收年增率未達 10%，顯示上漲缺乏業績支撐，極易出現「假突破、真倒貨」。\n"
            "3. 缺乏法人背書： 漲勢若由短線主力或散戶衝動推升（法人買超未達 3 億），籌碼結構相對鬆散，不符合我們「穩中求噴」的選股精神。\n\n"
            "📌 我們的鐵律：只推薦有「基本面」與「大資金」雙重背書的優質標的！"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=logic_text))
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
        
    # [功能 2] 個股/ETF 診斷 (優化版)
    stock_id = get_stock_id(msg)
    user_cost = None
    cost_match = re.search(r'(成本|cost)[:\s]*(\d+\.?\d*)', msg, re.IGNORECASE)
    if cost_match: user_cost = float(cost_match.group(2))

    # 🔥 [修改處 3] 防呆引導：攔截無效輸入，回傳 Flex 導覽選單
    if not stock_id:
        welcome_flex = {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical", "spacing": "md",
                "contents": [
                    {"type": "text", "text": "⚠️ 找不到您輸入的代號或指令喔！", "weight": "bold", "color": "#D32F2F", "wrap": True},
                    {"type": "text", "text": "💡 【程式高手 Bot 使用指南】\n請直接輸入股票名稱/代號，或點擊下方按鈕探索功能：", "wrap": True, "size": "sm", "color": "#666666"},
                    
                    # 第一顆按鈕：全市場飆股推薦
                    {"type": "button", "style": "primary", "color": "#1E88E5", "action": {"type": "message", "label": "🚀 今日推薦", "text": "推薦"}, "margin": "md"},
                    
                    # 第二顆按鈕：(新增) 單純詢問個股，不帶成本
                    {"type": "button", "style": "secondary", "action": {"type": "message", "label": "🔎 個股評估", "text": "台積電"}},
                    
                    # 第三顆按鈕：帶有成本的持股健檢
                    {"type": "button", "style": "secondary", "action": {"type": "message", "label": "📊 持股診斷", "text": "2330 成本 1800"}},
                    
                    # 第四顆按鈕：隔日沖名單查詢
                    {"type": "button", "style": "secondary", "action": {"type": "message", "label": "🚨 隔日沖券商名單", "text": "隔日沖"}},

                    # 🔥 [新增] 第 5 顆按鈕：選股邏輯說明
                    {"type": "button", "style": "secondary", "color": "#F57C00", "action": {"type": "message", "label": "🧠 AI 選股邏輯說明", "text": "選股邏輯"}}
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
            # 🔥 將原本的 e 改為 repr(e)，這樣就算是 TimeoutError 也能印出確切原因
            print(f"並行錯誤: {repr(e)}")
            
            # 🛑 絕對不能再呼叫一次 fetch_data_light！逾時就是逾時了，果斷告訴使用者，避免伺服器被砍！
            if not data: 
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 網路連線擁塞，讀取股價資料逾時，請稍後再試。"))
                return
        
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
