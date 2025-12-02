# app.py - XAUT/USDT 終極馬丁機器人（固定首倉 0.0005 張）
from flask import Flask, render_template, jsonify
import ccxt
import time
import os
import threading
import asyncio
from telegram import Bot
from telegram.ext import Application, CommandHandler
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# ==================== BingX 設定 ====================
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
    print(f"BingX 初始化失敗: {e}")
    exchange = None

symbol = 'XAUT/USDT:USDT'

# ==================== 參數設定 ====================
BASE_SIZE = 0.0005                 # 第一倉固定 0.0005 張（永不變）
MULTIPLIER = 1.33
GRID_PCT_1 = 0.0005                # 前12筆 0.05%
GRID_PCT_2 = 0.0010                # 第13筆起 0.1%
PROFIT_PER_GRID = 0.05             # 每筆要賺 0.05 USDT 才出場

# ==================== 精度（防崩） ====================
def load_precision():
    try:
        market = exchange.market(symbol)
        TICK_SIZE = 10 ** -market['precision']['price']
        LOT_SIZE  = 10 ** -market['precision']['amount']
        MIN_QTY   = market['limits']['amount']['min'] or 0.000001
        print(f"精度載入成功: tick={TICK_SIZE}, lot={LOT_SIZE}")
        return TICK_SIZE, LOT_SIZE, MIN_QTY
    except:
        print("精度載入失敗，使用安全預設")
        return 0.01, 0.000001, 0.000001

TICK_SIZE, LOT_SIZE, MIN_QTY = load_precision()

def fmt_price(p): return round(p / TICK_SIZE) * TICK_SIZE
def fmt_qty(q):   return max(MIN_QTY, round(q / LOT_SIZE) * LOT_SIZE)

# ==================== 全域狀態 ====================
state = {
    'price': 0.0,
    'long_size': 0.0,
    'long_entry': 0.0,
    'entries': [],
    'status': '初始化中...',
    'trades': [],           # 這一行一定要有！
    'total_pnl': 0.0
}
TRADING_ENABLED = True
peak_price = 0.0
alert_sent = False
last_grid_price = None

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# ==================== 通知 ====================
async def tg_notify(msg):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            bot = Bot(token=TELEGRAM_TOKEN)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML')
        except Exception as e:
            print(f"TG發送失敗: {e}")

