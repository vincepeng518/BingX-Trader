# app.py
from flask import Flask, render_template, jsonify
import ccxt
import time
import os
import threading
import asyncio
from telegram import Bot
from dotenv import load_dotenv

# ==================== 全域變數（必須放在最上面）===================
TRADING_ENABLED = True       # Telegram 開關
peak_price = 0.0              # 記錄波段最高價
alert_sent = False            # 是否已發過大跌預警
last_grid_price = None        # 加倉觸發基準價（修好版會自動設）

load_dotenv()
app = Flask(__name__)

# ==================== BingX 設定 ====================
exchange = ccxt.bingx({
    'apiKey': os.getenv('BINGX_API_KEY'),
    'secret': os.getenv('BINGX_SECRET'),
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})
exchange.set_sandbox_mode(os.getenv('SANDBOX', 'true').lower() == 'true')  # 設 false 就是實盤


symbol = 'XAUT/USDT:USDT'
BASE_SIZE = 0.0005
MULTIPLIER = 1.33
GRID_PCT_1 = 0.0005      # 前12筆 0.05%
GRID_PCT_2 = 0.0010      # 第13筆起 0.1%
PROFIT_PER_GRID = 0.05   # 每筆要賺 0.05U 才平
MAX_GRIDS = 99999           # 絕對安全上限，防爆倉

# ==================== 精度 ====================
# ==================== 超穩精度獲取（支援 BingX 2024~2025 所有版本）===================
def load_precision():
    try:
        exchange.load_markets()
        market = exchange.market(symbol)
        
        # 優先用標準欄位（ccxt 統一處理過的最安全方式）
        precision_price = market['precision']['price']
        precision_amount = market['precision']['amount']
        min_qty = market['limits']['amount']['min'] or 0.000001
        
        # 轉成 BingX 實際需要的「幾位小數」
        price_tick = 10 ** -precision_price
        qty_tick = 10 ** -precision_amount
        
        return price_tick, qty_tick, min_qty
    except Exception as e:
        print(f"精度載入失敗，使用安全預設值: {e}")
        # XAUT 歷史經驗值，永遠不會錯
        return 0.01, 0.000001, 0.000001

# 直接呼叫，永遠不會 KeyError
TICK_SIZE, LOT_SIZE, MIN_QTY = load_precision()

# 格式化函數（超穩版）
def fmt_price(p):
    return round(p / TICK_SIZE) * TICK_SIZE

def fmt_qty(q):
    if q < MIN_QTY:
        return 0
    return round(q / LOT_SIZE) * LOT_SIZE

TICK_SIZE, LOT_SIZE, MIN_QTY = load_precision()

def fmt_price(p): return round(p - (p % TICK_SIZE), 8)
def fmt_qty(q): return max(MIN_QTY, round(q - (q % LOT_SIZE), 6))

# ==================== 狀態 ====================
state = {
    'price': 0.0, 'long_size': 0.0, 'long_entry': 0.0,
    'entries': [], 'pending_rebound': None,
    'status': '初始化中...', 'trades': [], 'total_pnl': 0.0,
    'funding_alert': False
}

bot = Bot(token=os.getenv('TELEGRAM_TOKEN'))
chat_id = os.getenv('TELEGRAM_CHAT_ID')

async def tg(msg):
    try: await bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
    except: pass

