/* 真浏览器探针：单程规划页签「点一下到底能不能出结果」。
 *
 *   node scripts/check_plan.mjs [宽] [高]
 *
 * 之前四个探针都没点过「开始规划」，所以「点了报请求失败」这类问题一路滑过去。
 * 这里就照着用户的动作走：填北京→上海、选日期、点开始规划，看状态栏与方案卡片，
 * 并把页面异常（console/exception）都收上来。
 */
import { spawn } from "node:child_process";
import net from "node:net";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const W = Number(process.argv[2] || 1440), H = Number(process.argv[3] || 900);
const URL_PAGE = process.env.PROBE_URL || "http://127.0.0.1:8000/";
const MODE = process.argv[4] || "mixed";
const EDGE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
].find((p) => fs.existsSync(p));
if (!EDGE) { console.error("找不到 Edge/Chrome"); process.exit(1); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dsh-check-plan-"));

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
await cdp.send("Network.enable");
await cdp.send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });
await cdp.send("Page.navigate", { url: URL_PAGE });
await sleep(2200);

console.log(`单程规划页签冒烟测试（模式 ${MODE}）`);
const filled = await evaluate(`(() => {
  const set = (id, v) => {
    const el = document.getElementById(id);
    el.value = v;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return el.value;
  };
  document.querySelector('button[data-tab="plan"]').click();
  const from = set("p-from", "北京");
  const to = set("p-to", "上海");
  const date = set("p-date", "2026-06-17");
  set("p-time", "08:00");
  set("p-mode", ${JSON.stringify(MODE)});
  set("p-transfers", "2");
  set("p-buffer", "30");
  return { from, to, date, mode: document.getElementById("p-mode").value };
})()`);
console.log("填表:", JSON.stringify(filled));
ok("起终点与日期都填上了", filled.from === "北京" && filled.to === "上海" && /^\d{4}-\d{2}-\d{2}$/.test(filled.date),
  JSON.stringify(filled));

/* 真正点「开始规划」 */
await evaluate(`document.getElementById("p-go").click()`);
let tries = 0, cards = 0, status = "";
while (tries < 90) {
  await sleep(500);
  const snap = await evaluate(`(() => ({
    cards: document.querySelectorAll("#p-results .journey").length,
    status: (document.getElementById("p-status") || {}).textContent || "",
    warn: (document.querySelector("#p-results .warn") || {}).textContent || "",
  }))()`);
  cards = snap.cards; status = snap.status;
  if (cards > 0 || /失败|错误|没有|未找到/.test(snap.status) || snap.warn) break;
  tries++;
}
/* 方案卡片出来之后，「相关路线参考」是另一个异步请求 —— 不等它就会偶发「无」（后台有爬取任务时更慢） */
{
  let waits = 0;
  while (waits < 20) {
    const done = await evaluate(`(() => {
      const box = document.getElementById("p-routes");
      return !!(box && box.textContent.trim().length);
    })()`);
    if (done) break;
    await sleep(400);
    waits++;
  }
}
const view = await evaluate(`(() => {
  const box = document.getElementById("p-results");
  const first = box.querySelector(".journey");
  const guides = box.querySelector(".city-guides");
  const cards = guides ? [...guides.querySelectorAll(".cg-card")] : [];
  return {
    cards: box.querySelectorAll(".journey").length,
    status: (document.getElementById("p-status") || {}).textContent || "",
    warn: (box.querySelector(".warn") || {}).textContent || "",
    firstHead: first ? (first.querySelector(".j-head") || {}).textContent.replace(/\\s+/g, " ").trim() : "",
    legs: first ? first.querySelectorAll(".leg").length : 0,
    hasLink: !!(first && first.querySelector("a.link")),
    routesBox: (document.getElementById("p-routes") || {}).textContent ? "有" : "无",
    guideCards: cards.length,
    guideHead: guides ? (guides.querySelector("h3") || {}).textContent || "" : "",
    guideNames: cards.map((c) => (c.querySelector(".cg-head b") || {}).textContent || ""),
    guideRoles: cards.map((c) => (c.querySelector(".cg-role") || {}).textContent || ""),
    guideHints: cards.map((c) => ((c.querySelector(".cg-meta") || {}).textContent || "").replace(/\\s+/g, " ").trim()),
    guideChips: cards.reduce((n, c) => n + c.querySelectorAll(".cg-chip").length, 0),
    guideHasFlow: cards.some((c) => !!c.querySelector(".gd-open")),
    guideExpandable: cards.some((c) => !!c.querySelector(".cg-toggle")),
  };
})()`);
console.log("规划结果:", JSON.stringify(view, null, 1));
if (errors.length) { console.log("== 页面报错 =="); errors.slice(0, 6).forEach((e) => console.log("  " + e)); }
if (netFailures.length) { console.log("== 请求失败 =="); [...new Set(netFailures)].slice(0, 6).forEach((e) => console.log("  " + e)); }

