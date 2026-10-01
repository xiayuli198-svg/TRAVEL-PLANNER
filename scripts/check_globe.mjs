/* 地球模式前端冒烟测试（可选开发工具，需要 Node 18+）
 *
 * web/globe3d.js 依赖 globe.gl + WebGL，无头环境跑不了真渲染，
 * 所以这里用一个「假 globe.gl」把它整条链路跑通：
 * 加载 /api/world/map → 建点/环/线 → 城市卡片 → 选环线 → 选城市 → 世界之最 → 生成行程单，
 * 任何抛错都会让脚本以非 0 退出。
 *
 *   node scripts/check_globe.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DATA = path.join(ROOT, "src", "travel_planner", "data");
const read = (p) => JSON.parse(fs.readFileSync(p, "utf8"));

const catalogPath = path.join(DATA, "world_catalog.json");
if (!fs.existsSync(catalogPath)) {
  console.log("跳过：还没有 world_catalog.json（先运行 python scripts/build_world_catalog.py）");
  process.exit(0);
}
const catalog = read(catalogPath);

/* ---------------- 最小 DOM 桩（与 check_map.mjs 同款） ---------------- */
function makeEl(tag = "div", id = "") {
  const selCache = new Map();
  const el = {
    tagName: tag.toUpperCase(), id, hidden: false, offsetParent: {}, offsetWidth: 100, offsetHeight: 116,
    clientWidth: 1200, clientHeight: 700, children: [], dataset: {}, value: "", _html: "", _text: "",
    style: new Proxy({ setProperty() {}, removeProperty() {} },
      { get: (t, p) => t[p] ?? "", set: (t, p, v) => { t[p] = v; return true; } }),
    classList: {
      _set: new Set(),
      add(...c) { c.forEach((x) => this._set.add(x)); },
      remove(...c) { c.forEach((x) => this._set.delete(x)); },
      toggle(c, on) { if (on === undefined) on = !this._set.has(c); on ? this._set.add(c) : this._set.delete(c); return on; },
      contains(c) { return this._set.has(c); },
    },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); this.children = []; },
    get textContent() { return this._text; },
    set textContent(v) {
      this._text = String(v == null ? "" : v);
      this._html = this._text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    },
    addEventListener(type, fn) {
      if (!this._listeners) this._listeners = {};
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
    removeEventListener() {},
    fire(type, event) {
      ((this._listeners && this._listeners[type]) || []).forEach((fn) => fn(event));
    },
    appendChild(c) { this.children.push(c); register(c); if (c && c._html) this._html += c._html; return c; },
    insertBefore(c) { this.children.unshift(c); register(c); if (c && c._html) this._html = c._html + this._html; return c; },
    remove() { if (this.id) registry.delete(this.id); },
    setAttribute() {}, getAttribute() { return null; },
    add(opt) { this.children.push(opt); return this; },
    querySelector(sel) {
      if (!selCache.has(sel)) selCache.set(sel, makeEl("div"));
      return selCache.get(sel);
    },
    querySelectorAll() { return []; },
    closest() { return null; },
    scrollIntoView() {},
    getBoundingClientRect: () => ({ width: 1200, height: 700, left: 0, top: 0, right: 1200, bottom: 700 }),
    getContext: () => new Proxy({}, { get: () => () => {}, set: () => true }),
  };
  return el;
}
const registry = new Map();
const register = (child) => { if (child && child.id) registry.set(child.id, child); return child; };
const htmlText = fs.readFileSync(path.join(ROOT, "web", "index.html"), "utf8");
const htmlIds = new Set([...htmlText.matchAll(/id="([A-Za-z0-9_-]+)"/g)].map((m) => m[1]));
const hiddenIds = new Set();
for (const m of htmlText.matchAll(/<[^>]*id="([A-Za-z0-9_-]+)"[^>]*>/g)) {
  if (/\shidden(\s|>|=)/.test(m[0])) hiddenIds.add(m[1]);
}
globalThis.__docListeners = {};
globalThis.document = {
  readyState: "complete",
  getElementById: (id) => {
    if (!htmlIds.has(id)) return registry.get(id) || null;
    if (!registry.has(id)) {
      const el = makeEl("div", id);
      el.hidden = hiddenIds.has(id);
      registry.set(id, el);
    }
    return registry.get(id);
  },
  createElement: (tag) => makeEl(tag),
  createElementNS: (_n, tag) => makeEl(tag),
  createDocumentFragment: () => makeEl("fragment"),
  addEventListener(type, fn) {
    (globalThis.__docListeners[type] = globalThis.__docListeners[type] || []).push(fn);
  },
  querySelectorAll: () => [],
  body: makeEl("body"),
};

