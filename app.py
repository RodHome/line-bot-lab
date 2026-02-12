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

app = Flask(__name__)

# 🟢 [版本號] v18.0 (直球對決版：無廢話、有說明書、讀GitHub)
BOT_VERSION = "v18.0 (Silent Mode)"

# --- 1. 全域資料庫初始化 ---
TWSE_CACHE = {"date": "", "data": []}
ALL_STOCK_DATA = {}      # 大字典
CODE_TO_NAME = {}        # 代號查名稱
ELITE_CODES = []         # 菁英池
SECTOR_INDEX = {}        # 產業索引

# 啟動時讀取資料 (從 stock_list.json)
def load_stock_db():
    global ALL_STOCK_DATA, CODE_TO_NAME, ELITE_CODES, SECTOR_INDEX
    
    # 這裡請確保 stock_list.json 存在 (或是從 GitHub 下載)
    # 建議直接讀取本地檔案 (由 generator.py 產出)
    GITHUB_LIST_URL = "https://raw.githubusercontent.com/RodHome/line-bot-lab/main/stock_list.json"
    
    try:
        print("[System] 載入股票資料庫...")
        headers = {'Cache-Control': 'no-cache'}
        # 如果是 Private Repo，需在此加入 Authorization header
        res = requests.get(GITHUB_LIST_URL, headers=headers, timeout=5)
        
        if res.status_code == 200:
            ALL_STOCK_DATA = res.json()
        elif os.path.exists('stock_list.json'):
            with open('stock_list.json', 'r', encoding='utf-8') as f:
                ALL_STOCK_DATA = json.load(f)
        else:
            ALL_STOCK_DATA = {"2330": {"name": "台積電", "sector": "半導體業", "is_elite": True}}

        # 重建索引
        ELITE_CODES = []
        SECTOR_INDEX = {}
        CODE_TO_NAME = {}

        for code, info in ALL_STOCK_DATA.items():
            name = info.get('name', code)
            CODE_TO_NAME[code] = name
            if info.get('is_elite'): ELITE_CODES.append(code)
            sec = info.get('sector', '其他')
            if sec not in SECTOR_INDEX: SECTOR_INDEX[sec] = []
            SECTOR_INDEX[sec].append(code)
            
        print(f"[System] 資料庫載入完成: {len(ALL_STOCK_DATA)} 檔")

    except Exception as e:
        print(f"[Error] Load DB Failed: {e}")

load_stock_db()

token = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
secret = os.environ.get('LINE_CHANNEL_SECRET')
line_bot_api = LineBotApi(token if token else 'UNKNOWN')
handler = WebhookHandler(secret if secret else 'UNKNOWN')

@app.route("/")
def health_check(): return f"OK ({BOT_VERSION})", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except: abort(400)
    return 'OK'

# --- 2. 輔助函式 ---

def get_taiwan_time_str():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime('%H:%M:%S')

def get_stock_id(text):
    text = text.strip().upper()
    # 移除 "成本" 等干擾詞
    clean = re.sub(r'(成本|cost|價位).*', '', text, flags=re.IGNORECASE).strip()
    
    if clean in ALL_STOCK_DATA: return clean
    for code, name in CODE_TO_NAME.items():
        if clean == name: return code
    if clean.isdigit() and len(clean) >= 4: return clean
    return None

# --- 3. 核心功能：抓資料與 AI ---

def fetch_data_light(stock_id):
    # 簡易抓取逻辑 (Twstock + FinMind)
    try:
        real = twstock.realtime.get(stock_id)
        if not real or not real['success']: return None
        
        latest_price = float(real['realtime']['latest_trade_price']) if real['realtime']['latest_trade_price'] != "-" else 0
        if latest_price == 0: return None # 沒開盤或錯誤

        # 這裡簡化：只抓即時價格，若需完整技術指標需接 FinMind
        # 為了速度，這裡先回傳基本資訊
        change = float(real['realtime']['best_bid_price'][0]) - float(real['realtime']['open']) # 暫時用這個算漲跌
        try:
             # 嘗試抓真實漲跌
             pre_close = float(real['realtime'].get('previous_close', 0))
             if pre_close > 0: change = latest_price - pre_close
        except: pass

        return {
            "code": stock_id,
            "name": ALL_STOCK_DATA.get(stock_id, {}).get('name', stock_id),
            "price": latest_price,
            "change": round(change, 2),
            "volume": real['realtime']['accumulate_trade_volume'],
            "update_time": get_taiwan_time_str(),
            "color": "#D32F2F" if change >= 0 else "#2E7D32"
        }
    except: return None

