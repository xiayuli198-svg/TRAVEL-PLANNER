/* 真浏览器探针：数据台页签（自动检查 + 一键执行）是否真的渲染出来。
 *
 *   node scripts/dev_probe_data.mjs [宽] [高]
 *
 * 只读检查 + 点一次「检查缺口」（只读接口），不写库、不爬取。
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
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dsh-probe-data-"));

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
        // 60s：本探针有几段「在页面里轮询等异步结果」的等待（最长一段是城内动线 24s），
        // 原来这里是 20s，比自家的等待还短 —— 那次查询稍慢一点，挂掉的就是探针自己
        // （报 "CDP 超时 Runtime.evaluate"，看着像页面坏了，其实是超时预算没算好）。
        setTimeout(() => { if (waiters.has(myId)) { waiters.delete(myId); rej(new Error("CDP 超时 " + method)); } }, 60000);
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
/* 收集页面异常与控制台报错：数据台渲染失败时必须看到原因 */
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
            (d.exception && d.exception.description ? d.exception.description.split("\n").slice(0, 3).join(" | ") : ""));
        } else if (msg.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(msg.params.type)) {
          errors.push("console." + msg.params.type + ": " +
            msg.params.args.map((a) => a.value ?? a.description ?? a.type).join(" ").slice(0, 300));
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
await cdp.send("Page.enable");
await cdp.send("Runtime.enable");
// 无头页面默认 visibilityState="hidden"，Chrome 不提交滚动（scrollIntoView 无效）——
// 这个开关等价于 DevTools 的 "Emulate a focused page"，让滚动与计时器正常。
try { await cdp.send("Emulation.setFocusEmulationEnabled", { enabled: true }); } catch (e) {}

await cdp.send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });
await cdp.send("Page.navigate", { url: URL_PAGE });
await sleep(2000);
await evaluate(`document.querySelector('button[data-tab="data"]').click()`);
/* 等渲染完成：轮询到底表里有行（别用固定 sleep 猜） */
{
  let tries = 0, rows = 0;
  while (tries < 150 && rows === 0) {        // 45s：overview 要给 10s+ 才出表
    await sleep(300);
    rows = await evaluate(`document.querySelectorAll("#data-table tbody tr").length`);
    tries++;
  }
  console.log(`等渲染：${tries} 次轮询后表格 ${rows} 行`);
}
console.log("点击后:", await evaluate(`(() => {
  const btn = document.querySelector('button[data-tab="data"]');
  const tab = document.getElementById("tab-data");
  return { hasBtn: !!btn, btnActive: btn && btn.classList.contains("active"),
           tabActive: tab && tab.classList.contains("active"),
           tabDisplay: tab ? getComputedStyle(tab).display : null,
           loadDataType: typeof window.loadData,
           appScripts: [...document.querySelectorAll("script[src]")].map(s => s.getAttribute("src")) };
})()`));

const fails = [];
const ok = (name, cond, extra = "") => {
  console.log(`  ${cond ? "✓" : "✗"} ${name}${extra ? " — " + extra : ""}`);
  if (!cond) fails.push(name);
};

/* 直接调一次 loadData，把真实错误抓出来 */
console.log("手动调用 loadData:", await evaluate(`(async () => {
  try { await window.loadData(); }
  catch (e) { return "loadData 抛错: " + e.message; }
  const T = (id) => (document.getElementById(id) || {}).textContent || "";
  return { summary: T("data-summary").slice(0, 80), rows: document.querySelectorAll("#data-table tbody tr").length,
           health: document.querySelectorAll("#health-list .health-item").length,
           status: T("c-status") };
})()`));
const view = await evaluate(`(() => {
  const T = (id) => (document.getElementById(id) || {}).textContent || "";
  const rows = [...document.querySelectorAll("#data-table tbody tr")];
  const badges = rows.map((tr) => {
    const b = tr.querySelector(".src-badge");
    return b ? b.textContent.trim() : "(无)";
  });
  const classCount = {};
  badges.forEach((b) => { classCount[b] = (classCount[b] || 0) + 1; });
  const tones = rows.slice(0, 40).map((tr) => {
    const b = tr.querySelector(".src-badge");
    return b ? [...b.classList].filter((c) => c !== "src-badge").join(",") : "";
  });
  return {
    summary: T("data-summary").slice(0, 200),
    hint: T("flight-data-hint").slice(0, 200),
    healthItems: [...document.querySelectorAll("#health-list .health-item")].map((el) => ({
      cls: [...el.classList].filter((c) => c !== "health-item").join(","),
      text: (el.querySelector(".hi-text") || {}).textContent || "",
      fix: (el.querySelector(".hi-fix") || {}).textContent || "",
    })),
    rowCount: rows.length,
    rowsWithBadge: rows.filter((tr) => tr.querySelector(".src-badge")).length,
    badges: classCount,
    distinctTones: [...new Set(tones)],
    hasCrawlButton: rows.some((tr) => [...tr.querySelectorAll("button")].some((b) => b.textContent === "真爬")),
    selectOptions: document.querySelectorAll("#c-from option").length,
    planHidden: document.getElementById("m-plan").hidden,
    progressHidden: document.getElementById("crawl-progress").hidden,
    logHidden: document.getElementById("crawl-log").hidden,
    startValue: (document.getElementById("m-start") || {}).value || "",
  };
})()`);
console.log(`数据台真机探测 @ ${W}x${H}`);
console.log(JSON.stringify(view, null, 1));
if (errors.length) {
  console.log("== 页面报错 ==");
  errors.slice(0, 8).forEach((e) => console.log("  " + e));
}
/* 直接问一次后端，区分「接口坏」与「前端没渲染」 */
const api = await evaluate(`fetch("/api/maintain/overview").then(r => r.json()).then(d => ({
  ok: d.ok, dates: (d.dates || []).length, base: d.base && d.base.date,
  items: (d.health && d.health.items || []).length })).catch(e => ({ error: String(e) }))`);
