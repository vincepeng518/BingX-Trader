# app.py - 2025 最終穩定版 XAUT 馬丁機器人（Render 完美運行）
from flask import Flask, render_template, jsonify
import ccxt
import time
import os
import threading
import requests
from dotenv import load_dotenv

load_dotenv()

# Render 必須這樣寫才找得到 templates
app = Flask(__name__, template_folder='templates')

# ==================== BingX ====================
try:
    exchange = ccxt.bingx({
        'apiKey': os.getenv('BINGX_API_KEY'),
        'secret': os.getenv('BINGX_SECRET'),
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'},
    })
    exchange.set_sandbox_mode(os.getenv('SANDBOX', 'true').lower() == 'true')
    print("BingX 連線成功")
except Exception as e:
    print(f"BingX 連線失敗: {e}")
    exchange = None

symbol = 'XAUT/USDT:USDT'

# ==================== 參數 ====================
BASE_SIZE = 0.0005                 # 首倉固定 0.0005 張
MULTIPLIER = 1.33
GRID_PCT_1 = 0.0005                # 前12筆 0.05%
GRID_PCT_2 = 0.0010                # 第13筆起 0.10%
PROFIT_PER_GRID = 0.05

# ==================== 精度 ====================
def load_precision():
    try:
        market = exchange.market(symbol)
        return (
            10 ** -market['precision']['price'],
            10 ** -market['precision']['amount'],
            market['limits']['amount']['min'] or 0.000001
        )
    except:
        return 0.01, 0.000001, 0.000001

TICK_SIZE, LOT_SIZE, MIN_QTY = load_precision()
def fmt_qty(q): return max(MIN_QTY, round(q / LOT_SIZE) * LOT_SIZE)

# ==================== 全域狀態 ====================
state = {
    'price': 0.0, 'long_size': 0.0, 'long_entry': 0.0,
    'entries': [], 'status': '初始化中...', 'trades': [], 'total_pnl': 0.0
}
TRADING_ENABLED = True
peak_price = 0.0
alert_sent = False
last_grid_price = None

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# ==================== Telegram 穩定發送 ====================
def send_tg(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}, timeout=10)
    except:
        pass