def call_gemini_analysis(stock_data, user_msg):
    # 呼叫 Gemini 產生評語
    keys = [os.environ.get(f'GEMINI_API_KEY_{i}') for i in range(1, 7) if os.environ.get(f'GEMINI_API_KEY_{i}')]
    if not keys and os.environ.get('GEMINI_API_KEY'): keys = [os.environ.get('GEMINI_API_KEY')]
    if not keys: return "AI 金鑰未設定，無法分析。"
    
    key = random.choice(keys)
    prompt = f"你是股市分析師。股票 {stock_data['name']}({stock_data['code']}) 現價 {stock_data['price']}，漲跌 {stock_data['change']}。用戶輸入：「{user_msg}」。請用繁體中文給出 50 字以內的精簡操作建議，包含支撐壓力點位。"
    
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={key}"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        res = requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=10)
        if res.status_code == 200:
            return res.json()['candidates'][0]['content']['parts'][0]['text']
    except: pass
    return "AI 連線忙碌中，建議觀察量能變化。"

# --- 4. 產生 Flex Messages ---

# A. 股票卡片 (診斷結果)
def create_stock_flex(stock_data, ai_comment):
    color = stock_data['color']
    sign = "+" if stock_data['change'] >= 0 else ""
    return {
      "type": "bubble",
      "size": "mega",
      "header": {
        "type": "box", "layout": "vertical", "backgroundColor": color,
        "contents": [
          {"type": "text", "text": f"{stock_data['name']} {stock_data['code']}", "color": "#FFFFFF", "weight": "bold", "size": "xl"},
          {"type": "text", "text": f"{stock_data['price']} ({sign}{stock_data['change']})", "color": "#FFFFFF", "size": "lg", "margin": "sm"}
        ]
      },
      "body": {
        "type": "box", "layout": "vertical", "contents": [
          {"type": "text", "text": "📊 AI 觀點", "weight": "bold", "color": "#1A237E"},
          {"type": "text", "text": ai_comment, "wrap": True, "size": "sm", "margin": "md", "color": "#555555"},
          {"type": "separator", "margin": "lg"},
          {"type": "box", "layout": "horizontal", "margin": "lg", "contents": [
            {"type": "text", "text": f"量能: {stock_data['volume']} 張", "size": "xs", "color": "#999999"},
            {"type": "text", "text": f"更新: {stock_data['update_time']}", "size": "xs", "color": "#999999", "align": "end"}
          ]}
        ]
      }
    }

# B. 幫助選單 (說明書)
def get_help_flex_message():
    return {
      "type": "bubble",
      "header": {
        "type": "box", "layout": "vertical", "contents": [
          {"type": "text", "text": "🤖 股市智囊使用指南", "weight": "bold", "size": "lg", "color": "#FFFFFF"}
        ], "backgroundColor": "#1A237E"
      },
      "body": {
        "type": "box", "layout": "vertical", "contents": [
          {"type": "text", "text": "您可以輸入以下指令：", "size": "xs", "color": "#8C8C8C", "margin": "md"},
          {"type": "separator", "margin": "md"},
          # 區塊 1
          {"type": "box", "layout": "vertical", "margin": "lg", "contents": [
            {"type": "text", "text": "🔍 個股診斷", "weight": "bold", "size": "md"},
            {"type": "text", "text": "輸入「代號」或「名稱」", "size": "sm", "color": "#666666"},
            {"type": "text", "text": "範例：2330 / 台積電 成本:600", "size": "xs", "color": "#999999"}
          ]},
          # 區塊 2 (Top 50 特徵)
          {"type": "box", "layout": "vertical", "margin": "lg", "contents": [
            {"type": "text", "text": "🔥 每日飆股推薦", "weight": "bold", "size": "md"},
            {"type": "text", "text": "輸入「推薦」獲取熱門 Top 50", "size": "sm", "color": "#666666"},
            {"type": "box", "layout": "vertical", "margin": "sm", "backgroundColor": "#F0F2F5", "paddingAll": "8px", "contents": [
              {"type": "text", "text": "✅ 成交量 > 2000張", "size": "xs", "color": "#444444"},
              {"type": "text", "text": "✅ 當日收紅、股價 > 10元", "size": "xs", "color": "#444444"},
              {"type": "text", "text": "✅ 排除 ETF/權證，專注個股", "size": "xs", "color": "#444444"}
            ]}
          ]},
          # 區塊 3
          {"type": "box", "layout": "vertical", "margin": "lg", "contents": [
            {"type": "text", "text": "🎯 產業龍頭", "weight": "bold", "size": "md"},
            {"type": "text", "text": "範例：推薦 航運 / 推薦 半導體", "size": "xs", "color": "#999999"}
          ]}
        ]
      },
      "footer": {
        "type": "box", "layout": "vertical", "contents": [
          {"type": "button", "action": {"type": "message", "label": "立即體驗「推薦」", "text": "推薦"}, "style": "primary", "color": "#1A237E"}
        ]
      }
    }

