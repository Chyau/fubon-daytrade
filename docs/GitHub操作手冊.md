# GitHub 雲端操作手冊（v1.0 — 2026-10-31）

> 適用對象：佛系當沖量化系統的雲端部分（GitHub repo／Actions／Pages／Vercel）
> 本機部分（Flask 儀表板、下單 CLI）請見《操作手冊.md》

---

## 1. 架構總覽

```
【GitHub Actions】交易日 09:15–13:50（台灣時段）每 15 分鐘自動跑
   yfinance 5 分K → 引擎重放（與富邦版同一套下單參數）→ is_dup 去重
   → 寫入 data/trades.db → 生成快照 → 自動 commit + push
        ↓ 觸發
【GitHub Pages】https://chyau.github.io/fubon-daytrade
【Vercel】https://fubon-daytrade-snapshot.vercel.app
   兩邊內容相同（九分頁儀表板），互為備援
        ↓ 來源
【repo】Chyau/fubon-daytrade（public）
   程式碼 + data/trades.db（模擬交易唯一真相）+ 承諾檔（多空清單）
        ↓ 本機（Windows）
【家裡 PC】完整 Flask 儀表板 + 富邦 SDK（dev 與進階操作）
```

## 2. 網址與入口

| 項目 | 網址／位置 |
|---|---|
| 雲端儀表板（主要） | https://chyau.github.io/fubon-daytrade |
| 雲端儀表板（備援） | https://fubon-daytrade-snapshot.vercel.app |
| Actions（觸發／看執行） | https://github.com/Chyau/fubon-daytrade/actions |
| Repo 首頁 | https://github.com/Chyau/fubon-daytrade |
| 本機儀表板（在家） | http://127.0.0.1:9757（start_web_forever.bat） |

## 3. 手動執行雲端回測（人在任何地方）

### 方式 A：GitHub 網頁（手機也可）
1. 開 https://github.com/Chyau/fubon-daytrade/actions
2. 左側選 **paper-intraday** → 右側 **Run workflow ▼**
3. 填三個欄位：
   - **自訂標的**：逗號分隔（例 `2330,2426,3653`）；**留空 = 多空 24 檔每日重放**
   - **期數**：`5d`／`10d`／`1mo`／`60d`（yfinance 5 分K 上限 60 天）
   - **風險預算**：`0`＝固定 1 張；填數字＝倉位公式自動算張數
4. 按 **Run workflow** → 約 1–3 分鐘完成

### 方式 B：gh CLI 一行
```bash
gh workflow run paper.yml --repo Chyau/fubon-daytrade \
  -f symbols="2330,2426" -f period="10d" -f risk_budget="5000"
```

### 結果在哪
| 觸發方式 | run 命名 | 出現分頁 |
|---|---|---|
| 留空標的（每日重放） | `PAPER-YYYYMMDD` | ⚡ 自動執行 |
| 自訂標的 | `BT-YYYYMMDD-HHMM` | ⏱ 回測（點開看明細） |

## 4. 排程說明（自動部分）

| 排程（UTC） | 台灣時間 | 內容 |
|---|---|---|
| `*/15 1-5 * * 1-5` | 週一～五 09:15–13:45 每 15 分 | 24 檔重放（多空清單） |
| `50 5 * * 1-5` | 週一～五 13:50 | 收盤收尾（補抓 yf 延遲的最後 K 棒） |

- 排程是 best-effort：高峰期可能延遲或跳過數次 — 下次重放會補齊，交易不漏
- 每次成功 run 自動 commit（`paper step 日期`）→ Pages/Vercel 自動更新

## 5. 本機日常命令（Windows）

