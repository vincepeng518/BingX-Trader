# BingX XAUT/USDT 強化馬丁格爾 + 上漲逆勢網格策略

專為黃金美元永續設計的超穩馬丁加網格系統  
已連續在 BingX 模擬盤穩定運行 30+ 天

## 策略特色
- 首單 0.05 張
- 1.33 倍等比加倉
- 前 12 筆：每跌 0.05% 加一筆  
- 第 13 筆起：每跌 0.1% 加一筆
- 每筆需賺 0.05 USDT 才集體出場
- 自動精度格式化 + 資金費率提醒
- Flask 即時儀表板 + Telegram 通知

## 快速開始
```bash
git clone https://github.com/yourname/bingx-xaut-martingale-grid.git
cd bingx-xaut-martingale-grid
cp .env.example .env
# 填入你的 API 與 TG
pip install -r requirements.txt
python app.py