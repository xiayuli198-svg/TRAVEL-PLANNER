/* 真浏览器探针：地球模式的卡片到底摆在哪儿。
 *
 *   node scripts/dev_probe_cards.mjs [宽] [高]
 *
 * 只读检查：打开页面 → 切到地球 → 等建球 → 把每张可见卡片的屏幕位置打出来，
 * 顺便给出「散得开还是挤成一堆」的统计。用来查「图片不在地球正确点位」这类问题。
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
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dsh-probe-cards-"));

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
        setTimeout(() => { if (waiters.has(myId)) { waiters.delete(myId); rej(new Error("CDP 超时 " + method)); } }, 20000);
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
await cdp.send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });
await cdp.send("Page.navigate", { url: URL_PAGE });
await sleep(2000);
await evaluate(`document.querySelector('button[data-tab="globe"]').click()`);
{
  let tries = 0, ready = false;
  while (tries < 60 && !ready) {
    await sleep(300);
    ready = await evaluate(`!!(window.DSHGlobe && window.DSHGlobe.__debug &&
      window.DSHGlobe.__debug.state && window.DSHGlobe.__debug.state.ready)`);
    tries++;
  }
  console.log(`等地球就绪：${tries} 次轮询`);
}
await sleep(1500);

const info = await evaluate(`(() => {
  const dbg = window.DSHGlobe.__debug;
  const host = document.getElementById("globe-viz");
  const rect = host.getBoundingClientRect();
  const cards = dbg.state.cardList.map((it) => {
    const el = it.el;
    const r = el.getBoundingClientRect();
    return {
      city: it.city.name, lat: it.city.lat, lon: it.city.lon,
      display: getComputedStyle(el).display,
      transform: el.style.transform,
      rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
      insideHost: r.x >= rect.x - 200 && r.x <= rect.right + 200 && r.y >= rect.y - 200 && r.y <= rect.bottom + 200,
    };
  });
  const visible = cards.filter((c) => c.display !== "none");
  const centers = visible.map((c) => [c.rect.x + c.rect.w / 2, c.rect.y + c.rect.h / 2]);
  const uniq = new Set(centers.map((p) => p.map((v) => Math.round(v / 4)).join(",")));
  const xs = centers.map((p) => p[0]), ys = centers.map((p) => p[1]);
  const stats = (arr) => {
    if (!arr.length) return { min: 0, max: 0, sd: 0 };
    const mean = arr.reduce((a, b) => a + b, 0) / arr.length;
    const sd = Math.sqrt(arr.reduce((a, b) => a + (b - mean) ** 2, 0) / arr.length);
    return { min: Math.round(Math.min(...arr)), max: Math.round(Math.max(...arr)), sd: Math.round(sd) };
  };
  return {
    host: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
    diag: {
      hasGetScreenCoords: typeof dbg.globe.getScreenCoords,
      hasToScreen: typeof dbg.globe.toScreenPosition,
      screenOfHome: dbg.screenOf ? dbg.screenOf(22, 20) : "no screenOf in debug",
      frontHome: dbg.isFront ? dbg.isFront(22, 20) : null,
      camPos: (() => { const c = dbg.globe.camera && dbg.globe.camera(); return c && c.position ? [Math.round(c.position.x), Math.round(c.position.y), Math.round(c.position.z)] : null; })(),
      firstCard: (() => {
        const it = dbg.state.cardList[0];
        if (!it) return null;
        return { city: it.city.name, lat: it.city.lat, lon: it.city.lon,
                 display: getComputedStyle(it.el).display, transform: it.el.style.transform,
                 screen: dbg.screenOf ? dbg.screenOf(it.city.lat, it.city.lon) : null };
      })(),
      ready: dbg.state.ready, cardList: dbg.state.cardList.length,
      loops: (() => {
        const sel = document.getElementById("globe-loop-select");
        const list = document.getElementById("globe-loops");
        const data = dbg.state.data || {};
        const opts = sel ? [...sel.options] : [];
        return {
          selectCount: opts.length,
          selectOptions: opts.slice(0, 4).map((o) => o.text + (o.value ? "=" + o.value : "")),
          listChildren: list ? list.children.length : -1,
          dataLoops: (data.loops || []).length,
          listText: list ? list.textContent.replace(/[\s]+/g, " ").trim().slice(0, 70) : "",
        };
      })(),
      rawProject: (() => {
        try {
          const host = document.getElementById("globe-viz");
          const w = dbg.worldOf(22, 20);
          const size = { width: host.clientWidth, height: host.clientHeight };
          const out = dbg.globe.getScreenCoords(size, w.x, w.y, w.z);
          const cam = dbg.globe.camera();
          const P = cam.projectionMatrix.elements, V = cam.matrixWorldInverse.elements;
          const mul = (m, v) => {
            const o = [0, 0, 0, 0];
            for (let r = 0; r < 4; r++) {
              o[r] = m[r] * v[0] + m[4 + r] * v[1] + m[8 + r] * v[2] + m[12 + r] * v[3];
            }
            return o;
          };
          const clip = mul(P, mul(V, [w.x, w.y, w.z, 1]));
          return {
            w, size, out, finite: isFinite(out && out.x) && isFinite(out && out.y),
            cam: { fov: cam.fov, aspect: cam.aspect, near: cam.near, far: cam.far,
                   pos: [cam.position.x, cam.position.y, cam.position.z] },
            projFirst6: Array.from(P.slice(0, 6)).map((v) => +v.toFixed(4)),
            viewFirst6: Array.from(V.slice(0, 6)).map((v) => +v.toFixed(4)),
            clip: clip.map((v) => +Number(v).toFixed(4)),
            manual: { x: +((clip[0] / clip[3] + 1) * size.width / 2).toFixed(1),
                      y: +(-(clip[1] / clip[3] - 1) * size.height / 2).toFixed(1) },
          };
        } catch (e) { return { err: String(e) }; }
      })(),
    },
    total: cards.length, visible: visible.length, uniquePositions: uniq.size,
    spreadX: stats(xs), spreadY: stats(ys),
    inside: visible.filter((c) => c.insideHost).length,
    sample: visible.slice(0, 14).map((c) => ({ city: c.city, lat: c.lat, lon: c.lon, xy: [c.rect.x, c.rect.y] })),
    sameTransform: new Set(visible.map((c) => c.transform)).size,
  };
})()`);
console.log("卡片定位:", JSON.stringify(info, null, 1));
if (errors.length) { console.log("== 页面报错 =="); errors.slice(0, 6).forEach((e) => console.log("  " + e)); }

const shot = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
const file = path.join(os.tmpdir(), `dev-probe-cards-${W}x${H}.png`);
fs.writeFileSync(file, Buffer.from(shot.result.data, "base64"));
console.log("截图:", file);

const fails = [];
const ok = (name, cond, extra = "") => {
  console.log(`  ${cond ? "✓" : "✗"} ${name}${extra ? " — " + extra : ""}`);
  if (!cond) fails.push(name);
};
ok("有卡片被定位出来", info.visible >= 10, `${info.visible}/${info.total} 张可见`);
ok("卡片位置各不相同（不是挤成一堆）", info.uniquePositions >= info.visible * 0.8,
  `${info.uniquePositions} 个不同位置 / ${info.visible} 张`);
ok("卡片在地球窗口范围内", info.inside >= info.visible * 0.9, `${info.inside}/${info.visible}`);
ok("位置横向铺得开", info.spreadX.sd > info.host.w * 0.1,
  `x 标准差 ${info.spreadX.sd}（窗口宽 ${info.host.w}）`);
ok("位置纵向铺得开", info.spreadY.sd > info.host.h * 0.1,
  `y 标准差 ${info.spreadY.sd}（窗口高 ${info.host.h}）`);
ok("transform 是逐个算出来的", info.sameTransform === info.visible,
  `${info.sameTransform} 个不同 transform`);
ok("页面没有异常", errors.length === 0, errors[0] || "无");

/* 环线「添加」流程：点列表里第一条 → 详情面板的「把这条环线放进路线」 → 看已选城市 */
{
  const before = await evaluate(`(() => {
    const list = document.querySelectorAll("#globe-loops .loop-item");
    return { items: list.length, picked: (document.getElementById("globe-picked-count") || {}).textContent || "" };
  })()`);
  await evaluate(`(() => {
    const item = document.querySelector("#globe-loops .loop-item");
    if (item) item.click();
    return true;
  })()`);
  await sleep(900);
  const detail = await evaluate(`(() => {
    const box = document.getElementById("globe-loop-detail");
    return {
      exists: !!box,
      visible: box ? box.offsetParent !== null : false,
      head: box ? (box.querySelector(".ld-head") || {}).textContent.trim() : "",
      buttons: box ? [...box.querySelectorAll("button")].map((b) => b.textContent.trim()) : [],
    };
  })()`);
  await evaluate(`(() => {
    const btn = document.querySelector("#globe-loop-detail .ld-use");
    if (btn) btn.click();
    return true;
  })()`);
  await sleep(600);
  const after = await evaluate(`(() => {
    const picked = document.getElementById("globe-picked-list");
    return {
      count: (document.getElementById("globe-picked-count") || {}).textContent || "",
      rows: picked ? picked.children.length : -1,
      first: picked && picked.firstElementChild ? picked.firstElementChild.textContent.trim().slice(0, 40) : "",
      hint: (document.getElementById("globe-picked-hint") || {}).textContent.slice(0, 60) || "",
      info: (document.getElementById("globe-info") || {}).textContent.slice(0, 70) || "",
    };
  })()`);
  console.log("环线添加:", JSON.stringify({ before, detail, after }, null, 1));
  ok("环线列表可点", before.items >= 1, `${before.items} 条`);
  ok("点环线后出现详情面板（含「放进路线」）",
    detail.exists && detail.buttons.some((t) => /放进路线/.test(t)),
    `${detail.head} | ${detail.buttons.join("/")}`);
  ok("点「放进路线」后已选城市真的有内容",
    Number(after.count) > 0 && after.rows > 0,
    `count=${after.count} rows=${after.rows} 首个=${after.first}`);

  /* 工具栏上的「＋ 加进路线」：选中环线后应当可点，并真把城市加进去 */
  const toolbar = await evaluate(`(() => {
    const sel = document.getElementById("globe-loop-select");
    const btn = document.getElementById("globe-loop-add");
    if (!sel || !btn) return { missing: true };
    sel.value = sel.options[1] ? sel.options[1].value : "";
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    return { disabled: btn.disabled, label: btn.textContent.trim(),
             options: sel.options.length };
  })()`);
  await sleep(700);
  const afterAdd = await evaluate(`(() => {
    const btn = document.getElementById("globe-loop-add");
    const before = (document.getElementById("globe-picked-count") || {}).textContent || "";
    if (btn && !btn.disabled) btn.click();
    return { before, btnDisabled: btn ? btn.disabled : null };
  })()`);
  await sleep(500);
  const picked = await evaluate(`(document.getElementById("globe-picked-count") || {}).textContent || ""`);
  console.log("工具栏加环线:", JSON.stringify({ toolbar, afterAdd, picked }, null, 1));
  ok("工具栏有「加进路线」按钮且选中环线后可点",
    !toolbar.missing && toolbar.options > 1 && toolbar.disabled === false,
    `${toolbar.label || ""} options=${toolbar.options} disabled=${toolbar.disabled}`);
  ok("点工具栏按钮能把环线城市加进路线", Number(picked) > 0, `已选 ${picked} 城`);
}
console.log(fails.length ? `\n✗ ${fails.length} 项异常：${fails.join("、")}` : "\n✓ 全部正常");
cdp.close(); child.kill(); await sleep(300);
try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
process.exit(fails.length ? 1 : 0);
