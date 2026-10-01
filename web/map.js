/* 地图漫游 —— 立体中国地图（three.js / WebGL 渲染）
 *
 * 用现成图形库做真正的三维：真实光照、投影阴影、GPU 抗锯齿、高 DPI 清晰度。
 * 地理数据仍来自 web/data/china.geo.json（省级轮廓）与 china.nature.json（山川湖河）；
 * 相机、交互、城市卡片、环线与规划流程沿用本项目自己的实现。
 *
 * 世界坐标：X = 兰伯特投影 x（公里），Z = -兰伯特投影 y（公里，北为 -Z），Y = 高度（公里）
 */
import * as THREE from "/static/vendor/three.module.min.js";

const $ = (id) => document.getElementById(id);
const D2R = Math.PI / 180;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const lerp = (a, b, t) => a + (b - a) * t;
const TP = () => window.TP || {};

/* ---------------------------------------------------------- 投影（Lambert 等角圆锥） */
const lcc = (function () {
  const p1 = 25 * D2R, p2 = 47 * D2R, p0 = 35 * D2R, lon0 = 105 * D2R, R = 6371;
  const n = Math.log(Math.cos(p1) / Math.cos(p2)) /
    Math.log(Math.tan(Math.PI / 4 + p2 / 2) / Math.tan(Math.PI / 4 + p1 / 2));
  const F = Math.cos(p1) * Math.pow(Math.tan(Math.PI / 4 + p1 / 2), n) / n;
  const rho0 = F / Math.pow(Math.tan(Math.PI / 4 + p0 / 2), n);
  return function (lon, lat) {
    const rho = F / Math.pow(Math.tan(Math.PI / 4 + lat * D2R / 2), n);
    const th = n * (lon * D2R - lon0);
    return [rho * Math.sin(th) * R, (rho0 - rho * Math.cos(th)) * R];
  };
})();

/* 地形抬升（公里，真实海拔的夸张表达） */
const ELEV = {
  西藏: 4.6, 青海: 3.6, 新疆: 1.3, 云南: 1.7, 四川: 1.6, 甘肃: 1.5, 贵州: 1.1,
  内蒙古: 1.0, 陕西: 1.0, 山西: 0.9, 宁夏: 1.1, 重庆: 0.6, 湖北: 0.4, 湖南: 0.3,
  江西: 0.2, 福建: 0.4, 浙江: 0.25, 安徽: 0.2, 江苏: 0.06, 上海: 0.02, 山东: 0.15,
  河南: 0.3, 河北: 0.5, 北京: 0.2, 天津: 0.05, 辽宁: 0.3, 吉林: 0.5, 黑龙江: 0.4,
  广东: 0.2, 广西: 0.4, 海南: 0.2, 台湾: 0.7, 香港: 0.1, 澳门: 0.02,
};
const BASE_H = 36;      // 基准板厚（公里）
const EXAG = 22;        // 地形夸张倍数
const FOV = 34;

const KIND_ICON = {
  博物馆: "🏛", 古迹: "🏯", 自然: "🏞", 街区: "🏙", 古镇: "🏮", 美食: "🍜",
  夜景: "🌃", 演出: "🎭", 海岛: "🏝", 雪山: "🏔", 湖泊: "🌊", 寺庙: "🛕",
  园林: "🌿", 工业: "🏭", 温泉: "♨", 边境: "🛂", 主题乐园: "🎡",
};
const STAY_OPTIONS = [
  { value: "halfday", label: "玩半天" },
  { value: "nights:1", label: "住 1 晚" },
  { value: "nights:2", label: "住 2 晚" },
  { value: "nights:3", label: "住 3 晚" },
  { value: "transit", label: "纯中转" },
];

/* ---------------------------------------------------------- 状态 */
const state = {
  data: null,
  provinces: [],
  region: null,
  selected: [],
  activeLoop: null,
  ready: false,
  loading: false,
  cards: new Map(),
  cardList: [],
  loopLabels: [],
  animating: false,
  closed: true,
  travelMode: "transit",     // transit | drive | rental
  legChips: [],
  rentalPlan: null,
  nature: null,
  scene: null,
  gl: null,
  groups: {},
  routeLine: null,
  frameError: null,
  pendingRegions: null,
  maxH: 1,
  chinaRect: null,
  plate: null,
  showIcons: true,           // 「显示/隐藏图标」：卡片与环线标签都是 DOM 浮层，收起来只看地形与路线
};

const cam = { tx: 0, ty: 0, dist: 6400, pitch: 20, bearing: 0 };
let W = 0, H = 0, dpr = 1;
let canvas, camera, renderer = null, scene = null;
let drag = null;
let sunLight = null;
const scratchV = new THREE.Vector3();
const raycaster = new THREE.Raycaster();
const groundPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);

/* ---------------------------------------------------------- 地形高度 */
const heightCache = new Map();
function pointInRings(x, y, rings) {
  for (const ring of rings) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
      if ((yi > y) !== (yj > y) &&
          x < ((xj - xi) * (y - yi)) / (yj - yi + 1e-12) + xi) inside = !inside;
    }
    if (inside) return true;
  }
  return false;
}
function heightAtWorld(x, y) {
  const key = `${Math.round(x / 40)},${Math.round(y / 40)}`;
  if (heightCache.has(key)) return heightCache.get(key);
  let h = BASE_H;
  for (const prov of state.provinces) {
    if (!pointInRings(x, y, prov.rings)) continue;
    h = prov.h;
    break;
  }
  if (heightCache.size > 20000) heightCache.clear();
  heightCache.set(key, h);
  return h;
}

/* ---------------------------------------------------------- 相机 */
const view = (function () {
  const pos = new THREE.Vector3();
  const target = new THREE.Vector3();
  return function () {
    const bearing = cam.bearing * D2R;
    const groundH = state.provinces.length ? heightAtWorld(cam.tx, cam.ty) : BASE_H;
    const minZ = groundH + 14;
    let pitchDeg = cam.pitch;
    if (cam.dist * Math.sin(pitchDeg * D2R) < minZ) {
      pitchDeg = Math.asin(clamp(minZ / cam.dist, 0, 1)) / D2R;
    }
    const pitch = pitchDeg * D2R;
    const horiz = cam.dist * Math.cos(pitch);
    pos.set(cam.tx - horiz * Math.sin(bearing), cam.dist * Math.sin(pitch),
            -cam.ty + horiz * Math.cos(bearing));
    target.set(cam.tx, 0, -cam.ty);
    return { pos, target, pitch: pitchDeg };
  };
})();

function syncCamera() {
  if (!camera) return;
  const v = view();
  camera.position.copy(v.pos);
  camera.up.set(0, 1, 0);
  camera.lookAt(v.target);
  camera.updateMatrixWorld();
  if (scene && scene.fog) {
    // 雾跟着视距走：全国视角(~10000km)不能被雾糊掉，贴近地面时又要有一点空气感
    scene.fog.near = cam.dist * 0.95;
    scene.fog.far = cam.dist * 2.8;
  }
  if (sunLight) {
    // 太阳跟着视点走，保证任何区域都有稳定光照与阴影
    sunLight.position.set(cam.tx - 2200, 3200, -cam.ty + 1500);
    sunLight.target.position.set(cam.tx, 0, -cam.ty);
    sunLight.target.updateMatrixWorld();
  }
}

function setCamera(p) {
  if (p.tx != null) cam.tx = p.tx;
  if (p.ty != null) cam.ty = p.ty;
  if (p.dist != null) cam.dist = clamp(p.dist, 160, 13000);
  if (p.pitch != null) cam.pitch = clamp(p.pitch, 6, 68);
  if (p.bearing != null) cam.bearing = p.bearing;
  syncCamera();
}

/** 世界点 -> 屏幕像素 + 深度（供 DOM 卡片定位） */
function projectPoint(v) {
  if (!camera) return null;
  scratchV.copy(v).project(camera);
  if (scratchV.z > 1) return null;
  const depth = camera.position.distanceTo(v);
  return [(scratchV.x * 0.5 + 0.5) * W, (0.5 - scratchV.y * 0.5) * H, depth];
}

/** 屏幕像素 -> 地面(y=0)交点，用于「抓住鼠标下的地面」平移与光标缩放 */
function groundPoint(px, py) {
  if (!camera) return null;
  const ndc = new THREE.Vector2((px / W) * 2 - 1, -(py / H) * 2 + 1);
  raycaster.setFromCamera(ndc, camera);
  const hit = new THREE.Vector3();
  if (!raycaster.ray.intersectPlane(groundPlane, hit)) return null;
  if (camera.position.distanceTo(hit) > 60000) return null;
  return [hit.x, -hit.z];
}

function zoomAt(px, py, factor) {
  const before = groundPoint(px, py);
  setCamera({ dist: cam.dist * factor });
  const after = groundPoint(px, py);
  if (before && after) setCamera({ tx: cam.tx + (before[0] - after[0]), ty: cam.ty + (before[1] - after[1]) });
}

function panByPixels(dx, dy) {
  const k = cam.dist / (H / (2 * Math.tan((FOV * D2R) / 2)));
  const bearing = cam.bearing * D2R;
  const rightX = Math.cos(bearing), rightY = Math.sin(bearing);
  const fwdX = -Math.sin(bearing), fwdY = Math.cos(bearing);
  const kz = Math.max(0.3, Math.sin(view().pitch * D2R));
  cam.tx -= dx * k * rightX + (dy * k * fwdX) / kz;
  cam.ty -= dx * k * rightY + (dy * k * fwdY) / kz;
  setCamera({});
}

function screenSize(rect, camLike) {
  const saved = { ...cam };
  setCamera(camLike);
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  const corners = [];
  for (const x of [rect[0], rect[2]]) {
    for (const z of [rect[1], rect[3]]) {
      for (const y of [0, rect[4] || 0]) corners.push(new THREE.Vector3(x, y, -z));
    }
  }
  for (const c of corners) {
    const p = projectPoint(c);
    if (!p) continue;
    x0 = Math.min(x0, p[0]); x1 = Math.max(x1, p[0]);
    y0 = Math.min(y0, p[1]); y1 = Math.max(y1, p[1]);
  }
  setCamera(saved);
  if (!isFinite(x0)) return [Infinity, Infinity];
  return [x1 - x0, y1 - y0];
}

function fitRect(rect, opts) {
  const o = Object.assign({ pitch: 20, bearing: 0, mx: 0.74, my: 0.66, dist: 3000 }, opts || {});
  const center = [(rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2];
  let dist = o.dist;
  const probe = { tx: center[0], ty: center[1], pitch: o.pitch, bearing: o.bearing };
  for (let i = 0; i < 7; i++) {
    probe.dist = dist;
    const [sw, sh] = screenSize(rect, probe);
    if (!isFinite(sw) || sw <= 0 || sh <= 0) { dist = Math.min(dist * 1.6, 13000); continue; }
    const k = Math.min((W * o.mx) / sw, (H * o.my) / sh);
    dist = clamp(dist / clamp(k, 0.2, 5), 160, 13000);
    if (Math.abs(k - 1) < 0.03) break;
  }
  return { tx: center[0], ty: center[1], dist, pitch: o.pitch, bearing: o.bearing };
}

/* ---------------------------------------------------------- 补间动画 */
const EASE = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
function flyTo(targets, opts) {
  const list = Array.isArray(targets) ? targets : [targets];
  const o = Object.assign({ duration: 900, onDone: null }, opts || {});
  let index = 0;
  const step = () => {
    if (index >= list.length) {
      state.animating = false;
      if (o.onDone) o.onDone();
      return;
    }
    const to = list[index];
    const keys = ["tx", "ty", "dist", "pitch", "bearing"].filter((k) => to[k] != null);
    const start = performance.now();
    const fromNow = {};
    keys.forEach((k) => { fromNow[k] = cam[k]; });
    state.animating = true;
    const tick = (now) => {
      const t = clamp((now - start) / (to.duration || o.duration), 0, 1);
      const e = EASE(t);
      const patch = {};
      keys.forEach((k) => { patch[k] = lerp(fromNow[k], to[k], e); });
      setCamera(patch);
      if (t < 1) requestAnimationFrame(tick);
      else { index++; step(); }
    };
    requestAnimationFrame(tick);
  };
  step();
}

/* ---------------------------------------------------------- 场景构建 */
function regionColorOf(name) {
  const r = state.data && state.data.regions.find((x) => x.name === name);
  return r ? r.color : "#6ee7c8";
}
function provinceRegion(short) {
  const regions = (state.data && state.data.regions) || state.pendingRegions || [];
  return regions.find((r) => (r.provinces || []).includes(short)) || null;
}
function hexToRgb(hex) {
  const h = (hex || "#6ee7c8").replace("#", "");
  const v = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
}
function mixHexStr(a, b, t) {
  const A = hexToRgb(a), B = hexToRgb(b);
  return `rgb(${Math.round(lerp(A[0], B[0], t))},${Math.round(lerp(A[1], B[1], t))},${Math.round(lerp(A[2], B[2], t))})`;
}

function disposeGroup(group) {
  if (!group) return;
  group.traverse((obj) => {
    if (obj.geometry) obj.geometry.dispose();
    if (obj.material) {
      const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
      mats.forEach((m) => { if (m.map) m.map.dispose(); m.dispose(); });
    }
  });
  if (group.parent) group.parent.remove(group);
}

function shapeFromRing(ring) {
  const shape = new THREE.Shape();
  for (let i = 0; i < ring.length; i++) {
    const p = ring[i];
    if (i === 0) shape.moveTo(p[0], p[1]); else shape.lineTo(p[0], p[1]);
  }
  return shape;
}

function buildScene() {
  if (scene) {
    ["provinces", "nature", "loops", "extras", "labels"].forEach((k) => disposeGroup(state.groups[k]));
  }
  scene = new THREE.Scene();
  scene.fog = new THREE.Fog(0x0d3340, 8000, 26000);
  camera = new THREE.PerspectiveCamera(FOV, Math.max(1, W) / Math.max(1, H), 4, 40000);

  scene.add(new THREE.HemisphereLight(0xd8f2ff, 0x0a2028, 1.15));
  const sun = new THREE.DirectionalLight(0xfff2dd, 2.1);
  sun.position.set(-2200, 3200, 1500);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  const sc = sun.shadow.camera;
  sc.left = -4200; sc.right = 4200; sc.top = 4200; sc.bottom = -4200; sc.near = 100; sc.far = 14000;
  sc.updateProjectionMatrix();
  sun.shadow.bias = -0.0008;
  sun.shadow.normalBias = 1.6;      // 大面积地形必需，否则顶面出现条纹阴影
  sun.target = new THREE.Object3D();
  scene.add(sun.target);
  scene.add(sun);
  sunLight = sun;
  const fill = new THREE.DirectionalLight(0x9fd8ff, 0.35);
  fill.position.set(2600, 1800, -1800);
  scene.add(fill);

  state.groups = {
    provinces: new THREE.Group(),
    nature: new THREE.Group(),
    loops: new THREE.Group(),
    extras: new THREE.Group(),
    labels: new THREE.Group(),
  };
  Object.values(state.groups).forEach((g) => scene.add(g));

  buildPlate(state.groups.extras);
  buildProvinces(state.groups.provinces, state.groups.extras);
  buildNature(state.groups.nature);
  buildLoopLines(state.groups.loops);
  buildRouteLine(state.groups.loops);
  syncCamera();
}

function buildPlate(group) {
  const rect = state.plate;
  const w = rect[2] - rect[0], d = rect[3] - rect[1];
  const geo = new THREE.PlaneGeometry(w, d, 1, 1);
  const cv = document.createElement("canvas");
  cv.width = cv.height = 512;
  const g2 = cv.getContext("2d");
  if (g2) {
    g2.fillStyle = "#0b2b36";
    g2.fillRect(0, 0, 512, 512);
    g2.strokeStyle = "rgba(140,220,215,.18)";
    g2.lineWidth = 1;
    for (let i = 0; i <= 8; i++) {
      const p = (i / 8) * 512;
      g2.beginPath(); g2.moveTo(p, 0); g2.lineTo(p, 512); g2.stroke();
      g2.beginPath(); g2.moveTo(0, p); g2.lineTo(512, p); g2.stroke();
    }
  }
  const tex = new THREE.CanvasTexture(cv);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.repeat.set(Math.max(1, w / 900), Math.max(1, d / 900));
  const mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
    map: tex, transparent: true, opacity: 0.85,
  }));
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.set((rect[0] + rect[2]) / 2, -1.2, -(rect[1] + rect[3]) / 2);
  group.add(mesh);
}

