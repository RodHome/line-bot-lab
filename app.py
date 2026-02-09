import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError

app = Flask(__name__)

token = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
secret = os.environ.get('LINE_CHANNEL_SECRET')

# 容錯處理：如果沒設定金鑰，不會馬上崩潰，方便先部署
line_bot_api = LineBotApi(token) if token else None
handler = WebhookHandler(secret) if secret else None

@app.route("/")
def hello():
    return "🟢 Lab Bot is Running!", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        if handler: handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    if line_bot_api:
        msg = event.message.text
        # 回傳一樣的字，證明活著
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"測試機收到：{msg}")
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
