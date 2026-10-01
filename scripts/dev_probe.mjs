/* 真浏览器探针：用 Edge 无头模式 + DevTools 协议打开地球页签，核对布局与交互。
 *
 *   node scripts/dev_probe.mjs [宽] [高]      默认 1440x900
 *
 * 为什么要它：假 DOM 桩（check_globe.mjs）测不出「方法名写错导致建球失败」「画布尺寸没跟上容器」
 * 这类真实浏览器里才暴露的问题——.pathAltitude() 那次事故就是靠它抓出来的。
 * 只读检查 + 发几次点击，不改任何项目文件。
 */
import { spawn } from "node:child_process";
import net from "node:net";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const W = Number(process.argv[2] || 1440);
const H = Number(process.argv[3] || 900);
const URL_PAGE = process.env.PROBE_URL || "http://127.0.0.1:8000/";
const EDGE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
].find((p) => fs.existsSync(p));
if (!EDGE) { console.error("找不到 Edge/Chrome，无法探测"); process.exit(1); }

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dsh-probe-"));

/* ---------- 极简 CDP over WebSocket（只用到 Runtime.evaluate / Page / Emulation） ---------- */
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
        buf = buf.subarray(i + 4);
        upgraded = true;
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
        try {
          const msg = JSON.parse(payload.toString("utf8"));
          if (msg.id && waiters.has(msg.id)) { waiters.get(msg.id)(msg); waiters.delete(msg.id); }
        } catch (e) { /* 忽略 */ }
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
        setTimeout(() => { if (waiters.has(myId)) { waiters.delete(myId); rej(new Error("CDP 超时 " + method)); } }, 20000);
      });
    }
  });
}

const child = spawn(EDGE, [
  "--headless=new", "--remote-debugging-port=0", `--user-data-dir=${profile}`,
  `--window-size=${W},${H}`, "--no-first-run", "--no-default-browser-check", "--use-angle=swiftshader",
  "--enable-unsafe-swiftshader", "about:blank",
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
const shot = async (name) => {
  const r = await cdp.send("Page.captureScreenshot", { format: "png" });
  const file = path.join(os.tmpdir(), name);
  fs.writeFileSync(file, Buffer.from(r.result.data, "base64"));
  return file;
};

await cdp.send("Page.enable");
await cdp.send("Runtime.enable");
// 无头页面默认 visibilityState="hidden"，Chrome 不提交滚动（scrollIntoView 无效）——
// 这个开关等价于 DevTools 的 "Emulate a focused page"，让滚动与计时器正常。
try { await cdp.send("Emulation.setFocusEmulationEnabled", { enabled: true }); } catch (e) {}

await cdp.send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });
await cdp.send("Page.navigate", { url: URL_PAGE });
await sleep(1800);
await evaluate(`document.querySelector('button[data-tab="globe"]').click()`);
await sleep(4200);
await evaluate(`document.getElementById("globe-stage").scrollIntoView({ block: "center" })`);
await sleep(900);

const fails = [];
const ok = (name, cond, extra = "") => {
  console.log(`  ${cond ? "✓" : "✗"} ${name}${extra ? " — " + extra : ""}`);
  if (!cond) fails.push(name);
};

console.log(`地球页签真机探测 @ ${W}x${H}`);

const geo = await evaluate(`(() => {
  const D = window.DSHGlobe && window.DSHGlobe.__debug;
  if (!D) return { error: "页面里没有 window.DSHGlobe.__debug" };
  const host = document.getElementById("globe-viz");
  const cv = host.querySelector("canvas");
  const g = D.globe;
  const B = (sel) => { const el = document.querySelector(sel); if (!el) return null;
    const b = el.getBoundingClientRect();
    return { x: Math.round(b.x), y: Math.round(b.y), r: Math.round(b.right), bottom: Math.round(b.bottom),
             w: Math.round(b.width), cx: Math.round(b.x + b.width / 2) }; };
  const stage = B("#globe-stage"), left = B(".globe-col-left .map-panel"), right = B(".globe-col-right .map-panel.map-records");
  const docW = document.documentElement.clientWidth;
  return {
    docW,
    scrollY: Math.round(window.scrollY),
    globe: !!g,
    globeError: D.state.globeError || "",
    info: (document.getElementById("globe-info") || {}).textContent || "",
    cards: D.state.cardList.length,
    records: D.state.recordEls.length,
    canvasFitsHost: !!cv && cv.style.width === host.clientWidth + "px" && cv.style.height === host.clientHeight + "px",
    canvas: cv ? cv.style.width + "x" + cv.style.height : null,
    host: host.clientWidth + "x" + host.clientHeight,
    aspect: g ? +g.camera().aspect.toFixed(3) : null,
    expectAspect: +(host.clientWidth / host.clientHeight).toFixed(3),
    stage, left, right,
    threeColumn: right ? right.x > stage.r : false,       // 右栏在地球右边才是三栏布局
    rightClipped: right ? right.r > docW + 16 : null,   // 容 16px：窄窗口 100vw 比内容宽出一条滚动条
    globeCentered: stage ? Math.abs(stage.cx - docW / 2) <= 2 : null,
    visibleCards: document.querySelectorAll("#globe-cards .city-card:not([style*='display: none'])").length,
  };
})()`);
if (geo.error) { console.error(geo.error); cdp.close(); child.kill(); process.exit(1); }
console.log(JSON.stringify(geo, null, 1));

