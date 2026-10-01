/* 看一眼新加的两处 UI：数据台「直达」跳转条、以及漫游/地球的「隐藏图标」效果。
 *
 *   node scripts/shot_ui.mjs [宽] [高]
 *
 * 只负责截图，不做断言（断言在 dev_probe_data / check_map / check_globe 里）：
 *   1. 数据台顶部（跳转条 + 城市快捷框）
 *   2. 数据台滚到「城市停留动线」（验证跳转落点确实在那张卡）
 *   3. 漫游页签：隐藏图标 / 显示图标 各一张
 *   4. 地球页签：隐藏图标后的干净球面
 */
import { spawn } from "node:child_process";
import net from "node:net";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const W = Number(process.argv[2] || 1440), H = Number(process.argv[3] || 900);
const URL_PAGE = process.env.PROBE_URL || "http://127.0.0.1:8000/";
const EDGE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
].find((p) => fs.existsSync(p));
if (!EDGE) { console.error("找不到 Edge/Chrome"); process.exit(1); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dsh-shot-ui-"));

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
        : Buffer.from([0x81, 0x80 | 126, data.length >> 8, data.length & 0xff]);
      sock.write(Buffer.concat([header, mask, masked]));
      return new Promise((res, rej) => {
        waiters.set(myId, res);
        setTimeout(() => { if (waiters.has(myId)) { waiters.delete(myId); rej(new Error("CDP 超时 " + method)); } }, 30000);
      });
    }
  });
}

const child = spawn(EDGE, ["--headless=new", "--remote-debugging-port=0", `--user-data-dir=${profile}`,
  `--window-size=${W},${H}`, "--no-first-run", "--no-default-browser-check", "about:blank"], { stdio: "ignore" });
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
const shots = [];
const shoot = async (name) => {
  const shot = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const file = path.join(os.tmpdir(), `ui-${name}-${W}x${H}.png`);
  fs.writeFileSync(file, Buffer.from(shot.result.data, "base64"));
  shots.push(file);
};

await cdp.send("Page.enable");
await cdp.send("Runtime.enable");
await cdp.send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });
await cdp.send("Page.navigate", { url: URL_PAGE });
await sleep(2600);

/* 1) 数据台顶部：跳转条 */
await evaluate(`document.querySelector('button[data-tab="data"]').click()`);
await sleep(2600);
await evaluate(`document.getElementById("data-jump").scrollIntoView({ block: "start" })`);
await sleep(600);
await shoot("data-jump");

/* 1a) 数据可信度那张表（等异步覆盖数字填完再截） */
await sleep(2500);
await evaluate(`document.getElementById("data-health").scrollIntoView({ block: "start" })`);
await sleep(700);
await shoot("data-credibility");

/* 1c) 断点续爬横幅：挑一个真的留过旧断点的日期（磁盘上那几份 crawl_state_*.txt） */
await evaluate(`(async () => {
  const sel = document.getElementById("crawl-date");
  if (!sel) return false;
  sel.value = "2026-10-10";
  sel.dispatchEvent(new Event("change", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 1200));
  document.getElementById("data-crawl").scrollIntoView({ block: "start" });
  return true;
})()`);
await sleep(900);
await shoot("data-crawl-resume");

/* 1b) 环线三套入口指路牌 */
await evaluate(`document.querySelector('button[data-tab="loop"]').click()`);
await sleep(1200);
await evaluate(`document.getElementById("loop-hubs").scrollIntoView({ block: "start" })`);
await sleep(600);
await shoot("loop-hubs");
await evaluate(`document.querySelector('button[data-tab="data"]').click()`);
await sleep(1200);

/* 2) 点「城市动线」跳过去 */
await evaluate(`document.querySelector('#data-jump button[data-jump="data-guide"]').click()`);
await sleep(2200);
await shoot("data-guide-jump");

/* 3) 漫游：显示图标 / 隐藏图标 */
await evaluate(`document.querySelector('button[data-tab="map"]').click()`);
await sleep(4200);
await shoot("map-icons-on");
await evaluate(`(() => {
  const b = document.getElementById("map-icons-toggle");
  if (b) b.click();
  return true;
})()`);
await sleep(1500);
await shoot("map-icons-off");

/* 4) 地球：隐藏图标后的干净球面 */
await evaluate(`document.querySelector('button[data-tab="globe"]').click()`);
await sleep(5200);
/* 4a) 把环线下拉**摊开**（size=10）单独截一张：原生弹层截不到，摊开才能看出选项字色对不对 */
await evaluate(`(() => {
  const sel = document.getElementById("globe-loop-select");
  if (!sel) return false;
  sel.setAttribute("size", "10");
  return true;
})()`);
await sleep(700);
await shoot("globe-select-options");
await evaluate(`(() => {
  const sel = document.getElementById("globe-loop-select");
  if (sel) sel.removeAttribute("size");
  return true;
})()`);
await sleep(400);
await evaluate(`(() => {
  const dbg = window.DSHGlobe && window.DSHGlobe.__debug;
  if (dbg) {
    const loop = dbg.state.data.loops.find((l) => l.env === "海洋") || dbg.state.data.loops[0];
    if (loop) dbg.selectLoop(loop);
  }
  return true;
})()`);
await sleep(3000);
await shoot("globe-icons-on");
await evaluate(`(() => {
  const b = document.getElementById("globe-icons-toggle");
  if (b) b.click();
  return true;
})()`);
await sleep(1500);
await shoot("globe-icons-off");

console.log("截图：\n" + shots.join("\n"));
cdp.close(); child.kill(); await sleep(300);
try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
