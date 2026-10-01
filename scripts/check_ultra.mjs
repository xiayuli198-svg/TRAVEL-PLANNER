/* 真浏览器探针：环线页签里的「环线 Ultra 版」按钮，从点下去到逐日安排能不能走通。
 *
 *   node scripts/check_ultra.mjs [宽] [高]
 *
 * 单独拿一个探针盯它，是因为这一块是「不另开界面」的设计：
 * 按钮 → 面板出现 → 普通环线结果让位 → 卡片列表 → 前两条自动展开逐日 → 筛选 → 城内动线。
 * 任何一步断了，用户看到的就是「点了没反应」，后端测试完全测不到。
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
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dsh-check-ultra-"));

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
await sleep(2400);

console.log("环线 Ultra 版冒烟测试");

/* 1) 切到环线页签，确认按钮在、面板本来是收着的 */
const before = await evaluate(`(() => {
  document.querySelector('button[data-tab="loop"]').click();
  const btn = document.getElementById("l-ultra");
  const panel = document.getElementById("ultra-panel");
  return {
    hasBtn: !!btn,
    label: btn ? btn.textContent.trim() : "",
    panelHidden: panel ? panel.hidden : null,
    wired: document.documentElement.dataset.ultraWired === "1",
  };
})()`);
ok("环线页签里有「环线 Ultra 版」按钮", before.hasBtn && /Ultra/.test(before.label), before.label);
ok("面板默认是收起的", before.panelHidden === true, String(before.panelHidden));
ok("按钮事件已接上（不靠内联 onclick）", before.wired);

/* 2) 点按钮：面板出现、普通环线结果让位 */
await evaluate(`document.getElementById("l-ultra").click()`);
let cards = 0;
for (let i = 0; i < 30; i++) {
  await sleep(400);
  cards = await evaluate(`document.querySelectorAll("#ultra-list .ultra-card").length`);
  if (cards > 0) break;
}
const opened = await evaluate(`(() => {
  const panel = document.getElementById("ultra-panel");
  const box = document.getElementById("ultra-list");
  const items = [...box.querySelectorAll(".ultra-card")];
  const first = items[0];
  const expanded = items.filter((el) => !el.querySelector(".rt-body").hidden);
  return {
    panelHidden: panel.hidden,
    btnLabel: document.getElementById("l-ultra").textContent.trim(),
    resultsHidden: document.getElementById("l-results").hidden,
    count: (document.getElementById("ultra-count") || {}).textContent || "",
    cards: items.length,
    origins: [...new Set(items.map((el) => (el.querySelector(".rt-kind.loop") || {}).textContent || ""))],
    scaleLabels: [...new Set(items.map((el) => {
      const kinds = [...el.querySelectorAll(".rt-head .rt-kind")];
      const t = kinds.find((k) => /环线$/.test(k.textContent.trim()));
      return t ? t.textContent.trim() : "";
    }))],
    heads: items.slice(0, 3).map((el) => (el.querySelector(".rt-head b") || {}).textContent || ""),
    firstName: first ? (first.querySelector(".rt-head b") || {}).textContent : "",
    firstChips: first ? first.querySelectorAll(".ultra-chips .cg-chip").length : 0,
    firstMeta: first ? (first.querySelector(".ultra-meta") || {}).textContent.replace(/\\s+/g, " ").trim() : "",
    firstSummary: first ? (first.querySelector(".rt-summary") || {}).textContent.slice(0, 60) : "",
    firstSource: first ? (first.querySelector(".rt-foot") || {}).textContent.replace(/\\s+/g, " ").trim() : "",
    expanded: expanded.length,
    dayRows: first ? first.querySelectorAll(".ultra-days .trip-day").length : 0,
    dayTitles: first ? [...first.querySelectorAll(".ultra-days .trip-day b")].map((b) => b.textContent).slice(0, 4) : [],
    dayText: first ? (first.querySelector(".ultra-days") || {}).textContent.replace(/\\s+/g, " ").trim().slice(0, 120) : "",
    notes: first ? first.querySelectorAll(".ultra-notes h5").length : 0,
    warn: first ? !!first.querySelector(".rt-warn") : false,
    flowBtn: first ? !!first.querySelector(".ud-flow") : false,
    flowBound: first ? (first.querySelector(".ud-flow") || {}).dataset?.bound === "1" : false,
    originOptions: [...document.querySelectorAll("#ultra-origin option")].map((o) => o.textContent),
  };
})()`);
console.log("面板:", JSON.stringify({ ...opened, heads: opened.heads }, null, 1));

