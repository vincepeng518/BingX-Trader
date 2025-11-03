# app.py
from flask import Flask, render_template, jsonify
import ccxt
import time
from dotenv import load_dotenv
import os
import threading
import asyncio
from telegram import Bot
from collections import deque

# === 載入設定 ===
load_dotenv(encoding='utf-8')
app = Flask(__name__)

# === BingX 設定 ===
exchange = ccxt.bingx({
    'apiKey': os.getenv('BINGX_API_KEY'),
    'secret': os.getenv('BINGX_SECRET'),
    'sandbox': True,  # 改 False 為實盤
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})

symbol = 'XAUT/USDT:USDT'
BASE_UNIT = 0.002          # 調整為 0.002（價值 ≈ 8 USDT，符合最低 5 USDT）
FEE_RATE = 0.0005
DROP_TRIGGER = 0.003
MULTIPLIER = 1.365
PROFIT_THRESHOLD = 0.3
FEE_PENALTY_RATE = 0.001

# === 全域狀態 ===
dashboard_data = {
    'price': 0,
    'long_pos': 0,
    'avg_price': 0,
    'total_cost': 0,
    'total_value': 0,
    'total_pnl': 0,
    'status': '監控中',
    'history': deque(maxlen=300),
    'trades': [],
    'add_count': 0,
    'entry_prices': [],
    'entry_sizes': [],
    'last_add_price': None
}

# === Telegram ===
bot = Bot(token=os.getenv('TELEGRAM_TOKEN'))
chat_id = os.getenv('TELEGRAM_CHAT_ID')

async def send_telegram(msg):
    try:
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
    except Exception as e:
        print(f"Telegram 發送失敗: {e}")

def notify(msg):
    print(msg)
    asyncio.run(send_telegram(msg))

# === 取得持倉 ===
def get_long_position():
    try:
        positions = exchange.fetch_positions([symbol])
        for pos in positions:
            # 關鍵：用 positionSide 判斷
            if pos.get('positionSide') == 'LONG' and float(pos.get('contracts', 0)) > 0:
                return {
                    'size': float(pos['contracts']),
                    'entry': float(pos['entryPrice']) if pos['entryPrice'] else 0
                }
        return {'size': 0, 'entry': 0}
    except Exception as e:
        print(f"持倉查詢錯誤: {e}")
        return {'size': 0, 'entry': 0}

# === 主策略迴圈（修好版）===
def trading_loop():
    global dashboard_data
    last_add_price = None
    dashboard_data['add_count'] = 0
    dashboard_data['entry_prices'] = []
    dashboard_data['entry_sizes'] = []

    while True:
        try:
            # === 取得即時價格 ===
            ticker = exchange.fetch_ticker(symbol)
            price = ticker['last']
            print(f"[{time.strftime('%H:%M:%S')}] trading_loop 正在運行，當前價格: {price}")

            # === 取得持倉 ===
            pos = get_long_position()
            size = pos['size']
            entry = pos['entry']

            # === 逆勢加碼邏輯 ===
            should_add = (
                size == 0 or 
                (last_add_price is not None and price < last_add_price * (1 - DROP_TRIGGER))
            )

            if should_add:
                level = dashboard_data['add_count']
                add_size = BASE_UNIT * (MULTIPLIER ** level)
        
                # === 計算「加入這筆後」的總成本 ===
                current_cost = sum(p * s for p, s in zip(dashboard_data['entry_prices'], dashboard_data['entry_sizes']))
                new_cost = current_cost + price * add_size  # 新增這筆的成本
        
            if should_add:
                level = dashboard_data['add_count']
                add_size = BASE_UNIT * (MULTIPLIER ** level)
                
                # === 直接下單，無資金限制 ===
                order = exchange.create_order(
                    symbol, 'market', 'buy', add_size,
                    params={'positionSide': 'LONG'}
                )
                dashboard_data['add_count'] += 1
                dashboard_data['entry_prices'].append(price)
                dashboard_data['entry_sizes'].append(add_size)
                last_add_price = price

                notify(f"<b>逆勢加碼 第 {dashboard_data['add_count']} 次</b>\n"
                    f"價格: <code>{price:.2f}</code>\n"
                    f"加倉: <code>{add_size:.5f}</code>\n"
                    f"訂單: <code>{order['id']}</code>")
                dashboard_data['trades'].append(f"加碼 {add_size:.5f} @ {price:.2f}")
            # === 動態獲利出場 ===
            total_held_size = sum(dashboard_data['entry_sizes'])  # 本地持倉
            if total_held_size > 0:
                # 用最新價格計算
                market_value = total_held_size * price
                total_cost = sum(p * s for p, s in zip(dashboard_data['entry_prices'], dashboard_data['entry_sizes']))
                gross_pnl = market_value - total_cost
                fee_penalty = market_value * FEE_PENALTY_RATE
                net_pnl = gross_pnl - fee_penalty

                if net_pnl > PROFIT_THRESHOLD:
                    # === 平倉 ===
                    order = exchange.create_order(
                        symbol, 'market', 'sell', total_held_size,
                        params={'positionSide': 'LONG'}
                    )   
                    final_fee = market_value * FEE_RATE
                    final_net = gross_pnl - final_fee

                    notify(f"<b>獲利了結全數出場！</b>\n"
                        f"淨利: <code>{final_net:+.2f}</code> USDT\n"
                        f"訂單: <code>{order['id']}</code>")
                    dashboard_data['trades'].append(f"出場 +{final_net:+.2f}")

                    # === 重置 ===
                    dashboard_data['add_count'] = 0
                    dashboard_data['entry_prices'] = []
                    dashboard_data['entry_sizes'] = []
                    last_add_price = None

            # === 更新儀表板 ===
            total_cost = sum(p * s for p, s in zip(dashboard_data['entry_prices'], dashboard_data['entry_sizes']))
            market_value = size * price
            gross_pnl = market_value - total_cost
            fee_penalty = market_value * FEE_PENALTY_RATE
            net_pnl = gross_pnl - fee_penalty

            dashboard_data.update({
                'price': price,
                'long_pos': size,
                'avg_price': entry,
                'total_cost': total_cost,
                'total_value': market_value,
                'total_pnl': net_pnl,
                'status': f"持倉 {size:.5f} | 淨利 {net_pnl:+.2f} USDT"
            })

            dashboard_data['history'].append({
                'time': int(time.time() * 1000),
                'price': price,
                'size': size,
                'pnl': net_pnl,
                'action': dashboard_data['trades'][-1] if dashboard_data['trades'] else None
            })

            time.sleep(10)

        except Exception as e:
            error_msg = f"錯誤: {e}"
            print(error_msg)
            dashboard_data['status'] = error_msg
            notify(f"<b>程式錯誤</b>\n<code>{e}</code>")
            time.sleep(10)

# === 啟動交易迴圈 ===
threading.Thread(target=trading_loop, daemon=True).start()

# === Flask 路由 ===
@app.route('/')
def dashboard():
    return render_template('dashboard.html')

@app.route('/api/data')
def api_data():
    return jsonify({
        'price': dashboard_data['price'],
        'long_pos': dashboard_data['long_pos'],
        'avg_price': dashboard_data['avg_price'],
        'total_cost': dashboard_data['total_cost'],
        'total_value': dashboard_data['total_value'],
        'total_pnl': dashboard_data['total_pnl'],
        'status': dashboard_data['status'],
        'history': list(dashboard_data['history']),
        'trades': dashboard_data['trades'][-10:],
        'add_count': dashboard_data['add_count']
    })

if __name__ == '__main__':
    # 雲端用 0.0.0.0 + 端口由系統分配
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)