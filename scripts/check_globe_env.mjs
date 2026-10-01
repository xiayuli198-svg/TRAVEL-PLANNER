/* 真浏览器探针：地球上的环线颜色是不是「跟着环境走、而且看得见」。
 *
 *   node scripts/check_globe_env.mjs [宽] [高]
 *
 * 用户的原话是「浅色我看不见，去换一套颜色，根据地球模型的环境，沙漠就是土黄色」。
 * 后端给每条环线 env/env_color，前端：
 *   ① 列表圆点与标签边框用原色（白底，用原色才准）；
 *   ② 球面上那条线用 globeStroke 提亮过 —— 否则海洋深蓝 #2f6f9f 会糊进近黑的球面。
 * 这里逐条选中海上 / 极地 / 沙漠环线，量一下真实画上去的颜色与亮度，并截图。
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
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dsh-globe-env-"));

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
const errors = [];
const netFailures = [];
{
  const u = new URL(wsUrl);
  const key = crypto.randomBytes(16).toString("base64");
  const sock = net.connect(Number(u.port), u.hostname, () => {
    sock.write(`GET ${u.pathname}${u.search} HTTP/1.1\r\nHost: ${u.host}\r\nUpgrade: websocket\r\n` +
      `Connection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`);
  });
  let buf = Buffer.alloc(0), upgraded = false;
  sock.on("data", (chunk) => {
    buf = Buffer.concat([buf, chunk]);
    if (!upgraded) { const i = buf.indexOf("\r\n\r\n"); if (i < 0) return; buf = buf.subarray(i + 4); upgraded = true; }
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
        if (msg.method === "Runtime.exceptionThrown") {
          const d = msg.params.exceptionDetails;
          errors.push("异常: " + d.text + " " +
            (d.exception && d.exception.description ? d.exception.description.split("\n").slice(0, 2).join(" | ") : ""));
        } else if (msg.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(msg.params.type)) {
          errors.push("console." + msg.params.type + ": " +
            msg.params.args.map((a) => a.value ?? a.description ?? a.type).join(" ").slice(0, 240));
        } else if (msg.method === "Network.loadingFailed") {
          netFailures.push(msg.params.errorText + " " + (msg.params.blockedReason || ""));
        }
      } catch (e) { /* 忽略 */ }
    }
  });
  sock.on("error", () => {});
}
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
await sleep(1500);
await evaluate(`document.querySelector('button[data-tab="globe"]').click()`);

console.log("地球环线配色冒烟测试（环境色 + 球面可见度）");
let ready = false;
for (let i = 0; i < 40 && !ready; i++) {
  await sleep(500);
  ready = await evaluate(`!!(window.DSHGlobe && window.DSHGlobe.__debug &&
    window.DSHGlobe.__debug.state.ready && window.DSHGlobe.__debug.globe)`);
}
ok("地球数据与 3D 场景都就绪", ready);
if (!ready) { cdp.close(); child.kill(); process.exit(1); }

