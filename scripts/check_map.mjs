/* 地图漫游前端无头冒烟测试（可选开发工具，需要 Node 18+）
 *
 * web/map.js 现在用 three.js 渲染，这里用最小 DOM 桩 + 真实 three.js 把地图跑一遍：
 * 装载 34 个省级轮廓 → 建场景（省/河流/湖泊/山脉/环线）→ 选大区 → 点环线 → 排路线，
 * 任何抛错都会让脚本以非 0 退出。（无头环境没有 WebGL，只验证几何、相机与 UI 逻辑。）
 *
 *   node scripts/check_map.mjs
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const MAP_JS = path.join(ROOT, "web", "map.js");
const VENDOR = path.join(ROOT, "web", "vendor", "three.module.min.js");
const GEO = path.join(ROOT, "web", "data", "china.geo.json");
const NATURE = path.join(ROOT, "web", "data", "china.nature.json");
const DATA = path.join(ROOT, "src", "travel_planner", "data");

const read = (p) => JSON.parse(fs.readFileSync(p, "utf8"));

/* ---------------- 最小 DOM 桩 ---------------- */
function makeCtx() {
  const gradient = { addColorStop() {} };
  return new Proxy({
    createLinearGradient: () => gradient,
    createRadialGradient: () => gradient,
    isPointInPath: () => false,
    setLineDash() {},
    measureText: () => ({ width: 10 }),
    getImageData: () => ({ data: new Uint8ClampedArray(4) }),
  }, {
    get(target, prop) { return prop in target ? target[prop] : () => {}; },
    set() { return true; },
  });
}

function makeEl(tag = "div", id = "") {
  const selCache = new Map();
  const el = {
    tagName: tag.toUpperCase(), id, hidden: false, offsetParent: {}, offsetWidth: 100, offsetHeight: 116,
    width: 0, height: 0, children: [], dataset: {}, value: "", _html: "", _text: "",
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
    removeEventListener(type, fn) {
      const list = this._listeners && this._listeners[type];
      if (list) this._listeners[type] = list.filter((f) => f !== fn);
    },
    /** 测试用：手动派发一个事件给已注册的监听器 */
    fire(type, event) {
      const list = (this._listeners && this._listeners[type]) || [];
      list.forEach((fn) => fn(event));
      return list.length;
    },
    appendChild(c) {
      this.children.push(c);
      register(c);
      if (c && c._html) this._html += c._html;        // 让 innerHTML 断言和浏览器一致
      return c;
    },
    insertBefore(c) {
      this.children.unshift(c);
      register(c);
      if (c && c._html) this._html = c._html + this._html;
      return c;
    },
    remove() { if (this.id) registry.delete(this.id); },
    setAttribute() {}, getAttribute() { return null; },
    querySelector(sel) {
      if (!selCache.has(sel)) selCache.set(sel, makeEl("div"));
      return selCache.get(sel);
    },
    querySelectorAll() { return []; },
    closest() { return null; },
    scrollIntoView() {},
    getBoundingClientRect: () => ({ width: 1200, height: 700, left: 0, top: 0, right: 1200, bottom: 700 }),
    parentElement: { getBoundingClientRect: () => ({ width: 1200, height: 700, left: 0, top: 0, right: 1200, bottom: 700 }) },
    getContext: () => makeCtx(),
    toDataURL: () => "data:,",
  };
  return el;
}