console.log("接口自测:", JSON.stringify(api));
const apiRunning = await evaluate(`fetch("/api/crawl/status").then(r => r.json())
  .then(d => !!d.running).catch(() => false)`);
console.log("有爬取任务在跑:", apiRunning);

ok("体检清单渲染出来了", view.healthItems.length >= 3, `${view.healthItems.length} 条`);
ok("拷贝占比那条被标成警告", view.healthItems.some((x) => x.cls.includes("tone-warn") && /拷贝/.test(x.text)),
  view.healthItems.filter((x) => /拷贝/.test(x.text)).map((x) => x.cls).join());
ok("日期表按来源上色（至少两种档位）", view.distinctTones.length >= 2, view.distinctTones.join(" | "));
ok("日期表行数与后端一致", view.rowCount === api.dates && view.rowCount > 0,
  `页面 ${view.rowCount} 行 vs 接口 ${api.dates} 天`);
ok("每行都有来源徽章", view.rowsWithBadge === view.rowCount, `${view.rowsWithBadge}/${view.rowCount}`);
ok("拷贝类（拷贝/定向/车次少）都有徽章", (view.badges["拷贝"] || 0) + (view.badges["定向"] || 0)
  + (view.badges["车次少"] || 0) > 0, JSON.stringify(view.badges));
ok("每行都有「真爬」按钮", view.hasCrawlButton);
ok("出发地下拉有日期选项", view.selectOptions === view.rowCount, `${view.selectOptions} 项`);
ok("计划/进度/日志默认收起（没有任务在跑时）",
  apiRunning
    ? view.planHidden            // 有任务在跑：进度条与日志显示是对的，只要求计划区仍收起
    : (view.planHidden && view.progressHidden && view.logHidden),
  apiRunning ? "当前有爬取任务在跑：进度条/日志显示符合预期" : "无任务在跑，三者都收起");
ok("起始日期预填了基准日", /^\d{4}-\d{2}-\d{2}$/.test(view.startValue), view.startValue);

/* 点「检查缺口」：只读接口，应当出现计划区 */
await evaluate(`(() => {
  const s = document.getElementById("m-start"), e = document.getElementById("m-end");
  s.value = "2026-09-30"; e.value = "2026-10-02";
  document.getElementById("m-plan-fill").click();
  return true;
})()`);
{
  let tries = 0, rows = 0;
  while (tries < 150 && rows === 0) {        // 45s：overview 要给 10s+ 才出表
    await sleep(300);
    rows = await evaluate(`document.querySelectorAll("#m-plan .plan-row").length`);
    tries++;
  }
  console.log(`等计划：${tries} 次轮询后 ${rows} 步`);
}
await sleep(300);
const plan = await evaluate(`(() => {
  const box = document.getElementById("m-plan");
  return { hidden: box.hidden, head: (box.querySelector(".plan-head") || {}).textContent || "",
           rows: [...box.querySelectorAll(".plan-row")].map((r) => r.textContent.trim().slice(0, 60)),
           actions: [...box.querySelectorAll(".plan-actions button")].map((b) => b.textContent) };
})()`);
console.log("计划区:", JSON.stringify(plan, null, 1));
ok("「检查缺口」能生成计划", !plan.hidden && plan.rows.length > 0, `${plan.rows.length} 步`);
ok("计划里给出可执行按钮（不自动写库）", plan.actions.some((t) => /执行计划/.test(t)), plan.actions.join());
ok("计划步骤写明动作与理由", plan.rows.some((r) => /基准日/.test(r) && /(拷贝|覆盖)/.test(r)),
  plan.rows[0] || "");