| 時機 | 命令 | 作用 |
|---|---|---|
| 盤前一晚 | 跑您的 `query_longshort.py` | 更新多空分類 DB |
| 盤前一晚（接著） | `python tools\paper_step.py --push` | 刷新清單承諾檔→重放→快照→推送 |
| 盤後 17:30 | `daily_update.bat` | 籌碼快取＋本地資料更新 |
| 盤後 17:35 | `push_snapshot.bat` | 快照＋**籌碼分頁**＋清單承諾檔 → 推送 |
| 啟動本機儀表板 | `start_web_forever.bat`（已開機自啟） | 顯示清單＋啟動 Flask |
| 檢視清單 | `python live.py`（或 --brief） | 顯示當前多空 24 檔 |

## 6. 密碼鎖（AES-256）

- **設定**：本機 `.env` 加 `SNAPSHOT_PASSWORD=您的密碼`；
  GitHub → Settings → Secrets and variables → Actions → New secret（同名）
- **效果**：每次管線推送的頁面變成「輸密碼解鎖」（真加密）；兩站同時生效
- **未設定**：頁面明碼（現在的狀態）
- ⚠ 密碼鎖只鎖「網頁顯示」；repo 是 public，`trades.db` 可被直接下載

## 7. 九分頁說明（雲端儀表板）

| 分頁 | 內容 | 資料新鮮度 |
|---|---|---|
| 📊 儀表板 | KPI＋淨值曲線＋標的統計＋最大回撤 | 每次 run |
| ⚡ 自動執行 | PAPER-*/LIVE-* 模擬交易（依 run 分組） | 盤中每 15 分 |
| 📋 交易紀錄 | 全部交易，搜尋＋篩選 | 每次 run |
| 📈 交易績效 | 每日損益、星期統計、R 分佈、持倉時間 | 每次 run |
| 🧠 學習迴路 | 坑點挖掘（12 維度）＋避坑規則 | 每次 run |
| 🧲 籌碼選股 | 觀察清單＋隔日沖候選＋漲停統計 | 最後一次本機推送 |
| 🧾 下單紀錄 | 富邦送單紀錄（test 模式後出現） | 最後一次本機推送 |
| ⏱ 回測 | BT-* 自訂回測（run_id 分組＋明細） | 手動觸發時 |
| 📖 手冊 | 本文件 | 最後一次本機推送 |

## 8. 故障排查

| 症狀 | 原因／處理 |
|---|---|
| Actions run 失敗（exit 1） | Actions 頁點開 run → 失敗步驟看 log；paper_step 現在會印出 git 的 stderr |
| yf 抓不到資料（稀疏／0 檔） | Yahoo 限流（尤其平行轟炸後）— 等 10–30 分再試；Actions 用 GitHub IP 通常正常 |
| 推送被拒（non-fast-forward） | 本機 `git pull --rebase` 後再 push（Actions 也會自動 commit） |
| 頁面沒更新 | Pages 部署約 30–90 秒；Vercel 約 10–20 秒；瀏覽器 Ctrl+F5 |
| 週末富邦測試環境離線 | 正常（平日 09:30–19:00）；雲端 yf 模擬不受影響 |
| 密碼忘了 | 換密碼＝改 .env＋Secrets 後重跑一次推送 |

## 9. 週一（交易日）檢查清單

```
09:15 前  確認 repo Actions 頁有排程 run 在跑（或手動 Run workflow 一次）
09:30 後  開 https://chyau.github.io/fubon-daytrade → ⚡ 自動執行分頁
          應出現 PAPER-20261102 run，逐 15 分鐘累積
13:50 後  當日模擬定稿；⏱ 回測分頁可跑自訂標的
17:30     本機 daily_update.bat → push_snapshot.bat（籌碼分頁更新）
```

## 10. 已知限制（誠實聲明）

1. yfinance 延遲 15–20 分 — 模擬成交價與真實盤有偏移（看策略表現方向可用）
2. 雲端無富邦 SDK — 富邦資料源回測、test 模式送單僅限本機
3. Actions 排程偶爾延遲/跳過 — 重放機制保證資料不漏
4. repo public — 策略程式碼與模擬交易紀錄公開；憑證與 .env 從未上傳
5. GitHub Pages 部署偶發延遲 — 以頁尾「生成時間」對照 commit 時間判斷