const registry = new Map();
const register = (child) => {
  if (child && child.id) registry.set(child.id, child);
  return child;
};
const htmlText = fs.readFileSync(path.join(ROOT, "web", "index.html"), "utf8");
const htmlIds = new Set([...htmlText.matchAll(/id="([A-Za-z0-9_-]+)"/g)].map((m) => m[1]));
// 带 hidden 属性的元素（如攻略弹层）初始就是隐藏的，和浏览器一致
const hiddenIds = new Set();
for (const m of htmlText.matchAll(/<[^>]*id="([A-Za-z0-9_-]+)"[^>]*>/g)) {
  if (/\shidden(\s|>|=)/.test(m[0])) hiddenIds.add(m[1]);
}
globalThis.document = {
  readyState: "complete",
  getElementById: (id) => {
    if (!htmlIds.has(id)) return registry.get(id) || null;
    if (!registry.has(id)) {
      const el = makeEl(id === "map-canvas" ? "canvas" : "div", id);
      el.hidden = hiddenIds.has(id);
      registry.set(id, el);
    }
    return registry.get(id);
  },
  createElement: (tag) => makeEl(tag),
  createElementNS: (_ns, tag) => makeEl(tag),
  createDocumentFragment: () => makeEl("fragment"),
  addEventListener() {},
  querySelectorAll: () => [],
  body: makeEl("body"),
};
globalThis.window = { devicePixelRatio: 1, innerWidth: 1440, addEventListener() {} };
let fakeNow = 0;
const rafQueue = [];
globalThis.performance = { now: () => fakeNow };
globalThis.requestAnimationFrame = (cb) => { rafQueue.push(cb); return rafQueue.length; };
function pump(frames = 600, dt = 16) {
  let n = 0;
  while (rafQueue.length && n < frames) {
    const cb = rafQueue.shift();
    fakeNow += dt;
    cb(fakeNow);
    n++;
  }
  return n;
}
/* fetch 走可替换转发，方便模拟「服务端还是旧版本」这类故障 */
let fetchImpl = () => Promise.reject(new Error("无头模式不联网"));
globalThis.fetch = (...args) => fetchImpl(...args);

/* ---------------- 以模块方式载入 web/map.js（把 three 的 URL 换成文件路径） ---------------- */
const vendorUrl = pathToFileURL(VENDOR).href;
const code = fs.readFileSync(MAP_JS, "utf8")
  .replace('"/static/vendor/three.module.min.js"', JSON.stringify(vendorUrl));
if (!code.includes(vendorUrl)) {
  console.error("✗ 没能在 map.js 里找到 three.js 的 import 路径，测试需要同步更新");
  process.exit(1);
}
const tmpFile = path.join(os.tmpdir(), `dsh-map-check-${process.pid}.mjs`);
fs.writeFileSync(tmpFile, code, "utf8");
await import(pathToFileURL(tmpFile).href);
fs.unlinkSync(tmpFile);

const map = globalThis.window.DSHTravelMap;
if (!map || !map.__debug) {
  console.error("✗ map.js 没有导出 window.DSHTravelMap.__debug");
  process.exit(1);
}

const geo = read(GEO);
const nature = read(NATURE);
const regions = read(path.join(DATA, "regions.json")).regions;
const catalog = read(path.join(DATA, "city_catalog.json")).cities;
const loops = read(path.join(DATA, "loop_presets.json")).loops;
const photoManifestPath = path.join(ROOT, "web", "img", "cities", "photos.json");
const photos = fs.existsSync(photoManifestPath) ? read(photoManifestPath) : {};
const cityIndex = new Map(catalog.map((c) => [c.name, c]));

/** 复刻服务端 /api/roam/map 的组装方式（含夜景照选择） */
function cityCard(c) {
  const p = photos[c.name] || {};
  const q = encodeURIComponent(c.name);
  const day = p.day ? `/static/img/cities/${q}.jpg` : null;
  const night = p.night ? `/static/img/cities/${q}@night.jpg` : null;
  return Object.assign({}, c, {
    photo: (c.night ? night : day) || day || night,
    photo_day: day,
    photo_night: night,
    has_guide: !!c.guide,
    guide_days: c.guide ? c.guide.days : null,
  });
}

const roamPayload = {
  ok: true,
  regions: regions.map((r) => {
    const items = catalog.filter((c) => c.region === r.name);
    return {
      id: r.id, name: r.name, subtitle: r.subtitle, blurb: r.blurb,
      provinces: r.provinces, color: r.color, themes: r.themes,
      city_count: items.length, rail_count: items.filter((c) => c.rail).length,
      top: items.slice(0, 4).map((c) => c.name),
    };
  }),
  cities: catalog.map(cityCard),
  loops: loops.map((l) => {
    const stops = (l.cities || []).map((n) => cityIndex.get(n)).filter(Boolean)
      .map((c) => ({
        name: c.name, plan_name: c.plan_name, province: c.province, region: c.region,
        lon: c.lon, lat: c.lat, score: c.score, rail: c.rail,
      }));
    const lons = stops.map((s) => s.lon), lats = stops.map((s) => s.lat);
    return {
      id: l.id, name: l.name, subtitle: l.subtitle, blurb: l.blurb, tips: l.tips || [],
      days: l.days, season: l.season, plan_mode: l.plan_mode || "rail",
      cities: stops.map((s) => s.name), plan_names: stops.map((s) => s.plan_name),
      regions: [...new Set(stops.map((s) => s.region))],
      cross_region: new Set(stops.map((s) => s.region)).size > 1,
      bounds: [Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)],
      missing: [], stops,
    };
  }),
  stats: {},
};