/* 省钱/舒适/巧思路线库（分页 + 折叠 + 单向/环线） */
{
  let tries = 0, cards = 0;
  while (tries < 150 && cards === 0) {       // 45s
    await sleep(300);
    cards = await evaluate(`document.querySelectorAll("#rt-list .rt-card").length`);
    tries++;
  }
  const rt = await evaluate(`(() => {
    const cards = [...document.querySelectorAll("#rt-list .rt-card")];
    const first = cards[0];
    const openOnes = cards.filter((c) => !c.querySelector(".rt-body").hidden).length;
    return {
      count: cards.length,
      total: (document.getElementById("rt-count") || {}).textContent || "",
      openOnes,
      loops: document.querySelectorAll("#rt-list .rt-card.is-loop").length,
      kindTags: [...new Set(cards.map((c) => (c.querySelector(".rt-kind") || {}).textContent))],
      moreLabel: (document.getElementById("rt-more") || {}).textContent || "",
      filters: [...document.querySelectorAll("#rt-filters .rec-chip")].map((b) => b.textContent),
      first: first ? {
        title: (first.querySelector(".rt-head b") || {}).textContent || "",
        inline: (first.querySelector(".rt-inline") || {}).textContent || "",
        scores: [...first.querySelectorAll(".rt-score")].map((s) => s.textContent.trim()),
        segs: first.querySelectorAll(".rt-seg").length,
        tips: first.querySelectorAll(".rt-tips li").length,
        foot: (first.querySelector(".rt-foot") || {}).textContent.trim().slice(0, 60),
      } : null,
      note: (document.querySelector("#rt-list .hint") || {}).textContent || "",
      when: (document.querySelector("#rt-list .rt-when") || {}).value || "",
    };
  })()`);
  console.log("路线库:", JSON.stringify({ ...rt, first: rt.first && rt.first.title }, null, 1));
  ok("路线卡片渲染出来了", rt.count >= 5, `${rt.count} 张（分页首屏）`);
  ok("首屏不会把整库都铺开（分页生效）", rt.count <= 60, `${rt.count} 张`);
  ok("默认只展开前几张，其余折叠", rt.openOnes >= 1 && rt.openOnes <= 5,
    `展开 ${rt.openOnes}/${rt.count}`);
  ok("摘要行显示行内信息与三档评分",
    !!rt.first && rt.first.scores.length === 3 && /→/.test(rt.first.inline) && /元/.test(rt.first.inline),
    `行内「${(rt.first && rt.first.inline || "").trim().slice(0, 40)}」 评分 ${JSON.stringify(rt.first && rt.first.scores)}`);
  ok("有单向/环线标识", rt.kindTags.length >= 1, rt.kindTags.join("/"));
  ok("统计行写明总数与单向/环线条数", /共 \d+ 条/.test(rt.total) && /单向/.test(rt.total),
    rt.total.slice(0, 70));
  ok("「载入更多」按钮存在", /载入更多/.test(rt.moreLabel) || rt.moreLabel === "", rt.moreLabel);
  ok("有「只看环线」筛选", rt.filters.includes("只看环线"), rt.filters.slice(-3).join("/"));
  ok("页面写明「价格为参考、以 12306 为准」", /12306|参考/.test(rt.note), rt.note.slice(0, 50));
  ok("有「只看巧思」开关", rt.filters.includes("只看巧思"));
  ok("路线卡里的出行日有默认值", /^\d{4}-\d{2}-\d{2}$/.test(rt.when),
    `rt-when=「${rt.when}」`);
}

/* 一键导入面板：粘一段 JSON → 校验报告 */
{
  const payload = {
    updated: "2026-09-22", collector: "探针自测",
    routes: [{
      id: "probe-import-one", name: "探针自测·上海到苏州", from_city: "上海", to_city: "苏州",
      category: "自测", tier: "观景", themes: ["自测"], summary: "探针用",
      segments: [{ frm: "上海", to: "苏州", mode: "high_speed", hours: 0.5, price: "约 40 元" }],
      total_hours: 0.5, cost_low: 40, cost_high: 60, clever_tags: ["local_trick"], clever_extra: 2,
      smart_tips: ["自测"], source: "探针", source_url: "https://www.12306.cn/", confidence: "low",
    }],
  };
  await evaluate(`(() => {
    document.getElementById("imp-text").value = ${JSON.stringify(JSON.stringify(payload))};
    document.getElementById("imp-check").click();
    return true;
  })()`);
  let tries = 0, rows = 0;
  while (tries < 150 && rows === 0) {        // 45s：overview 要给 10s+ 才出表
    await sleep(300);
    rows = await evaluate(`document.querySelectorAll("#imp-report .imp-row").length`);
    tries++;
  }
  const imp = await evaluate(`(() => {
    const box = document.getElementById("imp-report");
    return {
      hidden: box.hidden,
      rows: box.querySelectorAll(".imp-row").length,
      okRows: box.querySelectorAll(".imp-row.ok").length,
      badRows: box.querySelectorAll(".imp-row.bad").length,
      head: (box.querySelector(".imp-head") || {}).textContent || "",
      status: (document.getElementById("imp-status") || {}).textContent || "",
    };
  })()`);
  console.log("导入校验:", JSON.stringify(imp));
  ok("导入面板能出校验报告", !imp.hidden && imp.rows >= 1, `${imp.rows} 行`);
  ok("好数据判为通过", imp.okRows >= 1 && imp.badRows === 0, imp.status.slice(0, 40));
  ok("报告里有条数统计", /共 \d+ 条/.test(imp.head), imp.head.slice(0, 50));
}