ok("点一下按钮面板就展开", opened.panelHidden === false);
ok("按钮改成「收起 Ultra 版」", /收起/.test(opened.btnLabel), opened.btnLabel);
ok("普通环线结果让位（不会两套混在一起）", opened.resultsHidden === true);
ok("列出了环线卡片", opened.cards >= 20, `${opened.cards} 条`);
ok("计数栏写了北京/河北与口径", /北京\s*\d+/.test(opened.count) && /河北\s*\d+/.test(opened.count) && /口径/.test(opened.count),
  opened.count.slice(0, 120));
ok("出发地下拉列出了各地（含河北城市）", opened.originOptions.length >= 8 &&
  opened.originOptions.some((t) => /河北/.test(t)) && opened.originOptions.some((t) => /北京/.test(t)),
  opened.originOptions.slice(0, 5).join(" / "));
ok("卡片上标了出发地与天数", opened.origins.length >= 2 && opened.origins.every((t) => /出发$/.test(t)),
  opened.origins.join("、"));
ok("卡片标了大小环线", opened.scaleLabels.length >= 1 && opened.scaleLabels.every((t) => /环线$/.test(t)),
  opened.scaleLabels.join("、"));
ok("卡片给预算区间与交通方式", /预算\s*¥\d+-\d+/.test(opened.firstMeta) && opened.firstMeta.length > 12, opened.firstMeta.slice(0, 110));
ok("卡片给可信度徽标与来源", /可信度/.test(opened.firstMeta) && /来源/.test(opened.firstSource), opened.firstSource.slice(0, 80));
ok("卡片给看点标签与摘要", opened.firstChips >= 2 && opened.firstSummary.length > 10, `${opened.firstChips} 个标签`);
ok("前两条默认展开逐日安排", opened.expanded >= 2, `${opened.expanded} 条展开`);
ok("展开后是一天一天的安排（不只是标题）", opened.dayRows >= 3 && opened.dayText.length > 60,
  `${opened.dayRows} 天：${opened.dayTitles.join(" ")}`);
ok("给了怎么省 / 怎么舒服点 / 适合谁", opened.notes >= 2, `${opened.notes} 组`);
ok("低可信度或注意项有单独提示位", typeof opened.warn === "boolean");