def notify(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")
    if TELEGRAM_TOKEN:
        run_async(tg_notify(msg))   # 用我們自己的 run_async

# ==================== BingX 同步持倉（關鍵！） ====================
def sync_positions():
    try:
        size, entry = 0.0, 0.0
        if exchange:
            positions = exchange.fetch_positions([symbol])
            for p in positions:
                if p['contracts'] > 0 and p['side'] == 'long':
                    size = float(p['contracts'])
                    entry = float(p['entryPrice'] or 0)
        state['long_size'] = size
        state['long_entry'] = entry
        
        if size > 0 and not state['entries']:
            state['entries'] = [{'price': entry, 'size': size}]
            notify(f"同步持倉: {size:.6f} 張 @ {entry:.2f}")
    except Exception as e:
        print(f"同步失敗: {e}")

# ==================== 核心函數 ====================
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
        notify(f"<b>加倉成功 第{len(state['entries'])}筆</b>\n{qty:.6f} 張 @ {state['price']:.2f}")
        sync_positions()
    except Exception as e:
        notify(f"<b>加倉失敗</b>\n{e}")

def close_all():
    size = state['long_size']
    if size == 0: return
    qty = fmt_qty(size)
    try:
        if exchange:
            exchange.create_market_sell_order(symbol, qty, params={'positionSide': 'LONG'})
        pnl = calc_pnl()
        notify(f"<b>全平成功！淨利 {pnl:+.2f} USDT</b>")
        state['trades'].append(f"全平 +{pnl:+.2f}")
        state['entries'].clear()
        sync_positions()
    except Exception as e:
        notify(f"<b>平倉失敗</b>\n{e}")

# ==================== Telegram 控制 ====================
async def status_cmd(update, context):
    sync_positions()
    pnl = calc_pnl()
    e = state['entries']

    # 防除以零 + 更安全的回撤計算
    if peak_price > 100:  # 正常金價不可能低於 100
        drawdown = (peak_price - state['price']) / peak_price * 100
    else:
        drawdown = 0.0

    if not e or state['long_size'] <= 0:
        text = (
            "<b>目前無持倉</b>\n"
            f"最新金價：<code>{state['price']:.2f}</code> USDT\n"
            f"波段高點：<code>{peak_price:.2f}</code> USDT\n"
            f"狀態：<code>{'運行中' if TRADING_ENABLED else '已暫停'}</code>"
        )
    else:
        lines = [f"<b>持倉明細（{len(e)} 筆）</b>"]
        total_size = total_cost = 0.0
        for i, x in enumerate(e, 1):
            val = x['size'] * x['price']
            total_size += x['size']
            total_cost += val
            lines.append(f"{i:>2} │ {x['size']:>8.6f} │ {x['price']:>7.2f} │ ${val:>7.2f}")

        avg = total_cost / total_size if total_size > 0 else 0

        lines += [
            "",
            f"總手數　　: <code>{total_size:.6f}</code> 張",
            f"平均成本　: <code>{avg:.2f}</code> USDT",
            f"最新價格　: <code>{state['price']:.2f}</code> USDT",
            f"浮動盈虧　: <code>{pnl:+.2f}</code> USDT",
            f"狀態　　　: {'運行中' if TRADING_ENABLED else '已暫停'}",
            f"波段高點　: <code>{peak_price:.2f}</code> (回撤 <code>{drawdown:+.2f}%</code>)"
        ]
        text = "\n".join(lines)

    # 發送或印 log
    if update:
        await update.message.reply_text(text, parse_mode='HTML')
    else:
        print("手動 /status 結果：\n" + text)
        notify("手動觸發 /status")
    
    # 發到 Telegram + 印 log 備份
    if update:
        await update.message.reply_text(text, parse_mode='HTML')
    else:
        print(f"手動 /status 結果:\n{text}")  # log 備份
    notify(f"Status 更新: {pnl:+.2f} USDT | {len(e)} 筆持倉")  # 總結通知

async def pause_cmd(update, context):
    global TRADING_ENABLED
    TRADING_ENABLED = False
    await update.message.reply_text("🔴 交易已暫停（加倉/出場停止）")

async def resume_cmd(update, context):
    global TRADING_ENABLED
    TRADING_ENABLED = True
    await update.message.reply_text("🟢 交易已恢復！繼續吃波動")

async def close_cmd(update, context):
    await update.message.reply_text("⚡ 強制全平中...")
    close_all()
    await update.message.reply_text("✅ 全平完成！持倉清零")

def start_tg_bot():
    if not TELEGRAM_TOKEN:
        print("⚠️ 未填 TELEGRAM_TOKEN，指令只在 log 顯示（用儀表板手動觸發）")
        return
    
    try:
        application = Application.builder().token(TELEGRAM_TOKEN).build()
        application.add_handler(CommandHandler("status", status_cmd))
        application.add_handler(CommandHandler("pause", pause_cmd))
        application.add_handler(CommandHandler("resume", resume_cmd))
        application.add_handler(CommandHandler("close", close_cmd))
        print("✅ Telegram Bot 監聽啟動！（私聊機器人打 /status）")
        # 背景執行，不阻塞主程式
        threading.Thread(target=lambda: asyncio.run(application.run_polling(drop_pending_updates=True)), daemon=True).start()
    except Exception as e:
        print(f"❌ Telegram 啟動失敗: {e} - 檢查 token 格式")
        # 備份：手動觸發 status
        asyncio.run(status_cmd(None, None))

# ==================== 主迴圈 ====================
def trading_loop():
    global last_grid_price, peak_price, alert_sent
    first = True
    last_grid_price = None
    peak_price = 0.0
    alert_sent = False
    
    print("交易迴圈已啟動")
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

            # 首倉
            if first and state['long_size'] == 0:
                add_long(BASE_SIZE)
                last_grid_price = state['price']      # 這裡才賦值
                peak_price = state['price']
                alert_sent = False
                first = False
                continue

            # 出場
            if state['long_size'] > 0 and should_exit():
                close_all()
                last_grid_price = None
                continue

            # 加倉
            if state['long_size'] > 0 and last_grid_price:
                grid = GRID_PCT_1 if len(state['entries']) < 12 else GRID_PCT_2
                if state['price'] <= last_grid_price * (1 - grid):
                    size = BASE_SIZE * (MULTIPLIER ** len(state['entries']))
                    add_long(size)
                    last_grid_price = state['price']

            # 大跌預警
            if state['price'] > peak_price:
                peak_price = state['price']
                alert_sent = False
            dd = (peak_price - state['price']) / peak_price if peak_price > 0 else 0
            if 0.010 < dd <= 0.013 and not alert_sent and state['entries']:
                notify(f"<b>大波動預警！</b>\n從 {peak_price:.1f} 跌 {dd*100:.2f}%\n最佳加倉區！")
                alert_sent = True

            state['status'] = f"持倉 {state['long_size']:.6f} | {len(state['entries'])}筆 | 盈虧 {calc_pnl():+.2f}"
            time.sleep(8)
        except Exception as e:
            notify(f"迴圈異常: {e}")
            time.sleep(15)

# ==================== Flask ====================
from threading import Thread

def run_async(coro):
    """在背景執行 async 函數，永遠不會炸"""
    def thread_target():
        try:
            asyncio.run(coro)
        except Exception as e:
            print(f"async 執行失敗: {e}")
    Thread(target=thread_target, daemon=True).start()

@app.route('/tg/status')
def tg_status():
    run_async(status_cmd(None, None))
    return "Status 已發送到 Telegram / log"

@app.route('/tg/pause')
def tg_pause():
    global TRADING_ENABLED
    TRADING_ENABLED = False
    run_async(tg_notify("交易已手動暫停"))
    return "已暫停"

@app.route('/tg/resume')
def tg_resume():
    global TRADING_ENABLED
    TRADING_ENABLED = True
    run_async(tg_notify("交易已手動恢復"))
    return "已恢復"

@app.route('/tg/close')
def tg_close():
    run_async(tg_notify("強制全平執行中..."))
    Thread(target=close_all, daemon=True).start()
    return "全平指令已發送"

# ==================== 啟動 ====================
if __name__ == '__main__':
    threading.Thread(target=start_tg_bot, daemon=True).start()
    threading.Thread(target=trading_loop, daemon=True).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)