/* ---------------- 断言 ---------------- */
const fails = [];
const check = (name, cond, extra = "") => {
  if (cond) console.log(`  ✓ ${name}${extra ? " — " + extra : ""}`);
  else { console.log(`  ✗ ${name}${extra ? " — " + extra : ""}`); fails.push(name); }
};

console.log("地图前端冒烟测试（three.js 场景）");
let boot;
try {
  boot = map.__debug.boot(geo, roamPayload, nature);
} catch (err) {
  console.error("✗ boot 抛错：", err);
  process.exit(1);
}
check("省级轮廓全部装载", boot.provinces === geo.provinces.length, `${boot.provinces} 个省`);
check("城市卡数据装载", boot.cities === catalog.length, `${boot.cities} 城`);
check("环线装载", boot.loops === loops.length, `${boot.loops} 条`);
check("环线标签生成", boot.loopLabels >= loops.length - 2, `${boot.loopLabels} 个标签`);
check("全国视角精选城市卡", boot.nationalCards === 18, `${boot.nationalCards} 张`);
check("山川湖河装载",
  !!boot.nature && boot.nature.rivers > 10 && boot.nature.lakes > 8 && boot.nature.ranges > 10,
  boot.nature ? `河 ${boot.nature.rivers} · 湖 ${boot.nature.lakes} · 山 ${boot.nature.ranges}` : "无");
check("场景网格已建立",
  boot.meshes >= boot.provinces + boot.nature.lakes && boot.verts > 20000,
  `网格 ${boot.meshes} 个 / 顶点 ${boot.verts}`);
check("山脉用实例化椎体", boot.instanced > 200, `${boot.instanced} 座`);
check("环线与省界已绘制", boot.lines >= loops.length + boot.provinces,
  `${boot.lines} 条线（含 ${loops.length} 条环线）`);
check("无头环境无 WebGL（应优雅降级）", boot.webgl === false);

const dbg = map.__debug;

