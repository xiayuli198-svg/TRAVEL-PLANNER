"""飞猪官方 MCP search_flight 探针。

先尝试明文 gzip 的 x-ff-ctx；若被拒绝（返回鉴权错误），
提示改用 AES-GCM 变体（需 pip install cryptography）。
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.request
from pathlib import Path

MCP_URL = "https://flyai.open.fliggy.com/mcp"
X_TTID = "ai2c(sk.clawhub)"
SIGN_VER = "7"
UA = "flyai-cli/1.0.6"


def credentials() -> tuple[str, str]:
    api_key = os.environ.get("FLIGGY_API_KEY", "").strip()
    sign_secret = os.environ.get("FLIGGY_SIGN_SECRET", "").strip()
    if not api_key or not sign_secret:
        raise RuntimeError("请设置 FLIGGY_API_KEY 和 FLIGGY_SIGN_SECRET")
    return api_key, sign_secret


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def make_signature(method: str, path: str, ts: str, nonce: str,
                   body: str, auth: str, secret: str) -> str:
    sign_string = f"{method}\n{path}\n{ts}\n{nonce}\n{sha256_hex(body)}\n{sha256_hex(auth)}"
    sig = hmac.new(secret.encode(), sign_string.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def build_ctx_plain(device_id: str) -> str:
    ctx = {
        "machine": {"platform": "windows", "arch": "AMD64", "cpus": 8,
                    "memoryTierGB": 16, "osType": "windows",
                    "nodeVersion": "v22.22.0", "osReleaseMajor": "10"},
        "fingerprint": {"language": "zh-CN", "platform": "windows",
                        "userAgent": f"{UA} (Node.js v22.22.0; windows AMD64)",
                        "hardwareConcurrency": 8, "deviceMemory": 8,
                        "clientSurface": "cli", "timezoneOffset": -480,
                        "deviceId": device_id},
    }
    return base64.b64encode(gzip.compress(json.dumps(ctx).encode())).decode()


def call_search(origin: str, destination: str, date: str, device_id: str,
                encrypted: bool = False) -> dict:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search_flight",
                   "arguments": {"origin": origin, "destination": destination,
                                 "depDate": date}},
    }, ensure_ascii=False)
    ts = str(int(time.time() * 1000))
    nonce = secrets.token_hex(16)
    api_key, sign_secret = credentials()
    auth = f"Bearer {api_key}"
    sig = make_signature("POST", "/mcp", ts, nonce, body, auth, sign_secret)

    if encrypted:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            key = hashlib.sha256(sign_secret.encode()).digest()
            iv = secrets.token_bytes(12)
            compressed = gzip.compress(json.dumps(_ctx_dict(device_id)).encode())
            enc = AESGCM(key).encrypt(iv, compressed, None)
            ff_ctx = base64.b64encode(bytes([0x01]) + iv + enc).decode()
        except ImportError:
            print("cryptography 未安装，回退明文 ctx")
            ff_ctx = build_ctx_plain(device_id)
    else:
        ff_ctx = build_ctx_plain(device_id)

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": auth,
        "x-ff-ctx": ff_ctx,
        "x-ttid": X_TTID,
        "User-Agent": UA,
        "x-flyai-ts": ts,
        "x-flyai-sign-ver": SIGN_VER,
        "x-flyai-sign-alg": "hmac-sha256",
        "x-flyai-nonce": nonce,
        "x-flyai-sign": sig,
    }
    req = urllib.request.Request(MCP_URL, data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
    return json.loads(raw)


def _ctx_dict(device_id: str) -> dict:
    return {
        "machine": {"platform": "windows", "arch": "AMD64", "cpus": 8,
                    "memoryTierGB": 16, "osType": "windows",
                    "nodeVersion": "v22.22.0", "osReleaseMajor": "10"},
        "fingerprint": {"language": "zh-CN", "platform": "windows",
                        "userAgent": f"{UA} (Node.js v22.22.0; windows AMD64)",
                        "hardwareConcurrency": 8, "deviceMemory": 8,
                        "clientSurface": "cli", "timezoneOffset": -480,
                        "deviceId": device_id},
    }


def main() -> None:
    did_path = Path(__file__).resolve().parent.parent / "data" / ".fliggy_device_id"
    did_path.parent.mkdir(parents=True, exist_ok=True)
    if did_path.exists():
        device_id = did_path.read_text().strip()
    else:
        device_id = hashlib.sha256(secrets.token_bytes(16)).hexdigest()
        did_path.write_text(device_id)

    origin = sys.argv[1] if len(sys.argv) > 1 else "北京"
    destination = sys.argv[2] if len(sys.argv) > 2 else "上海"
    date = sys.argv[3] if len(sys.argv) > 3 else "2026-09-09"
    print(f"search_flight({origin} -> {destination}, {date})")

    try:
        rj = call_search(origin, destination, date, device_id, encrypted=True)
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(600)
        except Exception:  # noqa: BLE001
            pass
        print(f"HTTP {e.code}: {body.decode('utf-8', errors='replace')}")
        return
    except Exception as e:  # noqa: BLE001
        print("请求失败:", type(e).__name__, str(e)[:300])
        return
    print("顶层键:", list(rj.keys()))
    content = (rj.get("result") or {}).get("content") or []
    if not content:
        print("响应无 content:", json.dumps(rj, ensure_ascii=False)[:600])
        return
    text = content[0].get("text")
    if not text:
        print("content[0] 无 text:", json.dumps(content[0], ensure_ascii=False)[:600])
        return
    data = json.loads(text)
    print("内层键:", list(data.keys()))
    payload = data.get("data") or {}
    items = payload.get("itemList") or []
    print(f"itemList 条数: {len(items)}")
    if items:
        j0 = items[0].get("journeys") or [{}]
        segs = j0[0].get("segments") or []
        print(f"首条 journeys[0].segments 段数: {len(segs)}")
        if segs:
            s = segs[0]
            print("  首段字段:", json.dumps(s, ensure_ascii=False)[:600])
            print("  首段解析: 航班号=%s 航司=%s %s(%s)->%s(%s) 起=%s 降=%s 时长=%s"
                  % (s.get("marketingTransportNo"), s.get("marketingTransportName"),
                     s.get("depCityName"), s.get("depStationCode"),
                     s.get("arrCityName"), s.get("arrStationCode"),
                     s.get("depDateTime"), s.get("arrDateTime"), s.get("duration")))


if __name__ == "__main__":
    main()