/* 我的行程：存档列表 + 点开看乘车表 + 排不出的条目留痕 */
{
  let tries = 0, cards = 0;
  while (tries < 150 && cards === 0) {       // 45s
    await sleep(300);
    cards = await evaluate(`document.querySelectorAll("#tp-list .trip-card").length`);
    tries++;
  }
  const tp = await evaluate(`(() => {
    const cards = [...document.querySelectorAll("#tp-list .trip-card")];
    const box = document.getElementById("tp-skips");
    return {
      count: cards.length,
      count_text: (document.getElementById("tp-count") || {}).textContent || "",
      date: (document.getElementById("tp-date") || {}).value || "",
      dateExists: !!document.getElementById("tp-date"),
      dateType: (document.getElementById("tp-date") || {}).type || "",
      wired: document.documentElement.dataset.tpWired || "",
      defaultDate: typeof window.defaultDate === "function" ? window.defaultDate() : "",
      progressHidden: document.getElementById("tp-progress").hidden,
      logsHidden: document.getElementById("tp-log").hidden,
      skipHidden: box.hidden,
      skipRows: box.querySelectorAll(".imp-row").length,
      skipHead: (box.querySelector(".imp-head") || {}).textContent || "",
      skipLabels: [...box.querySelectorAll(".imp-kind")].map((el) => el.textContent),
      groundBadges: document.querySelectorAll("#tp-list .trip-meta .cc-ground").length,
      buttons: ["tp-build", "tp-refresh", "tp-export", "tp-cancel", "tp-more"]
        .filter((id) => document.getElementById(id)),
      first: cards[0] ? {
        id: cards[0].dataset.tripId || "",
        title: (cards[0].querySelector(".trip-head b") || {}).textContent || "",
        meta: (cards[0].querySelector(".trip-meta") || {}).textContent || "",
        stops: (cards[0].querySelector(".trip-stops") || {}).textContent || "",
        bodyHidden: cards[0].querySelector(".trip-body").hidden,
      } : null,
    };
  })()`);
  console.log("我的行程:", JSON.stringify(tp, null, 1));
  ok("存档列表渲染出来了", tp.count >= 1, `${tp.count} 张`);
  ok("统计行写明覆盖路线库多少条", /覆盖 \d+\/\d+ 条/.test(tp.count_text), tp.count_text.slice(0, 80));
  ok("出行日默认填好", /^\d{4}-\d{2}-\d{2}$/.test(tp.date), tp.date);
  ok("批量建档/刷新/导出/取消按钮都在", tp.buttons.length === 5, tp.buttons.join(","));
  ok("进度与日志默认收起", tp.progressHidden && tp.logsHidden);
  ok("存档卡摘要有一行停靠城市", !!tp.first && /→/.test(tp.first.stops),
    (tp.first && tp.first.stops || "").slice(0, 50));
  ok("排不出的条目单独列出来（不静默丢掉）",
    !tp.skipsHidden && tp.skipRows >= 1 && /排不出/.test(tp.skipHead),
    `${tp.skipRows} 条`);
  ok("排不出的原因分了类（不是一坨）",
    /基地式周边游|同城条目|缺这一天/.test(tp.skipHead) && tp.skipLabels.length >= 1,
    tp.skipHead.replace(/\s+/g, " ").slice(0, 70));
  ok("存档统计里写明含地面接驳的份数", /地面接驳/.test(tp.count_text),
    tp.count_text.slice(-70));

  /* 优先点开一份「含地面接驳」的存档：地面段要和车次用不同颜色标出来 */
  await evaluate(`(() => {
    const cards = [...document.querySelectorAll("#tp-list .trip-card")];
    const target = cards.find((c) => c.querySelector(".trip-meta .cc-ground")) || cards[0];
    if (target) target.querySelector(".trip-head").click();
    return true;
  })()`);
  {
    let tries = 0, days = 0;
    while (tries < 150 && days === 0) {        // 45s
      await sleep(300);
      days = await evaluate(`document.querySelectorAll("#tp-list .trip-card .trip-day").length`);
      tries++;
    }
  }
  const opened = await evaluate(`(() => {
    const card = [...document.querySelectorAll("#tp-list .trip-card")]
      .find((c) => !c.querySelector(".trip-body").hidden) ||
      document.querySelector("#tp-list .trip-card");
    const body = card.querySelector(".trip-body");
    return {
      hidden: body.hidden,
      days: card.querySelectorAll(".trip-day").length,
      head: (body.querySelector(".cc-head") || {}).textContent || "",
      acts: [...body.querySelectorAll(".trip-actions button")].map((b) => b.textContent),
      warning: (body.querySelector(".rt-warn") || {}).textContent || "",
      note: (body.querySelector(".rt-foot") || {}).textContent || "",
      ground: [...card.querySelectorAll(".trip-rides .cc-ground")].map((el) => el.textContent.trim()),
    };
  })()`);
  console.log("存档详情:", JSON.stringify(opened, null, 1));
  ok("点开能看到逐日乘车表", !opened.hidden && opened.days >= 1, `${opened.days} 天`);
  ok("详情里给出改名/删除", opened.acts.some((t) => /改名/.test(t)) && opened.acts.some((t) => /删掉/.test(t)),
    opened.acts.join("/"));
  ok("详情照原样带出数据来源说明", /12306|参考|时刻表/.test(opened.note), opened.note.slice(0, 60));
  ok("地面接驳段在乘车表里单独标出（含估算字样）",
    opened.ground.length === 0 || opened.ground.some((t) => /地面/.test(t)),
    opened.ground.slice(0, 3).join(" | ").slice(0, 90));
}

/* 城内点位（只读预览）：名单覆盖率 + 命中/未命中都看得见 */
{
  let tries = 0, rows = 0;
  while (tries < 120 && rows === 0) {        // 30s（点位列表要查库）
    await sleep(250);
    rows = await evaluate(`document.querySelectorAll("#poi-list .poi-row").length`);
    tries++;
  }
  const poiView = await evaluate(`(() => {
    const rows = [...document.querySelectorAll("#poi-list .poi-row")];
    return {
      visible: !!document.getElementById("poi-list"),
      wired: document.documentElement.dataset.poiWired || "",
      count: rows.length,
      count_text: (document.getElementById("poi-count") || {}).textContent || "",
      hits: rows.filter((r) => !r.classList.contains("poi-miss")).length,
      misses: rows.filter((r) => r.classList.contains("poi-miss")).length,
      sample: (rows[0] ? rows[0].textContent.replace(/\\s+/g, " ").trim().slice(0, 120) : ""),
      buttons: ["poi-run", "poi-go", "poi-cancel", "poi-export", "poi-more"]
        .filter((id) => document.getElementById(id)),
      progressHidden: document.getElementById("poi-progress").hidden,
      logHidden: document.getElementById("poi-log").hidden,
      note: (() => {
        const card = document.getElementById("poi-list");
        const hint = card && card.closest(".card") ? card.closest(".card").querySelector(".hint") : null;
        return hint ? hint.textContent : "";
      })(),
    };
  })()`);
  console.log("城内点位:", JSON.stringify(poiView, null, 1));
  ok("城内点位预览渲染出来了", poiView.visible && poiView.count >= 1, `${poiView.count} 行`);
  ok("统计行写明名单覆盖率", /名单 \d+ 个看点/.test(poiView.count_text) && /命中/.test(poiView.count_text),
    poiView.count_text.slice(0, 90));
  ok("补坐标/取消/导出按钮都在", poiView.buttons.length === 5, poiView.buttons.join(","));
  ok("进度与日志默认收起", poiView.progressHidden && poiView.logHidden);
  ok("命中行写清返回的名字与类型", poiView.hits === 0 || /高德返回：/.test(poiView.sample),
    poiView.sample.slice(0, 80));
  ok("页面写明只做预览、不做导航", /不做导航|只读预览/.test(poiView.note));
}

