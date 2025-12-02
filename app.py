# app.py - 雙向獨立馬丁終極版（下跌加多 / 上漲加空）
from flask import Flask, render_template, jsonify
import ccxt
import time
import os
import threading
import requests

app = Flask(__name__, template_folder='templates')

symbol = 'XAUT/USDT:USDT'
# ==================== BingX ====================
try:
    exchange = ccxt.bingx({
        'apiKey': os.getenv('BINGX_API_KEY'),
        'secret': os.getenv('BINGX_SECRET'),
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'},
    })
    exchange.set_sandbox_mode(os.getenv('SANDBOX', 'true').lower() == 'true')
    
    # 強制加載市場資料（Render 必備！）
    exchange.load_markets()
    print(f"市場載入成功，共 {len(exchange.markets)} 個交易對")
    
    market = exchange.market(symbol)  # 現在安全了
    print("XAUT/USDT:USDT 交易對已就緒")
except Exception as e:
    print(f"BingX 初始化失敗: {e}")
    exchange = None
    market = None



# ==================== 雙向參數（可獨立調整）===================
LONG_BASE     = 0.0005
LONG_MULT     = 1.33
LONG_GRID1    = 0.0005   # 前12筆 0.05%
LONG_GRID2    = 0.0010   # 第13筆起 0.10%
LONG_PROFIT   = 0.05

SHORT_BASE    = 0.0005
SHORT_MULT    = 1.33
SHORT_GRID1   = 0.0005
SHORT_GRID2   = 0.0010
SHORT_PROFIT  = 0.05

# ==================== 狀態 ====================
state = {
    'price': 0.0,
    'long_size': 0.0, 'long_entries': [], 'long_pnl': 0.0,
    'short_size': 0.0, 'short_entries': [], 'short_pnl': 0.0,
    'status': '初始化中...', 'trades': []
}

long_last_grid = None
short_last_grid = None

# ==================== Telegram ====================
def tg(text):
    token = os.getenv('TELEGRAM_TOKEN')
    chat = os.getenv('TELEGRAM_CHAT_ID')
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={'chat_id': chat, 'text': text, 'parse_mode': 'HTML'}, timeout=8)
        except: pass

def notify(msg): print(f"[{time.strftime('%H:%M:%S')}] {msg}"); tg(msg)

# ==================== 工具 ====================
market = exchange.market(symbol)
TICK = 10 ** -market['precision']['price']
LOT  = 10 ** -market['precision']['amount']
MINQ = market['limits']['amount']['min']

def qty(q): return max(MINQ, round(q / LOT) * LOT)

def get_pos():
    try:
        for p in exchange.fetch_positions([symbol]):
            contracts = p['contracts']
            side = p['side']
            entry = float(p['entryPrice'] or 0)
            if contracts > 0:
                if side == 'long':  return float(contracts), entry, 0.0, 0
                if side == 'short': return 0.0, 0, float(contracts), entry
        return 0,0,0,0
    except: return 0,0,0,0

def sync():
    l_size, l_entry, s_size, s_entry = get_pos()
    state['long_size'] = l_size
    state['short_size'] = s_size

# ==================== 多單邏輯 ====================
# 原本的 add_long() 改成：
def long_add():
    q = qty(LONG_BASE * (LONG_MULT ** len(state['long_entries'])))
    if open_long(q):
        state['long_entries'].append({'price': state['price'], 'size': q})
        state['trades'].append(f"多單加碼 {q:.6f}")
        notify(f"多單加碼 第{len(state['long_entries'])}筆\n{q:.6f} 張")
        global long_last_grid
        long_last_grid = state['price']