function buildProvinces(group, extras) {
  const maxH = state.maxH || 1;
  for (const prov of state.provinces) {
    const shapes = prov.rings.map(shapeFromRing);
    const geo = new THREE.ExtrudeGeometry(shapes, { depth: prov.h, bevelEnabled: false });
    const heightT = clamp((prov.h - BASE_H) / Math.max(1, maxH - BASE_H), 0, 1);
    const base = new THREE.Color(prov.color);
    if (heightT > 0.4) base.lerp(new THREE.Color(0xeef6ff), (heightT - 0.4) * 0.55);
    const mat = new THREE.MeshStandardMaterial({
      color: base, roughness: 0.88, metalness: 0.06,
      emissive: new THREE.Color(prov.color).multiplyScalar(0.06),
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.rotation.x = -Math.PI / 2;
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    mesh.name = `province:${prov.short}`;
    prov.mesh = mesh;
    prov.baseColor = base.clone();
    group.add(mesh);

    // 省界：顶面轮廓单独描边，边界更清晰
    const linePts = [];
    for (const ring of prov.rings) {
      for (let i = 0; i + 1 < ring.length; i++) {
        linePts.push(new THREE.Vector3(ring[i][0], prov.h + 0.35, -ring[i][1]));
        linePts.push(new THREE.Vector3(ring[i + 1][0], prov.h + 0.35, -ring[i + 1][1]));
      }
    }
    const line = new THREE.LineSegments(
      new THREE.BufferGeometry().setFromPoints(linePts),
      new THREE.LineBasicMaterial({ color: 0x08222a, transparent: true, opacity: 0.45 }));
    extras.add(line);
    prov.outline = line;
  }
}

function buildNature(group) {
  const nat = state.nature;
  if (!nat) return;
  for (const lake of nat.lakes) {
    const shape = new THREE.Shape();
    lake.pts.forEach((p, i) => (i === 0 ? shape.moveTo(p.x, p.y) : shape.lineTo(p.x, p.y)));
    const mesh = new THREE.Mesh(
      new THREE.ShapeGeometry(shape),
      new THREE.MeshStandardMaterial({
        color: 0x3f9fc4, emissive: 0x11384a, roughness: 0.25, metalness: 0.25,
        transparent: true, opacity: 0.95, side: THREE.DoubleSide,
      }));
    mesh.rotation.x = -Math.PI / 2;
    mesh.position.y = lake.pts[0].h + 0.6;
    group.add(mesh);
  }
  for (const river of nat.rivers) {
    const pts = river.pts.map((p) => new THREE.Vector3(p.x, p.h + 1.6, -p.y));
    if (pts.length < 2) continue;
    const curve = new THREE.CatmullRomCurve3(pts);
    const geo = new THREE.TubeGeometry(curve, Math.max(12, pts.length * 4),
      Math.max(2.2, river.width * 2.4), 6, false);
    group.add(new THREE.Mesh(geo, new THREE.MeshStandardMaterial({
      color: 0x63c2e6, emissive: 0x1d5f7d, roughness: 0.3, metalness: 0.2,
    })));
  }
  // 山脉：沿脊线撒锥体（实例化，几百个也轻松）
  const cones = [];
  for (const range of nat.ranges) {
    const pts = range.pts;
    for (let i = 0; i + 1 < pts.length; i++) {
      const a = pts[i], b = pts[i + 1];
      const segLen = Math.hypot(b.x - a.x, b.y - a.y);
      const count = clamp(Math.round(segLen / 26), 2, 16);
      for (let k = 0; k < count; k++) {
        const t = (k + 0.5) / count;
        cones.push({
          x: lerp(a.x, b.x, t) + (Math.random() - 0.5) * 8,
          y: lerp(a.h, b.h, t),
          z: -(lerp(a.y, b.y, t) + (Math.random() - 0.5) * 8),
          r: 14 + Math.random() * 12,
          h: 34 + Math.random() * 42 + (range.snow ? 26 : 0),
          snow: range.snow,
        });
      }
    }
  }
  if (cones.length) {
    const geo = new THREE.ConeGeometry(1, 1, 5);
    geo.translate(0, 0.5, 0);
    const inst = new THREE.InstancedMesh(
      geo, new THREE.MeshStandardMaterial({ roughness: 0.95, metalness: 0.02, flatShading: true }),
      cones.length);
    const m = new THREE.Matrix4();
    const col = new THREE.Color();
    cones.forEach((c, i) => {
      m.makeScale(c.r, c.h, c.r);
      m.setPosition(c.x, c.y, c.z);
      inst.setMatrixAt(i, m);
      col.set(c.snow ? 0xeef7ff : 0xbda98a).offsetHSL(0, 0, (Math.random() - 0.5) * 0.08);
      inst.setColorAt(i, col);
    });
    inst.castShadow = true;
    inst.receiveShadow = true;
    inst.instanceMatrix.needsUpdate = true;
    if (inst.instanceColor) inst.instanceColor.needsUpdate = true;
    group.add(inst);
  }
}

function worldOf(city) {
  const p = lcc(city.lon, city.lat);
  return new THREE.Vector3(p[0], 0, -p[1]);
}

function buildLoopLines(group) {
  for (const loop of state.data.loops) {
    if (!loop.stops || loop.stops.length < 2) continue;
    const pts = loop.stops.filter((s) => s.lon != null).map((s) => {
      const p = lcc(s.lon, s.lat);
      return new THREE.Vector3(p[0], heightAtWorld(p[0], p[1]) + 7, -p[1]);
    });
    if (pts.length < 2) continue;
    pts.push(pts[0].clone());
    const line = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({
        color: loop.plan_mode === "reference" ? 0xffbe78 : 0x7ee7c8,
        transparent: true, opacity: 0.4,
      }));
    line.name = `loop:${loop.id}`;
    loop.line = line;
    group.add(line);
  }
}

function buildRouteLine(group) {
  const g = new THREE.Group();
  g.name = "roster";
  group.add(g);
  state.routeGroup = g;
  state.routePulses = [];
  state.routeCurve = null;
  state.routeKey = "";
}

/** 选中城市之间的「金色光带」：外发光 + 实心光带 + 站点光环 + 流动光点。
 *  管径随视距自适应，全国视角下也看得很清楚（之前是 1px 线，几乎看不见）。 */
/** 选中城市之间的「光带」：公交模式金色，自驾/租车模式公路蓝。
 *  外发光 + 实心光带 + 站点光环 + 流动光点，管径随视距自适应。 */
function routePalette() {
  if (state.travelMode === "transit") {
    return { core: 0xfff0bd, emissive: 0xffab1f, glow: 0xffd873, pulse: 0xfff8e2, ring: 0xffe6a0 };
  }
  return { core: 0xcdeeff, emissive: 0x1f86d6, glow: 0x3fa9f5, pulse: 0xffffff, ring: 0x9fd8ff };
}

function updateRouteLine(force) {
  const groups = state.groups || {};
  const parent = groups.loops || scene;
  const cities = (state.data && state.selected.length)
    ? state.selected.map((s) => state.data.cityIndex[s.name]).filter((c) => c && c.lon != null)
    : [];
  const key = cities.length < 2
    ? ""
    : cities.map((c) => c.name).join(">") +
      `|${state.closed !== false}|${Math.round(Math.log2(cam.dist / 900) * 2)}|${state.travelMode}`;
  if (!force && key === state.routeKey) return;
  state.routeKey = key;

  if (state.routeGroup) disposeGroup(state.routeGroup);
  const group = new THREE.Group();
  group.name = "roster";
  parent.add(group);
  state.routeGroup = group;
  state.routePulses = [];
  state.routeCurve = null;
  if (cities.length < 2) return;

  const pal = routePalette();
  const radius = clamp(cam.dist / 430, 4.5, 26);
  const pts = cities.map((c) => {
    const p = lcc(c.lon, c.lat);
    return new THREE.Vector3(p[0], heightAtWorld(p[0], p[1]) + 22, -p[1]);
  });
  const closed = state.closed !== false;
  if (closed) pts.push(pts[0].clone());
  const curve = new THREE.CatmullRomCurve3(pts, closed, "catmullrom", 0.25);
  state.routeCurve = curve;
  const seg = Math.max(24, pts.length * 30);

  const glow = new THREE.Mesh(
    new THREE.TubeGeometry(curve, seg, radius * 2.7, 10, closed),
    new THREE.MeshBasicMaterial({
      color: pal.glow, transparent: true, opacity: 0.17,
      blending: THREE.AdditiveBlending, depthWrite: false,
    }));
  glow.renderOrder = 3;
  group.add(glow);

  const core = new THREE.Mesh(
    new THREE.TubeGeometry(curve, seg, radius, 10, closed),
    new THREE.MeshStandardMaterial({
      color: pal.core, emissive: pal.emissive, emissiveIntensity: 1.2,
      roughness: 0.3, metalness: 0.15,
    }));
  core.renderOrder = 4;
  group.add(core);

  // 每个选中城市：一圈光环 + 一根立柱，远看就是明显的站点标记
  for (const city of cities) {
    const p = lcc(city.lon, city.lat);
    const h = heightAtWorld(p[0], p[1]);
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(radius * 3.2, radius * 0.6, 8, 28),
      new THREE.MeshStandardMaterial({
        color: pal.ring, emissive: pal.emissive, emissiveIntensity: 0.95, roughness: 0.35,
      }));
    ring.rotation.x = -Math.PI / 2;
    ring.position.set(p[0], h + 26, -p[1]);
    ring.renderOrder = 5;
    group.add(ring);
    const pole = new THREE.Mesh(
      new THREE.CylinderGeometry(radius * 0.32, radius * 0.32, 30, 6),
      new THREE.MeshStandardMaterial({
        color: pal.ring, emissive: pal.emissive, emissiveIntensity: 0.85, roughness: 0.4,
      }));
    pole.position.set(p[0], h + 15, -p[1]);
    group.add(pole);
  }

  const pulseCount = clamp(Math.round(curve.getLength() / Math.max(120, radius * 22)), 6, 56);
  for (let i = 0; i < pulseCount; i++) {
    const pulse = new THREE.Mesh(
      new THREE.SphereGeometry(radius * 1.6, 10, 8),
      new THREE.MeshBasicMaterial({ color: pal.pulse, transparent: true, opacity: 0.95 }));
    pulse.renderOrder = 6;
    pulse.userData.offset = i / pulseCount;
    group.add(pulse);
    state.routePulses.push(pulse);
  }
}

/** 每帧推进光带上的流动光点 */
function updateRoutePulses(t) {
  const curve = state.routeCurve;
  if (!curve || !state.routePulses.length) return;
  const speed = 0.06;
  for (const pulse of state.routePulses) {
    const u = (pulse.userData.offset + t * speed) % 1;
    curve.getPointAt(u, pulse.position);
  }
}