ok("地球建起来了", geo.globe && !geo.globeError, geo.globeError || "无错误");
ok("没有「初始化失败」提示", !/初始化失败/.test(geo.info), geo.info.slice(0, 60));
ok("城市卡片全部就位", geo.cards > 60 && geo.records > 20, `${geo.cards} 城 / ${geo.records} 条`);
ok("画布尺寸等于容器", geo.canvasFitsHost, `canvas ${geo.canvas} vs host ${geo.host}`);
ok("相机比例等于容器比例", Math.abs(geo.aspect - geo.expectAspect) < 0.005, `${geo.aspect} vs ${geo.expectAspect}`);
ok("地球窗口在视口正中", geo.globeCentered, `stage.cx=${geo.stage && geo.stage.cx} 视口中心=${geo.docW / 2}`);
ok("右栏没有被视口切掉", geo.rightClipped === false, geo.right ? `右栏 right=${geo.right.r} 视口宽=${geo.docW}` : "");
/* 宽窗口是三栏（两栏分列地球左右），窄窗口（<=1240px）退回上下堆叠：
   左栏在地球上方、右栏在地球下方（DOM 顺序就是左-地球-右，靠 CSS 排不出别的顺序） */
if (geo.threeColumn) {
  ok("左右两栏在地球窗口之外（页面白底上）",
    !!geo.left && !!geo.right && geo.left.r < geo.stage.x && geo.right.x > geo.stage.r,
    `左栏 ${geo.left && geo.left.r} < 地球 ${geo.stage.x}，地球 ${geo.stage.r} < 右栏 ${geo.right && geo.right.x}`);
} else {
  const stageTopDoc = geo.stage.y + geo.scrollY, stageBottomDoc = geo.stage.bottom + geo.scrollY;
  const rightTopDoc = geo.right ? geo.right.y + geo.scrollY : -1;
  ok("窄窗口：两栏堆叠在地球上下（左栏在上、右栏在下）",
    !!geo.right && rightTopDoc >= stageBottomDoc - 2,
    `地球 ${stageTopDoc}..${stageBottomDoc}，右栏顶 ${rightTopDoc}`);
}

/* 定位：点世界之最 → 相机要动、卡片要高亮 */
const before = await evaluate(`(() => { const c = window.DSHGlobe.__debug.globe.camera(); return [c.position.x, c.position.y, c.position.z]; })()`);
const clicked = await evaluate(`(() => {
  const it = document.querySelector("#globe-records .rec-item");
  if (!it) return null;
  it.click();
  return it.textContent.trim().slice(0, 20);
})()`);
await sleep(1800);
const afterRec = await evaluate(`(() => {
  const D = window.DSHGlobe.__debug;
  const c = D.globe.camera();
  return { pos: [c.position.x, c.position.y, c.position.z],
           activeRecord: D.state.activeRecord,
           focused: document.querySelectorAll("#globe-cards .record-card.globe-focus").length,
           info: (document.getElementById("globe-info") || {}).textContent };
})()`);
const moved = JSON.stringify(before.map((v) => +v.toFixed(1))) !== JSON.stringify(afterRec.pos.map((v) => +v.toFixed(1)));
ok("点世界之最：镜头飞过去", moved, `${before.map((v) => +v.toFixed(0))} → ${afterRec.pos.map((v) => +v.toFixed(0))}`);
ok("点世界之最：对应卡片高亮", afterRec.focused > 0, `高亮 ${afterRec.focused} 张`);
ok("点世界之最：记下了 activeRecord", !!afterRec.activeRecord, String(afterRec.activeRecord));

/* 定位：点环线 → 镜头对准整条线、这一串卡片高亮 */
await evaluate(`(() => { window.DSHGlobe.__debug.closeGuide(); return true; })()`);
await evaluate(`document.querySelector("#globe-loops .loop-item").click()`);
await sleep(1800);
const afterLoop = await evaluate(`(() => {
  const D = window.DSHGlobe.__debug;
  const c = D.globe.camera();
  return { loop: D.state.activeLoop && D.state.activeLoop.name,
           cards: D.state.cardList.length,
           focused: document.querySelectorAll("#globe-cards .city-card.globe-focus").length,
           pos: [c.position.x, c.position.y, c.position.z].map((v) => +v.toFixed(0)),
           info: (document.getElementById("globe-info") || {}).textContent };
})()`);
ok("点环线：镜头移到该环线", afterLoop.pos.join() !== afterRec.pos.map((v) => +v.toFixed(0)).join(),
  `${afterRec.pos.map((v) => +v.toFixed(0))} → ${afterLoop.pos}`);
ok("点环线：整条线的卡片高亮", afterLoop.focused > 1, `${afterLoop.focused} 张（${afterLoop.cards} 张城市卡）`);
ok("点环线：只铺开这条线的城市", afterLoop.cards >= 3 && afterLoop.cards <= 12, `${afterLoop.cards} 张`);

console.log("截图:", await shot(`dev-probe-globe-${W}x${H}.png`));
console.log(fails.length ? `\n✗ ${fails.length} 项异常：${fails.join("、")}` : "\n✓ 全部正常");
cdp.close();
child.kill();
await sleep(300);
try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) { /* 忽略 */ }
process.exit(fails.length ? 1 : 0);
