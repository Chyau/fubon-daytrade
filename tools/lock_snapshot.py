# -*- coding: utf-8 -*-
"""lock_snapshot.py — 快照密碼鎖（StatiCrypt 模式：AES-256-GCM 真加密）

將 index.html 以密碼加密為「自我解密頁面」：訪客輸入密碼後，瀏覽器以
WebCrypto（PBKDF2-SHA256 150k 次迭代 + AES-256-GCM）解密還原內容。
密碼錯誤 → GCM 認證失敗 → 無法取得原文。真加密，不是做樣子的擋板。

用法：
  設定環境變數 SNAPSHOT_PASSWORD 後：
  python tools/lock_snapshot.py snapshot/index.html docs/index.html
  （未設密碼 → 原檔保留不加密）

密碼管理：
  本機：.env 加一行 SNAPSHOT_PASSWORD=你的密碼
  Actions：repo Settings → Secrets and variables → Actions → SNAPSHOT_PASSWORD
"""
import base64
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ITERATIONS = 150_000

PAGE = """<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🔒 需要密碼</title>
<style>
body{font-family:"Microsoft JhengHei",system-ui,sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#0f172a;color:#e2e8f0}
.box{background:#1e293b;padding:36px 40px;border-radius:14px;box-shadow:0 10px 40px rgba(0,0,0,.5)}
h2{margin:0 0 8px}p{color:#94a3b8;font-size:14px}
input,button{padding:10px 14px;font-size:16px;border-radius:8px;border:1px solid #334155;background:#0f172a;color:#e2e8f0}
button{background:#2563eb;border-color:#2563eb;cursor:pointer;margin-left:8px}
button:hover{background:#1d4ed8}
#err{color:#f87171;font-size:14px;margin-top:10px;min-height:20px}
</style></head><body>
<div class="box"><h2>🔒 佛系當沖快照（已加密）</h2>
<p>請輸入存取密碼以解鎖本頁內容</p>
<form onsubmit="return tryUnlock()">
<input id="pw" type="password" placeholder="存取密碼" autofocus><button>解鎖</button>
</form><div id="err"></div></div>
<script>
const DATA="__PAYLOAD__";
const te=new TextEncoder(),td=new TextDecoder();
function b64(s){const b=atob(s),a=new Uint8Array(b.length);for(let i=0;i<b.length;i++)a[i]=b.charCodeAt(i);return a}
async function unlock(pw){
  const raw=b64(DATA),salt=raw.slice(0,16),iv=raw.slice(16,28),ct=raw.slice(28);
  const km=await crypto.subtle.importKey("raw",te.encode(pw),"PBKDF2",false,["deriveKey"]);
  const key=await crypto.subtle.deriveKey({name:"PBKDF2",salt,iterations:__ITER__,hash:"SHA-256"},km,{name:"AES-GCM",length:256},false,["decrypt"]);
  const plain=await crypto.subtle.decrypt({name:"AES-GCM",iv},key,ct);
  document.open();document.write(td.decode(plain));document.close();
}
function tryUnlock(){
  document.getElementById("err").textContent="";
  unlock(document.getElementById("pw").value)
    .catch(()=>{document.getElementById("err").textContent="密碼錯誤，請再試一次";});
  return false;
}
</script></body></html>"""


def encrypt_file(path: str, password: str) -> None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    p = Path(path)
    plaintext = p.read_bytes()
    salt, iv = os.urandom(16), os.urandom(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=ITERATIONS)
    key = kdf.derive(password.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, plaintext, None)
    payload = base64.b64encode(salt + iv + ct).decode("ascii")
    page = PAGE.replace("__PAYLOAD__", payload).replace("__ITER__", str(ITERATIONS))
    p.write_text(page, encoding="utf-8")
    print(f"  ✓ 已加密：{path}（{len(plaintext):,} → {len(page):,} bytes）")


def main():
    pw = os.environ.get("SNAPSHOT_PASSWORD", "").strip()
    files = [a for a in sys.argv[1:] if Path(a).exists()]
    if not pw:
        print("（SNAPSHOT_PASSWORD 未設定 — 快照維持明碼）")
        return 0
    if not files:
        print("（找不到要加密的檔案）")
        return 0
    for f in files:
        encrypt_file(f, pw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
