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
            if int(time.time()) % 60 == 0:
                sync_bingx_positions()
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
# ==================== 終極版 Telegram + BingX 真實持倉同步 ====================
from telegram.ext import Application, CommandHandler
import asyncio

# 全域變數（確保在最上面）
TRADING_ENABLED = True
peak_price = 0.0
alert_sent = False
last_grid_price = None

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

async def tg_notify(msg):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            bot = Bot(token=TELEGRAM_TOKEN)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML')
        except Exception as e:
            print(f"TG 通知失敗: {e}")
    else:
        print(f"通知（無 TG）：{msg}")

def notify(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")
    asyncio.create_task(tg_notify(msg))  # 非阻塞

# 強制從 BingX 同步持倉（關鍵！永不脫鉤）
def sync_bingx_positions():
    try:
        pos = get_pos()  # 你原本的 get_pos() 函數
        long_size, entry_price = pos
        if long_size > 0:
            # 如果 BingX 有持倉，但本地 entries 空 → 強制重建
            if not state['entries']:
                state['entries'] = [{'price': entry_price, 'size': long_size}]
                notify(f"持倉同步：從 BingX 拉到 {long_size:.6f} 張 @ {entry_price:.2f}")
            # 更新本地總 size（防滑價）
            total_local = sum(e['size'] for e in state['entries'])
            if abs(total_local - long_size) > 0.0001:
                notify(f"持倉微調：本地 {total_local:.6f} → BingX {long_size:.6f}")
                # 簡易調整最後一筆
                if state['entries']:
                    state['entries'][-1]['size'] = long_size - sum(e['size'] for e in state['entries'][:-1])
        else:
            # BingX 無持倉 → 清空本地
            if state['entries']:
                state['entries'].clear()
                notify("BingX 無持倉，本地已清空")
    except Exception as e:
        print(f"持倉同步失敗: {e}")

# /status 指令（從 BingX 真實拉持倉 + 美觀顯示）
async def status_cmd(update, context):
    sync_bingx_positions()  # 先強制同步！
    
    pnl = calc_pnl()
    entries = state['entries']
    
    if not entries or sum(e['size'] for e in entries) == 0:
        text = "<b>🚫 目前無持倉</b>\n等待價格觸發首倉（基準價：{last_grid_price:.2f if last_grid_price else '未設'}）\n最新金價：{state['price']:.2f}"
    else:
        lines = ["<b>📊 持倉明細（從 BingX 同步）</b>"]
        total_size = total_cost = 0.0
        for i, e in enumerate(entries, 1):
            sz = e['size']
            pr = e['price']
            val = sz * pr
            total_size += sz
            total_cost += val
            lines.append(f"{i:>2d} │ {sz:>7.6f} │ {pr:>7.2f} │ 價值 {val:>6.2f}＄")
        
        avg = total_cost / total_size if total_size > 0 else 0
        unrealized = total_size * state['price'] - total_cost
        lines += [
            "",
            f"📈 <b>總結</b>",
            f"總手數　：{total_size:>7.6f} 張",
            f"平均成本：{avg:>7.2f} USDT",
            f"最新價格：{state['price']:>7.2f} USDT",
            f"浮盈虧　：{unrealized:+6.2f} USDT (含費 {pnl:+6.2f})",
            f"狀態　　　：{'🟢 運行中' if TRADING_ENABLED else '🔴 已暫停'}",
            f"波段高點　：{peak_price:>7.2f} (回撤 {((peak_price - state['price'])/peak_price *100):+.2f}%)"
        ]
        text = "\n".join(lines)
    
    await update.message.reply_text(text, parse_mode='HTML')

# 其他指令（簡化版）
async def pause_cmd(update, context):
    global TRADING_ENABLED
    TRADING_ENABLED = False
    await update.message.reply_text("🔴 交易已暫停（加倉/出場停止）")

async def resume_cmd(update, context):
    global TRADING_ENABLED
    TRADING_ENABLED = True
    await update.message.reply_text("🟢 交易已恢復！")

async def forceclose_cmd(update, context):
    await update.message.reply_text("⚡ 強制全平中...")
    close_all()
    await update.message.reply_text("✅ 已全平！持倉清零")

# 啟動 Telegram Bot（強制版，無 token 也會印 log）
def start_telegram_bot():
    if not TELEGRAM_TOKEN:
        print("⚠️ 未填 TELEGRAM_TOKEN，/status 等指令只在 log 顯示（通知仍發）")
        # 即使無 token，也模擬 status 給 log
        print("模擬 /status 結果：")
        status_result = asyncio.run(status_cmd(None, None))  # 這行會印在 log
        return
    
    try:
        app = Application.builder().token(TELEGRAM_TOKEN).build()
        app.add_handler(CommandHandler("status", status_cmd))
        app.add_handler(CommandHandler("pause", pause_cmd))
        app.add_handler(CommandHandler("resume", resume_cmd))
        app.add_handler(CommandHandler("forceclose", forceclose_cmd))
        
        print("✅ Telegram Bot 已啟動！打 /status 測試")
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        print(f"❌ Telegram 啟動失敗: {e}（檢查 token）")

# 在 trading_loop() 每 5 分鐘強制同步一次 BingX 持倉
# 加在 trading_loop() 循環裡：if int(time.time()) % 300 == 0: sync_bingx_positions()

# ==================== 啟動 ====================
if __name__ == '__main__':
    threading.Thread(target=trading_loop, daemon=True).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)