/* 城市停留动线：散文 → 有序点位 → 市内腿 → 示意图 */
{
  const info = await evaluate(`(() => {
    if (!document.getElementById("guide-city")) return { missing: true };
    document.getElementById("guide-city").value = "哈尔滨";
    document.getElementById("guide-go").click();
    return { wired: document.documentElement.dataset.guideWired || "" };
  })()`);
  if (info.missing) {
    ok("动线卡片存在", false, "页面上没有 #guide-city");
  } else {
    let tries = 0, cards = 0;
    while (tries < 110 && cards === 0) {     // 44s：首次打开要现抓 POI 池
      await sleep(400);
      cards = await evaluate(`document.querySelectorAll("#guide-days .guide-day").length`);
      tries++;
    }
    const dock = await evaluate(`(() => {
      const cards = [...document.querySelectorAll("#guide-days .guide-day")];
      return {
        wired: document.documentElement.dataset.guideWired || "",
        count: cards.length,
        summary: (document.getElementById("guide-summary") || {}).textContent || "",
        stopChips: cards.reduce((n, c) => n + c.querySelectorAll(".guide-stop").length, 0),
        drawable: cards.filter((c) => !c.querySelector(".gd-legs").disabled).length,
      };
    })()`);
    console.log("停留动线:", JSON.stringify(dock, null, 1));
    ok("动线卡片渲染出来了", dock.count >= 1, `${dock.count} 天`);
    ok("统计行写明对上多少点位", /对上 \d+ 个带坐标的点位/.test(dock.summary),
      dock.summary.slice(0, 90));
    ok("点位以编号小标签列出来", dock.stopChips >= 2, `${dock.stopChips} 个点位`);
    ok("能画动线的天数被标出来", dock.drawable >= 1, `${dock.drawable} 天可画`);

    /* 点开第一张卡的散文 + 算市内腿（会真查一次高德，然后落缓存） */
    await evaluate(`(() => {
      const card = document.querySelector("#guide-days .guide-day");
      card.querySelector(".rt-head").click();
      card.querySelector(".gd-legs").click();
      return true;
    })()`);
    let waits = 0, segs = 0;
    while (waits < 90 && segs === 0) {         // 45s（高德查市内腿）
      await sleep(500);
      segs = await evaluate(`document.querySelectorAll("#guide-days .gd-box .cc-seg").length`);
      waits++;
    }
    const dayView = await evaluate(`(() => {
      const box = document.querySelector("#guide-days .gd-box");
      const segs = [...box.querySelectorAll(".cc-seg")];
      return {
        text: (box.querySelector(".guide-detail") || {}).textContent ||
              (document.querySelector("#guide-days .guide-detail") || {}).textContent || "",
        segments: segs.length,
        withTime: segs.filter((s) => /分钟/.test(s.textContent)).length,
        svg: !!box.querySelector("svg.guide-sketch"),
        legend: (box.querySelector(".guide-legend") || {}).textContent || "",
        note: (box.querySelector(".rt-foot") || {}).textContent || "",
      };
    })()`);
    console.log("动线详情:", JSON.stringify(dayView, null, 1));
    ok("点开能看到那天的散文", dayView.text.length > 20, dayView.text.slice(0, 40));
    ok("示意图画出来了（SVG）", dayView.svg);
    ok("每段都给出走法/用时", dayView.segments >= 1 && dayView.withTime >= 1,
      `${dayView.withTime}/${dayView.segments} 段有用时`);
    ok("图下标明是示意、不是导航", /非导航|示意图/.test(dayView.legend), dayView.legend);
    ok("腿的口径写明不是时刻表", /不是时刻表|实时/.test(dayView.note), dayView.note.slice(0, 60));
  }
}