# --- 5. 推薦邏輯 (讀 GitHub) ---

def fetch_twse_candidates():
    GITHUB_REC_URL = "https://raw.githubusercontent.com/RodHome/line-bot-lab/main/daily_recommendations.json"
    global TWSE_CACHE
    today = (datetime.now(timezone.utc)+timedelta(hours=8)).strftime('%Y%m%d')
    
    if TWSE_CACHE['date'] == today and TWSE_CACHE['data']: return TWSE_CACHE['data']
    
    try:
        res = requests.get(GITHUB_REC_URL, headers={'Cache-Control': 'no-cache'}, timeout=5)
        if res.status_code == 200:
            data = res.json()
            TWSE_CACHE = {"date": today, "data": data}
            return data
    except: pass
    return []

def scan_recommendations_turbo(target_sector=None):
    candidates_pool = []
    twse_list = fetch_twse_candidates() # Top 50
    
    if target_sector:
        if twse_list:
            # 在 Top 50 找產業
            pool = [c for c in twse_list if target_sector in ALL_STOCK_DATA.get(c, {}).get('sector', '')]
            candidates_pool = pool if pool else [c for c in ELITE_CODES if target_sector in ALL_STOCK_DATA.get(c, {}).get('sector', '')]
        else:
            candidates_pool = [c for c in ELITE_CODES if target_sector in ALL_STOCK_DATA.get(c, {}).get('sector', '')]
    else:
        # 一般推薦 (隨機 5 檔)
        if twse_list:
            random.shuffle(twse_list)
            candidates_pool = twse_list[:5] # 只取 5 檔，速度最快
        else:
            candidates_pool = random.sample(ELITE_CODES, 5)

    # 轉成卡片格式 (這裡簡化，不爬太深技術指標，只顯示基本面，確保秒回)
    results = []
    for code in candidates_pool:
        d = fetch_data_light(code)
        if d: results.append(d)
        
    return results

# --- 6. 訊息處理主入口 ---

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    
    # [功能 A] 推薦
    if msg.startswith("推薦") or msg.startswith("選股"):
        parts = msg.split()
        target_sector = parts[1] if len(parts) > 1 else None
        
        stocks = scan_recommendations_turbo(target_sector)
        
        if not stocks:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("⚠️ 目前無符合條件標的"))
            return
            
        # 製作推薦結果卡片 (Carousel)
        bubbles = []
        for s in stocks:
            bubbles.append(create_stock_flex(s, f"熱門標的：{s['code']}"))
            
        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(alt_text="推薦結果", contents={"type": "carousel", "contents": bubbles})
        )
        return

    # [功能 B] 個股診斷 (無廢話版)
    stock_id = get_stock_id(msg)
    if stock_id:
        # 🔥 這裡不回傳 "正在分析"，直接運算
        data = fetch_data_light(stock_id)
        if data:
            ai_comment = call_gemini_analysis(data, msg)
            flex = create_stock_flex(data, ai_comment)
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"{data['name']} 分析報告", contents=flex))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("❌ 查無此股票資料"))
        return

    # [功能 C] 預設：功能說明書 (Flex Message)
    flex_help = get_help_flex_message()
    line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="使用指南", contents=flex_help))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