/* 选大区：卡片数量应等于该大区城市数，并且相机真的飞过去了 */
for (const region of ["江南", "东北", "青藏", "港澳台"]) {
  const expected = catalog.filter((c) => c.region === region).length;
  const before = dbg.cam.dist;
  dbg.selectRegion(dbg.state.data.regions.find((r) => r.name === region));
  pump();
  const got = dbg.state.cardList.length;
  check(`选「${region}」弹出城市卡片`, got === expected, `${got}/${expected}`);
  check(`选「${region}」视角已飞入`, Math.abs(dbg.cam.dist - before) > 1,
    `距离 ${Math.round(before)} → ${Math.round(dbg.cam.dist)} km`);
  const photoEl = dbg.state.cardList[0].el.querySelector(".cc-photo");
  const img = photoEl.style.backgroundImage || "";
  check(`「${region}」卡片有配图`, /^url\("(data:image\/svg\+xml|\/static\/img\/cities)/.test(img),
    img.slice(0, 46));
  check(`「${region}」卡片没有污染 HTML 属性`,
    !/style="background-image/.test(dbg.state.cardList[0].el.innerHTML));
}

/* 环线：跨大区环线应先退到全国，再落到环线范围 */
const cross = roamPayload.loops.find((l) => l.cross_region);
if (cross) {
  const path = [];
  const startDist = dbg.cam.dist;
  dbg.focusLoop(cross);
  let frames = 0;
  while (rafQueue.length && frames < 600) {
    const cb = rafQueue.shift();
    fakeNow += 16; frames++;
    cb(fakeNow);
    path.push(Math.round(dbg.cam.dist));
  }
  check(`跨大区环线「${cross.name}」可聚焦`, dbg.state.cardList.length === cross.stops.length,
    `${dbg.state.cardList.length}/${cross.stops.length} 站`);
  const maxDist = Math.max(...path);
  const endDist = path[path.length - 1];
  check("跨大区视角先退到全国再落回环线", maxDist > endDist * 1.3,
    `最远 ${maxDist} km → 落点 ${endDist} km（起点 ${Math.round(startDist)} km）`);
}

/* 缩放健壮性：贴地放大 / 拉到最远都必须还在（相机防钻地） */
for (const d of [3000, 900, 320, 13000]) {
  try {
    dbg.cam.dist = d;
    dbg.layoutCards();
    check(`距离 ${d} km 仍能工作`, true, `相机高度 ${Math.round(d * Math.sin(dbg.cam.pitch * Math.PI / 180))} km`);
  } catch (err) {
    check(`距离 ${d} km 仍能工作`, false, String(err));
  }
}

/* 地面拾取与光标缩放 */
const gp = dbg.groundPoint(600, 380);
check("屏幕像素可反算地面点", !!gp && Number.isFinite(gp[0]) && Number.isFinite(gp[1]),
  gp ? `(${Math.round(gp[0])},${Math.round(gp[1])}) km` : "null");
const gpBefore = dbg.groundPoint(600, 380);
dbg.zoomAt(600, 380, 0.5);
const gpAfter = dbg.groundPoint(600, 380);
const drift = gpBefore && gpAfter ? Math.hypot(gpBefore[0] - gpAfter[0], gpBefore[1] - gpAfter[1]) : 1e9;
check("光标缩放锚定光标下的地面", drift < 60, `漂移 ${Math.round(drift)} km`);

/* 选择 + Esc 逐层退出 */
const crossLoop = cross || roamPayload.loops[0];
dbg.selectRegion(roamPayload.regions.find((r) => r.name === "江南"));
dbg.toggleCity("杭州");
dbg.toggleCity("绍兴");
check("点选城市记入序列", dbg.state.selected.length === 2, `${dbg.state.selected.length} 站`);
check("选中后画出金色光带", !!dbg.state.routeCurve && dbg.state.routePulses.length > 0,
  `光带控制点 ${dbg.state.routeCurve ? dbg.state.routeCurve.points.length : 0} · 流动光点 ${dbg.state.routePulses.length}`);
{
  let tubes = 0, rings = 0, maxRadius = 0;
  dbg.state.routeGroup.traverse((o) => {
    if (!o.isMesh) return;
    const type = o.geometry.type;
    if (type === "TubeGeometry") {
      tubes++;
      maxRadius = Math.max(maxRadius, o.geometry.parameters.radius);
    } else if (type === "TorusGeometry") rings++;
  });
  check("光带是实体管道（不再是 1px 细线）", tubes >= 2 && maxRadius > 3,
    `${tubes} 根管 / 最粗半径 ${maxRadius.toFixed(1)} km`);
  check("每个选中城市一个光环", rings === 2, `${rings} 个`);
}
dbg.toggleCity("杭州");
check("再点一次可取消选中", dbg.state.selected.length === 1, `${dbg.state.selected.length} 站`);
dbg.toggleCity("杭州");
dbg.focusLoop(crossLoop);
pump();
dbg.toggleFullscreen(true);
const steps = [];
for (let i = 0; i < 7; i++) {
  const msg = dbg.escapeAction();
  if (!msg) break;
  steps.push(msg);
}
check("Esc 逐层退出链路", steps.length === 5 && dbg.state.selected.length === 0 && !dbg.state.region,
  steps.join(" → "));

/* 夜景照：适合夜游的城市用 @night 图 */
{
  const nightCity = catalog.find((c) => c.night && c.name === "上海") || catalog.find((c) => c.night);
  const dayCity = catalog.find((c) => !c.night);
  dbg.buildCards([cityCard(nightCity), cityCard(dayCity)]);
  const nightImg = dbg.state.cardList[0].el.querySelector(".cc-photo").style.backgroundImage;
  const dayImg = dbg.state.cardList[1].el.querySelector(".cc-photo").style.backgroundImage;
  check("夜游城市用夜景照", /@night\.jpg/.test(nightImg), nightImg.slice(0, 52));
  check("白天城市用白天照", /\.jpg/.test(dayImg) && !/night/.test(dayImg), dayImg.slice(0, 52));
  check("夜游城市卡片带月亮标记", /cc-moon/.test(dbg.state.cardList[0].el.innerHTML));
}

/* 完整攻略：内联展开 + 弹层 */
{
  const full = read(path.join(DATA, "city_catalog.json"));
  const sample = full.cities.find((c) => c.name === "杭州") || full.cities[0];
  const guide = sample.guide;
  check("目录里带完整攻略数据", !!guide && Array.isArray(guide.itinerary) && guide.itinerary.length > 0,
    guide ? `${guide.days} 天 / ${guide.itinerary.length} 段` : "无");
  const html = dbg.guideBlockHtml(guide);
  check("攻略正文含逐日行程与实用信息",
    /完整旅游攻略/.test(html) && /gg-day/.test(html) && /预算|交通/.test(html), `${html.length} 字`);
  fetchImpl = () => Promise.resolve({
    ok: true, status: 200,
    json: async () => ({
      ok: true, guide,
      city: Object.assign({}, sample, { photo: "/static/img/cities/x.jpg" }),
    }),
  });
  await dbg.openGuide(sample.name);
  const panel = document.getElementById("cg-panel").innerHTML;
  check("点「更多攻略」能打开完整攻略层",
    /cg-hero/.test(panel) && /cg-day/.test(panel) && /实用信息/.test(panel), `${panel.length} 字`);
  dbg.closeGuide();
  check("攻略层可关闭", document.getElementById("city-guide").hidden === true);
  fetchImpl = () => Promise.reject(new Error("无头模式不联网"));
}

/* 卡片定位 */
pump();
dbg.layoutCards();
const card = dbg.state.cardList[0];
check("卡片 transform 已写入", /translate3d\(/.test(card.el.style.transform || ""), card.el.style.transform);

/* 出行方式切换：公共交通 / 自驾 / 租车 */
{
  const tab = document.getElementById("tab-map");
  dbg.toggleCity("成都");
  dbg.toggleCity("乐山");
  dbg.setTravelMode("drive");
  check("切到自驾模式", dbg.state.travelMode === "drive" && tab.classList.contains("mode-drive"));
  check("自驾模式按钮文案", document.getElementById("m-go").textContent.includes("自驾"));
  check("自驾模式提示", /高德驾车/.test(document.getElementById("travel-mode-hint").textContent || ""));
  check("自驾模式光带换色", (() => {
    const core = [];
    dbg.state.routeGroup.traverse((o) => {
      if (o.isMesh && o.geometry.type === "TubeGeometry") core.push(o.material.color.getHex());
    });
    return core.length > 0 && core.every((c) => (c >> 16) < 0xe0 && (c & 0xff) > 0xd0);
  })(), (() => {
    const core = [];
    dbg.state.routeGroup.traverse((o) => {
      if (o.isMesh && o.geometry.type === "TubeGeometry") core.push(o.material.color.getHexString());
    });
    return core.join(",");
  })());

  /* 自驾结果渲染（用假响应） */
  const driveRes = {
    ok: true, kind: "drive", date: "2026-06-17", closed: true,
    order_cities: ["成都", "乐山"], plan_order: ["成都", "乐山", "成都"],
    total_days: 5,
    summary: { city_count: 2, total_days: 5, total_km: 320, drive_hours: 4.2, tolls: 130,
               fuel: 200, regions: ["西南"], estimated: false, date: "2026-06-17" },
    legs: [{ day_no: 1, date: "2026-06-17", frm: "成都", to: "乐山", stay: "住2晚",
             drive: { distance_km: 140, minutes: 105, break_minutes: 0, total_minutes: 105,
                      hours_text: "1小时45分", total_text: "1小时45分", dep_text: "08:00",
                      arr_text: "09:45", tolls: 50, fuel: 87, source: "高德驾车", note: "" } }],
    cities: [cityCard(catalog.find((c) => c.name === "成都")), cityCard(catalog.find((c) => c.name === "乐山"))],
    warnings: [], start_notes: [],
    rental: { ok: true, pickup: "成都", dropoff: "成都", one_way: false, one_way_km: 0, one_way_fee: 0,
              tier: "B", tier_label: "省会与热门城市", car_class: "SUV", car_options: ["经济型", "SUV", "商务"],
              days: 5, daily_low: 180, daily_high: 320, insurance_per_day: [30, 80],
              rent_only_low: 1050, rent_only_high: 2000, fuel: 200, tolls: 130,
              running_low: 330, running_high: 380, total_low: 1380, total_high: 2380, km: 320,
              fuel_price: 7.8, consumption: 8, platforms: [{ name: "神州租车", url: "https://www.zuche.com/", note: "网点多" }],
              disclaimer: "估算" },
  };
  dbg.state.travelMode = "rental";
  dbg.renderDrive(driveRes);
  const results = document.getElementById("map-results").innerHTML;
  const rental = document.getElementById("map-rental").innerHTML;
  check("自驾结果含车程与费用", /140 公里/.test(results) && /过路费约 ¥50/.test(results) && /油费约 ¥87/.test(results));
  check("租车面板给出费用区间与平台入口",
    /¥1380 - 2380|1380/.test(rental) && /神州租车/.test(rental) && /日租/.test(rental), `${rental.length} 字`);
  check("自驾模式在地图上标出分段里程", dbg.state.legChips.length === 1,
    dbg.state.legChips.map((c) => c.el.textContent).join(" / "));
  dbg.setTravelMode("transit");
  check("切回公共交通模式", dbg.state.travelMode === "transit" && !tab.classList.contains("mode-drive"));
}

/* 滚轮：面板上滚动说明，不抢地图；地图上仍然缩放 */
{
  const stage = document.getElementById("map-stage");
  const panel = document.getElementById("map-loops-panel");
  const before = dbg.cam.dist;
  let prevented = false;
  stage.fire("wheel", {
    target: { closest: (sel) => (String(sel).includes("map-panel") ? panel : null) },
    preventDefault: () => { prevented = true; },
    clientX: 120, clientY: 300, deltaY: 120, deltaX: 0, deltaMode: 0,
  });
  check("在说明面板上滚动：不抢地图", !prevented && dbg.cam.dist === before,
    `preventDefault=${prevented} dist=${Math.round(dbg.cam.dist)}`);
  stage.fire("wheel", {
    target: { closest: () => null },
    preventDefault: () => { prevented = true; },
    clientX: 600, clientY: 300, deltaY: 120, deltaX: 0, deltaMode: 0,
  });
  check("在地图上滚动：仍然缩放", prevented && dbg.cam.dist !== before,
    `dist ${Math.round(before)} → ${Math.round(dbg.cam.dist)}`);
}

/* 悬停提示卡：可交互（能移上去点「完整攻略」「加入行程」） */
{
  const item = dbg.state.cardList[0];
  item.city.has_guide = true;
  dbg.showTip(item.city, item.el);
  const tip = document.getElementById("map-tip");
  check("提示卡里带操作按钮", /tip-actions/.test(tip.innerHTML) && /看完整攻略/.test(tip.innerHTML)
    && /加入行程|移出行程/.test(tip.innerHTML));
  const before = dbg.state.selected.length;
  tip.fire("click", {
    target: { closest: (sel) => (String(sel).includes("tip-btn") ? { dataset: { act: "pick" } } : null) },
    stopPropagation() {},
  });
  check("点提示卡上的「加入行程」能选中", dbg.state.selected.length === before + 1,
    `${before} → ${dbg.state.selected.length}`);
  // 鼠标从卡片移到提示卡上：不立即消失（有缓冲）
  dbg.hideTipSoon(200);
  tip.fire("mouseenter", {});
  check("鼠标移到提示卡上不会消失", document.getElementById("map-tip").hidden === false);
  dbg.hideTip();
  check("离开后正常收起", document.getElementById("map-tip").hidden === true);
}

/* 舞台里的弹层不能被地图拖拽接管（否则按钮点不动：setPointerCapture 会吞掉 click） */
{
  const stage = document.getElementById("map-stage");
  const cases = [
    ["攻略弹层", ".city-guide"],
    ["弹层面板", ".cg-panel"],
    ["悬停提示卡", ".map-tip"],
    ["错误提示框", ".map-error"],
    ["环线面板", ".map-panel"],
    ["城市卡片", ".city-card"],
  ];
  let bad = [];
  for (const [label, sel] of cases) {
    stage.classList.remove("dragging");
    stage.fire("pointerdown", {
      button: 0, pointerId: 1, clientX: 40, clientY: 40,
      target: { closest: (query) => (String(query).includes(sel) ? { sel } : null) },
    });
    if (stage.classList.contains("dragging")) bad.push(label);
    stage.classList.remove("dragging");
  }
  check("弹层/提示卡里的按下不会触发地图拖拽", bad.length === 0, bad.length ? "被接管：" + bad.join("、") : "6 处均可正常点击");
  // 地图空白处按下仍然可以拖拽
  stage.fire("pointerdown", {
    button: 0, pointerId: 2, clientX: 300, clientY: 300,
    target: { closest: () => null },
  });
  check("地图空白处仍可拖拽", stage.classList.contains("dragging"));
  stage.fire("pointerup", { clientX: 300, clientY: 300, target: { closest: () => null } });
  stage.classList.remove("dragging");
  dbg.hideTip();
}

/* 故障路径：服务端还是旧版本（/api/roam/map 404）时必须给出可见错误提示 */
const errBox = document.getElementById("map-error");
fetchImpl = (url) => {
  const isGeo = String(url).includes("china.geo.json");
  return Promise.resolve({
    ok: isGeo, status: isGeo ? 200 : 404,
    json: async () => (isGeo ? geo : { detail: "Not Found" }),
  });
};
dbg.state.ready = false;
/* 这两条是**故意**走失败路径：loadData 会把原因写进 #map-error 后抛出。
   不 catch 的话 Node 会报一个 unhandled rejection 并以退出码 1 收场 ——
   明明「✓ 全部通过」却看着像失败（踩过，排查了半天）。 */
try { await dbg.loadData(); } catch (e) { /* 预期：服务端旧版本 */ }
check("旧版本服务端 → 显示错误提示", errBox.hidden === false && /旧版本/.test(errBox.innerHTML),
  errBox.innerHTML.replace(/<[^>]+>/g, " ").trim().slice(0, 60));
check("旧版本服务端 → 不谎报就绪", dbg.state.ready === false);

/* 无 WebGL 的机器：必须给出明确原因，而不是白屏 */
fetchImpl = (url) => {
  const isGeo = String(url).includes("china.geo.json");
  const isNature = String(url).includes("china.nature.json");
  return Promise.resolve({
    ok: true, status: 200,
    json: async () => (isGeo ? geo : isNature ? nature : roamPayload),
  });
};
try { await dbg.loadData(); } catch (e) { /* 预期：没有 WebGL */ }
check("缺 WebGL 时提示原因而不是白屏",
  errBox.hidden === false && /WebGL/.test(errBox.innerHTML) && dbg.state.ready === false,
  errBox.innerHTML.replace(/<[^>]+>/g, " ").trim().slice(0, 56));

/* 「显示/隐藏图标」：卡片与环线标签都是 DOM 浮层，收起来只剩地形与路线 */
{
  const cards = document.getElementById("map-cards");
  const loopsLayer = document.getElementById("map-loops-layer");
  const btn = document.getElementById("map-icons-toggle");
  const routeBefore = dbg.state.routeLine ? 1 : 0;
  dbg.setIcons(false);
  check("隐藏图标：城市卡浮层收起来了", cards.hidden === true);
  check("隐藏图标：环线标签浮层也收起来了", loopsLayer.hidden === true);
  check("隐藏图标：按钮文案换成「显示图标」", btn.textContent.trim() === "显示图标", btn.textContent.trim());
  check("隐藏图标：canvas 上的路线不受影响（不是把路线一起藏了）",
    (dbg.state.routeLine ? 1 : 0) === routeBefore);
  dbg.setIcons(true);
  check("再点回来：卡片与标签都恢复", cards.hidden === false && loopsLayer.hidden === false &&
    btn.textContent.trim() === "隐藏图标");
}

/* 每帧 DOM/投影开销 */
{
  dbg.cam.dist = 1400;
  const t0 = process.hrtime.bigint();
  const N = 20;
  for (let i = 0; i < N; i++) { dbg.layoutCards(); dbg.updateRouteLine(); }
  const ms = Number(process.hrtime.bigint() - t0) / 1e6 / N;
  check("每帧 DOM/投影耗时可接受", ms < 20, `${ms.toFixed(2)} ms/帧`);
}

console.log(fails.length ? `\n✗ ${fails.length} 项失败：${fails.join("、")}` : "\n✓ 全部通过");
process.exit(fails.length ? 1 : 0);