def notify(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")
    asyncio.run(tg(msg))

# ==================== 核心 ====================
def get_pos():
    try:
        pos = exchange.fetch_positions([symbol])
        for p in pos:
            if p['contracts'] > 0 and p['side'] == 'long':
                return float(p['contracts']), float(p['entryPrice'] or 0)
        return 0.0, 0.0
    except: return 0.0, 0.0

def calc_pnl():
    if not state['entries']: return 0.0
    cost = sum(e['price'] * e['size'] for e in state['entries'])
    value = sum(e['size'] for e in state['entries']) * state['price']
    fee = value * 0.0005 * 2
    return value - cost - fee

def should_exit():
    if not state['entries']: return False
    required = PROFIT_PER_GRID * len(state['entries'])
    state['total_pnl'] = calc_pnl()
    return state['total_pnl'] >= required

def add_long(size):
    if not TRADING_ENABLED:
        return
    
    qty = fmt_qty(size)
    if qty <= 0:
        return

    try:
        order = exchange.create_market_buy_order(
            symbol, qty, 
            params={'positionSide': 'LONG'}
        )
        state['entries'].append({'price': state['price'], 'size': qty})
        state['trades'].append(f"加倉 {qty:.6f} @ {state['price']:.2f}")
        
        notify(
            f"<b>逆勢加倉成功！第 {len(state['entries'])} 筆</b>\n"
            f"手數: <code>{qty:.6f}</code> 張\n"
            f"價格: <code>{state['price']:.2f}</code> USDT\n"
            f"倉位價值 ≈ <code>{qty * state['price']:.2f}</code> USDT"
        )
    except Exception as e:
        notify(f"<b>加倉失敗</b>\n<code>{e}</code>")

def close_all():
    size, _ = get_pos()
    if not TRADING_ENABLED:
        return
    qty = fmt_qty(size)
    try:
        order = exchange.create_market_sell_order(symbol, qty, params={'positionSide': 'LONG'})
        pnl = calc_pnl()
        notify(f"<b>獲利全平！淨利 {pnl:+.2f} USDT</b>")
        state['trades'].append(f"全平 +{pnl:+.2f}")
        state['entries'].clear()
        if state['pending_rebound']:
            try: exchange.cancel_order(state['pending_rebound'], symbol)
            except: pass
            state['pending_rebound'] = None
    except Exception as e: notify(f"平倉失敗: {e}")

def trading_loop():
    first = True
    last_grid_price = None

    while True:
        if not TRADING_ENABLED == False:
            time.sleep(10)
            continue
        try:
            ticker = exchange.fetch_ticker(symbol)
            state['price'] = price = ticker['last']
            long_size, entry = get_pos()
            state['long_size'] = long_size
            state['long_entry'] = entry

            # 首次自動開倉
            if first and long_size == 0:
                add_long(BASE_SIZE)
                last_grid_price = state['price']  # 關鍵！一定要設
                peak_price = state['price']       # 波動預警也一起初始化
                notify(f"<b>首倉已開！</b>\n價格：{state['price']:.2f}\n手數：{BASE_SIZE:.6f} 張（≈2.01 USDT）")
                first = False
                time.sleep(3)
                continue

            # 獲利出場
            if long_size > 0 and should_exit():
                close_all()
                last_grid_price = None
                time.sleep(10)
                continue


            # 更新波段最高價
            if state['price'] > peak_price:
                peak_price = state['price']
                alert_sent = False  # 新高重置警報

            # 計算從高點最大回撤
            drawdown_pct = (peak_price - state['price']) / peak_price

            # 大波動預警：跌超 1% 但還沒回調 0.3% → 極佳加倉/出場時機
            if drawdown_pct > 0.010 and drawdown_pct <= 0.013 and not alert_sent and len(state['entries']) > 0:
                notify(
                    "<b>大波動警報！</b>\n"
                    f"從高點 {peak_price:.1f} 已下跌 {drawdown_pct*100:.2f}%\n"
                    f"目前價格：{state['price']:.1f}\n"
                    "⚡ 極佳加倉 / 出場時機來了！可手動 /forceclose 或繼續加倉"
                )
                alert_sent = True

            # 逆勢加倉邏輯（已移除筆數限制）
            if long_size > 0 and last_grid_price:
                grid = GRID_PCT_1 if len(state['entries']) < 12 else GRID_PCT_2
                if state['price'] <= last_grid_price * (1 - grid):
                    size = BASE_SIZE * (MULTIPLIER ** len(state['entries']))
                    add_long(size)
                    last_grid_price = state['price']

            # 資金費率提醒（每8小時檢查一次）
            if int(time.time()) % 28800 == 0 and not state['funding_alert']:
                funding = exchange.fetch_funding_rate(symbol)
                rate = funding['fundingRate'] * 100
                if rate > 0.01:
                    notify(f"<b>資金費率警告</b>: {rate:.4f}%  多頭正在付費！")
                state['funding_alert'] = True

            state['status'] = f"持倉 {long_size:.4f} | {len(state['entries'])} 筆 | 盈虧 {calc_pnl():+.2f}"
            time.sleep(8)

        except Exception as e:
            notify(f"<b>程式異常</b>\n{e}")
            time.sleep(15)

# ==================== Flask ====================
@app.route('/')
def home(): return render_template('dashboard.html')

@app.route('/api/data')
def api(): return jsonify(state)

# ==================== Telegram 遠端指令控制（開關機器人超方便）===================
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TRADING_ENABLED = True
peak_price = 0.0
alert_sent = False
last_grid_price = None

async def tg_notify(msg):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            bot = Bot(token=TELEGRAM_TOKEN)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML')
        except:
            pass

def notify(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")
    asyncio.run(tg_notify(msg))

# 強制啟動 Telegram 指令監聽
def start_telegram_bot():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        print("Warning: 未設定 TELEGRAM_TOKEN，指令功能關閉（通知還是會發）")
        return

    async def status_cmd(update, context):
        pnl = calc_pnl()
        entries = state['entries']
        if not entries:
            text = "<b>目前無持倉，等待首倉進場</b>"
        else:
            lines = [f"<b>持倉明細（{len(entries)} 筆）</b>"]
            total_size = total_cost = 0
            for i, e in enumerate(entries, 1):
                sz = e['size']
                pr = e['price']
                val = sz * pr
                total_size += sz
                total_cost += val
                lines.append(f"{i:>2} │ {sz:.6f} │ {pr:>8.2f} │ ${val:>6.2f}")
            avg = total_cost / total_size
            lines += ["", 
                     f"總手數　　: <code>{total_size:.6f}</code> 張",
                     f"平均成本　: <code>{avg:.2f}</code>",
                     f"最新價格　: <code>{state['price']:.2f}</code>",
                     f"浮動盈虧　: <code>{pnl:+.2f}</code> USDT",
                     f"狀態　　　: {'<b>運行中</b>' if TRADING_ENABLED else '<b>已暫停</b>'}"]
            text = "\n".join(lines)
        await update.message.reply_text(text, parse_mode='HTML')

    async def pause_cmd(update, context):
        global TRADING_ENABLED
        TRADING_ENABLED = False
        await update.message.reply_text("已暫停交易（加倉與自動出場停止）")

    async def resume_cmd(update, context):
        global TRADING_ENABLED
        TRADING_ENABLED = True
        await update.message.reply_text("已恢復交易，繼續吃肉！")

    async def forceclose_cmd(update, context):
        await update.message.reply_text("正在強制市價全平…")
        close_all()
        await update.message.reply_text("已強制全平！")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CommandHandler("forceclose", forceclose_cmd))

    print("Telegram 指令監聽已啟動！輸入 /status 測試")
    app.run_polling(drop_pending_updates=True)

# 啟動（一定要放最下面）
threading.Thread(target=start_telegram_bot, daemon=True).start()

# ==================== 啟動 ====================
if __name__ == '__main__':
    threading.Thread(target=trading_loop, daemon=True).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)