/* 3) 筛选：按出发地 + 天数 + 预算 */
const filtered = await evaluate(`(async () => {
  const set = (id, v) => {
    const el = document.getElementById(id);
    el.value = v;
    el.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const wait = () => new Promise((r) => setTimeout(r, 1200));
  set("ultra-origin", "北京");
  await wait();
  const beijing = document.querySelectorAll("#ultra-list .ultra-card").length;
  const beijingNames = [...document.querySelectorAll("#ultra-list .ultra-card .rt-kind.loop")]
    .map((el) => el.textContent.trim());
  set("ultra-origin", "石家庄");
  await wait();
  const shijiazhuang = document.querySelectorAll("#ultra-list .ultra-card").length;
  set("ultra-origin", "");
  await wait();
  set("ultra-days", "5");
  await wait();
  const daysOnly = document.querySelectorAll("#ultra-list .ultra-card").length;
  set("ultra-budget", "800");
  await wait();
  const tight = [...document.querySelectorAll("#ultra-list .ultra-card")];
  // 连着改筛选（不等待）也得收敛到最后一个条件，不能被慢的旧请求盖回去
  set("ultra-days", "4");
  set("ultra-budget", "600");
  await wait();
  await wait();
  const raced = [...document.querySelectorAll("#ultra-list .ultra-card")];
  // 只看大环线：跨省 ≥8 天 ≥6 城，卡片必须都带「大环线」标签
  set("ultra-days", "0");
  set("ultra-budget", "0");
  set("ultra-scale", "large");
  await wait();
  const largeCards = [...document.querySelectorAll("#ultra-list .ultra-card")];
  const largeDays = largeCards.map((el) => Number((el.querySelectorAll(".rt-kind")[2] || {}).textContent.replace(/\\D/g, "")) || 0);
  const largeCityCount = largeCards.map((el) => (el.querySelector(".rt-inline") || {}).textContent.split("→").length);
  const largeTags = largeCards.map((el) => {
    const t = [...el.querySelectorAll(".rt-head .rt-kind")].find((k) => /环线$/.test(k.textContent.trim()));
    return t ? t.textContent.trim() : "";
  });
  const largeCount = (document.getElementById("ultra-count") || {}).textContent || "";
  // 复位，别影响后面的步骤
  set("ultra-scale", "");
  await wait();
  return {
    beijing, shijiazhuang, daysOnly,
    beijingAllBeijing: beijingNames.every((t) => t === "北京出发"),
    tight: tight.length,
    tightDays: tight.map((el) => Number((el.querySelectorAll(".rt-kind")[2] || {}).textContent.replace(/\\D/g, "")) || 0),
    tightBudget: tight.map((el) => {
      const m = (el.querySelector(".ultra-meta") || {}).textContent.match(/¥(\\d+)-(\\d+)/);
      return m ? Number(m[1]) : 0;
    }),
    raced: raced.length,
    racedDays: raced.map((el) => Number((el.querySelectorAll(".rt-kind")[2] || {}).textContent.replace(/\\D/g, "")) || 0),
    racedBudget: raced.map((el) => {
      const m = (el.querySelector(".ultra-meta") || {}).textContent.match(/¥(\\d+)-(\\d+)/);
      return m ? Number(m[1]) : 0;
    }),
    large: largeCards.length, largeDays, largeCityCount, largeTags, largeCount,
  };
})()`);
console.log("筛选:", JSON.stringify(filtered));
ok("按出发地筛选只留北京出发", filtered.beijing >= 5 && filtered.beijingAllBeijing, `北京 ${filtered.beijing} 条`);
ok("河北城市也能单独筛（石家庄）", filtered.shijiazhuang >= 1, `${filtered.shijiazhuang} 条`);
ok("只改天数就少了（天数上限生效）", filtered.daysOnly >= 1 && filtered.daysOnly <= filtered.beijing,
  `≤5 天 ${filtered.daysOnly} 条`);
ok("天数 + 预算上限同时生效", filtered.tight >= 1 && filtered.tight <= filtered.daysOnly &&
  filtered.tightDays.every((d) => d > 0 && d <= 5) && filtered.tightBudget.every((b) => b <= 800),
  `${filtered.tight} 条，天数 ${filtered.tightDays.join("/")}，低价 ${filtered.tightBudget.join("/")}`);
ok("连着改筛选不会被慢的旧请求盖回去", filtered.raced >= 1 &&
  filtered.racedDays.every((d) => d > 0 && d <= 4) && filtered.racedBudget.every((b) => b <= 600),
  `${filtered.raced} 条，天数 ${filtered.racedDays.join("/")}，低价 ${filtered.racedBudget.join("/")}`);
ok("「只看大环线」筛得出跨省长线（≥8 天、≥6 城、都带大环线标签）",
  filtered.large >= 1 && filtered.largeTags.every((t) => t === "大环线") &&
  filtered.largeDays.every((d) => d >= 8) && filtered.largeCityCount.every((n) => n >= 6),
  `${filtered.large} 条，天数 ${filtered.largeDays.join("/")}，城市数 ${filtered.largeCityCount.join("/")}`);
ok("计数栏写明含多少条大环线", /大环线\s*\d+\s*条/.test(filtered.largeCount) || filtered.large >= 1,
  filtered.largeCount.slice(0, 90));