function applyRegionHighlight() {
  const active = state.region ? state.region.name : null;
  for (const prov of state.provinces) {
    if (!prov.mesh) continue;
    const inRegion = !active || prov.region === active;
    const target = prov.baseColor.clone();
    if (!inRegion) target.multiplyScalar(0.34);
    prov.mesh.material.color.copy(target);
    prov.mesh.material.emissive.copy(target).multiplyScalar(inRegion && active ? 0.22 : 0.06);
    if (prov.outline) prov.outline.material.opacity = inRegion ? 0.45 : 0.18;
  }
  for (const loop of state.data.loops) {
    if (!loop.line) continue;
    const isActive = state.activeLoop && state.activeLoop.id === loop.id;
    const touching = !active || (loop.regions || []).includes(active);
    loop.line.material.opacity = isActive ? 0.95 : touching ? (active ? 0.5 : 0.34) : 0.1;
  }
}

/* ---------------------------------------------------------- 城市卡面
 * 没有真实照片时用「城市海报」矢量图；想换真实照片，把图片放到
 * web/img/cities/<城市名>.jpg 即可自动使用。
 */
function hashCode(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}
function rngOf(seed) {
  let s = seed >>> 0;
  return function () { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; };
}

function cityArt(city) {
  const rand = rngOf(hashCode(city.name));
  const kinds = new Set(((city.highlights || []).map((x) => x.kind)));
  const tint = regionColorOf(city.region);
  const uid = "a" + hashCode(city.name).toString(36);
  const Wd = 70, Hd = 86;
  let sceneKind = "mixed";
  if (kinds.has("海岛")) sceneKind = "coast";
  else if (kinds.has("雪山") || kinds.has("湖泊") || kinds.has("自然")) sceneKind = "mountain";
  else if (kinds.has("古镇") || kinds.has("古迹") || kinds.has("寺庙") || kinds.has("园林")) sceneKind = "ancient";
  else if (kinds.has("街区") || kinds.has("夜景") || kinds.has("工业") || kinds.has("美食")) sceneKind = "city";
  const sky1 = mixHexStr(tint, "#07222c", 0.45);
  const sky2 = mixHexStr(tint, "#04161e", 0.78);
  const far = mixHexStr(tint, "#062028", 0.66);
  const mid = mixHexStr(tint, "#04202a", 0.82);
  const near = "#03161d";
  const glow = mixHexStr(tint, "#ffffff", 0.35);
  const parts = [];
  parts.push(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${Wd} ${Hd}" width="${Wd}" height="${Hd}">`);
  parts.push(`<defs>
      <linearGradient id="${uid}s" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="${sky1}"/><stop offset="1" stop-color="${sky2}"/>
      </linearGradient>
      <radialGradient id="${uid}g" cx="0.5" cy="0.42" r="0.55">
        <stop offset="0" stop-color="${glow}" stop-opacity="0.55"/>
        <stop offset="1" stop-color="${glow}" stop-opacity="0"/>
      </radialGradient>
    </defs>`);
  parts.push(`<rect width="${Wd}" height="${Hd}" fill="url(#${uid}s)"/>`);
  parts.push(`<rect width="${Wd}" height="${Hd}" fill="url(#${uid}g)"/>`);
  parts.push(`<circle cx="${(16 + rand() * 38).toFixed(1)}" cy="${(20 + rand() * 12).toFixed(1)}" r="${(6 + rand() * 3).toFixed(1)}" fill="${glow}" opacity="0.55"/>`);
  const ridge = (y0, amp, color, opacity, n) => {
    let d = `M0 ${Hd} L0 ${y0}`;
    for (let i = 1; i <= n; i++) {
      d += ` L${((Wd / n) * i).toFixed(1)} ${(y0 + (rand() - 0.5) * amp * 2).toFixed(1)}`;
    }
    d += ` L${Wd} ${Hd} Z`;
    parts.push(`<path d="${d}" fill="${color}" opacity="${opacity}"/>`);
  };
  const skyline = (y0, color, opacity, n, minH, maxH) => {
    let x = -4;
    let d = `M0 ${Hd} L0 ${y0}`;
    for (let i = 0; i < n; i++) {
      const w = 6 + rand() * 9, hh = minH + rand() * (maxH - minH);
      d += ` L${x.toFixed(1)} ${(y0 - hh).toFixed(1)} L${(x + w).toFixed(1)} ${(y0 - hh).toFixed(1)}`;
      x += w + 1 + rand() * 3;
      if (x > Wd) break;
    }
    d += ` L${Wd} ${y0} L${Wd} ${Hd} Z`;
    parts.push(`<path d="${d}" fill="${color}" opacity="${opacity}"/>`);
  };
  if (sceneKind === "coast") {
    parts.push(`<rect y="58" width="${Wd}" height="${Hd - 58}" fill="${mixHexStr(tint, "#04222c", 0.62)}"/>`);
    ridge(56, 3, far, 0.9, 5);
    parts.push(`<path d="M0 62 Q ${Wd * 0.3} 58 ${Wd * 0.55} 62 T ${Wd} 61 L${Wd} ${Hd} L0 ${Hd} Z" fill="${near}" opacity="0.75"/>`);
    for (let i = 0; i < 3; i++) {
      const y = 66 + i * 6;
      parts.push(`<path d="M4 ${y} Q ${Wd * 0.35} ${y - 2} ${Wd * 0.7} ${y + 1} T ${Wd - 4} ${y}" stroke="${glow}" stroke-opacity="${0.3 - i * 0.07}" stroke-width="1.2" fill="none"/>`);
    }
  } else if (sceneKind === "mountain") {
    parts.push(`<path d="M0 ${Hd} L14 40 L26 60 L40 26 L54 56 L${Wd} ${Hd} Z" fill="${far}" opacity="0.85"/>`);
    parts.push(`<path d="M40 26 L34 38 L39 36 L44 42 L47 34 Z" fill="#ffffff" opacity="0.55"/>`);
    ridge(58, 4, mid, 0.95, 6);
    parts.push(`<rect y="72" width="${Wd}" height="${Hd - 72}" fill="${near}" opacity="0.9"/>`);
    parts.push(`<ellipse cx="${(20 + rand() * 30).toFixed(1)}" cy="76" rx="18" ry="3.2" fill="${glow}" opacity="0.22"/>`);
  } else if (sceneKind === "ancient") {
    ridge(54, 3, far, 0.8, 5);
    for (let i = 0; i < 3; i++) {
      const x = 10 + i * 22 + rand() * 6, base = 74, hh = 16 + rand() * 8, w = 16 + rand() * 6;
      parts.push(`<rect x="${x.toFixed(1)}" y="${(base - hh).toFixed(1)}" width="${w.toFixed(1)}" height="${hh.toFixed(1)}" fill="${mid}"/>`);
      parts.push(`<path d="M${(x - 4).toFixed(1)} ${(base - hh).toFixed(1)} L${(x + w / 2).toFixed(1)} ${(base - hh - 6).toFixed(1)} L${(x + w + 4).toFixed(1)} ${(base - hh).toFixed(1)} Z" fill="${near}"/>`);
    }
    parts.push(`<rect y="74" width="${Wd}" height="${Hd - 74}" fill="${near}"/>`);
  } else if (sceneKind === "city") {
    ridge(50, 2, far, 0.55, 4);
    skyline(72, mid, 0.95, 9, 12, 34);
    for (let i = 0; i < 26; i++) {
      parts.push(`<rect x="${(rand() * Wd).toFixed(1)}" y="${(46 + rand() * 24).toFixed(1)}" width="1.6" height="1.6" fill="${glow}" opacity="${(0.25 + rand() * 0.5).toFixed(2)}"/>`);
    }
  } else {
    ridge(50, 5, far, 0.8, 5);
    skyline(70, mid, 0.9, 7, 8, 22);
    parts.push(`<rect y="74" width="${Wd}" height="${Hd - 74}" fill="${near}"/>`);
  }
  parts.push(`<rect width="${Wd}" height="${Hd}" fill="none" stroke="rgba(255,255,255,.14)"/>`);
  parts.push("</svg>");
  return parts.join("");
}

function artUri(city) {
  if (city.photo) return `url("${city.photo}")`;
  return `url("data:image/svg+xml,${encodeURIComponent(cityArt(city))}")`;
}

function escapeHtml(s) {
  const fn = TP().esc;
  if (fn) return fn(s);
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

function cardHtml(city) {
  const kinds = [...new Set((city.highlights || []).map((h) => h.kind))]
    .slice(0, 3).map((k) => KIND_ICON[k] || "📍").join("");
  const railTag = city.rail ? "" : `<span class="cc-tag" title="无铁路客运，需飞机/包车">✈</span>`;
  const moonTag = city.night
    ? `<span class="cc-moon" title="更适合晚上玩：配的是夜景照片">🌙</span>` : "";
  const guideBtn = city.has_guide
    ? `<button type="button" class="cc-guide" title="看 ${escapeHtml(city.name)} 的完整攻略">📖</button>` : "";
  return `<span class="cc-inner">
      <span class="cc-photo">${railTag}${moonTag}${guideBtn}<span class="cc-badge" hidden></span></span>
      <span class="cc-name">${escapeHtml(city.name)}</span>
      <span class="cc-kinds">${kinds}</span>
    </span>`;
}

function buildCards(cities) {
  const layer = $("map-cards");
  layer.innerHTML = "";
  state.cards = new Map();
  state.cardList = [];
  const frag = document.createDocumentFragment();
  cities.forEach((city, i) => {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "city-card";
    el.dataset.city = city.name;
    el.style.animationDelay = Math.min(i * 42, 900) + "ms";
    el.innerHTML = cardHtml(city);
    const photo = el.querySelector(".cc-photo");
    if (photo) {
      photo.style.setProperty("--tint", regionColorOf(city.region));
      photo.style.backgroundImage = artUri(city);   // 用 CSSOM 赋值，避免 url() 引号截断 HTML 属性
      // 照片加载失败时退回内置矢量海报（无头测试环境没有 Image，跳过即可）
      if (city.photo && typeof Image === "function") {
        const probe = new Image();
        probe.onerror = () => {
          photo.style.backgroundImage = `url("data:image/svg+xml,${encodeURIComponent(cityArt(city))}")`;
        };
        probe.src = city.photo;
      }
    }
    el.addEventListener("mouseenter", () => showTip(city, el));
    el.addEventListener("focus", () => showTip(city, el));
    el.addEventListener("mouseleave", () => hideTipSoon());
    el.addEventListener("blur", hideTip);
    const guideBtn = el.querySelector(".cc-guide");
    if (guideBtn) {
      guideBtn.addEventListener("click", (e) => {
        e.stopPropagation();          // 不要触发「加入行程」
        hideTip();
        openGuide(city.name);
      });
      guideBtn.addEventListener("pointerdown", (e) => e.stopPropagation());
    }
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleCity(city.name);
    });
    frag.appendChild(el);
    state.cards.set(city.name, el);
    state.cardList.push({ city, el, world: worldOf(city) });
  });
  layer.appendChild(frag);
  refreshBadges();
  refreshRentalBadges();
}

function clearCards() {
  $("map-cards").innerHTML = "";
  state.cards = new Map();
  state.cardList = [];
  hideTip();
}

function layoutCards() {
  if (!state.showIcons) return;          // 图标收起来了：不投影、不排版，省掉每帧的开销
  for (const item of state.cardList) {
    const anchor = item.world.clone();
    anchor.y = (item.city.h || 0) + 40;
    const q = projectPoint(anchor);
    if (!q || q[1] > H * 0.995) { item.el.style.display = "none"; continue; }
    const pad = 120;
    if (q[0] < -pad || q[0] > W + pad) { item.el.style.display = "none"; continue; }
    item.el.style.display = "";
    if (!item.w0) {
      item.w0 = item.el.offsetWidth || 92;
      item.h0 = item.el.offsetHeight || 116;
    }
    const s = clamp(1400 / q[2], 0.5, 1.7);
    item.el.style.transform =
      `translate3d(${(q[0] - item.w0 / 2).toFixed(1)}px, ${(q[1] - item.h0).toFixed(1)}px, 0) scale(${s.toFixed(3)})`;
    item.el.style.zIndex = String(Math.max(1, 4000 - Math.round(q[2])));
    item.el.style.opacity = String(clamp(1.25 - q[2] / 6400, 0.35, 1));
  }
  layoutLoopLabels();
}

function layoutLoopLabels() {
  if (!state.showIcons) return;
  const active = state.region ? state.region.name : null;
  for (const item of state.loopLabels) {
    const loop = item.loop;
    const touching = !active || (loop.regions || []).includes(active);
    const isActive = state.activeLoop && state.activeLoop.id === loop.id;
    if (!touching && !isActive) { item.el.style.display = "none"; continue; }
    const q = projectPoint(item.world);
    if (!q) { item.el.style.display = "none"; continue; }
    item.el.style.display = "";
    item.el.style.transform = `translate3d(${q[0].toFixed(1)}px, ${q[1].toFixed(1)}px, 0) translate(-50%,-50%)`;
    item.el.style.zIndex = String(Math.max(1, 3000 - Math.round(q[2])));
    item.el.classList.toggle("active", !!isActive);
  }
}

/** 显示 / 隐藏图标：城市卡、点位标签与环线标签都是 DOM 浮层，
 * 收起来就只剩地形与路线 —— 看地形走向时不被一堆卡片挡住。
 * 注意别把 canvas 上的路线一起藏了：那是画在底图里的，与浮层无关。 */
function setIcons(show) {
  state.showIcons = !!show;
  const cards = $("map-cards"), loops = $("map-loops-layer");
  if (cards) cards.hidden = !state.showIcons;
  if (loops) loops.hidden = !state.showIcons;
  const btn = $("map-icons-toggle");
  if (btn) btn.textContent = state.showIcons ? "隐藏图标" : "显示图标";
  hideTip();
  if (state.showIcons) layoutCards();          // 重新显示时立刻排一次版，不然要等下一帧
}