ok("点「开始规划」没有报请求失败", !/请求失败|HTTP \\d{3}/.test(view.status), view.status.slice(0, 90));
ok("算出了方案卡片", view.cards >= 1, `${view.cards} 个方案`);
ok("首个方案有车次分段与时刻", view.legs >= 1 && /→/.test(view.firstHead), view.firstHead.slice(0, 80));
ok("给了 12306 购票链接", view.hasLink);
ok("底部带出相关路线参考", view.routesBox === "有");
ok("沿途城市资料挂出来了（不用恰好命中攻略）", view.guideCards >= 2 && /沿途城市资料/.test(view.guideHead),
  `${view.guideCards} 座：${view.guideNames.join("、")}`);
ok("每座城标了角色（起点/中转/终点）", view.guideRoles.length === view.guideCards && view.guideRoles.every(Boolean),
  view.guideRoles.join("/"));
ok("写了建议停留多久", view.guideHints.some((t) => /建议留|半天|一整天|顺路停/.test(t)),
  view.guideHints[0] || "");
ok("带上看点标签", view.guideChips >= 2, `${view.guideChips} 个标签`);
ok("能直接跳到「城内动线」并能展开攻略", view.guideHasFlow && view.guideExpandable);
ok("页面没有异常", errors.length === 0, errors[0] || "无");

/* 抓到「请求失败」时，单独把返回值丢给 renderPlan，把真正的堆栈打出来 */
if (fails.length) {
  const diag = await evaluate(`(async () => {
    const body = {
      frm: "北京", to: "上海", date: "2026-06-17", time: "08:00",
      mode: ${JSON.stringify(MODE)}, max_transfers: 2, buffer_min: 30,
      objective: "fast", slack_hours: 2, tour_stay: "halfday",
      with_flights: false, max_days: null, tour_city_count: null,
      rest_days: 0, top: 10, links: true, guide: true,
    };
    const res = await fetch("/api/plan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => r.json());
    const out = { ok: res.ok, keys: Object.keys(res), journeys: Array.isArray(res.journeys) ? res.journeys.length : typeof res.journeys };
    try {
      window.renderPlan(res);
      out.render = "ok";
    } catch (e) {
      out.render = "throw";
      out.message = String(e && e.message);
      out.stack = String(e && e.stack).split("\\n").slice(0, 4).join(" | ");
    }
    try { window.suggestRoutes(["北京", "上海"]); out.suggest = "called"; }
    catch (e) { out.suggest = "throw: " + e.message; }
    const first = res.journeys && res.journeys[0];
    out.firstKeys = first ? Object.keys(first) : null;
    out.firstLegs = first && first.legs ? first.legs.length : null;
    out.firstLegKeys = first && first.legs && first.legs[0] ? Object.keys(first.legs[0]) : null;
    return out;
  })()`);
  console.log("诊断:", JSON.stringify(diag, null, 1));
}

/* 截图前滚到「沿途城市资料」，方便一眼看这段联动长什么样 */
await evaluate(`(() => {
  const box = document.querySelector("#p-results .city-guides") ||
              document.getElementById("p-results");
  if (box) box.scrollIntoView({ block: "start" });
  return true;
})()`);
await sleep(400);
const shot = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
const file = path.join(os.tmpdir(), `check-plan-${MODE}-${W}x${H}.png`);
fs.writeFileSync(file, Buffer.from(shot.result.data, "base64"));
console.log("截图:", file);
console.log(fails.length ? `\n✗ ${fails.length} 项异常：${fails.join("、")}` : "\n✓ 全部正常");
cdp.close(); child.kill(); await sleep(300);
try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
process.exit(fails.length ? 1 : 0);
