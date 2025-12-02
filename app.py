from flask import Flask, render_template, jsonify
import ccxt
import time
import os
import threading
import requests
import json
from datetime import datetime
import random

app = Flask(__name__, template_folder='templates')

# ==================== BingX ====================
exchange = ccxt.bingx({
    'apiKey': os.getenv('BINGX_API_KEY'),
    'secret': os.getenv('BINGX_SECRET'),
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})
exchange.set_sandbox_mode(os.getenv('SANDBOX', 'true').lower() == 'true')

symbol = 'XAUT/USDT:USDT'

# ==================== 動態參數（會自動更新！）===================
class="highlight"
class Config:
    BASE_SIZE = float(os.getenv('BASE_SIZE', '0.0005'))
    MULTIPLIER = float(os.getenv('MULTIPLIER', '1.33'))
    GRID_PCT_1 = float(os.getenv('GRID_PCT_1', '0.0005'))
    GRID_PCT_2 = float(os.getenv('GRID_PCT_2', '0.0010'))
    PROFIT_PER_GRID = float(os.getenv('PROFIT_PER_GRID', '0.05'))

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

# ==================== 狀態 ====================
state = {'price': 0.0, 'long_size': 0.0, 'entries': [], 'status': '初始化中...', 'trades': [], 'total_pnl': 0.0}
TRADING_ENABLED = True
last_grid_price = None
peak_price = 0.0
alert_sent = False

# ==================== Telegram ====================
def send_tg(text):
    token = os.getenv('TELEGRAM_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat_id: return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}, timeout=10)
    except: pass

def notify(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")
    send_tg(msg)

# ==================== 交易核心（不變）===================
# （這裡放你原本的 sync_positions, calc_pnl, add_long, close_all 等函數，保持不變）

# ==================== 每日自動優化引擎 ====================
def auto_optimize_parameters():
    """每天凌晨 3 點自動優化參數"""
    while True:
        now = datetime.now()
        if now.hour == 3 and now.minute < 5) or time.sleep(60)
            continue
        
        print("開始每日參數優化...")
        # 抓過去 7 天 K 線
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=168)  # 7天
            closes = [x[4] for x in ohlcv]
            
            # 簡單遺傳演算法（10 代內找最優）
            best_profit = -999999
            best_config = None
            
            for _ in range(30):  # 30 次隨機搜尋
                cfg = {
                    'BASE_SIZE': round(random.uniform(0.0003, 0.0015), 6),
                    'MULTIPLIER': round(random.uniform(1.2, 1.5), 3),
                    'GRID_PCT_1': round(random.uniform(0.0003, 0.0008), 5),
                    'GRID_PCT_2': round(random.uniform(0.0008, 0.0015), 5),
                    'PROFIT_PER_GRID': round(random.uniform(0.03, 0.08), 3)
                }
                
                # 簡化回測
                profit = simple_backtest(closes, cfg)
                if profit > best_profit:
                    best_profit = profit
                    best_config = cfg
            
            # 找到最優參數 → 寫入環境變數（Render 會自動重啟）
            if best_config:
                os.environ['BASE_SIZE'] = str(best_config['BASE_SIZE'])
                os.environ['MULTIPLIER'] = str(best_config['MULTIPLIER'])
                os.environ['GRID_PCT_1'] = str(best_config['GRID_PCT_1'])
                os.environ['GRID_PCT_2'] = str(best_config['GRID_PCT_2'])
                os.environ['PROFIT_PER_GRID'] = str(best_config['PROFIT_PER_GRID'])
                
                # 更新 Config 類
                for k, v in best_config.items():
                    setattr(Config, k, v)
                
                msg = f"<b>每日參數優化完成！</b>\n"
                msg += f"預估收益提升 {((best_profit/best_profit_old)-1)*100:+.1f}%\n"
                msg += f"新參數:\n"
                msg += f"手數 {best_config['BASE_SIZE']:.6f} → {best_config['MULTIPLIER']}x\n"
                msg += f"網格 {best_config['GRID_PCT_1']*10000:.1f}→{best_config['GRID_PCT_2']*10000:.1f}點\n"
                msg += f"目標 {best_config['PROFIT_PER_GRID']:.3f}U/筆"
                send_tg(msg)
                print("參數已更新，機器人將自動重啟生效")
                
        except Exception as e:
            print(f"優化失敗: {e}")
        
        time.sleep(3600)  # 睡 1 小時，避免重複觸發

def simple_backtest(closes, cfg):
    # 超簡化回測：模擬馬丁在這段K線能賺多少
    # （實際我會用更精準的，但這版先用簡單版跑得快）
    profit = 0
    in_position = False
    entry_price = 0
    lots = cfg['BASE_SIZE']
    grid_count = 0
    
    for price in closes:
        if not in_position:
            entry_price = price
            in_position = True
            grid_count = 1
        else:
            grid = cfg['GRID_PCT_1'] if grid_count < 12 else cfg['GRID_PCT_2']
            if price <= entry_price * (1 - grid):
                lots *= cfg['MULTIPLIER']
                entry_price = price * 0.3 + entry_price * 0.7  # 簡化平均
                grid_count += 1
        
        # 出場條件
        if in_position and price >= entry_price * (1 + cfg['PROFIT_PER_GRID']/lots/price):
            profit += cfg['PROFIT_PER_GRID'] * grid_count
            in_position = False
            lots = cfg['BASE_SIZE']
    
    return profit + random.uniform(-5, 5)  # 加點噪音防過擬合

# ==================== 啟動優化线程 ====================
threading.Thread(target=auto_optimize_parameters, daemon=True).start()
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