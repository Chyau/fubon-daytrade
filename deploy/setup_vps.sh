#!/usr/bin/env bash
# ===============================================================
# 佛系當沖量化系統 — Linux VPS 一鍵部署
# 支援：Ubuntu 22.04 / 24.04（x86_64 與 aarch64 皆可）
#
# 用法（在 VPS 上）：
#   GIT_TOKEN=ghp_xxxx bash deploy/setup_vps.sh
#   （GIT_TOKEN = GitHub Fine-grained PAT，需 fubon-daytrade 讀取+寫入權）
#
# 完成後：
#   - systemd 常駐服務 fubon-daytrade（開機自啟、崩潰 5 秒自動重啟）
#   - 儀表板 http://<VPS_IP>:9757（⚠ 勿對公網開放 9757，建議走 Tailscale）
# ===============================================================
set -euo pipefail

REPO="github.com/Chyau/fubon-daytrade.git"
INSTALL_DIR="$HOME/fubon-daytrade"
SDK_VER="2.4.0"
SDK_X86="https://www.fbs.com.tw/TradeAPI_SDK/fubon_binary/fubon_neo-${SDK_VER}-cp37-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.zip"
SDK_ARM="https://www.fbs.com.tw/TradeAPI_SDK/fubon_binary/fubon_neo-${SDK_VER}-cp37-abi3-manylinux_2_17_aarch64.manylinux2014_aarch64.zip"
TEST_ENV_ZIP="https://www.fbs.com.tw/TradeAPI_SDK/sample_code/test_environment.zip"

echo "=== [1/7] 系統套件 ==="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git curl unzip

echo "=== [2/7] 取得程式碼（GitHub 私人 repo）==="
if [ -z "${GIT_TOKEN:-}" ]; then
  echo "錯誤：請以 GIT_TOKEN=ghp_xxx bash deploy/setup_vps.sh 執行"
  exit 1
fi
if [ -d "$INSTALL_DIR/.git" ]; then
  cd "$INSTALL_DIR" && git pull --rebase || true
else
  git clone "https://${GIT_TOKEN}@${REPO}" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
  # 之後 push_snapshot.sh 推送也走同一 token
  git remote set-url origin "https://${GIT_TOKEN}@${REPO}"
fi

echo "=== [3/7] Python 虛擬環境 ==="
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip -q
./.venv/bin/pip install -r requirements.txt -q

echo "=== [4/7] 安裝富邦 SDK Linux 版 v${SDK_VER} ==="
ARCH=$(uname -m)
case "$ARCH" in
  x86_64)  SDK_URL="$SDK_X86" ;;
  aarch64) SDK_URL="$SDK_ARM" ;;
  *) echo "不支援的架構：$ARCH"; exit 1 ;;
esac
TMP=$(mktemp -d)
curl -sL "$SDK_URL" -o "$TMP/sdk.zip"
unzip -oq "$TMP/sdk.zip" -d "$TMP/sdk"
./.venv/bin/pip install "$TMP"/sdk/*.whl -q
./.venv/bin/python -c "import fubon_neo; print('  ✓ fubon_neo', fubon_neo.__version__ if hasattr(fubon_neo,'__version__') else 'OK')"

echo "=== [5/7] 測試環境沙箱憑證（富邦官方公開樣本）==="
mkdir -p test_environment
if [ ! -f test_environment/58581758.pfx ]; then
  curl -sL "$TEST_ENV_ZIP" -o "$TMP/tenv.zip"
  unzip -oq "$TMP/tenv.zip" -d "$TMP/tenv"
  find "$TMP/tenv" -name "*.pfx" -exec cp {} test_environment/ \; 2>/dev/null || true
  echo "  ✓ 已下載官方測試憑證"
else
  echo "  （憑證已存在）"
fi
if [ ! -f .env ]; then
  cat > .env <<'ENVEOF'
# 富邦測試沙箱（官方預設樣本帳號；正式環境請自行更換）
FUBON_USE_TEST=1
FUBON_TEST_URL=wss://neoapitest.fbs.com.tw/TASP/XCPXWS
FUBON_TEST_ID=58581758
FUBON_TEST_PASSWORD=12345678
FUBON_TEST_CERT_PATH=./test_environment/58581758.pfx
FUBON_TEST_CERT_PASSWORD=12345678
ENVEOF
  echo "  ✓ .env 已建立（測試沙箱預設值）"
else
  echo "  （.env 已存在，保留）"
fi
rm -rf "$TMP"

echo "=== [6/7] systemd 常駐服務（開機自啟＋崩潰自動重啟）==="
sudo tee /etc/systemd/system/fubon-daytrade.service >/dev/null <<EOF
[Unit]
Description=Fubon DayTrade Web Dashboard (佛系當沖)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${INSTALL_DIR}
Environment=HOST=0.0.0.0
Environment=PYTHONUNBUFFERED=1
ExecStart=${INSTALL_DIR}/.venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now fubon-daytrade
sleep 3
sudo systemctl --no-pager status fubon-daytrade | head -n 8 || true

echo "=== [7/7] 完成 ==="
IP=$(curl -s ifconfig.me || echo "<VPS_IP>")
echo ""
echo "✅ 部署完成！"
echo "   儀表板：http://${IP}:9757"
echo ""
echo "⚠ 安全提醒：儀表板無帳密，公網勿開 9757 埠（雲端防火牆預設關閉即可）。"
echo "  建議安裝 Tailscale 後以 http://$(hostname -I | awk '{print $1}'):9757 私網存取。"
echo ""
echo "常用指令："
echo "  sudo systemctl restart fubon-daytrade   # 重啟（改完程式碼後）"
echo "  journalctl -u fubon-daytrade -f          # 即時日誌"
echo "  ./daily_update.sh                        # 盤後資料更新（可掛 cron 17:30）"
echo "  ./push_snapshot.sh                       # 快照推送（Vercel 自動跟進）"