/* 「直达」跳转条：数据台九张卡，滑到「城内点位 / 城市动线」要很久 —— 必须一键能到 */
{
  const bar = await evaluate(`(() => {
    const el = document.getElementById("data-jump");
    const btns = el ? [...el.querySelectorAll("button.dj")] : [];
    const targets = btns.map((b) => b.dataset.jump);
    return {
      exists: !!el,
      wired: document.documentElement.dataset.jumpWired || "",
      count: btns.length,
      labels: btns.map((b) => b.textContent.trim()),
      missingTargets: targets.filter((t) => !document.getElementById(t)),
      sticky: el ? getComputedStyle(el).position : "",
      hasQuick: !!document.getElementById("jump-city-go"),
      hasTop: !!document.getElementById("jump-top"),
    };
  })()`);
  console.log("跳转条:", JSON.stringify(bar, null, 1));
  ok("数据台顶部有「直达」跳转条", bar.exists && bar.count >= 6, `${bar.count} 个入口：${bar.labels.join("/")}`);
  ok("跳转条是吸顶的（滚到下面也点得到）", bar.sticky === "sticky", bar.sticky);
  ok("每个入口都指向真实存在的卡片", bar.missingTargets.length === 0, bar.missingTargets.join("、") || "全部命中");
  ok("有城市快捷查询与回顶部", bar.hasQuick && bar.hasTop);

  /* 真的点一下「城市动线」：要滚过去、要闪一下、城市框要能被填上 */
  const jumped = await evaluate(`(async () => {
    const card = document.getElementById("data-guide");
    const before = window.scrollY;
    const scroller = document.scrollingElement;
    document.querySelector('#data-jump button[data-jump="data-guide"]').click();
    // 闪烁类只挂 1.8 秒：**必须点击后立刻查**，等轮询完再查永远是 false（踩过）
    const flashedRightAway = card.classList.contains("jump-flash");
    // 平滑滚动要时间，轮询到「卡片进视口」为止（给 8 秒）
    let waited = 0;
    while (waited < 8000) {
      await new Promise((r) => setTimeout(r, 200));
      waited += 200;
      const b = card.getBoundingClientRect();
      if (b.top > -80 && b.top < window.innerHeight * 0.6) break;
    }
    const box = card.getBoundingClientRect();
    return {
      moved: Math.abs(window.scrollY - before) > 200,
      flashed: flashedRightAway,
      waited,
      before,
      after: window.scrollY,
      scrollerTop: scroller ? scroller.scrollTop : null,
      bodyCls: String(document.body.className),
      bodyOverflow: getComputedStyle(document.body).overflowY,
      visibility: document.visibilityState,
      top: Math.round(box.top),
      inView: box.top > -80 && box.top < window.innerHeight * 0.6,
    };
  })()`);
  console.log("点「城市动线」:", JSON.stringify(jumped));
  ok("点了真的滚到那张卡（不是原地不动）", jumped.moved && jumped.inView, `卡片距顶 ${jumped.top}px`);
  ok("滚过去还会闪一下边框", jumped.flashed);

  /* 城市快捷框：填城市名 → 跳过去并开始查动线 */
  const quick = await evaluate(`(async () => {
    const input = document.getElementById("jump-city");
    input.value = "承德";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    document.getElementById("jump-city-go").click();
    let waited = 0;
    while (waited < 20000) {
      await new Promise((r) => setTimeout(r, 500));
      waited += 500;
      const st = (document.getElementById("guide-status") || {}).textContent || "";
      const rows = document.querySelectorAll("#guide-days .guide-day").length;
      if (rows > 0 || /失败|取不到/.test(st)) break;
    }
    return {
      cityValue: (document.getElementById("guide-city") || {}).value || "",
      rows: document.querySelectorAll("#guide-days .guide-day").length,
      status: ((document.getElementById("guide-status") || {}).textContent || "").slice(0, 70),
      hint: document.querySelectorAll("#jump-city-list option").length,
    };
  })()`);
  console.log("快捷查动线:", JSON.stringify(quick));
  ok("城市快捷框会跳过去并真的开始查动线", quick.cityValue === "承德" && quick.rows >= 1,
    `${quick.rows} 天 · ${quick.status}`);
  ok("城市候选来自后端轻量接口", quick.hint >= 50, `${quick.hint} 个候选`);
}

/* 数据可信度：原来覆盖率散在三处，现在必须收在一张表里，每行能跳 */
{
  let tries = 0, rows = 0;
  while (tries < 60 && rows < 3) {           // 30s：可信度表要等三个异步接口
    await sleep(500);
    rows = await evaluate(`document.querySelectorAll("#cred-grid .cred-row").length`);
    tries++;
  }
  const cred = await evaluate(`(() => {
    const rows = [...document.querySelectorAll("#cred-grid .cred-row")];
    return {
      count: rows.length,
      names: rows.map((r) => (r.querySelector(".cr-name") || {}).textContent || ""),
      vals: rows.map((r) => ((r.querySelector(".cr-val") || {}).textContent || "").replace(/\\s+/g, " ").trim()),
      loading: rows.filter((r) => /读取中/.test(r.textContent)).length,
      gaps: rows.filter((r) => r.classList.contains("is-gap")).length,
      buttons: rows.filter((r) => r.querySelector("button.go")).length,
      title: (document.querySelector("#data-health h3") || {}).textContent || "",
    };
  })()`);
  console.log("数据可信度:", JSON.stringify(cred, null, 1));
  ok("体检卡改成「数据可信度」并把覆盖率收成一张表", /可信度/.test(cred.title) && cred.count >= 5,
    `${cred.count} 行：${cred.names.join("/")}`);
  ok("每行的数字都填上了（不是一直「读取中」）", cred.loading === 0, `${cred.loading} 行还在读取`);
  ok("城内点位写了命中率、路线库写了条数与口径、世界数据写了规模",
    cred.vals.some((v) => /命中/.test(v)) && cred.vals.some((v) => /库内/.test(v)) &&
    cred.vals.some((v) => /条世界之最/.test(v)),
    cred.vals.join(" ｜ ").slice(0, 200));
  ok("几乎每行都带「去看」跳转", cred.buttons >= 4, `${cred.buttons} 个按钮`);

  const credJump = await evaluate(`(async () => {
    const rows = [...document.querySelectorAll("#cred-grid .cred-row")];
    const row = rows.find((r) => /城内点位/.test(r.textContent));
    const btn = row && row.querySelector("button.go");
    if (!btn) return { ok: false };
    btn.click();
    let waited = 0;
    while (waited < 4000) {
      await new Promise((r) => setTimeout(r, 200));
      waited += 200;
      const b = document.getElementById("data-poi").getBoundingClientRect();
      if (b.top > -80 && b.top < window.innerHeight * 0.6) break;
    }
    return { ok: true, top: Math.round(document.getElementById("data-poi").getBoundingClientRect().top) };
  })()`);
  ok("「去看点位」真的跳到城内点位那张卡", credJump.ok && credJump.top < 400, `距顶 ${credJump.top}px`);
}