/* 4) 详情里的「看城内动线」要真的跳过去（曾经类名没绑事件，点了没反应） */
const flow = await evaluate(`(async () => {
  document.getElementById("ultra-days").value = "0";
  document.getElementById("ultra-budget").value = "0";
  document.getElementById("ultra-go").click();
  let waited0 = 0;
  while (waited0 < 8000 && !document.querySelector("#ultra-list .ud-flow")) {
    await new Promise((r) => setTimeout(r, 400));
    waited0 += 400;
  }
  const btn = document.querySelector("#ultra-list .ud-flow");
  if (!btn) return { found: false };
  const city = btn.dataset.city;
  btn.click();
  // 首次打开那座城可能要现抓 POI 池，等它把状态栏写出来
  let waited = 0;
  while (waited < 16000) {
    await new Promise((r) => setTimeout(r, 500));
    waited += 500;
    const status = (document.getElementById("guide-status") || {}).textContent || "";
    const rows = document.querySelectorAll("#guide-days .guide-day").length;
    if (rows > 0 || /失败|取不到/.test(status)) break;
  }
  const active = document.querySelector(".tab-nav button.active, button[data-tab].active");
  return {
    found: true, city, waitedMs: waited,
    activeTab: active ? active.dataset.tab : "",
    guideCity: (document.getElementById("guide-city") || {}).value || "",
    guideRows: document.querySelectorAll("#guide-days .guide-day").length,
    guideStatus: ((document.getElementById("guide-status") || {}).textContent || "").trim().slice(0, 90),
    guideSummary: ((document.getElementById("guide-summary") || {}).textContent || "").trim().slice(0, 120),
  };
})()`);
console.log("城内动线:", JSON.stringify(flow));
ok("Ultra 详情里有「看城内动线」按钮", flow.found === true, flow.city || "");
ok("点它会切到数据台并把这天/出发城市填好", flow.found && flow.activeTab === "data" && flow.guideCity === flow.city,
  `页签 ${flow.activeTab}，城市 ${flow.guideCity}`);
ok("那座城的动线真的铺出来了", flow.found && flow.guideRows >= 1 && !/失败|取不到/.test(flow.guideStatus || ""),
  `${flow.guideRows} 天 · ${flow.guideSummary || flow.guideStatus}`);

/* 5) 再点一次按钮应该收起 */
const closed = await evaluate(`(() => {
  const tab = document.querySelector('button[data-tab="loop"]');
  tab.click();
  document.getElementById("l-ultra").click();
  return {
    panelHidden: document.getElementById("ultra-panel").hidden,
    resultsHidden: document.getElementById("l-results").hidden,
    label: document.getElementById("l-ultra").textContent.trim(),
  };
})()`);
ok("再点一次收起，普通环线结果回来", closed.panelHidden === true && closed.resultsHidden === false &&
  /环线 Ultra 版/.test(closed.label), closed.label);

/* 截图：面板 + 前两条展开的样子 */
await evaluate(`(() => {
  document.querySelector('button[data-tab="loop"]').click();
  document.getElementById("l-ultra").click();
  const panel = document.getElementById("ultra-panel");
  if (panel) panel.scrollIntoView({ block: "start" });
  return true;
})()`);
await sleep(2200);
const shot = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
const file = path.join(os.tmpdir(), `check-ultra-${W}x${H}.png`);
fs.writeFileSync(file, Buffer.from(shot.result.data, "base64"));
console.log("截图:", file);
if (errors.length) { console.log("== 页面报错 =="); errors.slice(0, 6).forEach((e) => console.log("  " + e)); }
if (netFailures.length) { console.log("== 请求失败 =="); [...new Set(netFailures)].slice(0, 6).forEach((e) => console.log("  " + e)); }
ok("页面没有异常", errors.length === 0, errors[0] || "无");
ok("没有请求失败", netFailures.length === 0, [...new Set(netFailures)][0] || "无");

console.log(fails.length ? `\n✗ ${fails.length} 项异常：${fails.join("、")}` : "\n✓ 全部正常");
cdp.close(); child.kill(); await sleep(300);
try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
process.exit(fails.length ? 1 : 0);
