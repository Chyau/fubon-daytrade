# fubon-daytrade — 富邦新一代 API 下單工具

獨立專案（與 OpenBell 無關）。以 **Fubon Neo Python SDK** 實作的當沖下單 CLI，
核心是 `PLACE_ORDER` 安全開關：

| 設定 | 行為 |
|---|---|
| `PLACE_ORDER=false`（**預設**） | **沒有真實下單**。只輸出「富邦新一代API下單參數」（Order 物件欄位＋等效程式碼），有 SDK＋憑證時附漲跌停／當沖資格驗證；未裝 SDK 也能輸出參數 |
| `PLACE_ORDER=true` + `--send` | **真實下單**。測試沙箱：`--yes` 即送；正式盤：`--yes` **且** 互動輸入股票代號二次確認 |

## 安全階梯（四道閘）

```
參數輸出（PLACE_ORDER 預設 false，永不送單）
  → --send（送單請求）
    → PLACE_ORDER=true（真實下單開關）
      → 環境確認：FUBON_USE_TEST=1 沙箱（--yes）／ FUBON_USE_TEST=0 正式（--yes＋輸入代號）
```

## 安裝

1. Python 3.8–3.13（官方暫不支援 3.14）。
2. SDK **不在 PyPI**，從官方下載 .whl：
   https://www.fbs.com.tw/TradeAPI/docs/download/download-sdk
   ```bash
   pip install <下載的檔名>.whl
   ```
3. 複製 `.env.example` 為 `.env`（或自行 set 環境變數）填入憑證。

## 測試環境（零風險沙箱）

- 憑證：https://www.fbs.com.tw/TradeAPI_SDK/sample_code/test_environment.zip
- 帳號 `FUBON_TEST_ID` ＝ 憑證 CN 值（如 `58581758`），**不是**身分證字號
- 登入密碼與憑證密碼皆為官方預設 `12345678`
- 開盤時間 09:30–19:00；庫存每日重設
- 測試環境查可下單價格請用 `query_symbol_quote`（本工具 dry-run 驗證已內建）

## 使用範例

```bash
# 下單 CLI（PLACE_ORDER 安全開關）
python fubon_orders.py --dry --symbol 2344 --side buy  --qty 1000 --price 60
python fubon_orders.py --dry --symbol 2344 --side sell --qty 1000 --short --price 60   # 融券放空
set PLACE_ORDER=true
python fubon_orders.py --send --symbol 2344 --side buy --qty 1000 --price 60 --yes     # 真實下單
python fubon_orders.py --status   # 查詢
python fubon_orders.py --cancel ORDER_NO   # 刪單

# Web 儀表板（Flask，port 9757）
python app.py          # 或雙擊 start_web.bat → http://127.0.0.1:9757

# 回測＋交易績效
python backtest.py --demo                       # 合成資料驗證管線
python backtest.py --demo --risk-budget 5000    # 倉位公式：預算→自動算張數
python backtest.py --yf 2426.TW --period 5d     # 真實 5 分K（60 天內）
python backtest.py --history 10                 # 查最近交易

# 學習迴路（避坑）
python journal.py pits --min-n 4                # 挖掘虧損模式
python journal.py rules --add-dim weekday --add-value Mon --name "週一不進場"
python journal.py rules --approve 1             # 核准後引擎自動擋單（E_RULE_BLOCK）

# 籌碼選股
python chips.py 2426              # 個股籌碼摘要（分點買超/融券/大戶）
python chips.py --nextday         # 漲停隔日表現統計 + 放空觀察池
python chips.py --candidates      # 今日隔日沖放空候選（漲停∩分點集中買超）
```

## 切換正式環境（真實盤）檢查清單

1. 已在測試環境完成至少 10 筆沙箱交易
2. 已申請正式憑證（TCEM.exe），檔名為身分證字號 `.pfx`
3. 設定 `FUBON_USE_TEST=0` 並填妥 `FUBON_LIVE_ID / FUBON_LIVE_PASSWORD / FUBON_LIVE_CERT_PATH / FUBON_LIVE_CERT_PASSWORD`
4. 設定 `PLACE_ORDER=true`，下單時加 `--send --yes` 並輸入股票代號確認
5. 正式環境整股數量為 1000 股（1 張）的倍數

## 檔案

| 檔案 | 說明 |
|---|---|
| `fubon_orders.py` | 下單 CLI（dry-run／送單／查詢／刪單；PLACE_ORDER 開關） |
| `strategy.py` | 佛系當沖法策略引擎（四階段狀態機、長黑K、移動停損、倉位公式） |
| `backtest.py` | 回測器（demo/CSV/yfinance）＋交易績效報告 |
| `journal.py` | 學習迴路 CLI（list/show/pits 挖掘/rules 規則管理/report） |
| `app.py` | **Web 儀表板（Flask，port 9757）**：儀表板/交易/績效/學習/籌碼/下單/回測 |
| `chips.py` | 籌碼整合：tw-chips-daily 個股籌碼 + chengwaye 漲停隔日表 → 隔日沖選股 |
| `db.py` | sqlite3 持久化（orders/trades/trade_events/pits/rules 等 8 表） |
| `logutil.py` | logging 設定（logs/fubon_daytrade.log，DEBUG 全量＋輪替） |
| `.env.example` | 環境變數範本（測試／正式／Telegram／AI） |
| `data/daytrade_candidates_full.csv` | 當沖掃描器全市場清單（2,342 支） |
| `data/chips_stock.html` | 籌碼資料快取（7.3MB，TTL 20 小時） |
| `data/trades.db` | 交易紀錄資料庫（自動建立） |

```bash
python chips.py 2426              # 個股籌碼摘要（分點買超/融券/大戶）
python chips.py --nextday         # 漲停隔日表現統計 + 放空觀察池
python chips.py --candidates      # 今日隔日沖放空候選（漲停∩分點集中買超）
```

## 部署：GitHub 備份 ＋ Vercel 唯讀快照站

### 1. GitHub（私人 repo，程式碼備份）
```bash
gh auth login            # 首次：登入 GitHub.com（瀏覽器授權）
gh repo create fubon-daytrade --private --source . --push
```
- `.gitignore` 已排除所有機密：`.env`（帳密）、`test_environment/`（.pfx 私鑰）、
  `data/`（交易紀錄）、`log*/`、`__pycache__/`
- `snapshot/`（唯讀快照站）**會**上傳 — 這是 Vercel 部署的來源

### 2. Vercel（唯讀快照站，免費）
1. https://vercel.com → 以 GitHub 帳號登入 → **Add New → Project → Import** 此 repo
2. 設定：**Root Directory = `snapshot`**｜Framework Preset = **Other**（純靜態）
3. Deploy → 得到公開網址（如 `fubon-daytrade.vercel.app`）
4. 之後每次 `git push` 更新 `snapshot/` → Vercel 自動重新部署

### 3. 每日更新快照
```bash
push_snapshot.bat        # 匯出快照 → git commit → push（Vercel 自動跟進）
```
⚠ 隱私提醒：Vercel 免費版網站為**公開網址**（知道網址者皆可看）。
快照只含交易統計（標的／價格／損益），不含帳號與憑證；若想私有，
需 Vercel Pro 的 Password Protection 或改用其他平台。

## 免責聲明

本工具為輔助下單介面，不構成投資建議。實單交易前請以富邦官方文件、
測試環境與券商回報為最終依據；條件單功能需先簽署「條件單使用同意書」。