/* ---------------- 假 globe.gl ---------------- */
/** 相机用 lookAt(0,0,0)：局部 -Z 指向球心，正面点的 local.z 一定是负数 */
function makeWorldToLocal(pos) {
  const inv = Math.hypot(pos.x, pos.y, pos.z) || 1;
  const b = [-pos.x / inv, -pos.y / inv, -pos.z / inv];                  // backward
  let r = [b[2], 0, -b[0]];                                             // right = up(0,1,0) × backward
  const rl = Math.hypot(...r) || 1;
  r = r.map((v) => v / rl);
  const u = [b[1] * r[2] - b[2] * r[1], b[2] * r[0] - b[0] * r[2], b[0] * r[1] - b[1] * r[0]];
  const e = [r[0], u[0], b[0], r[1], u[1], b[1], r[2], u[2], b[2]];     // 无平移时逆矩阵 = 旋转转置
  return function (v) {
    const x = v.x - pos.x, y = v.y - pos.y, z = v.z - pos.z;
    return { x: e[0] * x + e[1] * y + e[2] * z, y: e[3] * x + e[4] * y + e[5] * z, z: e[6] * x + e[7] * y + e[8] * z };
  };
}
function fakeGlobe() {
  const g = {
    _points: [], _rings: [], _paths: [], _pov: null, _handlers: {}, _povCalls: [],
    globeImageUrl() { return this; }, bumpImageUrl() { return this; }, backgroundColor() { return this; },
    showAtmosphere() { return this; }, atmosphereColor() { return this; }, atmosphereAltitude() { return this; },
    pointLat() { return this; }, pointLng() { return this; }, pointAltitude() { return this; },
    pointRadius() { return this; }, pointLabel() { return this; },
    pointColor(fn) { this._pointColor = fn; return this; },
    pathPoints() { return this; }, pathPointLat() { return this; }, pathPointLng() { return this; },
    pathPointAlt(fn) { this._pathAlt = fn; return this; },
    pathColor(fn) { this._pathColor = fn; return this; }, pathStroke() { return this; }, pathAltitude() { return this; },
    ringLat() { return this; }, ringLng() { return this; }, ringColor(fn) { this._ringColor = fn; return this; },
    ringMaxRadius() { return this; }, ringPropagationSpeed() { return this; }, ringRepeatPeriod() { return this; },
    pointsData(d) { this._points = d; return this; },
    ringsData(d) { this._rings = d; return this; },
    pathsData(d) { this._paths = d; return this; },
    onPointClick(fn) { this._handlers.click = fn; return this; },
    pointOfView(pv, dur) {
      this._pov = pv;
      this._povDur = dur;
      this._povCalls.push(pv);
      // 假相机真的跟着走：这样正反面判定、卡片投影都能被测到。
      // 机位必须和 globe3d.js 的 worldOf 用**同一套**换算（极角写法）——
      // 以前这里写的是另一套公式，于是假球里「正面」和真球里正好相反，
      // 真球上 81 张卡片全看不到的 bug 在假球里测不出来。
      const alt = pv.altitude == null ? 1 : pv.altitude;
      const R = 100 * (1 + alt);
      const phi = ((90 - (pv.lat || 0)) * Math.PI) / 180;
      const theta = ((90 - (pv.lng || 0)) * Math.PI) / 180;
      const sp = Math.sin(phi);
      this._cam = {
        position: {
          x: R * sp * Math.cos(theta),
          y: R * Math.cos(phi),
          z: R * sp * Math.sin(theta),
        },
        matrixWorldInverse: {}, projectionMatrix: {},
      };
      this._cam.worldToLocal = makeWorldToLocal(this._cam.position);
      return this;
    },
    controls() {
      if (!this._controls) this._controls = { autoRotate: false, autoRotateSpeed: 0, minDistance: 0, maxDistance: 0 };
      return this._controls;
    },
    width() { return this; }, height() { return this; },
    viewOffset() { return this; },
    /* 下面几个必须和真实 globe.gl 的**签名**一致 —— 假球太宽容，
       曾经让「投影全 NaN、81 张卡片一张都不显示」这种事故一路滑过去。
       真球里：getCoords(lat, lng, altitude) 用极角公式；
       getScreenCoords(viewportSize, x, y, z) 第一个参数是 {width, height}。 */
    getGlobeRadius() { return 100; },
    getCoords(lat, lng, altitude = 0) {
      const phi = ((90 - lat) * Math.PI) / 180, theta = ((90 - lng) * Math.PI) / 180;
      const r = 100 * (1 + altitude), sp = Math.sin(phi);
      return { x: r * sp * Math.cos(theta), y: r * Math.cos(phi), z: r * sp * Math.sin(theta) };
    },
    getScreenCoords(size, x, y, z) {
      if (!size || typeof size.width !== "number" || typeof size.height !== "number") {
        throw new Error("getScreenCoords 的第一个参数应当是 {width, height} 对象");
      }
      // 假球不需要真矩阵：按「视线基」做一次正交投影就够了 ——
      // 正面点会落在画布中间、边缘点落在画布边上，正好够测卡片定位与在框内。
      const cam = this.camera();
      const c = cam.position;
      const len = Math.hypot(c.x, c.y, c.z) || 1;
      const f = { x: -c.x / len, y: -c.y / len, z: -c.z / len };      // 视线：朝球心
      let up = Math.abs(f.y) > 0.99 ? { x: 0, y: 0, z: 1 } : { x: 0, y: 1, z: 0 };
      const cross = (a, b) => ({ x: a.y * b.z - a.z * b.y, y: a.z * b.x - a.x * b.z,
                                 z: a.x * b.y - a.y * b.x });
      const unit = (v) => { const n = Math.hypot(v.x, v.y, v.z) || 1;
                            return { x: v.x / n, y: v.y / n, z: v.z / n }; };
      const right = unit(cross(up, f));
      const upN = unit(cross(f, right));
      const k = Math.min(size.width, size.height) / 260;              // 球半径 100 → 约 77% 画布
      return {
        x: size.width / 2 + (x * right.x + y * right.y + z * right.z) * k,
        y: size.height / 2 - (x * upN.x + y * upN.y + z * upN.z) * k,
      };
    },
    camera() { return this._cam || (this._cam = this.pointOfView({ lat: 0, lng: 0, altitude: 3 })._cam); },
  };
  return g;
}
globalThis.window = {
  devicePixelRatio: 1, innerWidth: 1440, addEventListener() {},
  Globe: () => (host) => { globalThis.__globe = fakeGlobe(); globalThis.__globe._host = host; return globalThis.__globe; },
  TP: { esc: (s) => String(s == null ? "" : s), postJSON: null },
};
/** <select> 的 new Option(...) 在 Node 里不存在，补一个 */
globalThis.Option = class Option {
  constructor(text, value) { this.text = text; this.value = value; }
};
let fakeNow = 0;
const rafQueue = [];
globalThis.performance = { now: () => fakeNow };
globalThis.requestAnimationFrame = (cb) => { rafQueue.push(cb); return rafQueue.length; };
function pump(n = 40) {
  let i = 0;
  while (rafQueue.length && i < n) { const cb = rafQueue.shift(); fakeNow += 16; cb(fakeNow); i++; }
  return i;
}