/* 攻略 ↔ 动线 双向入口：动线卡里要能一键去看散文版攻略，弹层里再能跳回来 */
{
  const link = await evaluate(`(() => {
    const btn = document.getElementById("guide-full");
    return { exists: !!btn, text: btn ? btn.textContent.trim() : "" };
  })()`);
  ok("城市动线卡里有「看完整攻略」的反向入口", link.exists && /完整攻略/.test(link.text),
    link.text);

  const jumped = await evaluate(`(async () => {
    document.getElementById("guide-city").value = "洛阳";
    document.getElementById("guide-full").click();
    await new Promise((r) => setTimeout(r, 3600));
    const active = document.querySelector("button[data-tab].active");
    const layer = document.getElementById("city-guide");
    const panel = document.getElementById("cg-panel");
    return {
      tab: active ? active.dataset.tab : "",
      visible: layer ? layer.hidden === false : false,
      hasContent: panel ? /洛阳/.test(panel.textContent) : false,
      hasFlowBack: panel ? !!panel.querySelector("#cg-city-flow") : false,
      status: (document.getElementById("guide-status") || {}).textContent || "",
    };
  })()`);
  console.log("看完整攻略:", JSON.stringify(jumped));
  ok("点它会切到地图页并弹出这座城的完整攻略",
    jumped.tab === "map" && jumped.visible && jumped.hasContent, jumped.status.slice(0, 60));
  ok("弹层里给了回「看城内动线」的路（闭环，不重复渲染）", jumped.hasFlowBack);

  const back = await evaluate(`(async () => {
    const btn = document.querySelector("#cg-panel #cg-city-flow");
    if (!btn) return { ok: false };
    btn.click();
    let waited = 0;
    while (waited < 24000) {      await new Promise((r) => setTimeout(r, 500));
      waited += 500;
      const rows = document.querySelectorAll("#guide-days .guide-day").length;
      const st = (document.getElementById("guide-status") || {}).textContent || "";
      if (rows > 0 || /失败|取不到/.test(st)) break;
    }
    const active = document.querySelector("button[data-tab].active");
    return {
      ok: true,
      tab: active ? active.dataset.tab : "",
      city: (document.getElementById("guide-city") || {}).value || "",
      rows: document.querySelectorAll("#guide-days .guide-day").length,
    };
  })()`);
  console.log("回看城内动线:", JSON.stringify(back));
  ok("弹层里的「看城内动线」切回数据台并真的查出了动线",
    back.ok && back.tab === "data" && back.city === "洛阳" && back.rows >= 1,
    `${back.city} ${back.rows} 天`);
}

/* 环线三套入口：三处环线要互相指得到路 */
{
  const hubs = await evaluate(`(() => {
    document.querySelector('button[data-tab="loop"]').click();
    const box = document.getElementById("loop-hubs");
    const btns = box ? [...box.querySelectorAll("button.lh")] : [];
    return {
      exists: !!box,
      wired: document.documentElement.dataset.loopHubs || "",
      count: btns.length,
      labels: btns.map((b) => (b.querySelector("b") || {}).textContent || ""),
      targets: btns.map((b) => b.dataset.gotoTab || "here"),
    };
  })()`);
  console.log("环线入口:", JSON.stringify(hubs));
  ok("环线页签顶部有三套环线的指路牌", hubs.exists && hubs.count === 3 && hubs.wired === "1",
    hubs.labels.join(" / "));
  ok("另外两套各自指向地图漫游与地球模式",
    hubs.targets.includes("map") && hubs.targets.includes("globe"), hubs.targets.join("、"));

  const toGlobe = await evaluate(`(async () => {
    document.querySelector('#loop-hubs button[data-goto-tab="globe"]').click();
    await new Promise((r) => setTimeout(r, 4500));
    const active = document.querySelector("button[data-tab].active");
    const panel = document.getElementById("globe-loops-panel");
    return {
      tab: active ? active.dataset.tab : "",
      top: panel ? Math.round(panel.getBoundingClientRect().top) : null,
      globeReady: !!(window.DSHGlobe && window.DSHGlobe.__debug && window.DSHGlobe.__debug.state.ready),
    };
  })()`);
  console.log("去世界长线:", JSON.stringify(toGlobe));
  ok("点「世界长线」会切到地球模式并滚到环线面板",
    toGlobe.tab === "globe" && toGlobe.globeReady && toGlobe.top !== null && toGlobe.top < 700,
    `距顶 ${toGlobe.top}px`);
}