function refreshBadges() {
  state.cards.forEach((el, name) => {
    const idx = state.selected.findIndex((s) => s.name === name);
    const badge = el.querySelector(".cc-badge");
    if (!badge) return;
    if (idx >= 0) {
      badge.hidden = false;
      badge.textContent = String(idx + 1);
      el.classList.add("picked");
    } else {
      badge.hidden = true;
      el.classList.remove("picked");
    }
  });
}

/* ---------------------------------------------------------- 悬停提示
 * 提示卡是可交互的：鼠标从卡片移到提示卡上不会消失（有 260ms 缓冲），
 * 提示卡里有「完整攻略」和「加入/移出行程」两个按钮。
 */
let tipTimer = null;

function showTip(city, el) {
  const tip = $("map-tip");
  if (tipTimer) { clearTimeout(tipTimer); tipTimer = null; }
  const kinds = (city.highlights || []).map((h) =>
    `<span class="tip-kind">${KIND_ICON[h.kind] || "📍"} ${escapeHtml(h.name)}</span>`).join("");
  const stay = city.days >= 1 ? `建议 ${city.days} 天` : "建议半天";
  const picked = state.selected.some((s) => s.name === city.name);
  tip.innerHTML =
    `<div class="tip-head"><b>${escapeHtml(city.name)}</b>
        <span class="tip-score">${city.score}</span>
        <span class="tip-region">${escapeHtml(city.region)}·${escapeHtml(city.province)}</span></div>
      <div class="tip-intro">${escapeHtml(city.intro || "")}</div>
      <div class="tip-half"><span class="tip-label">半日游</span>${escapeHtml(city.halfday || "暂无半日建议")}</div>
      ${city.oneday ? `<div class="tip-day"><span class="tip-label">一日</span>${escapeHtml(city.oneday)}</div>` : ""}
      <div class="tip-kinds">${kinds}</div>
      <div class="tip-foot">${escapeHtml(stay)} · ${city.hours} 小时 · ${escapeHtml(city.best_season || "全年")}
        ${city.rail ? "" : " · 无铁路，需飞机/包车"}${city.night ? " · 🌙 夜景照" : ""}${
        city.has_guide ? ` · 📖 ${city.guide_days || ""} 天完整攻略` : ""}</div>
      <div class="tip-actions">
        <button type="button" class="tip-btn primary-sm" data-act="guide"${
          city.has_guide ? "" : " disabled"}>📖 看完整攻略</button>
        <button type="button" class="tip-btn" data-act="pick">${picked ? "✓ 移出行程" : "＋ 加入行程"}</button>
      </div>`;
  tip.hidden = false;
  tip.dataset.city = city.name;
  // 提示卡自己也响应鼠标：移上去不消失
  if (!tip.dataset.bound) {
    tip.dataset.bound = "1";
    tip.addEventListener("mouseenter", () => {
      if (tipTimer) { clearTimeout(tipTimer); tipTimer = null; }
    });
    tip.addEventListener("mouseleave", hideTip);
    tip.addEventListener("click", (e) => {
      const btn = e.target.closest(".tip-btn");
      if (!btn) return;
      e.stopPropagation();
      const name = tip.dataset.city;
      if (!name) return;
      if (btn.dataset.act === "guide") {
        hideTip();
        openGuide(name);
      } else if (btn.dataset.act === "pick") {
        toggleCity(name);
        const card = state.cards.get(name);
        const item = card && state.cardList.find((x) => x.el === card);
        if (item) showTip(item.city, card);      // 按钮文案跟着变
      }
    });
  }
  const stage = $("map-stage").getBoundingClientRect();
  const r = el.getBoundingClientRect();
  let left = r.left - stage.left + r.width / 2;
  let top = r.top - stage.top - 8;
  const tw = tip.offsetWidth || 300, th = tip.offsetHeight || 200;
  left = clamp(left, tw / 2 + 8, W - tw / 2 - 8);
  top = top - th < 8 ? r.bottom - stage.top + 10 : top - th;
  tip.style.left = left + "px";
  tip.style.top = Math.max(8, top) + "px";
}

/** 鼠标离开卡片后给一点缓冲，方便移到提示卡上 */
function hideTipSoon(delay) {
  if (tipTimer) clearTimeout(tipTimer);
  tipTimer = setTimeout(() => {
    tipTimer = null;
    const t = $("map-tip");
    if (t) t.hidden = true;
  }, delay || 260);
}

function hideTip() {
  if (tipTimer) { clearTimeout(tipTimer); tipTimer = null; }
  const t = $("map-tip");
  if (t) t.hidden = true;
}

/* ---------------------------------------------------------- 出行方式 */
const MODE_HINT = {
  transit: "公共交通：每段用环线引擎查真实车次（火车 / 飞机 / 空铁联运），给出逐日乘车表与购票链接。",
  drive: "自驾：每段走高德驾车路径，给出真实里程、耗时与过路费；点环线看该线路的自驾推荐（公路、季节、封路提醒）。",
  rental: "租车：在自驾行程基础上估算租车费用（日租 × 天数 + 异地还车 + 保险 + 油费 + 过路费），并给出各平台比价入口。",
};
const MODE_BUTTON = { transit: "规划乘车表", drive: "生成自驾行程", rental: "生成行程 + 租车单" };

function setTravelMode(mode, opts) {
  const next = MODE_HINT[mode] ? mode : "transit";
  state.travelMode = next;
  const tab = $("tab-map");
  if (tab) tab.classList.toggle("mode-drive", next !== "transit");
  document.querySelectorAll(".tm-btn").forEach((b) => {
    const on = b.dataset.mode === next;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  const go = $("m-go");
  if (go) go.textContent = MODE_BUTTON[next];
  const hint = $("travel-mode-hint");
  if (hint) hint.textContent = MODE_HINT[next];
  if (!opts || !opts.silent) {
    updateRouteLine(true);
    renderLoops();
    refreshRentalBadges();
  }
}

/** 租车模式下在卡片上标出该城市 SUV 日租区间 */
function refreshRentalBadges() {
  const on = state.travelMode === "rental";
  state.cards.forEach((el, name) => {
    const photo = el.querySelector(".cc-photo");
    if (!photo) return;
    let badge = photo.querySelector(".cc-rate");
    const city = state.data && state.data.cityIndex[name];
    if (!on || !city || !city.rental_daily) {
      if (badge) badge.remove();
      return;
    }
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "cc-rate";
      photo.appendChild(badge);
    }
    badge.textContent = `🚗¥${city.rental_daily[0]}+`;
    badge.title = `${city.rental_tier || ""} 档 · ${city.car_class || "SUV"} 日租约 ¥${city.rental_daily[0]}-${city.rental_daily[1]}`;
  });
}

async function fetchCityDetail(name) {
  const resp = await fetch("/api/roam/city?name=" + encodeURIComponent(name));
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return await resp.json();
}

/** 攻略正文（城市攻略卡里内联展开用） */
function guideBlockHtml(guide) {
  const g = guide || {};
  const days = (g.itinerary || []).map((d) => `
    <div class="gg-day"><b>${escapeHtml(d.day || "")}${d.title ? " · " + escapeHtml(d.title) : ""}</b>
      <p>${escapeHtml(d.detail || "")}</p></div>`).join("");
  if (!days && !g.summary) {
    return `<div class="gg-loading">这座城市暂时没有更详细的攻略，可以先用上面的半日/一日路线。</div>`;
  }
  return `<h5>完整旅游攻略${g.days ? ` · 建议 ${g.days} 天` : ""}</h5>
    ${g.summary ? `<div class="gg-sum">${escapeHtml(g.summary)}</div>` : ""}
    ${days}
    <dl>
      ${g.transport ? `<dt>交通</dt><dd>${escapeHtml(g.transport)}</dd>` : ""}
      ${g.stay ? `<dt>住宿</dt><dd>${escapeHtml(g.stay)}</dd>` : ""}
      ${g.eat ? `<dt>吃</dt><dd>${escapeHtml(g.eat)}</dd>` : ""}
      ${g.budget ? `<dt>预算</dt><dd>${escapeHtml(g.budget)}</dd>` : ""}
    </dl>
    ${(g.tips || []).length ? `<ul>${g.tips.map((t) => `<li>${escapeHtml(t)}</li>`).join("")}</ul>` : ""}`;
}

async function openGuide(name) {
  const layer = $("city-guide");
  const panel = $("cg-panel");
  if (!layer || !panel) return;
  layer.hidden = false;
  panel.innerHTML = `<div class="cg-body"><div class="gg-loading">正在加载「${escapeHtml(name)}」的完整攻略…</div></div>`;
  try {
    const data = await fetchCityDetail(name);
    if (!data.ok) throw new Error(data.error || "没有这座城市");
    const c = data.city || {};
    const g = data.guide || {};
    const kinds = (c.highlights || []).map((h) =>
      `<span class="cg-kind">${KIND_ICON[h.kind] || "📍"} ${escapeHtml(h.name)}</span>`).join("");
    panel.innerHTML = `
      <div class="cg-hero" id="cg-hero">
        <button type="button" class="cg-close" title="关闭（Esc）">✕</button>
        <div class="cg-title">
          <b>${escapeHtml(c.name)}</b>
          <span class="cg-score">${c.score}</span>
          <span class="cg-where">${escapeHtml(c.region || "")} · ${escapeHtml(c.province || "")}
            ${c.night ? " · 🌙 更适合夜游" : ""}${c.rail ? "" : " · ✈ 无铁路"}</span>
        </div>
      </div>
      <div class="cg-body">
        <div class="cg-tags">${(c.tags || []).map((t) => `<span class="cg-tag">${escapeHtml(t)}</span>`).join("")}</div>
        <div class="cg-lead">${escapeHtml(c.intro || "")}</div>
        <div class="cg-sec"><h4>半日路线</h4><div class="cg-lead">${escapeHtml(c.halfday || "—")}</div></div>
        ${c.oneday ? `<div class="cg-sec"><h4>一日路线</h4><div class="cg-lead">${escapeHtml(c.oneday)}</div></div>` : ""}
        ${kinds ? `<div class="cg-sec"><h4>看点</h4><div class="cg-kinds">${kinds}</div></div>` : ""}
        <div class="cg-sec"><h4>完整攻略</h4>
          <div class="cg-itinerary">${(g.itinerary || []).map((d) => `
            <div class="cg-day"><div class="cg-day-head">${escapeHtml(d.day || "")}${d.title ? " · " + escapeHtml(d.title) : ""}</div>
              <div class="cg-day-body">${escapeHtml(d.detail || "")}</div></div>`).join("")
      || `<div class="cg-note">这座城市暂时没有更详细的多日攻略。</div>`}</div>
        </div>
        <div class="cg-sec"><h4>实用信息</h4>
          <div class="cg-row"><span class="cg-label">交通</span>${escapeHtml(g.transport || "—")}</div>
          <div class="cg-row"><span class="cg-label">住宿</span>${escapeHtml(g.stay || "—")}</div>
          <div class="cg-row"><span class="cg-label">吃</span>${escapeHtml(g.eat || c.food || "—")}</div>
          <div class="cg-row"><span class="cg-label">预算</span>${escapeHtml(g.budget || "—")}</div>
          <div class="cg-row"><span class="cg-label">季节</span>${escapeHtml(c.best_season || "全年")}</div>
        </div>
        ${(g.tips || []).length ? `<div class="cg-sec"><h4>提示</h4>
          <ul class="cg-tips">${g.tips.map((t) => `<li>${escapeHtml(t)}</li>`).join("")}</ul></div>` : ""}
        <div class="cg-actions">
          <button type="button" class="primary" id="cg-toggle-pick">加入 / 移出行程</button>
          <button type="button" class="ghost-sm" id="cg-locate">在地图上定位</button>
          <button type="button" class="ghost-sm" id="cg-city-flow"
            title="切到数据台，把这座城的攻略散文对齐成有序点位（带坐标的动线）">看城内动线</button>
        </div>
        <div class="cg-note">攻略为个人整理的行程建议，门票、班次与预约规则以景区当季公告为准。</div>
      </div>`;
    const hero = panel.querySelector("#cg-hero");
    if (hero && c.photo) hero.style.backgroundImage = `url("${c.photo}")`;
    panel.querySelector(".cg-close").addEventListener("click", closeGuide);
    const toggle = panel.querySelector("#cg-toggle-pick");
    if (toggle) {
      toggle.textContent = state.selected.some((s) => s.name === c.name) ? "从行程里移除" : "加入行程";
      toggle.addEventListener("click", () => {
        toggleCity(c.name);
        toggle.textContent = state.selected.some((s) => s.name === c.name) ? "从行程里移除" : "加入行程";
      });
    }
    const locate = panel.querySelector("#cg-locate");
    if (locate) locate.addEventListener("click", () => { closeGuide(); focusCity(c.name); });
    /* 反方向的入口：攻略看完想知道「这几个看点怎么串起来、市内怎么走」→ 去数据台看动线。
       注意别在这里再渲染一遍动线 —— 那就又变成两处重复渲染了。 */
    const flow = panel.querySelector("#cg-city-flow");
    if (flow) flow.addEventListener("click", () => {
      closeGuide();
      const tp = window.TP || {};
      if (tp.openCityFlow) tp.openCityFlow(c.name);
    });
  } catch (err) {
    panel.innerHTML = `<div class="cg-body"><div class="gg-loading">攻略加载失败：${escapeHtml(err.message)}</div>
      <div class="cg-actions"><button type="button" class="ghost-sm" id="cg-close2">关闭</button></div></div>`;
    const btn = panel.querySelector("#cg-close2");
    if (btn) btn.addEventListener("click", closeGuide);
  }
}

function closeGuide() {
  const layer = $("city-guide");
  if (layer) layer.hidden = true;
}

/** 城市攻略卡（公交与自驾模式共用）：半日/一日 + 看点 + 「需要更多攻略」按需展开 */
function renderCityGuides(cities, container) {
  const host = container || $("map-guides");
  const grid = document.createElement("div");
  grid.className = "guide-grid";
  (cities || []).forEach((c, i) => {
    const card = document.createElement("div");
    card.className = "guide-card";
    card.style.setProperty("--tint", regionColorOf(c.region));
    const kinds = (c.highlights || []).map((h) =>
      `<span class="g-kind">${KIND_ICON[h.kind] || "📍"} ${escapeHtml(h.name)}</span>`).join("");
    card.innerHTML =
      `<div class="g-head"><span class="g-no">${i + 1}</span><b>${escapeHtml(c.name)}</b>
           <span class="g-score">${c.score}</span></div>
         <div class="g-intro">${escapeHtml(c.intro || "")}</div>
         <div class="g-row"><span class="g-label">半日</span>${escapeHtml(c.halfday || "—")}</div>
         ${c.oneday ? `<div class="g-row"><span class="g-label">一日</span>${escapeHtml(c.oneday)}</div>` : ""}
         <div class="g-kinds">${kinds}</div>
         <div class="g-foot">建议 ${c.days} 天 / ${c.hours} 小时 · ${escapeHtml(c.best_season || "全年")}
           ${c.rail ? "" : " · 无铁路，需飞机/包车"}${c.night ? " · 🌙 适合夜游" : ""}</div>
         ${c.note ? `<div class="g-note">${escapeHtml(c.note)}</div>` : ""}`;
    // 「需要更多攻略」：点了才去取，攻略正文不进首屏
    const more = document.createElement("div");
    more.className = "g-more";
    more.innerHTML = `<button type="button" class="g-more-btn">需要更多攻略？展开完整攻略 ▾</button>`;
    card.appendChild(more);
    const moreBtn = more.querySelector(".g-more-btn");
    moreBtn.addEventListener("click", async () => {
      if (more.dataset.open === "1") {
        const block = more.querySelector(".g-guide");
        if (block) block.remove();
        more.dataset.open = "";
        moreBtn.textContent = "需要更多攻略？展开完整攻略 ▾";
        return;
      }
      moreBtn.disabled = true;
      moreBtn.textContent = "正在加载攻略…";
      try {
        const data = await fetchCityDetail(c.name);
        const div = document.createElement("div");
        div.className = "g-guide";
        div.innerHTML = guideBlockHtml(data.guide);
        more.appendChild(div);
        more.dataset.open = "1";
        moreBtn.textContent = "收起攻略 ▴";
      } catch (err) {
        moreBtn.textContent = "加载失败，点我再试一次";
      } finally {
        moreBtn.disabled = false;
      }
    });
    const head = card.querySelector(".g-head");
    if (head) {
      head.style.cursor = "pointer";
      head.title = "打开完整攻略";
      head.addEventListener("click", () => openGuide(c.name));
    }
    grid.appendChild(card);
  });
  host.appendChild(grid);
}

/* ---------------------------------------------------------- 大区 */
function regionRect(region) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity, h = 0;
  for (const prov of state.provinces) {
    if (region && prov.region !== region.name) continue;
    if (!region && !prov.region) continue;
    x0 = Math.min(x0, prov.bbox[0]); y0 = Math.min(y0, prov.bbox[1]);
    x1 = Math.max(x1, prov.bbox[2]); y1 = Math.max(y1, prov.bbox[3]);
    h = Math.max(h, prov.h);
  }
  if (!isFinite(x0)) return null;
  return [x0, y0, x1, y1, h];
}