def notify(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")
    send_tg(msg)

# ==================== BingX 同步 ====================
def sync_positions():
    try:
        size, entry = 0.0, 0.0
        if exchange:
            pos = exchange.fetch_positions([symbol])
            for p in pos:
                if p['contracts'] > 0 and p['side'] == 'long':
                    size = float(p['contracts'])
                    entry = float(p['entryPrice'] or 0)
        state['long_size'] = size
        state['long_entry'] = entry
        if size > 0 and not state['entries']:
            state['entries'] = [{'price': entry, 'size': size}]
            notify(f"同步持倉 {size:.6f} @ {entry:.2f}")
    except Exception as e:
        print(f"同步失敗: {e}")

# ==================== 核心 ====================
def calc_pnl():
    if not state['entries']: return 0.0
    cost = sum(e['price'] * e['size'] for e in state['entries'])
    value = sum(e['size'] for e in state['entries']) * state['price']
    return value - cost - value*0.0005*2

def should_exit():
    if not state['entries']: return False
    return calc_pnl() >= PROFIT_PER_GRID * len(state['entries'])

def add_long(size):
    if not TRADING_ENABLED: return
    qty = fmt_qty(size)
    if qty <= 0: return
    try:
        if exchange:
            exchange.create_market_buy_order(symbol, qty, params={'positionSide': 'LONG'})
        state['entries'].append({'price': state['price'], 'size': qty})
        state['trades'].append(f"加倉 {qty:.6f} @ {state['price']:.2f}")
        notify(f"<b>加倉成功 第{len(state['entries'])}筆</b>\n{qty:.6f} 張")
        sync_positions()
    except Exception as e:
        notify(f"<b>加倉失敗</b>\n{e}")

def close_all():
    size = state['long_size']
    if size == 0: return
    try:
        if exchange:
            exchange.create_market_sell_order(symbol, fmt_qty(size), params={'positionSide': 'LONG'})
        pnl = calc_pnl()
        notify(f"<b>全平成功！淨利 {pnl:+.2f} USDT</b>")
        state['trades'].append(f"全平 +{pnl:+.2f}")
        state['entries'].clear()
        sync_positions()
    except Exception as e:
        notify(f"<b>平倉失敗</b>\n{e}")

# ==================== 手動控制路由（正確分行版）===================
@app.route('/tg/status')
def tg_status():
    text = get_status_text()
    send_tg(text)
    return "Status 已發送"

@app.route('/tg/pause')
def tg_pause():
    global TRADING_ENABLED
    TRADING_ENABLED = False
    send_tg("交易已手動暫停")
    return "Paused"

@app.route('/tg/resume')
def tg_resume():
    global TRADING_ENABLED
    TRADING_ENABLED = True
    send_tg("交易已手動恢復")
    return "Resumed"

@app.route('/tg/close')
def tg_close():
    send_tg("強制全平執行中...")
    threading.Thread(target=close_all, daemon=True).start()
    return "Closing..."

def get_status_text():
    sync_positions()
    pnl = calc_pnl()
    drawdown = 0.0
    if peak_price > 100:
        drawdown = (peak_price - state['price']) / peak_price * 100
    if not state['entries']:
        return f"<b>無持倉</b>\n金價 {state['price']:.2f}\n狀態 {'運行中' if TRADING_ENABLED else '已暫停'}"
    lines = [f"<b>持倉明細（{len(state['entries'])}筆）</b>"]
    total = 0.0
    for i, e in enumerate(state['entries'], 1):
        val = e['size'] * e['price']
        total += val
        lines.append(f"{i:>2} │ {e['size']:8.6f} │ {e['price']:7.2f} │ ${val:7.2f}")
    avg = total / state['long_size'] if state['long_size']>0 else 0
    lines += ["", f"總手數: <code>{state['long_size']:.6f}</code>",
              f"平均: <code>{avg:.2f}</code>", f"盈虧: <code>{pnl:+.2f}</code>", f"回撤: <code>{drawdown:+.2f}%</code>"]
    return "\n".join(lines)

# ==================== 主迴圈 ====================
def trading_loop():
    global last_grid_price, peak_price, alert_sent
    first = True
    last_grid_price = None
    peak_price = 0.0
    alert_sent = False
    print("交易迴圈啟動")
    while True:
        try:
            if not TRADING_ENABLED:
                time.sleep(10)
                continue
            if not exchange:
                time.sleep(30)
                continue

            ticker = exchange.fetch_ticker(symbol)
            state['price'] = ticker['last']
            sync_positions()

            if first and state['long_size'] == 0:
                add_long(BASE_SIZE)
                last_grid_price = state['price']
                peak_price = state['price']
                alert_sent = False
                first = False
                continue

            if state['long_size'] > 0 and should_exit():
                close_all()
                last_grid_price = None
                continue

            if state['long_size'] > 0 and last_grid_price:
                grid = GRID_PCT_1 if len(state['entries']) < 12 else GRID_PCT_2
                if state['price'] <= last_grid_price * (1 - grid):
                    size = BASE_SIZE * (MULTIPLIER ** len(state['entries']))
                    add_long(size)
                    last_grid_price = state['price']

            if state['price'] > peak_price:
                peak_price = state['price']
                alert_sent = False
            dd = (peak_price - state['price']) / peak_price if peak_price > 0 else 0
            if 0.010 < dd <= 0.013 and not alert_sent and state['entries']:
                notify(f"<b>大波動預警！</b> 從 {peak_price:.1f} 跌 {dd*100:.2f}%")
                alert_sent = True

            state['status'] = f"持倉 {state['long_size']:.6f} | {len(state['entries'])}筆 | 盈虧 {calc_pnl():+.2f}"
            time.sleep(8)
        except Exception as e:
            print(f"迴圈異常: {e}")
            time.sleep(15)

# ==================== Flask ====================
@app.route('/')
def home():
    return render_template('dashboard.html')

@app.route('/api/data')
def api():
    sync_positions()
    return jsonify(state)

# ==================== 啟動 ====================
if __name__ == '__main__':
    threading.Thread(target=trading_loop, daemon=True).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)