/* ---------------- 假 fetch ---------------- */
const payload = {
  ok: true,
  cities: catalog.cities.map((c) => ({
    name: c.name, name_en: c.name_en, country: c.country, area: c.area,
    lat: c.lat, lon: c.lon, lng: c.lon, tz: c.tz, score: c.score, days: c.days,
    best_season: c.best_season, tags: c.tags, intro: c.intro, halfday: c.halfday,
    oneday: c.oneday, highlights: c.highlights, food: c.food, note: c.note,
    night: !!c.night, photo: null, photo_day: null, photo_night: null,
    has_guide: !!c.guide, guide_days: c.guide ? c.guide.days : null,
  })),
  loops: catalog.loops.map((l) => ({
    id: l.id, name: l.name, subtitle: l.subtitle, cities: l.cities, days: l.days,
    season: l.season, area: l.area, transport: l.transport, blurb: l.blurb,
    tips: l.tips || [], regions: l.regions || [], cross_region: !!l.cross_region,
    center: l.center, stops: l.stops || [], km: 1000,
  })),
  records: catalog.records.map((r) => Object.assign({}, r, { photo: null })),
  stats: { cities: catalog.cities.length, loops: catalog.loops.length, records: catalog.records.length },
};
let planCalls = 0;
globalThis.fetch = async (url) => {
  const u = String(url);
  if (u.includes("/api/world/map")) return { ok: true, status: 200, json: async () => payload };
  if (u.includes("/api/world/city")) {
    const name = decodeURIComponent(u.split("name=")[1] || "");
    const c = catalog.cities.find((x) => x.name === name);
    return { ok: true, status: 200, json: async () => ({ ok: !!c, city: c, guide: (c && c.guide) || {} }) };
  }
  if (u.includes("/api/world/record")) {
    const id = decodeURIComponent(u.split("id=")[1] || "");
    const r = catalog.records.find((x) => x.id === id);
    return { ok: true, status: 200, json: async () => ({ ok: !!r, record: r }) };
  }
  return { ok: false, status: 404, json: async () => ({ detail: "Not Found" }) };
};
globalThis.window.TP.postJSON = async (url, body) => {
  if (String(url).includes("/api/world/plan")) {
    planCalls++;
    const cities = body.cities.map((n) => catalog.cities.find((c) => c.name === n)).filter(Boolean);
    return {
      ok: true, order: body.cities, cities,
      legs: cities.slice(0, -1).map((c, i) => ({
        frm: c.name, to: cities[i + 1].name, km: 9000, flight_hours: 13.0,
        flight_text: "13小时00分", tz_shift: 6, same_country: false,
      })),
      summary: { city_count: cities.length, total_km: 9000 * (cities.length - 1), flight_hours: 13,
                 visit_days: 6, travel_days: 3, total_days: 9, areas: ["欧洲", "东亚"],
                 tz_span: [-7, 1], closed: true },
      warnings: ["世界行程的机票必须实时查询：这里只给飞行距离与时间估算，不含航班时刻与票价。"],
    };
  }
  throw new Error("unexpected POST " + url);
};