function selectRegion(region, opts) {
  const o = opts || {};
  state.region = region;
  applyRegionHighlight();
  renderRegionBar();
  renderLoops();
  if (region) {
    const rect = regionRect(region);
    buildCards(citiesOf(region.name));
    flyTo(Object.assign(fitRect(rect, { pitch: 21, mx: 0.72, my: 0.62 }),
      { duration: o.duration || 1050 }));
    setLegend(`${region.name} · ${region.subtitle} —— ${region.blurb}`);
  } else {
    const picks = nationalPicks();
    buildCards(picks);
    flyTo(Object.assign(fitRect(state.chinaRect, { pitch: 26, mx: 0.8, my: 0.7 }), { duration: 950 }));
    setLegend(`全国视角 · 精选 ${picks.length} 城（评分最高）· 点上方大区进入区块看全部城市`);
  }
}

function citiesOf(regionName) {
  return state.data.cities.filter((c) => c.region === regionName);
}
function nationalPicks(limit) {
  return state.data.cities.slice()
    .sort((a, b) => (b.score || 0) - (a.score || 0))
    .slice(0, limit || 18);
}

function renderRegionBar() {
  const bar = $("map-regions");
  const active = state.region ? state.region.name : null;
  const keepSearch = ($("map-search") && $("map-search").value) || "";
  bar.innerHTML = "";
  const all = document.createElement("button");
  all.type = "button";
  all.className = "region-chip" + (active ? "" : " active");
  all.innerHTML = `<span class="dot" style="background:#9fd8ff"></span>全国`;
  all.addEventListener("click", () => selectRegion(null));
  bar.appendChild(all);
  state.data.regions.forEach((r) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "region-chip" + (active === r.name ? " active" : "");
    b.style.setProperty("--chip", r.color);
    b.innerHTML = `<span class="dot" style="background:${r.color}"></span>${r.name}` +
      `<span class="count">${r.city_count}</span>`;
    b.title = `${r.subtitle} · ${r.blurb}`;
    b.addEventListener("click", () => selectRegion(r));
    bar.appendChild(b);
  });
  const search = document.createElement("input");
  search.type = "search";
  search.id = "map-search";
  search.value = keepSearch;
  search.placeholder = "搜城市，如 敦煌 / 喀什";
  search.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const q = search.value.trim();
    if (!q) return;
    const hit = state.data.cities.find((c) => c.name === q) ||
      state.data.cities.find((c) => c.name.includes(q)) ||
      state.data.cities.find((c) => (c.province || "").includes(q));
    if (!hit) { setLegend(`没有找到「${q}」`); return; }
    const region = state.data.regions.find((r) => r.name === hit.region);
    if (region && (!state.region || state.region.name !== region.name)) selectRegion(region);
    setTimeout(() => focusCity(hit.name), 700);
    search.blur();
  });
  bar.appendChild(search);
}

function focusCity(name) {
  const city = state.data.cityIndex[name];
  if (!city) return;
  const p = lcc(city.lon, city.lat);
  flyTo({ tx: p[0], ty: p[1], dist: Math.min(cam.dist, 1700), pitch: Math.max(cam.pitch, 24), duration: 900 });
  const el = state.cards.get(name);
  if (el) {
    el.classList.remove("flash");
    void el.offsetWidth;
    el.classList.add("flash");
  }
}

function setLegend(text) { const el = $("map-legend"); if (el) el.textContent = text; }

/* ---------------------------------------------------------- 环线 */
function lifecycleActive(loop) {
  return state.activeLoop && state.activeLoop.id === loop.id;
}

function renderLoops() {
  const box = $("map-loops");
  const active = state.region ? state.region.name : null;
  box.innerHTML = "";
  const list = state.data.loops.slice().sort((a, b) => {
    const at = active ? (a.regions || []).includes(active) ? 0 : 1 : 0;
    const bt = active ? (b.regions || []).includes(active) ? 0 : 1 : 0;
    return at - bt || (a.days || 0) - (b.days || 0);
  });
  list.forEach((loop) => {
    const item = document.createElement("div");
    item.className = "loop-item" + (lifecycleActive(loop) ? " active" : "");
    const d = state.travelMode === "transit" ? null : loop.drive;
    item.innerHTML =
      `<div class="loop-title">${escapeHtml(loop.name)}
           ${loop.cross_region ? '<span class="tag cross">跨大区</span>' : ""}
           ${loop.plan_mode === "reference" ? '<span class="tag ref">参考线路</span>' : ""}
         </div>
         <div class="loop-sub">${escapeHtml(loop.cities.join(" → "))}</div>
         <div class="loop-meta">${loop.days} 天 · ${escapeHtml(loop.season || "全年")}</div>
         ${d ? `<div class="loop-drive">🚗 自驾 <b>${d.km} 公里</b> · 建议 ${d.days} 天<br>
            ${escapeHtml((d.roads || []).slice(0, 2).join(" · "))}</div>` : ""}`;
    item.addEventListener("mouseenter", () => highlightLoop(loop, true));
    item.addEventListener("mouseleave", () => highlightLoop(loop, false));
    item.addEventListener("click", () => focusLoop(loop));
    box.appendChild(item);
  });
  buildLoopLabels();
}

function highlightLoop(loop, on) {
  const tag = $("map-legend");
  if (!tag) return;
  if (on) {
    tag.dataset.prev = tag.dataset.prev || tag.textContent;
    tag.textContent = `${loop.name}：${loop.blurb}`;
  } else if (tag.dataset.prev) {
    tag.textContent = tag.dataset.prev;
    delete tag.dataset.prev;
  }
}

function buildLoopLabels() {
  const layer = $("map-loops-layer");
  state.loopLabels.forEach((x) => x.el.remove());
  state.loopLabels = [];
  const frag = document.createDocumentFragment();
  state.data.loops.forEach((loop) => {
    if (!loop.stops || loop.stops.length < 2 || !loop.bounds) return;
    let sx = 0, sy = 0, n = 0;
    loop.stops.forEach((s) => {
      if (s.lon == null) return;
      const p = lcc(s.lon, s.lat);
      sx += p[0]; sy += p[1]; n++;
    });
    if (!n) return;
    const el = document.createElement("button");
    el.type = "button";
    el.className = "loop-label";
    el.textContent = loop.name;
    el.addEventListener("click", (e) => { e.stopPropagation(); focusLoop(loop); });
    el.addEventListener("mouseenter", () => highlightLoop(loop, true));
    el.addEventListener("mouseleave", () => highlightLoop(loop, false));
    frag.appendChild(el);
    const wx = sx / n, wy = sy / n;
    state.loopLabels.push({
      loop, el,
      world: new THREE.Vector3(wx, heightAtWorld(wx, wy) + 30, -wy),
    });
  });
  layer.appendChild(frag);
}

function focusLoop(loop) {
  state.activeLoop = loop;
  applyRegionHighlight();
  renderLoops();
  const points = (loop.stops || []).filter((s) => s.lon != null).map((s) => lcc(s.lon, s.lat));
  if (!points.length) return;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  points.forEach((p) => {
    x0 = Math.min(x0, p[0]); x1 = Math.max(x1, p[0]);
    y0 = Math.min(y0, p[1]); y1 = Math.max(y1, p[1]);
  });
  const pad = loop.cross_region ? 420 : 200;
  const rect = [x0 - pad, y0 - pad, x1 + pad, y1 + pad, 180];
  const targets = [];
  if (loop.cross_region) {
    targets.push(Object.assign(fitRect(state.chinaRect, { pitch: 30, mx: 0.72, my: 0.6 }), { duration: 780 }));
  }
  targets.push(Object.assign(fitRect(rect, { pitch: loop.cross_region ? 17 : 20, mx: 0.7, my: 0.6 }),
    { duration: loop.cross_region ? 1150 : 1000 }));
  flyTo(targets, {
    onDone: () => buildCards(loop.stops.map((s) => state.data.cityIndex[s.name]).filter(Boolean)),
  });
  setLegend(`${loop.name}（${loop.cities.join(" → ")}）· ${loop.blurb}`);
  showLoopDetail(loop);
}

function showLoopDetail(loop) {
  const panel = $("map-loops-panel");
  panel.classList.remove("collapsed");
  const toggle = panel.querySelector(".panel-toggle");
  if (toggle) toggle.textContent = "–";
  let box = $("map-loop-detail");
  if (!box) {
    box = document.createElement("div");
    box.id = "map-loop-detail";
    box.className = "loop-detail";
    panel.insertBefore(box, panel.querySelector(".panel-body"));
  }
  const drive = state.travelMode === "transit" ? null : loop.drive;
  box.innerHTML =
    `<div class="ld-head">${escapeHtml(loop.name)}
        <button type="button" class="ld-close" title="关闭">✕</button></div>
       <div class="ld-blurb">${escapeHtml(loop.blurb)}</div>
       <div class="ld-meta">${loop.cities.length} 站 · ${loop.days} 天 · ${escapeHtml(loop.season || "全年")}
         ${loop.cross_region ? " · 跨大区" : ""}</div>
       ${drive ? `<div class="loop-drive">
          🚗 <b>自驾约 ${drive.km} 公里 · 建议 ${drive.days} 天</b><br>
          路线：${escapeHtml((drive.roads || []).join(" → "))}<br>
          ${escapeHtml(drive.road_note || "")}<br>
          季节：${escapeHtml(drive.season || "全年")}<br>
          取还车：${escapeHtml(drive.rental?.pickup || "")}${drive.rental?.one_way ? ` 取 / ${escapeHtml(drive.rental?.dropoff || "")} 还（异地）` : " 原地取还"} ·
          ${escapeHtml(drive.rental?.car || "")}<br>
          ${escapeHtml(drive.rental?.note || "")}
        </div>
        <ul class="ld-tips">${(drive.warnings || []).map((t) => `<li>⚠ ${escapeHtml(t)}</li>`).join("")}</ul>`
      : ""}
       <ul class="ld-tips">${(loop.tips || []).map((t) => `<li>${escapeHtml(t)}</li>`).join("")}</ul>
       <button type="button" class="primary ld-use">用这条环线排行程</button>`;
  box.querySelector(".ld-close").addEventListener("click", () => box.remove());
  box.querySelector(".ld-use").addEventListener("click", () => {
    const cities = loop.stops.map((s) => state.data.cityIndex[s.name]).filter(Boolean);
    state.selected = cities.map((c) => ({ name: c.name, stay: autoStay(c) }));
    state.closed = true;
    $("m-closed").checked = true;
    renderPicked();
    refreshBadges();
    updateRouteLine(true);
    const verb = state.travelMode === "transit" ? "规划乘车表" : (state.travelMode === "rental" ? "生成行程 + 租车单" : "生成自驾行程");
    setLegend(`已载入「${loop.name}」，可以在右侧改停留方式后点「${verb}」`);
    $("map-form").classList.remove("map-form-hidden");
    const stage = $("map-stage");
    if (stage) stage.classList.add("selection-done");
    $("map-form").scrollIntoView({ behavior: "smooth", block: "center" });
  });
}

