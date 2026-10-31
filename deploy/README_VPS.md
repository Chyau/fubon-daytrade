# 雲端 VPS 部署指南 — 不在家也能回測＋自動執行

> 目標：把整套系統（儀表板／回測／⚡自動執行引擎）搬到雲端常駐，
> 家裡電腦可以關機。手機／外地電腦直接開網頁操作。

## 0. 架構總覽

```
【雲端 VPS】24 小時常駐
 ├ app.py（systemd 服務：開機自啟、崩潰 5 秒自動重啟）
 ├ ⚡自動執行引擎（/live 頁啟動，時段閘 08:45–13:35）
 ├ SQLite 交易紀錄（VPS 本地磁碟）
 └ cron 17:30 daily_update.sh → push_snapshot.sh（可選自動化）
        │
【外地】手機／筆電 → Tailscale 私網 → http://100.x.x.x:9757
        │
【Vercel】唯讀快照站（不變）
```

## 1. 開一台 VPS（三選一）

| 平台 | 費用 | 建議規格 |
|---|---|---|
| **Oracle Cloud Free** | 永久免費 | VM.Standard.A1.Flex（ARM 2核12G）或 E2.1.Micro |
| AWS Lightsail | $5/月 | Ubuntu 24.04，1核1G 足夠 |
| Hetzner / RackNerd | €4/月 | Ubuntu 24.04 x86_64 |

> 系統很輕（Python＋SQLite），1G RAM／1 核就夠。SDK 的 Linux 版
> 同時支援 x86_64 與 ARM，選哪家都行。
> **防火牆／安全組：只開 22（SSH），不要開 9757。**

## 2. SSH 進入後一行部署

```bash
export GIT_TOKEN=ghp_你的token
bash <(curl -sL -H "Authorization: Bearer $GIT_TOKEN" https://raw.githubusercontent.com/Chyau/fubon-daytrade/main/deploy/setup_vps.sh)
```

或分兩步（先 clone 再跑）：

```bash
git clone https://ghp_你的token@github.com/Chyau/fubon-daytrade.git && cd fubon-daytrade
GIT_TOKEN=ghp_你的token bash deploy/setup_vps.sh
```

> **GIT_TOKEN 哪裡拿**：GitHub → Settings → Developer settings →
> Fine-grained personal access tokens → Generate：
> Repository access 選 `fubon-daytrade`，權限給 `Contents: Read and write`。

腳本會自動完成：系統套件 → clone → venv → **富邦 SDK Linux 版 v2.4.0** →
**官方測試沙箱憑證下載＋.env 建立** → systemd 常駐服務 → 啟動。

## 3. 驗證

```bash
sudo systemctl status fubon-daytrade     # active (running)
curl -s http://127.0.0.1:9757/live | head -n 5   # 有 HTML 即正常
```

## 4. 從外地存取（重要：儀表板無帳密，勿裸露公網）

**Tailscale（推薦，免費）**：
1. VPS：`curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`
2. 手機／筆電：裝 Tailscale App，登入同一帳號
3. 瀏覽器開 `http://<VPS的Tailscale IP>:9757`（100.x.x.x）
4. VPS 的 HOST 已綁 0.0.0.0，但雲端防火牆沒開 9757 → **只有 Tailscale 私網進得來**

## 5. 日常操作（全在網頁）

- **回測**：`/backtest`（表單已預填做空＋偏空 24 檔，富邦/yf/demo 三來源）
- **自動執行**：`/live` 啟動引擎（dry 先跑 → test 沙箱送單）
- **盤後更新**：cron 自動（見下）或 SSH 手動 `./daily_update.sh && ./push_snapshot.sh`

### 可選：cron 自動化（VPS 的最大優勢）

```bash
crontab -e
# 加入兩行：
30 17 * * 1-5  /home/你的帳號/fubon-daytrade/daily_update.sh  >> /home/你的帳號/fubon-daytrade/logs/cron.log 2>&1
35 17 * * 1-5  /home/你的帳號/fubon-daytrade/push_snapshot.sh >> /home/你的帳號/fubon-daytrade/logs/cron.log 2>&1
```
（週一～五 17:30 自動更新資料、17:35 自動推送快照 — 人完全不用在家）

## 6. 交易資料遷移（可選：延續本機 572 筆歷史）

在家裡電腦執行：
```bash
scp C:\Users\Chyau\Documents\預設專案\fubon-daytrade\data\trades.db 帳號@VPS_IP:~/fubon-daytrade/data/
```

## 7. 程式碼更新流程

```
本機改程式 → git push → VPS: cd ~/fubon-daytrade && git pull
           → sudo systemctl restart fubon-daytrade
```

## 8. 故障排查

| 症狀 | 指令 |
|---|---|
| 服務沒起來 | `journalctl -u fubon-daytrade -n 50 --no-pager` |
| SDK 匯入錯誤 | `.venv/bin/python -c "import fubon_neo"` |
| 測試環境週末離線 | 正常現象（平日 09:30–19:00 供應），yf/demo 來源不受影響 |
| 快照推送失敗 | token 過期 → 重新 `git remote set-url origin https://新token@github.com/...` |

## 9. 成本與限制

- Oracle Free：$0｜Lightsail/Hetzner：~$5/月
- 富邦測試環境對雲端 IP 的連線**部署當天記得先驗證**：`/live` 啟動引擎 → 看 events 有無行情
- ⚠ 誠實聲明：本系統目前掛在**測試沙箱**（庫存每日重設、非真實資金）。
  未來切正式環境前，請重新評估「憑證上雲」的資安決策。