/* ---------------- 载入并测试 ---------------- */
const code = fs.readFileSync(path.join(ROOT, "web", "globe3d.js"), "utf8");
new Function("window", "document", "performance", "requestAnimationFrame", "fetch", code)(
  globalThis.window, globalThis.document, globalThis.performance,
  globalThis.requestAnimationFrame, globalThis.fetch,
);
const globe = globalThis.window.DSHGlobe;
if (!globe || !globe.__debug) {
  console.error("✗ globe3d.js 没有导出 window.DSHGlobe.__debug");
  process.exit(1);
}
const dbg = globe.__debug;
const esc = () => (globalThis.__docListeners.keydown || []).forEach((fn) => fn({ key: "Escape" }));

const fails = [];
const check = (name, cond, extra = "") => {
  if (cond) console.log(`  ✓ ${name}${extra ? " — " + extra : ""}`);
  else { console.log(`  ✗ ${name}${extra ? " — " + extra : ""}`); fails.push(name); }
};

console.log("地球模式冒烟测试（假 globe.gl）");

/* 真实 globe.gl 里的方法名：检查「调用的方法真的存在」。
   曾经的坑：写了 .pathAltitude()（真实 API 里没有，只有 pathPointAlt），整条配置链抛错，
   假球却什么都能接受，于是「地球压根没建出来」这种事故一路滑到线上。
   现在两头夹：源码里链上用到的方法名要能在 vendor 包里找到，运行时也要在真球上存在。 */
{
  const bundle = path.join(ROOT, "web", "vendor", "globe.gl.min.js");
  if (fs.existsSync(bundle)) {
    const src = fs.readFileSync(bundle, "utf8");
    const src3d = fs.readFileSync(path.join(ROOT, "web", "globe3d.js"), "utf8");
    const from = src3d.indexOf("window.Globe()(host)");
    const to = src3d.indexOf(".onPointClick(", from);
    const chain = src3d.slice(from, to > from ? to : from + 2000);
    const called = [...new Set([...chain.matchAll(/\.([a-z][A-Za-z0-9]*)\(/g)].map((m) => m[1]))];
    const missing = called.filter((m) => !src.includes(m + ":"));
    check("建球链上用到的方法名在 globe.gl 里找得到", missing.length === 0,
      missing.length ? "找不到: " + missing.join("、") : `${called.length} 个方法名核对通过`);
  }
}

dbg.bind();
await dbg.loadWorld();
const g = dbg.globe;
check("世界数据加载成功", dbg.state.ready === true && dbg.state.data.cities.length === catalog.cities.length,
  `${dbg.state.data ? dbg.state.data.cities.length : 0} 城`);
check("地球建立并推送点位", !!g && g._points.length === catalog.cities.length + payload.records.length,
  `${g ? g._points.length : 0} 个点 = 城市 ${catalog.cities.length} + 世界之最 ${payload.records.length}`);
check("建球没抛错（页面信息条不该出现「初始化失败」）",
  !dbg.state.globeError && !/初始化失败/.test(document.getElementById("globe-info").textContent),
  dbg.state.globeError || "无错误");
check("链上用到的方法在球实例上都存在", Array.isArray(g && g._missingApi) && g._missingApi.length === 0,
  g && g._missingApi && g._missingApi.length ? "缺失: " + g._missingApi.join("、") : "全部存在");
check("世界之最用脉冲光圈标出", !!g && g._rings.length === payload.records.length, `${g ? g._rings.length : 0} 圈`);
check("环线列表渲染", document.getElementById("globe-loops").children.length === catalog.loops.length,
  `${document.getElementById("globe-loops").children.length} 条`);
check("环线下拉框填充", document.getElementById("globe-loop-select").children.length === catalog.loops.length + 1);
check("世界之最列表 + 筛选", document.getElementById("globe-records").children.length === catalog.records.length,
  `${document.getElementById("globe-records").children.length} 条`);
// 一进来（还没选环线）就该铺满全世界的城市卡
check("打开页签就能看到全世界城市卡片（不用先点环线）",
  dbg.state.cardList.length === catalog.cities.length && dbg.state.cards.size === catalog.cities.length,
  `${dbg.state.cardList.length} 张`);
check("世界之最卡片也就位", dbg.state.recordEls.length === catalog.records.length,
  `${dbg.state.recordEls.length} 张`);
pump();
check("初次进入就有卡片被定位（不是等交互才出现）",
  dbg.visibleCards().length > 0,
  `可见 ${dbg.visibleCards().length}/${dbg.state.cardList.length}`);

const loop = payload.loops.find((l) => l.id === "europe_west") || payload.loops[0];
dbg.selectLoop(loop);
check("点环线→铺开该环线城市卡片", dbg.state.cardList.length === loop.cities.length,
  `${dbg.state.cardList.length}/${loop.cities.length} 张`);
check("点环线→画出路线", dbg.globe._paths.length >= loop.cities.length - 1,
  `${dbg.globe._paths.length} 段`);
dbg.selectLoop(loop);                       // 让机位重新飞到环线中心
check("点环线→相机飞过去", !!dbg.globe._pov && typeof dbg.globe._pov.altitude === "number"
  && typeof dbg.globe._pov.lat === "number" && typeof dbg.globe._pov.lng === "number",
  JSON.stringify(dbg.globe._pov) + ` 时长 ${dbg.globe._povDur}ms`);
// 镜头应当落在环线城市的中心附近（包围盒中心，跨 ±180° 也成立）
{
  const cs = loop.cities.map((n) => payload.cities.find((c) => c.name === n)).filter(Boolean);
  const win = dbg.lonWindow(cs.map((c) => c.lon));
  let lonMid = (win.min + win.max) / 2;
  lonMid = ((((lonMid + 180) % 360) + 360) % 360) - 180;
  const latMid = (Math.min(...cs.map((c) => c.lat)) + Math.max(...cs.map((c) => c.lat))) / 2;
  const dLat = Math.abs(dbg.globe._pov.lat - latMid);
  const dLon = Math.abs(((dbg.globe._pov.lng - lonMid + 540) % 360) - 180);
  check("环线视角落在城市中心", dLat < 1.5 && dLon < 1.5,
    `机位 ${dbg.globe._pov.lat.toFixed(2)},${dbg.globe._pov.lng.toFixed(2)} vs 中心 ${latMid.toFixed(2)},${lonMid.toFixed(2)}`);
  check("环线视角高度按跨度取值", dbg.globe._pov.altitude >= 0.8 && dbg.globe._pov.altitude <= 2.4,
    "altitude=" + dbg.globe._pov.altitude.toFixed(2));
}
// 相机已经在环线上方：正面判定应该成立（曾经因为球面坐标 z 符号写反而整体判反）
{
  const cs = loop.cities.map((n) => payload.cities.find((c) => c.name === n)).filter(Boolean);
  const front = dbg.frontNames(cs);
  check("环线城市被判定为正面", front.length === cs.length,
    `${front.length}/${cs.length} 正面`);
}
dbg.setAutoRotate(true);
dbg.selectLoop(loop);                       // 让机位重新飞到环线中心（上面的自转开关不动机位）
check("相机飞行会停下自转", dbg.globe.controls().autoRotate === false
  && document.getElementById("globe-auto").textContent === "开始自转",
  `autoRotate=${dbg.globe.controls().autoRotate} 按钮="${document.getElementById("globe-auto").textContent}"`);
pump();
const card = dbg.state.cardList[0];
check("卡片定位已写入", /translate3d\(/.test(card.el.style.transform || ""),
  `transform="${card.el.style.transform}" display="${card.el.style.display}" ` +
  `lat=${card.city.lat} lon=${card.city.lon}`);
check("每张卡片的经纬度都有效（曾经前端读 lng、接口给 lon → 全部 undefined）",
  dbg.state.cardList.every((it) => isFinite(it.city.lat) && isFinite(it.city.lon)));
{
  const cen = dbg.fitView(loop.cities.map((n) => payload.cities.find((c) => c.name === n)).filter(Boolean));
  // 与机位方向相隔 90° 以上的城市必定在地球背面（正反面判据就是这个半球）
  const angleTo = (c) => {
    const p1 = (c.lat * Math.PI) / 180, p2 = (cen.lat * Math.PI) / 180;
    const dl = ((cen.lng - c.lon) * Math.PI) / 180;
    return (Math.acos(Math.max(-1, Math.min(1,
      Math.sin(p1) * Math.sin(p2) + Math.cos(p1) * Math.cos(p2) * Math.cos(dl))))) * 180 / Math.PI;
  };
  const far = payload.cities.find((c) => angleTo(c) > 95);
  if (far) {
    check("背面的城市判为背面（曾经正反面反了：正面卡片被藏起来）",
      dbg.isFront(far.lat, far.lon) === false,
      `${far.name} 与环线中心相隔 ${angleTo(far).toFixed(0)}°`);
  } else {
    check("背面的城市判为背面（跳过：数据里没有 90° 以外的城市）", false, "样本缺失");
  }
}
// 卡片只给环线里的 10 座城市，镜头正对欧洲：应当全部可见
check("正面卡片可见（背面城市已被滤掉）",
  dbg.visibleCards().length === dbg.state.cardList.length && dbg.visibleCards().length > 0,
  `可见 ${dbg.visibleCards().length}/${dbg.state.cardList.length}`);
// 三栏布局下地球只占中间那一栏：卡片必须落在这个窄框里，别跑到左右面板底下
{
  const viz = document.getElementById("globe-viz");
  const W = 792, H = 600;
  viz.clientWidth = W; viz.clientHeight = H;
  viz.getBoundingClientRect = () => ({ width: W, height: H, left: 324, top: 12, right: 324 + W, bottom: 12 + H });
  pump();
  const shown = dbg.state.cardList.filter((it) => it.el.style.display !== "none");
  const xs = shown.map((it) => parseFloat((String(it.el.style.transform).match(/translate3d\(([-0-9.]+)px/) || [0, 0])[1]));
  check("地球只占中间一栏时卡片仍在框内",
    shown.length > 0 && Math.min(...xs) > -140 && Math.max(...xs) < W + 140,
    `可见 ${shown.length} 张，x ${Math.min(...xs).toFixed(0)}..${Math.max(...xs).toFixed(0)}（框宽 ${W}）`);
}
// 把镜头转到地球另一侧，同一批卡片应当全部藏起来
dbg.selectLoop(loop, { detail: false });
{
  const cs = loop.cities.map((n) => payload.cities.find((c) => c.name === n)).filter(Boolean);
  const win = dbg.lonWindow(cs.map((c) => c.lon));
  const opp = ((win.min + win.max) / 2 + 180 + 540) % 360 - 180;
  dbg.globe.pointOfView({ lat: -cs[0].lat, lng: opp, altitude: 1.1 }, 0);
  pump();
  check("镜头转到对侧后卡片全部隐藏",
    dbg.visibleCards().length === 0 && dbg.state.cardList.length > 0,
    `可见 ${dbg.visibleCards().length}/${dbg.state.cardList.length}，机位 ${opp.toFixed(1)}°`);
}

dbg.toggleCity(loop.cities[0]);
dbg.toggleCity(loop.cities[1]);
check("点城市→编号入列", dbg.state.active.length === 2, dbg.state.active.join(" → "));
check("选中城市已画线", dbg.globe._paths.some((p) => p.color === "#ffd36b"), `${dbg.globe._paths.length} 段`);
check("右侧列表同步", /picked-item|已选/.test(document.getElementById("globe-picked-list").innerHTML)
  || document.getElementById("globe-picked-count").textContent === "2",
  "count=" + document.getElementById("globe-picked-count").textContent);
dbg.toggleCity(loop.cities[0]);
check("再点一次可移出", dbg.state.active.length === 1);

const rec = payload.records[0];
const povBefore = JSON.stringify(dbg.globe._pov);
dbg.focusRecord(rec);
await new Promise((r) => setTimeout(r, 10));
check("点世界之最→飞过去并记状态", dbg.state.activeRecord === rec.id && !!dbg.globe._pov,
  `${rec.name} @ ${rec.lat},${rec.lon}`);
check("世界之最先把镜头飞过去（不是只弹介绍）",
  JSON.stringify(dbg.globe._pov) !== povBefore
  && Math.abs(dbg.globe._pov.lat - rec.lat) < 0.01
  && Math.abs(dbg.globe._pov.lng - (rec.lon != null ? rec.lon : rec.lng)) < 0.01,
  `机位 ${dbg.globe._pov.lat},${dbg.globe._pov.lng} vs ${rec.name} ${rec.lat},${rec.lon}`);
check("世界之最详情弹层打开", document.getElementById("globe-guide").hidden === false
  && /怎么去/.test(document.getElementById("gg-panel").innerHTML));
check("世界之最卡片带有效坐标", dbg.state.recordEls.every((it) => isFinite(it.rec.lat)
  && isFinite(it.rec.lon != null ? it.rec.lon : it.rec.lng)),
  `${dbg.state.recordEls.length} 张世界之最卡片`);
check("定位后对应卡片有高亮（不然看不出飞到哪儿了）",
  dbg.state.recordEls.some((it) => it.el.classList.contains("globe-focus")),
  `高亮 ${dbg.state.recordEls.filter((it) => it.el.classList.contains("globe-focus")).length} 张`);

const gid = catalog.cities[0].name;
await dbg.openCityGuide(gid);
const guideHtml = document.getElementById("gg-panel").innerHTML;
check("城市攻略弹层可打开", document.getElementById("globe-guide").hidden === false
  && /完整攻略/.test(guideHtml) && /半日路线/.test(guideHtml), `${guideHtml.length} 字`);
dbg.closeGuide();
check("攻略弹层可关闭", document.getElementById("globe-guide").hidden === true);

// Esc 逐层退出：弹层 → 世界之最 → 环线 → 已选城市 → 全球视角
dbg.closeGuide();          // 上面刚开过城市攻略
esc();
check("Esc 先关掉弹层", document.getElementById("globe-guide").hidden === true);
esc();
check("Esc 再退出世界之最高亮", !dbg.state.activeRecord);
// 此刻弹层已关、世界之最已取消，接下来的 Esc 才轮到环线与已选城市
dbg.state.active = [];        // 前面测试留下的选择先清掉
dbg.selectLoop(loop);
dbg.toggleCity(loop.cities[0]);
esc();
check("Esc 退出环线高亮", !dbg.state.activeLoop, "环线已取消高亮");
check("退出环线后恢复全世界的城市卡", dbg.state.cardList.length === catalog.cities.length,
  `${dbg.state.cardList.length} 张`);
check("Esc 不会顺手清空已选城市", dbg.state.active.length === 1);
esc();
check("Esc 清空已选城市", dbg.state.active.length === 0);
const homePov = JSON.stringify(dbg.globe._pov);
esc();
check("Esc 回到全球视角", JSON.stringify(dbg.globe._pov) !== homePov
  && dbg.globe._pov.altitude >= 2, JSON.stringify(dbg.globe._pov));

// 全屏：只留地球，左右两栏由 CSS（.globe-stage.full + :has）让位
document.getElementById("globe-full").fire("click", {});
check("全屏按钮进入全屏态", dbg.isFull()
  && document.getElementById("globe-stage").classList.contains("full")
  && document.body.classList.contains("map-locked")
  && document.getElementById("globe-full").textContent === "⛶ 退出全屏",
  `isFull=${dbg.isFull()} 按钮="${document.getElementById("globe-full").textContent}"`);
esc();
check("Esc 退出全屏（不误清空其他状态）", !dbg.isFull()
  && document.getElementById("globe-full").textContent === "⛶ 全屏");

dbg.state.active = [catalog.cities[0].name, catalog.cities[1].name, catalog.cities[2].name];
dbg.renderPicked();
await dbg.planWorld();
const planHtml = document.getElementById("globe-plan").innerHTML;
check("生成世界行程单", planCalls === 1 && /公里/.test(planHtml) && /预计飞行/.test(planHtml)
  && /时差/.test(planHtml), `${planHtml.length} 字`);
check("行程单明确说明不含票价", /机票必须实时查询|不含航班时刻与票价/.test(planHtml));

const before = dbg.globe.controls().autoRotate;
document.getElementById("globe-auto").fire("click", {});
check("自转开关仍可用", dbg.globe.controls().autoRotate === !before);

/* 「显示/隐藏图标」：城市卡与世界之最数值卡都在 #globe-cards 这一层，收起来只剩球与航线 */
{
  const layer = document.getElementById("globe-cards");
  const btn = document.getElementById("globe-icons-toggle");
  const cardsBefore = dbg.state.cardList.length;
  // 读假球内部的数组：`pointsData()` 是 setter，不带参数调用返回 undefined ——
  // 之前这里写成 pointsData().length，两边都是 undefined 于是「假通过」（报告体检抓出来的）。
  const pointsBefore = dbg.globe._points.length;
  dbg.setIcons(false);
  check("隐藏图标：卡片浮层收起来了", layer.hidden === true);
  check("隐藏图标：按钮文案换成「显示图标」", btn.textContent.trim() === "显示图标", btn.textContent.trim());
  check("隐藏图标：球上的点位没被一起清掉（藏的是浮层）",
    pointsBefore > 0 && dbg.globe._points.length === pointsBefore
    && dbg.state.cardList.length === cardsBefore,
    `点位 ${pointsBefore} · 卡片 ${cardsBefore}`);
  dbg.setIcons(true);
  check("再点回来：卡片恢复显示且按钮文案复原",
    layer.hidden === false && btn.textContent.trim() === "隐藏图标");
}

console.log(fails.length ? `\n✗ ${fails.length} 项失败：${fails.join("、")}` : "\n✓ 全部通过");
process.exit(fails.length ? 1 : 0);