/* 断点续爬：进度必须来自数据库账本，断电/重启之后仍然看得见、点得到「接着爬」 */
{
  const real = await evaluate(`(async () => {
    const r = await fetch("/api/crawl/progress?date=2026-10-10").then((x) => x.json());
    return { ok: r.ok, planned: r.planned, legacy: r.legacy ? r.legacy.text.slice(0, 40) : "" };
  })()`);
  console.log("真实进度接口:", JSON.stringify(real));
  ok("进度接口读的是落盘账本（不是内存任务）", real.ok === true);

  /* 用假响应渲染一次：造一份「爬到 596/6006、断电时有一对停在 running」的账本 */
  const banner = await evaluate(`(async () => {
    const orig = window.fetch;
    window.fetch = (url, opts) => {
      if (String(url).includes("/api/crawl/progress")) {
        return Promise.resolve({ ok: true, json: async () => ({
          ok: true, date: "2099-01-01", planned: 6006, done: 596, failed: 3, running: 1,
          remaining: 5410, percent: 10, state: "cancelled", note_text: "上次中断在 596/6006 对 OD（剩余 5410）",
          discovered: 1518, ingested: 900, updated_at: "2099-01-01T10:00:00", resumable: true,
          rows: [{ od: "BJP:SHH", status: "running", trains: 0, tries: 1, updated_at: "2099-01-01T10:00:00" }],
        })});
      }
      return orig(url, opts);
    };
    document.getElementById("crawl-date").value = "2099-01-01";
    await loadCrawlProgress("2099-01-01");
    await new Promise((r) => setTimeout(r, 300));
    const box = document.getElementById("crawl-resume-box");
    const view = {
      visible: box.hidden === false,
      text: box.textContent.replace(/\\s+/g, " ").trim(),
      hasGo: !!box.querySelector("#crawl-resume-go"),
      goText: (box.querySelector("#crawl-resume-go") || {}).textContent || "",
      hasDetail: !!box.querySelector("#crawl-resume-detail"),
      barWidth: (box.querySelector(".rb-bar i") || {}).style ? box.querySelector(".rb-bar i").style.width : "",
    };
    // 明细按钮：点一下应当展开逐对明细
    const d = box.querySelector("#crawl-resume-detail");
    if (d) d.click();
    await new Promise((r) => setTimeout(r, 200));
    const pv = document.getElementById("crawl-preview-box");
    view.detailShown = pv.hidden === false && /BJP:SHH/.test(pv.textContent);
    view.detailText = pv.textContent.replace(/\\s+/g, " ").trim().slice(0, 80);

    // 点「接着爬」：应当把日期填好、勾上续爬，并走到确认框。
    // 注意：**不能**让确认框返回 true —— startCrawl 走的是模块内的 postJSON，
    // 外面的 window.TP.postJSON 打桩拦不住，确认通过就会真的向后端发起一次全国爬取
    // （踩过：探针在 2099-01-01 真拉起了 6006 对任务，已用 scripts/purge_probe_crawl.py 清掉）。
    let confirmText = "";
    let confirmCalls = 0;
    window.confirm = (msg) => { confirmCalls++; confirmText = String(msg); return false; };
    const go = box.querySelector("#crawl-resume-go");
    if (go) go.click();
    await new Promise((r) => setTimeout(r, 400));
    view.confirmCalls = confirmCalls;
    view.confirmText = confirmText.replace(/\s+/g, " ").slice(0, 90);
    view.dateBox = document.getElementById("crawl-date").value;
    view.resumeChecked = document.getElementById("crawl-resume").checked;
    window.fetch = orig;
    return view;
  })()`);
  console.log("断点横幅:", JSON.stringify(banner, null, 1));
  ok("有残留进度时显示横幅（含完成/剩余/停在哪）",
    banner.visible && /596/.test(banner.text) && /5410/.test(banner.text), banner.text.slice(0, 110));
  ok("横幅里有「接着爬」按钮与明细入口", banner.hasGo && banner.hasDetail, banner.goText);
  ok("进度条按百分比画出来", banner.barWidth === "10%", banner.barWidth);
  ok("逐对明细能展开（看得到 running 的那一对）", banner.detailShown, banner.detailText);
  ok("点「接着爬」按同一天 + 勾上续爬发起（停在确认框上，探针不真的开爬）",
    banner.confirmCalls >= 1 && /2099-01-01/.test(banner.confirmText) &&
    banner.dateBox === "2099-01-01" && banner.resumeChecked === true,
    banner.confirmText);

  /* 旧格式断点（只有序号、没有成员账）也要如实说出来 */
  const legacy = await evaluate(`(async () => {
    const orig = window.fetch;
    window.fetch = (url, opts) => {
      if (String(url).includes("/api/crawl/progress")) {
        return Promise.resolve({ ok: true, json: async () => ({
          ok: true, date: "2026-10-10", planned: 0, done: 0, failed: 0, running: 0, remaining: 0,
          state: "", note_text: "", discovered: 1518, ingested: 0, resumable: false,
          legacy: { phase: "pairs", index: 596, discovered_lines: 1518,
                    files: ["crawl_state_2026-10-10.txt"],
                    text: "发现旧格式断点：pairs 阶段第 596 对，已发现 1518 趟车。旧断点只记序号…" },
        })});
      }
      return orig(url, opts);
    };
    await loadCrawlProgress("2026-10-10");
    await new Promise((r) => setTimeout(r, 300));
    const box = document.getElementById("crawl-resume-box");
    const view = {
      visible: box.hidden === false,
      text: box.textContent.replace(/\\s+/g, " ").trim(),
      hasGo: !!box.querySelector("#crawl-resume-go"),
    };
    window.fetch = orig;
    return view;
  })()`);
  console.log("旧断点:", JSON.stringify(legacy));
  ok("有旧格式断点残留时也如实提示（不装作没爬过）",
    legacy.visible && /旧格式断点/.test(legacy.text) && legacy.hasGo, legacy.text.slice(0, 90));
}

const shot = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
const file = path.join(os.tmpdir(), `dev-probe-data-${W}x${H}.png`);
fs.writeFileSync(file, Buffer.from(shot.result.data, "base64"));
console.log("整页截图:", file);
console.log(fails.length ? `\n✗ ${fails.length} 项异常：${fails.join("、")}` : "\n✓ 全部正常");
cdp.close(); child.kill(); await sleep(300);
try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
process.exit(fails.length ? 1 : 0);
