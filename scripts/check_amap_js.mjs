/* 验证地球页签的「高德 3D」按钮：这一处用的是 Web端(JS API) key，只能真浏览器验。
 *
 *   node scripts/check_amap_js.mjs
 *
 * 高德两类 key 不通用：服务端那串是 Web 服务 key，浏览器这串必须是 Web端(JS API)。
 * 用错了会在这里看出来（脚本加载失败 / AMap 未定义 / 控制台报 10006、10009）。
 * 只在装了 Edge/Chrome 的机器上跑；不联外网时脚本会明确报「加载失败」而不是假装通过。
 */
import { spawn } from "node:child_process";
import net from "node:net";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const W = 1280, H = 900;
const URL_PAGE = process.env.PROBE_URL || "http://127.0.0.1:8000/";
const EDGE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
].find((p) => fs.existsSync(p));
if (!EDGE) { console.error("找不到 Edge/Chrome"); process.exit(1); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dsh-amap-js-"));

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
    const events = [];
    sock.on("data", (chunk) => {
      buf = Buffer.concat([buf, chunk]);
      if (!upgraded) {
        const i = buf.indexOf("\r\n\r\n");
        if (i < 0) return;
        buf = buf.subarray(i + 4); upgraded = true;
        resolve({ send, close: () => sock.destroy(), events });
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
        try { const msg = JSON.parse(payload.toString("utf8")); events.push(msg); if (msg.id && waiters.has(msg.id)) { waiters.get(msg.id)(msg); waiters.delete(msg.id); } } catch (e) {}
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
        setTimeout(() => { if (waiters.has(myId)) { waiters.delete(myId); rej(new Error("CDP 超时 " + method)); } }, 40000);
      });
    }
  });
}

const child = spawn(EDGE, [
  "--headless=new", "--remote-debugging-port=0", `--user-data-dir=${profile}`,
  `--window-size=${W},${H}`, "--no-first-run", "--no-default-browser-check", "about:blank",
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
const evaluate = async (expr) => {
  const r = await cdp.send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result && r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.text);
  return r.result && r.result.result ? r.result.result.value : undefined;
};
const fails = [];
const ok = (name, cond, extra = "") => {
  console.log(`  ${cond ? "✓" : "✗"} ${name}${extra ? " — " + extra : ""}`);
  if (!cond) fails.push(name);
};

await cdp.send("Page.enable");
await cdp.send("Runtime.enable");
// 无头页面默认 visibilityState="hidden"，Chrome 不提交滚动（scrollIntoView 无效）——
// 这个开关等价于 DevTools 的 "Emulate a focused page"，让滚动与计时器正常。
try { await cdp.send("Emulation.setFocusEmulationEnabled", { enabled: true }); } catch (e) {}

await cdp.send("Network.enable");
await cdp.send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });
await cdp.send("Page.navigate", { url: URL_PAGE });
await sleep(2500);

const keyInfo = await evaluate(`(async () => {
  const js = await fetch("/static/globe3d.js").then((r) => r.text());
  const m = js.match(/const\\s+AMAP_KEY\\s*=\\s*"([^"]*)"/);
  return { key: m ? m[1] : "", hasBtn: !!document.getElementById("globe-amap") };
})()`);
console.log(`前端 key：${keyInfo.key ? keyInfo.key.slice(0, 6) + "…" + keyInfo.key.slice(-4) : "(没读到)"}`);
ok("globe3d.js 里有 AMAP_KEY 且页面有「高德 3D」按钮", !!keyInfo.key && keyInfo.hasBtn);

/* 直达那个按钮：它会注入 webapi.amap.com 的 JS API，加载成功 window.AMap 就在 */
await evaluate(`document.querySelector('button[data-tab="globe"]').click()`);
await sleep(4000);
await evaluate(`document.getElementById("globe-amap").click()`);
let info = "", hasAMap = false;
for (let i = 0; i < 30; i++) {
  await sleep(1000);
  const snap = await evaluate(`(() => ({
    info: (document.getElementById("amap-info") || {}).textContent || "",
    amap: typeof window.AMap !== "undefined",
    canvas: !!document.querySelector("#globe-viz canvas"),
  }))()`);
  info = snap.info; hasAMap = snap.amap;
  if (hasAMap || /失败/.test(info)) break;
}
console.log(`按钮反馈：${info.slice(0, 80)}`);
const netFails = cdp.events
  .filter((m) => m.method === "Network.loadingFailed")
  .map((m) => m.params.errorText + " " + (m.params.blockedReason || ""));
const jsApiReq = cdp.events
  .filter((m) => m.method === "Network.responseReceived"
    && /webapi\.amap\.com/.test((m.params.response || {}).url || ""))
  .map((m) => `${m.params.response.status} ${m.params.response.url.slice(0, 60)}`);
console.log(`webapi.amap.com 请求：${jsApiReq.slice(0, 3).join(" | ") || "（没有发出请求）"}`);

ok("点了「高德 3D」会去加载 JS API（发出 webapi.amap.com 请求）", jsApiReq.length > 0,
  jsApiReq[0] || netFails[0] || "无请求");
ok("JS API 加载成功（window.AMap 存在）", hasAMap, info.slice(0, 60));
if (!hasAMap) {
  console.log("\n提示：如果这里失败但服务端接口正常，说明这串 key 不是「Web端(JS API)」类型，");
  console.log("      去高德控制台建一个 Web端(JS API) key（或给现有 key 加域名白名单）再换前端那一处：");
  console.log("      python scripts/set_amap_key.py <JS_API_KEY>");
}
console.log(fails.length ? `\n✗ ${fails.length} 项异常` : "\n✓ 高德 3D 这条链路正常");
cdp.close(); child.kill(); await sleep(300);
try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
process.exit(fails.length ? 1 : 0);
