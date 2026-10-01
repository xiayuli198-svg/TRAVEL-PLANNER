/* 在真实页面里跑一段 JS 并打印结果（调试用）。
 *
 *   node scripts/eval_in_page.mjs "document.title"
 *   node scripts/eval_in_page.mjs --tab data "document.querySelectorAll('.card').length"
 *   node scripts/eval_in_page.mjs --url http://127.0.0.1:8000/ "location.href"
 *
 * 用途：探针断言失败时，不用改探针就能立刻问「现在 DOM 到底是什么样」。
 */
import { spawn } from "node:child_process";
import net from "node:net";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const args = process.argv.slice(2);
let tab = "", url = process.env.PROBE_URL || "http://127.0.0.1:8000/";
const exprParts = [];
for (let i = 0; i < args.length; i++) {
  if (args[i] === "--tab") { tab = args[++i]; continue; }
  if (args[i] === "--url") { url = args[++i]; continue; }
  exprParts.push(args[i]);
}
const expr = exprParts.join(" ");
if (!expr) { console.error("用法：node scripts/eval_in_page.mjs [--tab data] \"<JS 表达式>\""); process.exit(2); }

const EDGE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
].find((p) => fs.existsSync(p));
if (!EDGE) { console.error("找不到 Edge/Chrome"); process.exit(1); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dsh-eval-"));

function wsConnect(wsUrl) {
  return new Promise((resolve, reject) => {
    const u = new URL(wsUrl);
    const key = crypto.randomBytes(16).toString("base64");
    const sock = net.connect(Number(u.port), u.hostname, () => {
      sock.write(`GET ${u.pathname}${u.search} HTTP/1.1\r\nHost: ${u.host}\r\nUpgrade: websocket\r\n` +
        `Connection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`);
    });
    let buf = Buffer.alloc(0), upgraded = false, id = 0;
    const waiters = new Map();
    sock.on("data", (chunk) => {
      buf = Buffer.concat([buf, chunk]);
      if (!upgraded) {
        const i = buf.indexOf("\r\n\r\n");
        if (i < 0) return;
        buf = buf.subarray(i + 4); upgraded = true;
        resolve({ send, close: () => sock.destroy() });
      }
      for (;;) {
        if (buf.length < 2) return;
        const b1 = buf[1], masked = (b1 & 0x80) !== 0;
        let len = b1 & 0x7f, off = 2;
        if (len === 126) { if (buf.length < 4) return; len = buf.readUInt16BE(2); off = 4; }
        else if (len === 127) { if (buf.length < 10) return; len = Number(buf.readBigUInt64BE(2)); off = 10; }
        const ml = masked ? 4 : 0;
        if (buf.length < off + ml + len) return;
        let payload = buf.subarray(off + ml, off + ml + len);
        if (masked) { const m = buf.subarray(off, off + 4); payload = Buffer.from(payload.map((v, i) => v ^ m[i % 4])); }
        buf = buf.subarray(off + ml + len);
        try { const msg = JSON.parse(payload.toString("utf8")); if (msg.id && waiters.has(msg.id)) { waiters.get(msg.id)(msg); waiters.delete(msg.id); } } catch (e) {}
      }
    });
    sock.on("error", reject);
    function send(method, params = {}) {
      const myId = ++id;
      const data = Buffer.from(JSON.stringify({ id: myId, method, params }));
      const mask = crypto.randomBytes(4);
      const masked = Buffer.from(data.map((v, i) => v ^ mask[i % 4]));
      const header = data.length < 126
        ? Buffer.from([0x81, 0x80 | data.length])
        : Buffer.from([0x81, 0x80 | 0x7e, data.length >> 8, data.length & 0xff]);
      sock.write(Buffer.concat([header, mask, masked]));
      return new Promise((res, rej) => {
        waiters.set(myId, res);
        setTimeout(() => { if (waiters.has(myId)) { waiters.delete(myId); rej(new Error("CDP 超时 " + method)); } }, 30000);
      });
    }
  });
}

const child = spawn(EDGE, [
  "--headless=new", "--remote-debugging-port=0", `--user-data-dir=${profile}`,
  "--window-size=1440,900", "--no-first-run", "--no-default-browser-check", "about:blank",
  "--disable-background-timer-throttling",
  "--disable-backgrounding-occluded-windows",
  "--disable-renderer-backgrounding",
  "--disable-features=CalculateNativeWinOcclusion",
], { stdio: "ignore" });
let wsUrl = null;
for (let i = 0; i < 60 && !wsUrl; i++) {
  await sleep(250);
  try {
    const f = path.join(profile, "DevToolsActivePort");
    if (fs.existsSync(f)) {
      const port = fs.readFileSync(f, "utf8").split("\n")[0].trim();
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      wsUrl = (list.find((t) => t.type === "page") || {}).webSocketDebuggerUrl;
    }
  } catch (e) { /* 还没起来 */ }
}
if (!wsUrl) { child.kill(); console.error("拿不到 DevTools 端口"); process.exit(1); }
const cdp = await wsConnect(wsUrl);
await cdp.send("Page.enable");
await cdp.send("Runtime.enable");
// 无头页面默认处于「隐藏」状态：`document.visibilityState === "hidden"` 时 Chrome 不提交滚动，
// `scrollIntoView` / `scrollTop` 都不会动（探针报「点了没反应」的真凶）。
// 这个开关等价于 DevTools 的 "Emulate a focused page"，滚动与计时器才正常。
try { await cdp.send("Emulation.setFocusEmulationEnabled", { enabled: true }); } catch (e) { /* 老版本没有 */ }
if (process.env.EVAL_METRICS === "1") {
  // 有些探针会用 setDeviceMetricsOverride；加上这个开关就能复现它们的视口条件
  await cdp.send("Emulation.setDeviceMetricsOverride",
    { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
}
await cdp.send("Page.navigate", { url });
await sleep(2500);
if (tab) {
  await cdp.send("Runtime.evaluate", {
    expression: `document.querySelector('button[data-tab="${tab}"]')?.click()`, returnByValue: true });
  await sleep(2500);
}
const r = await cdp.send("Runtime.evaluate",
  { expression: `(async () => {
      try { const v = await (${expr}); return JSON.stringify(v, null, 1); }
      catch (e) { return "表达式抛错: " + e.message; }
    })()`,
    returnByValue: true, awaitPromise: true });
const out = r.result && r.result.result ? r.result.result.value : "(没有返回值)";
console.log(out);
cdp.close(); child.kill(); await sleep(200);
try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