/* 后端给的 env / env_color 才是「环境色」的源头 */
const backend = await evaluate(`(async () => {
  const data = await fetch("/api/world/map").then((r) => r.json());
  return data.loops.map((l) => ({ id: l.id, name: l.name, env: l.env, env_color: l.env_color, mode: l.mode }));
})()`);
const byName = Object.fromEntries(backend.map((l) => [l.name, l]));
console.log("后端环线环境色：", backend.map((l) => `${l.name}=${l.env}${l.env_color}`).join(" · "));
ok("每条环线都带 env 与 env_color", backend.every((l) => l.env && /^#[0-9a-f]{6}$/i.test(l.env_color)),
  `${backend.length} 条`);
ok("海上知名线路在数据里", backend.filter((l) => l.env === "海洋").length >= 3,
  backend.filter((l) => l.env === "海洋").map((l) => l.name).join("、"));
ok("南北极圈在数据里", backend.filter((l) => l.env === "极地").length >= 2,
  backend.filter((l) => l.env === "极地").map((l) => l.name).join("、"));
ok("沙漠线是土黄色", (byName["丝绸之路环线"] || {}).env_color === "#d9a441" ||
  backend.some((l) => l.env === "沙漠" && l.env_color === "#d9a441"),
  (backend.find((l) => l.env === "沙漠") || {}).env_color || "");

/* 逐条选中，量球面上真正画出来的线的颜色 */
const targets = ["地中海邮轮环线", "南极半岛航线", "北极圈·斯瓦尔巴与极光带", "中亚丝路环线"];
const rgb = (hex) => {
  const n = parseInt(hex.replace("#", ""), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
};
const lum = (hex) => {
  const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  const [r, g, b] = rgb(hex);
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
};
/** 色相（度）：用来判断「还是不是同一个环境色系」，只看亮度会被误导 */
const hue = (hex) => {
  const [r0, g0, b0] = rgb(hex);
  const r = r0 / 255, g = g0 / 255, b = b0 / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  if (!d) return 0;
  let h;
  if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
  else if (max === g) h = ((b - r) / d + 2) / 6;
  else h = ((r - g) / d + 4) / 6;
  return h * 360;
};
const hueGap = (a, b) => { const d = Math.abs(hue(a) - hue(b)) % 360; return d > 180 ? 360 - d : d; };
const shots = [];
for (const name of targets) {
  const res = await evaluate(`(async () => {
    const dbg = window.DSHGlobe.__debug;
    const loop = dbg.state.data.loops.find((l) => l.name === ${JSON.stringify(name)});
    if (!loop) return { ok: false };
    dbg.selectLoop(loop);
    await new Promise((r) => setTimeout(r, 2600));
    const paths = (dbg.globe.pathsData() || []).map((p) => p.color);
    const listItem = [...document.querySelectorAll("#globe-loops .loop-item")]
      .find((el) => (el.querySelector(".loop-title") || {}).textContent.includes(loop.name));
    const dot = listItem ? listItem.querySelector(".loop-env") : null;
    const tag = listItem ? listItem.querySelector(".tag.env") : null;
    return {
      ok: true, name: loop.name, env: loop.env, envColor: loop.env_color,
      mode: loop.mode, cities: loop.cities.length,
      pathColors: [...new Set(paths)],
      pathCount: paths.length,
      dotColor: dot ? getComputedStyle(dot).backgroundColor : "",
      tagText: tag ? tag.textContent.trim() : "",
      tagColor: tag ? getComputedStyle(tag).color : "",
      tagBorder: tag ? getComputedStyle(tag).borderTopColor : "",
      visible: dbg.visibleCards().length,
      cards: dbg.state.cardList.length,
      info: (document.getElementById("globe-info") || {}).textContent || "",
    };
  })()`);
  if (!res || !res.ok) { ok(`选中「${name}」`, false, "列表里没有这条环线"); continue; }
  const stroke = res.pathColors[0] || "";
  const sameFamily = !!stroke && hueGap(stroke, res.envColor) <= 25;
  console.log(`  · ${res.name}｜env=${res.env}(${res.envColor})｜mode=${res.mode}｜` +
    `球面线=${res.pathColors.join(",")}｜${res.pathCount} 段｜可见卡片 ${res.visible}/${res.cards}｜` +
    `列表圆点=${res.dotColor}｜标签「${res.tagText}」字色 ${res.tagColor} 边框 ${res.tagBorder}`);
  ok(`「${res.name}」球面上的线还是同一个环境色系（色相差 ≤25°）`, sameFamily,
    `${res.envColor}(h${Math.round(hue(res.envColor))}) → ${stroke}(h${Math.round(hue(stroke))})`);
  ok(`「${res.name}」的线在球面上亮度够（≥0.18）`, lum(stroke) >= 0.18,
    `亮度 ${lum(stroke).toFixed(3)}`);
  ok(`「${res.name}」列表里有点/标签标识环境`, !!res.dotColor && res.tagText === res.env,
    `${res.tagText} 字色 ${res.tagColor}`);
  ok(`「${res.name}」选中后卡片画得出来（不是空球）`, res.visible >= 1, `${res.visible}/${res.cards}`);

  const shot = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  const file = path.join(os.tmpdir(), `globe-env-${targets.indexOf(name)}-${W}x${H}.png`);
  fs.writeFileSync(file, Buffer.from(shot.result.data, "base64"));
  shots.push(file);
}
console.log("截图:", shots.join("  "));
if (errors.length) { console.log("== 页面报错 =="); errors.slice(0, 6).forEach((e) => console.log("  " + e)); }
if (netFailures.length) { console.log("== 请求失败 =="); [...new Set(netFailures)].slice(0, 6).forEach((e) => console.log("  " + e)); }
ok("页面没有异常", errors.length === 0, errors[0] || "无");

console.log(fails.length ? `\n✗ ${fails.length} 项异常：${fails.join("、")}` : "\n✓ 全部正常");
cdp.close(); child.kill(); await sleep(300);
try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
process.exit(fails.length ? 1 : 0);