# 原本的 short_add() 改成：
def short_add():
    q = qty(SHORT_BASE * (SHORT_MULT ** len(state['short_entries'])))
    if open_short(q):
        state['short_entries'].append({'price': state['price'], 'size': q})
        state['trades'].append(f"空單加碼 {q:.6f}")
        notify(f"空單加碼 第{len(state['short_entries'])}筆\n{q:.6f} 張")
        global short_last_grid
        short_last_grid = state['price]
# ==================== 必開單版下單函數 ====================

def open_long(qty):
    try:
        exchange.create_order(
            symbol=symbol,
            type='market',
            side='buy',
            amount=qty,
            params={
                'positionSide': 'LONG',
                'reduceOnly': False          # 關鍵！必須加這行！
            }
        )
        return True
    except Exception as e:
        print(f"開多失敗: {e}")
        return False

def open_short(qty):
    try:
        exchange.create_order(
            symbol=symbol,
            type='market',
            side='sell',
            amount=qty,
            params={
                'positionSide': 'SHORT',
                'reduceOnly': False          # 關鍵！必須加這行！
            }
        )
        return True
    except Exception as e:
        print(f"開空失敗: {e}")
        return False

def close_long():
    if state['long_size'] <= 0: return
    exchange.create_order(symbol, 'market', 'sell', state['long_size'],
                         params={'positionSide': 'LONG', 'reduceOnly': True})

def close_short():
    if state['short_size'] <= 0: return
    exchange.create_order(symbol, 'market', 'buy', state['short_size'],
                         params={'positionSide': 'SHORT', 'reduceOnly': True})

# ==================== 空單邏輯 ====================
def short_add():
    q = qty(SHORT_BASE * (SHORT_MULT ** len(state['short_entries'])))
    exchange.create_market_sell_order(symbol, q, params={'positionSide': 'SHORT'})
    state['short_entries'].append({'price': state['price'], 'size': q})
    state['trades'].append(f"空單加碼 {q:.6f}")
    notify(f"🔴 <b>空單加碼 第{len(state['short_entries'])}筆</b>\n{q:.6f} 張 @ {state['price']:.2f}")

def short_close():
    if state['short_size'] == 0: return
    exchange.create_market_buy_order(symbol, state['short_size'], params={'positionSide': 'SHORT'})
    pnl = (sum(e['price']*e['size'] for e in state['short_entries'])/state['short_size'] - state['price']) * state['short_size']
    notify(f"🔴 <b>空單全平！獲利 {pnl:+.2f} USDT</b>")
    state['short_entries'].clear()
    state['trades'].append(f"空單出場 +{pnl:+.2f}")

# ==================== 主迴圈 ====================
def run():
    global long_last_grid, short_last_grid
    long_last_grid = short_last_grid = None

    while True:
        try:
            ticker = exchange.fetch_ticker(symbol)
            state['price'] = ticker['last']
            sync()

            # 多單加碼（價格下跌）
                        if state['long_size'] == 0 and state['short_size'] == 0:
                # 強制先開一張測試（你自己決定多或空）
                if open_long(LONG_BASE):
                    long_last_grid = state['price']
                    state['long_entries'].append({'price': state['price'], 'size': LONG_BASE})
                    notify("強制開多首倉成功！機器人已活！")
                elif open_short(SHORT_BASE):
                    short_last_grid = state['price']
                    state['short_entries'].append({'price': state['price'], 'size': SHORT_BASE})
                    notify("強制開空首倉成功！機器人已活！")
                time.sleep(10)
                continue

            # 空單加碼（價格上漲）
            if state['short_size'] > 0 and short_last_grid is not None:
                grid = SHORT_GRID1 if len(state['short_entries']) < 12 else SHORT_GRID2
                if state['price'] >= short_last_grid * (1 + grid):
                    short_add()
                    short_last_grid = state['price']

            # 多單出場
            if state['long_size'] > 0:
                long_cost = sum(e['price']*e['size'] for e in state['long_entries']) / state['long_size']
                if state['price'] >= long_cost + LONG_PROFIT / state['long_size']:
                    long_close()
                    long_last_grid = None

            # 空單出場
            if state['short_size'] > 0:
                short_cost = sum(e['price']*e['size'] for e in state['short_entries']) / state['short_size']
                if state['price'] <= short_cost - SHORT_PROFIT / state['short_size']:
                    short_close()
                    short_last_grid = None

            # 首倉邏輯（可選：第一次上漲開空，下跌開多）
            if state['long_size'] == 0 and state['short_size'] == 0:
                if ticker['change'] > 0:   # 上漲先開空
                    short_add()
                    short_last_grid = state['price']
                else:
                    long_add()
                    long_last_grid = state['price']

            state['status'] = f"多{state['long_size']:.6f}｜空{state['short_size']:.6f}｜價{state['price']:.1f}"
            time.sleep(7)
        except Exception as e:
            print("錯誤:", e)
            time.sleep(10)

# ==================== Flask ====================
@app.route('/')
def index(): return render_template('dashboard.html')

@app.route('/api/data')
def api():
    sync()
    return jsonify(state)

if __name__ == '__main__':
    threading.Thread(target=run, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))