function autoStay(city) {
  const days = Number(city.days || 1);
  if (days < 0.9) return { mode: "halfday", nights: 0 };
  if (days < 2) return { mode: "nights", nights: 1 };
  if (days < 3) return { mode: "nights", nights: 2 };
  return { mode: "nights", nights: 3 };
}

/* ---------------------------------------------------------- 选择 */
function toggleCity(name) {
  const idx = state.selected.findIndex((s) => s.name === name);
  if (idx >= 0) {
    state.selected.splice(idx, 1);
  } else {
    const city = state.data.cityIndex[name];
    if (!city) return;
    if (state.selected.length >= 12) {
      setLegend("一次最多 12 站，先规划一段或删掉几站");
      return;
    }
    state.selected.push({ name, stay: autoStay(city) });
  }
  renderPicked();
  refreshBadges();
  updateRouteLine();
}

function renderPicked() {
  const list = $("map-picked");
  $("map-picked-count").textContent = String(state.selected.length);
  list.innerHTML = "";
  state.selected.forEach((item, i) => {
    const city = state.data.cityIndex[item.name] || { name: item.name };
    const li = document.createElement("li");
    li.className = "picked-item";
    li.style.setProperty("--tint", regionColorOf(city.region || ""));
    const value = item.stay.mode === "nights" ? `nights:${item.stay.nights}` : item.stay.mode;
    li.innerHTML =
      `<span class="pi-no">${i + 1}</span>
         <span class="pi-name">${escapeHtml(item.name)}<em>${escapeHtml(city.region || "")}</em></span>
         <span class="pi-tools">
           <button type="button" class="pi-btn" data-act="guide" title="完整旅游攻略">📖</button>
           <button type="button" class="pi-btn" data-act="up" title="上移">↑</button>
           <button type="button" class="pi-btn" data-act="down" title="下移">↓</button>
           <button type="button" class="pi-btn" data-act="fly" title="在地图上定位">◎</button>
           <button type="button" class="pi-btn danger" data-act="del" title="移除">✕</button>
         </span>
         <select class="pi-stay" title="这站怎么玩">
           ${STAY_OPTIONS.map((o) => `<option value="${o.value}"${o.value === value ? " selected" : ""}>${o.label}</option>`).join("")}
         </select>`;
    li.querySelector(".pi-stay").addEventListener("change", (e) => {
      const v = e.target.value;
      item.stay = v.startsWith("nights:")
        ? { mode: "nights", nights: parseInt(v.split(":")[1], 10) || 1 }
        : { mode: v, nights: 0 };
    });
    li.querySelectorAll(".pi-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const act = btn.dataset.act;
        if (act === "guide") { openGuide(item.name); return; }
        if (act === "del") state.selected.splice(i, 1);
        else if (act === "up" && i > 0) state.selected.splice(i - 1, 0, state.selected.splice(i, 1)[0]);
        else if (act === "down" && i < state.selected.length - 1) {
          state.selected.splice(i + 1, 0, state.selected.splice(i, 1)[0]);
        } else if (act === "fly") { focusCity(item.name); return; }
        renderPicked();
        refreshBadges();
        updateRouteLine();
      });
    });
    li.addEventListener("mouseenter", () => {
      const el = state.cards.get(item.name);
      if (el) el.classList.add("hover-sync");
    });
    li.addEventListener("mouseleave", () => {
      const el = state.cards.get(item.name);
      if (el) el.classList.remove("hover-sync");
    });
    list.appendChild(li);
  });
  $("map-picked-hint").hidden = state.selected.length > 0;
}

function optimizeOrder() {
  if (state.selected.length < 4) return;
  const head = state.selected[0];
  const tail = state.closed === false ? state.selected[state.selected.length - 1] : null;
  const middles = state.selected.slice(1, tail ? -1 : undefined);
  const d = (a, b) => {
    const ca = state.data.cityIndex[a.name], cb = state.data.cityIndex[b.name];
    if (!ca || !cb) return 1e9;
    return Math.hypot(ca.lon - cb.lon, (ca.lat - cb.lat) * 1.3);
  };
  const rest = middles.slice();
  const order = [];
  let cur = head;
  while (rest.length) {
    let bi = 0, bd = Infinity;
    rest.forEach((m, i) => { const v = d(cur, m); if (v < bd) { bd = v; bi = i; } });
    cur = rest.splice(bi, 1)[0];
    order.push(cur);
  }
  const full = tail ? [head, ...order, tail] : [head, ...order];
  const cost = (arr) => {
    let s = 0;
    for (let i = 0; i < arr.length - 1; i++) s += d(arr[i], arr[i + 1]);
    return s;
  };
  let improved = true, guard = 0;
  while (improved && guard++ < 40) {
    improved = false;
    for (let i = 1; i < full.length - 2; i++) {
      for (let j = i + 1; j < full.length - 1; j++) {
        const cand = full.slice(0, i).concat(full.slice(i, j + 1).reverse(), full.slice(j + 1));
        if (cost(cand) < cost(full) - 1e-9) {
          full.length = 0; full.push(...cand); improved = true;
        }
      }
    }
  }
  state.selected = full;
  renderPicked();
  refreshBadges();
  updateRouteLine();
  setLegend("已按地理距离重排中间城市（起终点不动）");
}

/* ---------------------------------------------------------- 规划 */
function planPayload() {
  const useBudget = $("m-budget").checked;
  return {
    date: $("m-date").value,
    cities: state.selected.map((s) => s.name),
    closed: $("m-closed").checked,
    stays: state.selected.map((s) => Object.assign({ mode: s.stay.mode, nights: s.stay.nights || 0 })),
    time: $("m-time").value,
    mode: $("m-mode").value,
    max_transfers: 2,
    links: $("m-links").checked,
    guide: $("m-guide").checked,
    buffer_min: parseInt($("m-buffer").value, 10) || 0,
    objective: $("m-objective").value,
    slack_hours: 2,
    max_days: useBudget ? (parseInt($("m-maxdays").value, 10) || null) : null,
  };
}

async function submitPlan(e) {
  if (e) e.preventDefault();
  if (state.selected.length < 2) {
    TP().setStatus && TP().setStatus("m-status", "至少选 2 座城市", true);
    return;
  }
  const go = $("m-go");
  go.disabled = true;
  const drive = state.travelMode !== "transit";
  TP().setStatus && TP().setStatus("m-status", drive
    ? "规划中…（每段都在走高德驾车路径，首次较慢）"
    : "规划中…（每段都在查真实车次，跨大区会慢一点）");
  $("map-results").innerHTML = "";
  $("map-guides").innerHTML = "";
  if ($("map-rental")) $("map-rental").innerHTML = "";
  clearLegChips();
  try {
    const res = drive
      ? await TP().postJSON("/api/roam/drive", drivePayload())
      : await TP().postJSON("/api/roam/plan", planPayload());
    if (!res.ok) {
      TP().setStatus && TP().setStatus("m-status", res.error || "规划失败", true);
      return;
    }
    TP().setStatus && TP().setStatus("m-status", "");
    if (drive) renderDrive(res);
    else renderPlan(res);
  } catch (err) {
    TP().setStatus && TP().setStatus("m-status", "请求失败: " + err.message, true);
  } finally {
    go.disabled = false;
  }
}

/** 自驾 / 租车请求体 */
function drivePayload() {
  return {
    date: $("m-date").value,
    cities: state.selected.map((s) => s.name),
    closed: $("m-closed").checked,
    stays: state.selected.map((s) => Object.assign({ mode: s.stay.mode, nights: s.stay.nights || 0 })),
    time: $("m-time").value,
    with_rental: state.travelMode === "rental",
    car_class: ($("m-car") && $("m-car").value) || "SUV",
    max_drive_hours: parseFloat(($("m-maxdrive") && $("m-maxdrive").value) || "6.5"),
  };
}

/* ---------------------------------------------------------- 自驾结果 */
function clearLegChips() {
  state.legChips.forEach((c) => c.el.remove());
  state.legChips = [];
}

/** 自驾模式下在地图上标出每段里程 */
function renderLegChips(res) {
  clearLegChips();
  const layer = $("map-loops-layer");
  if (!layer || state.travelMode === "transit") return;
  const cities = (res.order_cities || []).map((n) => state.data.cityIndex[n]).filter(Boolean);
  (res.legs || []).forEach((leg, i) => {
    const d = leg.drive;
    if (!d) return;
    const a = state.data.cityIndex[leg.frm], b = state.data.cityIndex[leg.to];
    if (!a || !b) return;
    const mid = lcc((a.lon + b.lon) / 2, (a.lat + b.lat) / 2);
    const el = document.createElement("span");
    el.className = "leg-km";
    el.textContent = `D${leg.day_no} · ${d.distance_km}km · ${d.hours_text}`;
    layer.appendChild(el);
    state.legChips.push({
      el,
      world: new THREE.Vector3(mid[0], heightAtWorld(mid[0], mid[1]) + 90, -mid[1]),
    });
  });
}

function layoutLegChips() {
  for (const chip of state.legChips) {
    const q = projectPoint(chip.world);
    if (!q) { chip.el.style.display = "none"; continue; }
    chip.el.style.display = "";
    chip.el.style.transform = `translate3d(${q[0].toFixed(1)}px, ${q[1].toFixed(1)}px, 0) translate(-50%,-50%)`;
    chip.el.style.zIndex = String(Math.max(1, 3200 - Math.round(q[2])));
  }
}

function renderDrive(res) {
  const box = $("map-results");
  const s = res.summary || {};
  const head = document.createElement("div");
  head.className = "summary roam-summary";
  head.innerHTML =
    `<b>${(res.order_cities || []).join(" → ")}</b>` + (res.closed ? " → 回到起点" : "") +
    `<br><span class="dim">🚗 ${s.city_count} 城 · ${s.total_km} 公里 · 驾驶 ${s.drive_hours} 小时 · ` +
    `全程 ${s.total_days} 天 · 过路费约 ¥${s.tolls} · 油费约 ¥${s.fuel}` +
    `${s.estimated ? "（含估算路段）" : "（高德真实里程）"}</span>`;
  box.appendChild(head);
  (res.start_notes || []).forEach((n) => {
    const d = document.createElement("div");
    d.className = "warn note";
    d.textContent = "ℹ " + n;
    box.appendChild(d);
  });
  (res.warnings || []).forEach((w) => {
    const d = document.createElement("div");
    d.className = "warn";
    d.textContent = "⚠ " + w;
    box.appendChild(d);
  });

  if (res.rental) renderRentalPanel(res.rental);

  // 逐日车程
  const title = document.createElement("div");
  title.className = "roam-subtitle";
  title.textContent = "逐日自驾行程";
  box.appendChild(title);
  (res.legs || []).forEach((rec) => {
    const card = document.createElement("div");
    card.className = "card day-card";
    const head2 = document.createElement("div");
    head2.className = "day-head";
    head2.innerHTML = `D${rec.day_no} · ${escapeHtml(rec.date)} · ${escapeHtml(rec.frm)} → ${escapeHtml(rec.to)}` +
      (rec.stay ? `<span class="stay">停留: ${escapeHtml(rec.stay)}</span>` : "");
    card.appendChild(head2);
    if (rec.error) {
      const err = document.createElement("div");
      err.className = "warn";
      err.textContent = "!! " + rec.error;
      card.appendChild(err);
      box.appendChild(card);
      return;
    }
    const d = rec.drive;
    const line = document.createElement("div");
    line.className = "drive-leg";
    line.innerHTML =
      `<span class="dl-km">${d.distance_km} 公里</span>` +
      `<span class="dl-time">驾驶 ${escapeHtml(d.hours_text)}` +
      (d.break_minutes ? `（含休息 ${d.break_minutes} 分钟）` : "") + `</span>` +
      `<span>${escapeHtml(d.dep_text)} 出发 → ${escapeHtml(d.arr_text)} 到达</span>` +
      `<span class="dl-cost">过路费约 ¥${Math.round(d.tolls)} · 油费约 ¥${d.fuel}</span>` +
      `<span class="dl-src">${escapeHtml(d.source || "")}</span>`;
    card.appendChild(line);
    if (d.note) {
      const note = document.createElement("div");
      note.className = "drive-note";
      note.textContent = d.note;
      card.appendChild(note);
    }
    box.appendChild(card);
  });

  const foot = document.createElement("div");
  foot.className = "summary";
  foot.textContent = `共 ${s.total_days} 天 · 全程 ${s.total_km} 公里`;
  box.appendChild(foot);

  renderCityGuides(res.cities || []);
  renderLegChips(res);
}

