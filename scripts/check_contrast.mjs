/* 真浏览器探针：把页面上「看不清」的文字揪出来。
 *
 *   node scripts/check_contrast.mjs [宽] [高] [最低对比度]
 *
 * 做法：遍历有文字的元素，取它自己的颜色、沿祖先找实际背景色（含透明叠加），
 * 按 WCAG 公式算对比度；小于阈值就报出来（大字号按 3.0 算）。
 * 忽略装饰性元素与空文本、隐藏元素。
 */
import { spawn } from "node:child_process";
import net from "node:net";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const W = Number(process.argv[2] || 1440), H = Number(process.argv[3] || 900);
const MIN = Number(process.argv[4] || 4.5);
const URL_PAGE = process.env.PROBE_URL || "http://127.0.0.1:8000/";
const EDGE = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
].find((p) => fs.existsSync(p));
if (!EDGE) { console.error("找不到 Edge/Chrome"); process.exit(1); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dsh-contrast-"));

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

/** 在页面里注入对比度审计函数（只读，不改样式） */
const COMFORT = Number(process.env.CONTRAST_COMFORT || 6.5);   // 舒适线（底线是 WCAG 的 4.5）
const AUDIT = `(() => {
  const parse = (c) => {
    const m = /rgba?\\(([^)]+)\\)/.exec(c || "");
    if (!m) return null;
    const p = m[1].split(",").map((x) => parseFloat(x));
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const over = (fg, bg) => ({
    r: fg.r * fg.a + bg.r * (1 - fg.a),
    g: fg.g * fg.a + bg.g * (1 - fg.a),
    b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1,
  });
  const lum = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const ratio = (a, b) => {
    const l1 = lum(a), l2 = lum(b);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  };
  const bgOf = (el) => {
    let node = el, acc = null;
    while (node && node !== document.documentElement.parentNode) {
      const st = getComputedStyle(node);
      const img = st.backgroundImage && st.backgroundImage !== "none";
      if (img) return { gradient: true, image: st.backgroundImage.slice(0, 40) };
      const c = parse(st.backgroundColor);
      if (c && c.a > 0) acc = acc ? over(acc, c) : c;
      if (acc && acc.a >= 1) return acc;
      node = node.parentElement;
    }
    return acc || { r: 255, g: 255, b: 255, a: 1 };
  };
  const visible = (el) => {
    const st = getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden" || parseFloat(st.opacity) < 0.15) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && el.offsetParent !== null;
  };
  const out = [];
  const weak = [];                 // 达标但偏弱（< 舒适线）：小字号时依然费眼
  const COMFORT = ${COMFORT};      // 由 Node 侧注入（浏览器里没有 process）
  let skipped = 0;

  /* 下拉选项单独查：原生 option 没有盒子（offsetParent 为 null），走不到下面的可见性判断，
     但它**会在白底的弹层里显示**。深色工具栏上的 select 是浅色字，选项若继承它就成了白底白字
     —— 用户截图里「环线下拉一片浅色看不清」就是这个。这里按「选项自己的颜色 / 自己的底色」算。 */
  document.querySelectorAll("select option").forEach((op) => {
    const st = getComputedStyle(op);
    const fg = parse(st.color);
    if (!fg) return;
    const bgRaw = parse(st.backgroundColor);
    const bg = (bgRaw && bgRaw.a > 0) ? bgRaw : { r: 255, g: 255, b: 255, a: 1 };
    const r = ratio(over(fg, bg), bg);
    if (r < 4.5) {
      const sel = op.closest("select");
      out.push({
        sel: "option@" + ((sel && sel.id) || "(无 id 的 select)"),
        text: (op.textContent || "").trim().slice(0, 26), ratio: Math.round(r * 100) / 100, need: 4.5,
        color: st.color,
        bg: "rgb(" + Math.round(bg.r) + "," + Math.round(bg.g) + "," + Math.round(bg.b) + ")",
        size: Math.round(parseFloat(st.fontSize) || 13),
      });
    }
  });

  document.querySelectorAll("body *").forEach((el) => {
    const text = [...el.childNodes].filter((n) => n.nodeType === 3)
      .map((n) => n.textContent.trim()).join("").trim();
    if (!text || text.length < 2) return;
    if (!visible(el)) return;
    const st = getComputedStyle(el);
    const fg = parse(st.color);
    if (!fg) return;
    const bg = bgOf(el);
    // 背景是渐变/图片（页头、地球舞台）的地方算不准，单独计数，不报成问题
    if (bg.gradient) { skipped++; return; }
    const eff = over(fg, bg);
    const size = parseFloat(st.fontSize);
    const bold = parseInt(st.fontWeight, 10) >= 700;
    const large = size >= 24 || (size >= 18.66 && bold);
    const need = large ? 3.0 : 4.5;
    const r = ratio(eff, bg);
    const chain = (() => {     // 报错时带上祖先链，省得再猜「这个底色是从哪来的」
      const out = [];
      let n = el, hops = 0;
      while (n && n !== document.documentElement && hops < 4) {
        const st2 = getComputedStyle(n);
        const sel = n.getAttribute && n.getAttribute("aria-selected");
        out.push(n.tagName.toLowerCase()
          + (n.className && typeof n.className === "string"
            ? "." + String(n.className).split(/\\s+/).join(".") : "")
          + (sel ? "(sel=" + sel + ")" : "")
          + "[" + st2.backgroundColor + (st2.backgroundImage !== "none" ? "+图" : "") + "]");
        n = n.parentElement;
        hops++;
      }
      return out.join(" < ");
    })();
    const row = {
      sel: el.tagName.toLowerCase() + (el.className && typeof el.className === "string"
        ? "." + el.className.split(/\\s+/).filter(Boolean).slice(0, 2).join(".") : ""),
      text: text.slice(0, 26), ratio: Math.round(r * 100) / 100, need,
      color: st.color, bg: "rgb(" + Math.round(bg.r) + "," + Math.round(bg.g) + "," + Math.round(bg.b) + ")",
      size: Math.round(size), chain,
    };
    if (r < need) out.push({ ...row, probe: {
      // 复现不了的问题最难查：把「这一刻 DOM 到底是什么」一起带上
      parentTag: el.parentElement ? el.parentElement.tagName : "",
      parentCls: el.parentElement ? String(el.parentElement.className) : "",
      parentInline: el.parentElement ? (el.parentElement.getAttribute("style") || "") : "",
      parentMatchesActive: el.parentElement
        ? !!el.parentElement.closest("header nav button.active") : false,
      parentHtml: el.parentElement ? el.parentElement.outerHTML.slice(0, 90) : "",
      ready: document.readyState,
      sheets: document.styleSheets.length,
    } });
    else if (r < COMFORT) weak.push(row);
  });
  return { issues: out, weak, skipped, options: document.querySelectorAll("select option").length };
})()`;

const tabs = ["plan", "loop", "map", "globe", "data"];
const bad = [];
const weak = [];          // 达标但偏弱：4.5 是底线，不是「看着舒服」
let skippedTotal = 0;
/**
 * 审计 + **复测**：只在「隔一会儿再看还在」时才认定为问题。
 *
 * 另外每次审计前会**临时关掉过渡动画**（`transition: none`）：页头页签按钮带
 * `transition: .2s ease`，改 class 时底色与字色是**渐变的**，审计若正好落在动画中间，
 * 会读到「底色已经是浅色、字色还是浅色」这种真实界面上不存在的组合
 * （本项目报过一次「页头 01 号标签 1.47 对比度」，事后在稳定 DOM 里怎么查都查不到）。
 * 视觉审计本来就不该量动画中间态 —— 关掉动画再量，问题依旧出现才是真问题。
 */
const FREEZE = `(() => {
  let s = document.getElementById("contrast-freeze");
  if (!s) {
    s = document.createElement("style");
    s.id = "contrast-freeze";
    s.textContent = "*, *::before, *::after { transition: none !important; animation: none !important; }";
    document.head.appendChild(s);
  }
  return true;
})()`;
const UNFREEZE = `(() => { const s = document.getElementById("contrast-freeze");
  if (s) s.remove(); return true; })()`;

async function auditStable(label) {
  await evaluate(FREEZE);
  await sleep(120);                      // 让样式重算一帧
  const first = await evaluate(AUDIT);
  const issues1 = (first && first.issues) || [];
  if (!issues1.length) { await evaluate(UNFREEZE); return { res: first, rows: [], dropped: 0 }; }
  await sleep(600);
  const second = await evaluate(AUDIT);
  const issues2 = (second && second.issues) || [];
  await evaluate(UNFREEZE);
  const keyOf = (r) => [r.sel, r.text, r.color, r.bg, r.size].join("|");
  const still = new Set(issues2.map(keyOf));
  const rows = issues1.filter((r) => still.has(keyOf(r)));
  const dropped = issues1.length - rows.length;
  if (dropped) {
    console.log(`  （${label}：首测 ${issues1.length} 处、复测后仍存在 ${rows.length} 处，` +
      `丢掉 ${dropped} 处加载/动画瞬间的假报）`);
  }
  const seen = new Set(rows.map(keyOf));
  issues2.forEach((r) => { if (!seen.has(keyOf(r))) rows.push(r); });
  return { res: first, rows, dropped };
}

for (const tab of tabs) {
  await cdp.send("Page.navigate", { url: URL_PAGE });
  await sleep(1800);
  await evaluate(`(() => { const b = document.querySelector('button[data-tab="${tab}"]'); if (b) b.click(); return true; })()`);
  await sleep(tab === "globe" ? 3500 : 1500);
  if (tab === "data") {
    // 数据台要多等：路线库、我的行程、城内点位都是异步来的
    await sleep(2500);
  }
  const audited = await auditStable(tab);
  const res = audited.res;
  const rows = audited.rows;
  skippedTotal += (res && res.skipped) || 0;
  rows.forEach((r) => bad.push({ ...r, tab }));
  ((res && res.weak) || []).forEach((r) => weak.push({ ...r, tab }));
  console.log(`${tab}: ${rows.length} 处对比度不足（另有 ${(res && res.skipped) || 0} 处背景是渐变，跳过；` +
    `查了 ${(res && res.options) || 0} 个下拉选项）`);

  // 环线 Ultra 版是「点开才出现」的面板，默认审计扫不到它 —— 单独展开再扫一遍
  if (tab === "loop") {
    await evaluate(`(() => { const b = document.getElementById("l-ultra"); if (b) b.click(); return true; })()`);
    await sleep(2200);
    await evaluate(`(() => {
      const first = document.querySelector("#ultra-list .ultra-card .rt-head");
      if (first) first.click();          // 展开一条，把逐日安排也扫进去
      return true;
    })()`);
    await sleep(2500);
    const res2 = await evaluate(AUDIT);
    const rows2 = (res2 && res2.issues) || [];
    skippedTotal += (res2 && res2.skipped) || 0;
    rows2.forEach((r) => bad.push({ ...r, tab: "loop+ultra" }));
    console.log(`loop+ultra: ${rows2.length} 处对比度不足（另有 ${(res2 && res2.skipped) || 0} 处背景是渐变，跳过）`);
  }

  // 地球页签的环线列表也是「选了环线才铺出来」，而且每条的「海洋/极地/沙漠」标签
  // 用的是环境色本身当字色 —— 浅色（极地冰蓝、沙漠土黄）在什么底上都可能看不清。
  if (tab === "globe") {
    const picked = await evaluate(`(async () => {
      const sel = document.getElementById("globe-loop-select");
      const opts = sel ? [...sel.options].filter((o) => o.value) : [];
      if (!opts.length) return { ok: false };
      sel.value = opts[0].value;
      sel.dispatchEvent(new Event("change", { bubbles: true }));
      await new Promise((r) => setTimeout(r, 2500));
      const tags = [...document.querySelectorAll("#globe-loops .tag.env")];
      const first = document.querySelector("#globe-loops .loop-title");
      const st = first ? getComputedStyle(first) : null;
      const panel = document.querySelector("#globe-loops-panel");
      return {
        ok: true, picked: opts[0].textContent.trim(),
        tagCount: tags.length,
        tagColor: tags[0] ? getComputedStyle(tags[0]).color : "",
        tagText: tags[0] ? tags[0].textContent.trim() : "",
        titleColor: st ? st.color : "",
        panelBg: panel ? getComputedStyle(panel).backgroundColor : "",
      };
    })()`);
    console.log(`globe+loop: 选中「${picked.picked}」，环线列表 ${picked.tagCount} 个环境标签，` +
      `标签字色 ${picked.tagColor}（${picked.tagText}），标题字色 ${picked.titleColor}，面板底 ${picked.panelBg}`);
    const res3 = await evaluate(AUDIT);
    const rows3 = (res3 && res3.issues) || [];
    skippedTotal += (res3 && res3.skipped) || 0;
    rows3.forEach((r) => bad.push({ ...r, tab: "globe+loop" }));
    console.log(`globe+loop: ${rows3.length} 处对比度不足（另有 ${(res3 && res3.skipped) || 0} 处背景是渐变，跳过）`);

    /* 自检：把旧的坏样式塞回去（选项继承浅色字），检查器必须报出来。
       否则「0 处问题」可能只是因为根本没查下拉 —— 这条保证检查器真的在干活。 */
    const selfTest = await evaluate(`(() => {
      const style = document.createElement("style");
      style.id = "contrast-selftest";
      style.textContent = "#globe-loop-select option { color: #e8fbff; background: transparent; }";
      document.head.appendChild(style);
      return true;
    })()`);
    const resBad = await evaluate(AUDIT);
    const caught = ((resBad && resBad.issues) || []).filter((r) => /option@globe-loop-select/.test(r.sel));
    await evaluate(`(() => { const s = document.getElementById("contrast-selftest"); if (s) s.remove(); return true; })()`);
    console.log(`globe+loop 自检（塞回浅色选项）：${selfTest ? "注入成功" : "注入失败"}，` +
      `检查器抓到 ${caught.length} 条` + (caught[0] ? ` → ${caught[0].ratio} ${caught[0].color} on ${caught[0].bg}` : ""));
    if (!caught.length) {
      bad.push({ tab: "self-test", sel: "option@globe-loop-select", text: "检查器没抓到故意注入的浅色选项",
                 ratio: 0, need: 4.5, color: "", bg: "", size: 0 });
    }
  }
}
console.log(`\n渐变背景跳过合计 ${skippedTotal} 处（页头/地球舞台那种，需人眼判断）`);

const seen = new Set();
const uniq = [];
for (const row of bad) {
  const key = row.tab + "|" + row.sel;
  if (seen.has(key)) continue;
  seen.add(key);
  uniq.push(row);
}
uniq.sort((a, b) => a.ratio - b.ratio);
console.log(`\n== 对比度不足（阈值 ${MIN}）共 ${bad.length} 处，去重后 ${uniq.length} 处 ==`);
uniq.slice(0, 40).forEach((r) => {
  console.log(`  ${String(r.ratio).padStart(5)} (需 ${r.need}) [${r.tab}] ${r.sel} ${r.size}px  ` +
    `${r.color} on ${r.bg}  「${r.text}」`);
  if (r.chain) console.log(`        祖先链：${r.chain}`);
  if (r.probe) console.log("        现场：" + JSON.stringify(r.probe));
});

/* 达标但偏弱的那批：4.5 只是「勉强能认出字」，小字号 + 蓝色系在浅底上依然费眼。
   用户反馈过「地球模式那个蓝色字体，白底还是看不清」——所以把这些也列出来。 */
const seenW = new Set();
const weakU = [];
for (const row of weak) {
  const key = row.tab + "|" + row.sel + "|" + row.color;
  if (seenW.has(key)) continue;
  seenW.add(key);
  weakU.push(row);
}
weakU.sort((a, b) => a.ratio - b.ratio);
console.log(`\n== 偏弱（达标但 < 舒适线 ${COMFORT}）去重后 ${weakU.length} 处 ==`);
weakU.slice(0, 30).forEach((r) => {
  console.log(`  ${String(r.ratio).padStart(5)} [${r.tab}] ${r.sel} ${r.size}px  ` +
    `${r.color} on ${r.bg}  「${r.text}」`);
});

const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
const file = path.join(os.tmpdir(), `check-contrast-${W}x${H}.png`);
fs.writeFileSync(file, Buffer.from(shot.result.data, "base64"));
console.log("截图（最后停在数据台）:", file);
console.log(uniq.length ? `\n✗ ${uniq.length} 类文字看不清` : "\n✓ 没有明显看不清的文字");
cdp.close(); child.kill(); await sleep(300);
try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
process.exit(uniq.length ? 1 : 0);