/** 租车费用面板 */
function renderRentalPanel(q) {
  const host = $("map-rental");
  if (!host) return;
  state.rentalPlan = q;
  const card = document.createElement("div");
  card.className = "card rental-card";
  card.innerHTML =
    `<h3>🔑 租车费用估算 <span class="hint" style="font-weight:400">` +
    `${escapeHtml(q.pickup)} 取车 · ${escapeHtml(q.car_class)} · ${escapeHtml(q.tier)} 档（${escapeHtml(q.tier_label)}）</span></h3>
     ${q.car_class_note ? `<p class="hint">${escapeHtml(q.car_class_note)}</p>` : ""}
     <div class="rental-grid">
       <div class="rental-cell"><div class="rc-label">日租（${escapeHtml(q.car_class)}）</div>
         <div class="rc-value">¥${q.daily_low}-${q.daily_high}</div>
         <div class="rc-sub">可选车型：${escapeHtml((q.car_options || []).join(" / "))}</div></div>
       <div class="rental-cell"><div class="rc-label">租期</div>
         <div class="rc-value">${q.days} 天</div>
         <div class="rc-sub">按行程天数计（含游玩日）</div></div>
       <div class="rental-cell"><div class="rc-label">保险</div>
         <div class="rc-value">¥${q.insurance_per_day[0]}-${q.insurance_per_day[1]}/天</div>
         <div class="rc-sub">基础险到全险区间</div></div>
       <div class="rental-cell"><div class="rc-label">${q.one_way ? "异地还车费" : "取还车"}</div>
         <div class="rc-value">${q.one_way ? "¥" + q.one_way_fee : "原地取还"}</div>
         <div class="rc-sub">${q.one_way ? `${q.dropoff} 还车 · 约 ${q.one_way_km} 公里` : "同城取还，无异地费"}</div></div>
       <div class="rental-cell"><div class="rc-label">油费</div>
         <div class="rc-value">¥${q.fuel}</div>
         <div class="rc-sub">${q.km} 公里 · ${q.consumption}L/100km · ¥${q.fuel_price}/L</div></div>
       <div class="rental-cell"><div class="rc-label">过路费</div>
         <div class="rc-value">¥${q.tolls}</div>
         <div class="rc-sub">高德驾车路径给出的实际金额</div></div>
     </div>
     <div class="rental-total">预计总花费 <b>¥${q.total_low} - ${q.total_high}</b>
       <span class="hint">（租金 ¥${q.rent_only_low}-${q.rent_only_high}${q.one_way ? ` + 异地 ¥${q.one_way_fee}` : ""} + 油费过路费 ¥${q.running_low}-${q.running_high}）</span></div>
     <p class="hint" style="margin-top:10px">${escapeHtml(q.disclaimer)}</p>
     <div class="rental-platforms">
       ${(q.platforms || []).map((p) => `<a class="rental-platform" href="${escapeHtml(p.url)}" target="_blank" rel="noopener">
          <b>${escapeHtml(p.name)} →</b><em>${escapeHtml(p.note || "")}</em></a>`).join("")}
     </div>
     <p class="hint">提示：把取车城市填成「${escapeHtml(q.pickup)}」、日期选 ${escapeHtml($("m-date").value)}，在平台上按 ${q.days} 天查询即可看到实时价；
      ${q.one_way ? "异地还车要单独勾选，费用通常比这里估的更高。" : "异地还车会额外收费，需在平台上勾选。"}</p>`;
  host.appendChild(card);
  host.scrollIntoView({ behavior: "smooth", block: "center" });
}

function renderPlan(res) {
  const box = $("map-results");
  const guides = $("map-guides");
  const s = res.summary || {};
  const head = document.createElement("div");
  head.className = "summary roam-summary";
  head.innerHTML =
    `<b>${(res.order_cities || []).join(" → ")}</b>` + (res.closed ? " → 回到起点" : "") +
    `<br><span class="dim">${s.city_count} 城 · ${(res.regions || []).join("/")} · 全程 ${s.total_days} 天 · ` +
    `建议游览 ${s.play_hours} 小时 · 在途 ${s.travel_hours} 小时` +
    (s.price_text ? ` · ${s.price_text}` : "") + `</span>`;
  box.appendChild(head);
  (res.start_notes || []).forEach((n) => {
    const d = document.createElement("div");
    d.className = "warn note";
    d.textContent = "ℹ " + n;
    box.appendChild(d);
  });
  (res.warnings || []).forEach((w) => {
    const d = document.createElement("div");
    d.className = "warn";
    d.textContent = "⚠ " + w;
    box.appendChild(d);
  });
  if (res.budget) {
    const d = document.createElement("div");
    d.className = "warn";
    d.innerHTML = `全程 ${res.total_days} 天，超出预算 ${res.budget.max_days} 天 ${res.budget.over_by_days} 天。删站建议：` +
      res.budget.suggestions.map((x) => `删「${escapeHtml(x.remove)}」→ ${x.days} 天`).join("；");
    box.appendChild(d);
  }
  const gtitle = document.createElement("div");
  gtitle.className = "roam-subtitle";
  gtitle.textContent = "每座城市怎么玩";
  guides.appendChild(gtitle);
  renderCityGuides(res.cities || [], guides);
  const rtitle = document.createElement("div");
  rtitle.className = "roam-subtitle";
  rtitle.textContent = "逐日乘车表";
  box.appendChild(rtitle);
  const dayCard = TP().dayCard;
  if (typeof dayCard === "function") {
    (res.legs || []).forEach((rec) => box.appendChild(dayCard(rec)));
  }
  const foot = document.createElement("div");
  foot.className = "summary";
  foot.textContent = `共 ${res.total_days || s.total_days} 天`;
  box.appendChild(foot);
  const sug = res.suggestions || {};
  const chips = (sug.near_route || []).map((x) => ({ ...x, why: `距路线 ${x.km} 公里` }));
  if (chips.length) {
    const sbox = document.createElement("div");
    sbox.className = "summary suggestion-box";
    sbox.innerHTML = `<b>顺路可加</b><div class="sugg-chips">` +
      chips.map((x) => `<button type="button" class="sugg-chip" data-city="${escapeHtml(x.name)}">
            ${escapeHtml(x.name)}<em>${x.score} · ${escapeHtml(x.why)}</em></button>`).join("") + `</div>`;
    sbox.querySelectorAll(".sugg-chip").forEach((b) => {
      b.addEventListener("click", () => {
        const city = state.data.cityIndex[b.dataset.city];
        if (!city) return;
        if (!state.selected.some((s2) => s2.name === city.name)) {
          state.selected.splice(Math.max(1, state.selected.length - (state.closed === false ? 1 : 0)),
            0, { name: city.name, stay: autoStay(city) });
          renderPicked();
          refreshBadges();
          updateRouteLine();
        }
        setLegend(`已把「${city.name}」插进路线，重新点「规划乘车表」即可`);
      });
    });
    box.insertBefore(sbox, box.firstChild.nextSibling);
  }
}

/* ---------------------------------------------------------- 交互 */
/** 舞台里所有「不该被地图拖拽接管」的可交互区域 */
const INTERACTIVE_SELECTOR = [
  ".city-card", ".loop-label", ".leg-km",
  ".map-panel", ".map-regions", ".map-controls",
  ".map-tip",            // 悬停提示卡上的按钮
  ".city-guide", ".cg-panel",   // 完整攻略弹层（含 ✕ 关闭）
  ".map-error",          // 出错提示里的「重新加载地图」
].join(", ");

function bindStage() {
  const stage = $("map-stage");
  const localX = (e) => e.clientX - canvas.getBoundingClientRect().left;
  const localY = (e) => e.clientY - canvas.getBoundingClientRect().top;

  stage.addEventListener("contextmenu", (e) => e.preventDefault());
  stage.addEventListener("pointerdown", (e) => {
    // 这些区域里的交互不属于地图拖拽：一旦在这里 setPointerCapture，
    // 后续 click 会被重定向到 stage，弹层里的按钮就点不动了。
    if (e.target.closest(INTERACTIVE_SELECTOR)) return;
    const mode = (e.button === 2 || e.button === 1) ? (e.shiftKey ? "bearing" : "pitch") : "pan";
    drag = {
      x: e.clientX, y: e.clientY, px: localX(e), py: localY(e),
      moved: 0, mode, pitch: cam.pitch, bearing: cam.bearing,
    };
    stage.classList.add("dragging");
    hideTip();
    try { stage.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
  });
  stage.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    drag.moved = Math.max(drag.moved, Math.abs(dx) + Math.abs(dy));
    if (drag.mode === "pan") {
      const x = localX(e), y = localY(e);
      const from = groundPoint(drag.px, drag.py);
      const to = groundPoint(x, y);
      if (from && to) {
        cam.tx += from[0] - to[0];
        cam.ty += from[1] - to[1];
        setCamera({});
      } else {
        panByPixels(x - drag.px, y - drag.py);
      }
      drag.px = x;
      drag.py = y;
    } else if (drag.mode === "pitch") {
      setCamera({ pitch: clamp(drag.pitch + dy * 0.25, 6, 68) });
      const el = $("map-pitch");
      if (el) el.value = String(Math.round(cam.pitch));
    } else {
      setCamera({ bearing: drag.bearing + dx * 0.32 });
    }
    drag.x = e.clientX;
    drag.y = e.clientY;
  });
  const endDrag = (e) => {
    if (!drag) return;
    const wasClick = drag.moved < 5;
    drag = null;
    stage.classList.remove("dragging");
    if (wasClick && e) handleStageClick(e);
  };
  stage.addEventListener("pointerup", endDrag);
  stage.addEventListener("pointercancel", () => { drag = null; stage.classList.remove("dragging"); });
  stage.addEventListener("wheel", (e) => {
    // 面板/攻略弹层里自己的滚动交给浏览器，别被地图缩放抢走
    if (e.target.closest(".map-panel, .city-guide, .cg-panel")) return;
    e.preventDefault();
    const px = localX(e), py = localY(e);
    if (e.ctrlKey || e.metaKey) { zoomAt(px, py, Math.exp(-e.deltaY * 0.01)); return; }
    if (e.shiftKey) { panByPixels(e.deltaY + e.deltaX, 0); return; }
    const mouseWheel = e.deltaMode !== 0 || Math.abs(e.deltaY) >= 40 || Math.abs(e.deltaX) >= 40;
    if (mouseWheel) zoomAt(px, py, Math.exp(-clamp(e.deltaY, -150, 150) * 0.0016));
    else panByPixels(e.deltaX, e.deltaY);
  }, { passive: false });
  stage.addEventListener("dblclick", (e) => {
    if (e.target.closest(INTERACTIVE_SELECTOR)) return;
    zoomAt(localX(e), localY(e), 0.6);
  });
}

/** 点省份 -> 进入大区（射线与省份网格求交，三维下也准确） */
function handleStageClick(ev) {
  if (!camera || !state.groups.provinces) return;
  const rect = canvas.getBoundingClientRect();
  const ndc = new THREE.Vector2(
    ((ev.clientX - rect.left) / W) * 2 - 1,
    -((ev.clientY - rect.top) / H) * 2 + 1);
  raycaster.setFromCamera(ndc, camera);
  const hits = raycaster.intersectObjects(state.groups.provinces.children, false);
  if (!hits.length) return;
  const name = (hits[0].object.name || "").replace("province:", "");
  const prov = state.provinces.find((p) => p.short === name);
  if (!prov) return;
  const region = provinceRegion(prov.short);
  if (region && (!state.region || state.region.name !== region.name)) selectRegion(region);
  else setLegend(`${prov.name} · ${region ? region.name + " · " + region.blurb : "未分组"}`);
}

function escapeAction() {
  const guide = $("city-guide");
  if (guide && !guide.hidden) { closeGuide(); return "已关闭攻略"; }
  if (isFull()) { toggleFullscreen(false); return "已退出全屏"; }
  const detail = $("map-loop-detail");
  if (detail) { detail.remove(); return "已关闭环线说明"; }
  if (state.activeLoop) {
    state.activeLoop = null;
    applyRegionHighlight();
    renderLoops();
    return "已取消环线高亮";
  }
  if (state.selected.length) {
    state.selected = [];
    renderPicked();
    refreshBadges();
    updateRouteLine();
    return "已清空所选城市";
  }
  if (state.region) { selectRegion(null); return "已返回全国视角"; }
  return "";
}

/* ---------------------------------------------------------- 全屏 */
function isFull() {
  const stage = $("map-stage");
  return !!(document.fullscreenElement || (stage && stage.classList.contains("full")));
}
function toggleFullscreen(want) {
  const stage = $("map-stage");
  if (!stage) return;
  const on = want === undefined ? !isFull() : !!want;
  stage.classList.toggle("full", on);
  document.body.classList.toggle("map-locked", on);
  const btn = $("map-full");
  if (btn) btn.textContent = on ? "⛶ 退出全屏" : "⛶ 全屏";
  if (on && stage.requestFullscreen) {
    try {
      const p = stage.requestFullscreen();
      if (p && p.catch) p.catch(() => { });
    } catch (err) { /* 保留 CSS 全屏 */ }
  } else if (!on && document.fullscreenElement && document.exitFullscreen) {
    try {
      const p = document.exitFullscreen();
      if (p && p.catch) p.catch(() => { });
    } catch (err) { /* 忽略 */ }
  }
  setTimeout(resizeAndRender, 60);
  setTimeout(resizeAndRender, 260);
}
function bindFullscreen() {
  const btn = $("map-full");
  if (btn) btn.addEventListener("click", () => toggleFullscreen());
  document.addEventListener("fullscreenchange", () => {
    const on = !!document.fullscreenElement;
    const stage = $("map-stage");
    if (stage) stage.classList.toggle("full", on || stage.classList.contains("full"));
    document.body.classList.toggle("map-locked", on);
    const b = $("map-full");
    if (b) b.textContent = on ? "⛶ 退出全屏" : "⛶ 全屏";
    resizeAndRender();
    setTimeout(resizeAndRender, 200);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const msg = escapeAction();
    if (msg) {
      e.preventDefault();
      setLegend(msg + "　（Esc 继续逐层退出：环线说明 → 环线高亮 → 所选城市 → 大区 → 全国）");
    }
  });
}

function bindControls() {
  $("map-home").addEventListener("click", () =>
    flyTo(Object.assign(fitRect(state.chinaRect, { pitch: 26, mx: 0.8, my: 0.7 }), { duration: 900 })));
  $("map-reset").addEventListener("click", () => {
    state.activeLoop = null;
    const d = $("map-loop-detail");
    if (d) d.remove();
    selectRegion(null);
  });
  $("map-pitch").addEventListener("input", (e) => setCamera({ pitch: Number(e.target.value) }));
  const iconsBtn = $("map-icons-toggle");
  if (iconsBtn) iconsBtn.addEventListener("click", () => setIcons(!state.showIcons));
  const left = $("map-rotate-left"), right = $("map-rotate-right");
  if (left) left.addEventListener("click", () => setCamera({ bearing: cam.bearing - 25 }));
  if (right) right.addEventListener("click", () => setCamera({ bearing: cam.bearing + 25 }));
  $("map-clear").addEventListener("click", () => {
    state.selected = [];
    renderPicked();
    refreshBadges();
    updateRouteLine();
  });
  $("map-optimize").addEventListener("click", optimizeOrder);
  $("m-closed").addEventListener("change", () => {
    state.closed = $("m-closed").checked;
    updateRouteLine();
  });
  $("m-budget").addEventListener("change", () => {
    $("m-maxdays-field").classList.toggle("tour-hidden", !$("m-budget").checked);
  });
  document.querySelectorAll(".panel-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const panel = $(btn.dataset.target);
      panel.classList.toggle("collapsed");
      btn.textContent = panel.classList.contains("collapsed") ? "+" : "–";
    });
  });
  $("map-form").addEventListener("submit", submitPlan);
  document.querySelectorAll(".tm-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      setTravelMode(btn.dataset.mode);
      const verb = MODE_BUTTON[state.travelMode];
      setLegend(`${btn.querySelector("b")?.textContent || ""}模式：${MODE_HINT[state.travelMode]}`
        + `　选好城市后点「${verb}」`);
    });
  });
  window.addEventListener("resize", () => resizeAndRender());
  if (typeof ResizeObserver === "function") {
    try { new ResizeObserver(() => resizeAndRender()).observe($("map-stage")); } catch (err) { /* 忽略 */ }
  }
}

/* ---------------------------------------------------------- 初始化 */
function showError(title, detail, retry) {
  const box = $("map-error");
  if (!box) return;
  box.hidden = false;
  box.innerHTML =
    `<div class="me-title">${escapeHtml(title)}</div>` +
    `<div class="me-detail">${escapeHtml(detail || "")}</div>` +
    (retry ? `<button type="button" class="ghost-sm me-retry">重新加载地图</button>` : "");
  const btn = box.querySelector(".me-retry");
  if (btn) {
    btn.addEventListener("click", () => {
      box.hidden = true;
      state.ready = false;
      state.loading = false;
      loadData();
    });
  }
}
function clearError() {
  const box = $("map-error");
  if (box) { box.hidden = true; box.innerHTML = ""; }
}

function initGL() {
  if (!canvas) return false;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas, antialias: true, alpha: true, powerPreference: "high-performance",
    });
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.setClearColor(0x000000, 0);
    state.gl = renderer;
    return true;
  } catch (err) {
    state.gl = null;
    renderer = null;
    return false;
  }
}

function resizeAndRender() {
  if (!canvas) return;
  const host = canvas.parentElement || canvas;
  const rect = host.getBoundingClientRect();
  W = Math.max(320, Math.round(rect.width));
  H = Math.max(320, Math.round(rect.height));
  dpr = Math.min(2, window.devicePixelRatio || 1);
  canvas.style.width = W + "px";
  canvas.style.height = H + "px";
  if (renderer) renderer.setSize(W, H, false);
  else { canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr); }
  if (camera) {
    camera.aspect = W / H;
    camera.updateProjectionMatrix();
  }
  syncCamera();
}

function frame() {
  requestAnimationFrame(frame);
  if (!state.ready) return;
  if (canvas.offsetParent === null) return;
  try {
    const t = performance.now() / 1000;
    updateRoutePulses(t);
    updateRouteLine();          // 视距变化时自动换粗细（键相同则直接返回）
    if (renderer) renderer.render(scene, camera);
    layoutCards();
    layoutLegChips();
    state.frameError = null;
  } catch (err) {
    if (!state.frameError) {
      state.frameError = err;
      console.error("[地图漫游] 渲染出错", err);
      showError("地图渲染出错", (err && err.message) || String(err), false);
    }
  }
}

/* ---------------------------------------------------------- 数据装载 */
function prepareGeo(geo) {
  state.provinces = [];
  let maxH = 0;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const prov of geo.provinces) {
    const h = BASE_H + (ELEV[prov.short] || 0.2) * EXAG;
    maxH = Math.max(maxH, h);
    const rings = [];
    let px0 = Infinity, py0 = Infinity, px1 = -Infinity, py1 = -Infinity, sx = 0, sy = 0, n = 0;
    for (const raw of prov.rings) {
      const ring = raw.map((p) => lcc(p[0], p[1]));
      let area = 0;
      for (let i = 0; i < ring.length - 1; i++) {
        area += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1];
      }
      if (area < 0) ring.reverse();     // 统一逆时针，挤出方向一致
      rings.push(ring);
      for (const p of ring) {
        px0 = Math.min(px0, p[0]); py0 = Math.min(py0, p[1]);
        px1 = Math.max(px1, p[0]); py1 = Math.max(py1, p[1]);
        sx += p[0]; sy += p[1]; n++;
      }
    }
    if (!n) continue;
    const region = provinceRegion(prov.short);
    state.provinces.push({
      short: prov.short, name: prov.name,
      region: region ? region.name : "",
      color: region ? region.color : "#8fa6a3",
      h, rings, bbox: [px0, py0, px1, py1], cx: sx / n, cy: sy / n,
    });
    x0 = Math.min(x0, px0); y0 = Math.min(y0, py0);
    x1 = Math.max(x1, px1); y1 = Math.max(y1, py1);
  }
  state.maxH = maxH;
  const pad = 420;
  state.chinaRect = [x0 - pad, y0 - pad, x1 + pad, y1 + pad, maxH];
  state.plate = [x0 - 900, y0 - 900, x1 + 900, y1 + 900];
}

function prepareRoam(roam) {
  state.data = { regions: roam.regions, cities: roam.cities, loops: roam.loops, cityIndex: {} };
  roam.cities.forEach((c) => { state.data.cityIndex[c.name] = c; });
  // 城市卡片悬浮高度：按所在省的地形高度抬升，避免插进地形里
  roam.cities.forEach((c) => {
    const p = lcc(c.lon, c.lat);
    c.h = heightAtWorld(p[0], p[1]);
  });
}

function prepareNature(nature) {
  if (!nature) { state.nature = null; return; }
  const toWorld = (pt) => {
    const p = lcc(pt[0], pt[1]);
    return { x: p[0], y: p[1], h: 0 };
  };
  const rivers = (nature.rivers || []).map((r) => {
    const pts = r.points.map(toWorld);
    pts.forEach((p) => { p.h = heightAtWorld(p.x, p.y) + 2.5; });
    return { name: r.name, width: r.width || 1.2, pts };
  });
  const lakes = (nature.lakes || []).map((l) => {
    const pts = l.points.map(toWorld);
    pts.forEach((p) => { p.h = heightAtWorld(p.x, p.y) + 3.5; });
    return { name: l.name, pts };
  });
  const ranges = (nature.ranges || []).map((r) => {
    const pts = r.points.map(toWorld);
    pts.forEach((p) => { p.h = heightAtWorld(p.x, p.y) + 6; });
    return { name: r.name, snow: !!r.snow, pts };
  });
  state.nature = { rivers, lakes, ranges };
}

async function loadData() {
  if (state.loading) return;
  state.loading = true;
  setLegend("正在加载地图数据…");
  try {
    const [geoResp, roamResp, natureResp] = await Promise.all([
      fetch("/static/data/china.geo.json"),
      fetch("/api/roam/map"),
      fetch("/static/data/china.nature.json", { cache: "no-cache" }).catch(() => null),
    ]);
    if (!geoResp.ok) {
      throw new Error("缺少地图几何数据 web/data/china.geo.json（可运行 scripts/fetch_china_geo.py 生成）");
    }
    if (!roamResp.ok) {
      if (roamResp.status === 404) {
        throw new Error("服务端还在跑旧版本代码：/api/roam/map 不存在。" +
          "请停掉后重新启动 python -m travel_planner.web（或 scripts/run_web.ps1）");
      }
      throw new Error(`/api/roam/map 返回 HTTP ${roamResp.status}`);
    }
    const geo = await geoResp.json();
    const roam = await roamResp.json();
    if (!geo || !Array.isArray(geo.provinces) || !geo.provinces.length) {
      throw new Error("china.geo.json 内容不是预期的省级轮廓数据");
    }
    if (!roam || !Array.isArray(roam.regions) || !Array.isArray(roam.cities) || !roam.cities.length) {
      throw new Error("/api/roam/map 返回的数据不完整（regions/cities 为空）");
    }
    state.pendingRegions = roam.regions;
    heightCache.clear();
    prepareGeo(geo);
    prepareRoam(roam);
    try {
      prepareNature(natureResp && natureResp.ok ? await natureResp.json() : null);
    } catch (err) {
      state.nature = null;
    }
    if (!state.gl) initGL();
    if (!state.gl) {
      throw new Error("这台设备的浏览器没有启用 WebGL，无法绘制立体地图。" +
        "请在 chrome://gpu 里确认硬件加速已开启，或改用 Chrome / Edge 打开。");
    }
    state.ready = true;
    state.loading = false;
    clearError();
    buildScene();
    resizeAndRender();
    setCamera(Object.assign(fitRect(state.chinaRect, { pitch: 20, mx: 0.8, my: 0.7 }), {}));
    renderRegionBar();
    renderLoops();
    renderPicked();
    buildLoopLabels();
    buildCards(nationalPicks());
    setLegend(`已载入 ${roam.cities.length} 座城市资料 · ${roam.loops.length} 条经典环线 · ` +
      `点大区进入区块，鼠标停一下看半天怎么玩`);
  } catch (err) {
    state.loading = false;
    state.ready = false;
    console.error("[地图漫游] 加载失败", err);
    showError("地图没能显示出来", err && err.message ? err.message : String(err), true);
    setLegend("地图数据加载失败，可在画面上点「重新加载地图」重试");
  }
}

function initCanvas() {
  canvas = $("map-canvas");
  if (!canvas) return false;
  const dateEl = $("m-date");
  if (dateEl && !dateEl.value) {
    dateEl.value = TP().defaultDate ? TP().defaultDate(5) : new Date().toISOString().slice(0, 10);
  }
  resizeAndRender();
  return true;
}

/* ---------------------------------------------------------- 对外接口 */
window.DSHTravelMap = {
  onShow() {
    if (!state.ready && !state.loading) loadData();
    else if (state.ready) resizeAndRender();
  },
  __debug: {
    state, cam, loadData, prepareGeo, prepareRoam, prepareNature, buildScene,
    lcc, projectPoint, fitRect, groundPoint, zoomAt, panByPixels,
    selectRegion, buildCards, focusLoop, clearCards, renderPicked, renderLoops,
    toggleCity, escapeAction, toggleFullscreen, isFull, updateRouteLine, layoutCards,
    setIcons,
    openGuide, closeGuide, guideBlockHtml, fetchCityDetail, updateRoutePulses,
    setTravelMode, renderDrive, renderRentalPanel, renderLegChips, layoutLegChips,
    routePalette, refreshRentalBadges, showTip, hideTip, hideTipSoon,
    get scene() { return scene; },
    get camera() { return camera; },
    boot(geo, roamPayload, nature) {
      if (!canvas && !initCanvas()) return { error: "no canvas" };
      state.pendingRegions = roamPayload.regions;
      heightCache.clear();
      prepareGeo(geo);
      prepareRoam(roamPayload);
      prepareNature(nature || null);
      initGL();
      state.ready = true;
      buildScene();
      resizeAndRender();
      setCamera(Object.assign(fitRect(state.chinaRect, { pitch: 20, mx: 0.8, my: 0.7 }), {}));
      renderRegionBar();
      renderLoops();
      renderPicked();
      buildLoopLabels();
      buildCards(nationalPicks());
      let meshes = 0, instanced = 0, lines = 0, verts = 0;
      scene.traverse((o) => {
        if (o.isInstancedMesh) { instanced += o.count; verts += o.geometry.attributes.position.count * o.count; }
        else if (o.isMesh) { meshes++; verts += o.geometry.attributes.position.count; }
        else if (o.isLine) lines++;
      });
      return {
        provinces: state.provinces.length,
        cities: state.data.cities.length,
        regions: state.data.regions.length,
        loops: state.data.loops.length,
        loopLabels: state.loopLabels.length,
        nationalCards: state.cardList.length,
        meshes, instanced, lines, verts,
        webgl: !!state.gl,
        nature: state.nature
          ? { rivers: state.nature.rivers.length, lakes: state.nature.lakes.length, ranges: state.nature.ranges.length }
          : null,
        chinaRect: state.chinaRect,
        dist: cam.dist,
      };
    },
  },
};

function bootstrap() {
  if (!initCanvas()) return;
  bindStage();
  bindControls();
  bindFullscreen();
  renderPicked();
  requestAnimationFrame(frame);
}
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bootstrap);
} else {
  bootstrap();
}
