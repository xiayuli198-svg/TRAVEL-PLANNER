/* 地图漫游 —— 立体中国地图（Canvas 2D 自绘，无第三方依赖）
 *
 * 设计要点：
 *  · 投影：Lambert 等角圆锥（标准纬线 25°N / 47°N），单位公里，符合中国地图习惯；
 *  · 立体：每个省是一块有厚度的「板」，厚度 = 基准 + 地形抬升，青藏明显隆起；
 *  · 视角：低仰角透视相机（默认俯角 20°），地平线在上方、地形向观众压过来；
 *  · 交互：拖拽平移 / 滚轮缩放 / 点大区或省份飞行 / 点城市卡片编号入列；
 *  · 城市卡片是 DOM 层，悬停弹出半日游建议，点击记录为旅游城市 1、2、3…
 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const D2R = Math.PI / 180;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const lerp = (a, b, t) => a + (b - a) * t;
  const TP = () => window.TP || {};

  /* ---------------------------------------------------------- 投影 */
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

  /* 地形抬升（公里，真实海拔的夸张表达）：越高越亮，青藏成为高原 */
  const ELEV = {
    西藏: 4.6, 青海: 3.6, 新疆: 1.3, 云南: 1.7, 四川: 1.6, 甘肃: 1.5, 贵州: 1.1,
    内蒙古: 1.0, 陕西: 1.0, 山西: 0.9, 宁夏: 1.1, 重庆: 0.6, 湖北: 0.4, 湖南: 0.3,
    江西: 0.2, 福建: 0.4, 浙江: 0.25, 安徽: 0.2, 江苏: 0.06, 上海: 0.02, 山东: 0.15,
    河南: 0.3, 河北: 0.5, 北京: 0.2, 天津: 0.05, 辽宁: 0.3, 吉林: 0.5, 黑龙江: 0.4,
    广东: 0.2, 广西: 0.4, 海南: 0.2, 台湾: 0.7, 香港: 0.1, 澳门: 0.02,
  };
  const BASE_H = 36;      // 基准板厚（公里）
  const EXAG = 22;        // 地形夸张倍数
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
    region: null,        // 当前大区
    selected: [],        // [{name, stay:{mode,nights}}]
    activeLoop: null,
    hoverCity: null,
    ready: false,
    loading: false,
    cards: new Map(),    // name -> element
    cardList: [],
    loopLabels: [],
    animating: false,
    closed: true,
    nature: null,
    frameTimes: [],
  };

  const cam = { tx: 0, ty: 0, dist: 6400, pitch: 20, bearing: 0 };
  let W = 0, H = 0, dpr = 1, focal = 900, cx = 0, cy = 0;
  let canvas, ctx, baseCv, baseCtx, baseDirty = true, baseScale = 1;
  let drag = null, pinch = null;

  /* ---------------------------------------------------------- 相机数学 */
  let V = null;

  /** 世界点 -> 屏幕（带深度）；相机背后返回 null */
  function project(p) {
    const dx = p[0] - V.pos[0], dy = p[1] - V.pos[1], dz = (p[2] || 0) - V.pos[2];
    const z = dx * V.f[0] + dy * V.f[1] + dz * V.f[2];
    if (z < NEAR) return null;
    const x = dx * V.r[0] + dy * V.r[1] + dz * V.r[2];
    const y = dx * V.u[0] + dy * V.u[1] + dz * V.u[2];
    return [cx + focal * x / z, cy - focal * y / z, z];
  }

  /** 某点所在省份的地形高度（公里）；用于河流/山脉贴地和相机防钻地 */
  const heightCache = new Map();
  function heightAtWorld(x, y) {
    const key = `${Math.round(x / 40)},${Math.round(y / 40)}`;
    if (heightCache.has(key)) return heightCache.get(key);
    let h = BASE_H;
    for (const prov of state.provinces) {
      if (!pointInRings(x, y, prov.rings.coarse)) continue;
      h = prov.h;
      break;
    }
    if (heightCache.size > 20000) heightCache.clear();
    heightCache.set(key, h);
    return h;
  }

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

  function view() {
    const bearing = cam.bearing * D2R;
    // 防钻地：低俯角 + 贴近地面时，相机高度不能低于当地地形
    const groundH = state.provinces.length ? heightAtWorld(cam.tx, cam.ty) : BASE_H;
    const minZ = groundH + 14;
    let pitchDeg = cam.pitch;
    if (cam.dist * Math.sin(pitchDeg * D2R) < minZ) {
      pitchDeg = Math.asin(clamp(minZ / cam.dist, 0, 1)) / D2R;
    }
    const pitch = pitchDeg * D2R;
    const horiz = cam.dist * Math.cos(pitch);
    const pos = [cam.tx - horiz * Math.sin(bearing), cam.ty - horiz * Math.cos(bearing),
    cam.dist * Math.sin(pitch)];
    const tgt = [cam.tx, cam.ty, 0];
    let f = [tgt[0] - pos[0], tgt[1] - pos[1], tgt[2] - pos[2]];
    const fl = Math.hypot(f[0], f[1], f[2]) || 1;
    f = [f[0] / fl, f[1] / fl, f[2] / fl];
    let r = [f[1], -f[0], 0];
    const rl = Math.hypot(r[0], r[1]) || 1;
    r = [r[0] / rl, r[1] / rl, 0];
    const u = [r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0]];
    return { pos, f, r, u, pitch: pitchDeg };
  }

  /** 屏幕像素 -> 地面(z=0)交点，用于「抓住鼠标下的地面」式平移与光标缩放 */
  function groundPoint(px, py) {
    if (!V) return null;
    const sx = (px - cx) / focal, sy = -(py - cy) / focal;
    const dir = [
      V.f[0] + V.r[0] * sx + V.u[0] * sy,
      V.f[1] + V.r[1] * sx + V.u[1] * sy,
      V.f[2] + V.r[2] * sx + V.u[2] * sy,
    ];
    if (Math.abs(dir[2]) < 1e-6) return null;
    const t = -V.pos[2] / dir[2];
    if (t <= 0 || t > 60000) return null;
    return [V.pos[0] + dir[0] * t, V.pos[1] + dir[1] * t];
  }

  function zoomAt(px, py, factor) {
    const before = groundPoint(px, py);
    setCamera({ dist: cam.dist * factor });
    const after = groundPoint(px, py);
    if (before && after) {
      setCamera({ tx: cam.tx + (before[0] - after[0]), ty: cam.ty + (before[1] - after[1]) });
    }
  }

  function panByPixels(dx, dy) {
    const dxw = dx * cam.dist / focal;
    const dyw = dy * cam.dist / focal;
    const fx = V.f[0], fy = V.f[1];
    const fl = Math.hypot(fx, fy) || 1;
    const k = Math.max(0.32, Math.sin(V.pitch * D2R));   // 低俯角时纵向要移动更多地面距离
    cam.tx -= dxw * V.r[0] + (dyw * (fx / fl)) / k;
    cam.ty -= dxw * V.r[1] + (dyw * (fy / fl)) / k;
    setCamera({});
  }

  /* ---- 相机空间 + 近平面裁剪：放大到贴着地面时地图也不会缺一块 ---- */
  const NEAR = 22;
  function camSpace(x, y, z) {
    const dx = x - V.pos[0], dy = y - V.pos[1], dz = (z || 0) - V.pos[2];
    return [
      dx * V.r[0] + dy * V.r[1] + dz * V.r[2],
      dx * V.u[0] + dy * V.u[1] + dz * V.u[2],
      dx * V.f[0] + dy * V.f[1] + dz * V.f[2],
    ];
  }
  const projCam = (c) => [cx + focal * c[0] / c[2], cy - focal * c[1] / c[2]];

  /** Sutherland–Hodgman：把相机空间多边形按 z >= NEAR 裁掉相机背后的部分 */
  function clipNear(poly) {
    const out = [];
    const n = poly.length;
    for (let i = 0; i < n; i++) {
      const a = poly[i], b = poly[(i + 1) % n];
      const ain = a[2] >= NEAR, bin = b[2] >= NEAR;
      if (ain) out.push(a);
      if (ain !== bin) {
        const t = (NEAR - a[2]) / (b[2] - a[2]);
        out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, NEAR]);
      }
    }
    return out;
  }

  /** 世界折线/多边形（带每点高度）裁剪后投影：返回屏幕点数组或 null */
  function worldPolyScreen(points, heights, close) {
    const cam = [];
    for (let i = 0; i < points.length; i++) {
      cam.push(camSpace(points[i][0], points[i][1], heights ? heights[i] : heights));
    }
    const clipped = clipNear(cam);
    if (clipped.length < 2) return null;
    const out = new Array(clipped.length);
    for (let i = 0; i < clipped.length; i++) out[i] = projCam(clipped[i]);
    if (close && out.length < 3) return null;
    return out;
  }

  function screenPath(path, pts, close) {
    if (!pts || pts.length < 2) return false;
    path.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) path.lineTo(pts[i][0], pts[i][1]);
    if (close) path.closePath();
    return true;
  }

  function setCamera(p) {
    if (p.tx != null) cam.tx = p.tx;
    if (p.ty != null) cam.ty = p.ty;
    if (p.dist != null) cam.dist = clamp(p.dist, 260, 13000);
    if (p.pitch != null) cam.pitch = clamp(p.pitch, 6, 68);
    if (p.bearing != null) cam.bearing = p.bearing;
    V = view();
    baseDirty = true;
  }

  function screenSize(rect, camLike) {
    const saved = { ...cam };
    setCamera(camLike);
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    const corners = [
      [rect[0], rect[1], 0], [rect[2], rect[1], 0], [rect[2], rect[3], 0], [rect[0], rect[3], 0],
      [rect[0], rect[1], rect[4] || 0], [rect[2], rect[1], rect[4] || 0],
      [rect[2], rect[3], rect[4] || 0], [rect[0], rect[3], rect[4] || 0],
    ];
    for (const c of corners) {
      const p = project(c);
      if (!p) continue;
      x0 = Math.min(x0, p[0]); x1 = Math.max(x1, p[0]);
      y0 = Math.min(y0, p[1]); y1 = Math.max(y1, p[1]);
    }
    setCamera(saved);
    if (!isFinite(x0)) return [Infinity, Infinity];
    return [x1 - x0, y1 - y0];
  }

  /** 让世界矩形（公里）落在视口内：迭代求相机距离 */
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
      dist = clamp(dist / clamp(k, 0.2, 5), 260, 13000);
      if (Math.abs(k - 1) < 0.03) break;
    }
    return { tx: center[0], ty: center[1], dist, pitch: o.pitch, bearing: o.bearing };
  }

  /* ---------------------------------------------------------- 补间动画 */
  const EASE = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

  function flyTo(targets, opts) {
    const list = Array.isArray(targets) ? targets : [targets];
    const o = Object.assign({ duration: 900, onDone: null }, opts || {});
    const from = { tx: cam.tx, ty: cam.ty, dist: cam.dist, pitch: cam.pitch, bearing: cam.bearing };
    let index = 0;
    const step = () => {
      if (index >= list.length) {
        state.animating = false;
        baseDirty = true;
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

  /* ---------------------------------------------------------- 底图渲染 */
  function hexToRgb(hex) {
    const h = (hex || "#6ee7c8").replace("#", "");
    const v = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
    return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
  }
  function shade(rgb, k, add) {
    const a = add || 0;
    return `rgb(${clamp(rgb[0] * k + a, 0, 255) | 0},${clamp(rgb[1] * k + a, 0, 255) | 0},${clamp(rgb[2] * k + a, 0, 255) | 0})`;
  }
  const SKY_TOP = "#04121b", SKY_MID = "#0a2c37", SKY_HORIZON = "#17505a";

  function renderBase(scale) {
    baseScale = scale;
    const w = Math.max(1, Math.round(W * dpr * scale));
    const h = Math.max(1, Math.round(H * dpr * scale));
    if (baseCv.width !== w || baseCv.height !== h) { baseCv.width = w; baseCv.height = h; }
    const g = baseCtx;
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, w, h);
    g.save();
    g.scale(dpr * scale, dpr * scale);

    // 天空
    const sky = g.createLinearGradient(0, 0, 0, H);
    sky.addColorStop(0, SKY_TOP);
    sky.addColorStop(0.42, SKY_MID);
    sky.addColorStop(1, SKY_HORIZON);
    g.fillStyle = sky;
    g.fillRect(0, 0, W, H);

    // 暗角：让画面更像一张立体地图而不是平面色块
    const vig = g.createRadialGradient(W / 2, H * 0.48, Math.min(W, H) * 0.22,
      W / 2, H * 0.5, Math.max(W, H) * 0.78);
    vig.addColorStop(0, "rgba(0,0,0,0)");
    vig.addColorStop(1, "rgba(0,0,0,.42)");
    g.fillStyle = vig;
    g.fillRect(0, 0, W, H);

    // 海面/底盘
    const plate = state.plate;
    if (plate) {
      const p = [
        project([plate[0], plate[1], -2]), project([plate[2], plate[1], -2]),
        project([plate[2], plate[3], -2]), project([plate[0], plate[3], -2]),
      ];
      if (p.every(Boolean)) {
        g.beginPath();
        g.moveTo(p[0][0], p[0][1]);
        for (let i = 1; i < 4; i++) g.lineTo(p[i][0], p[i][1]);
        g.closePath();
        const sea = g.createLinearGradient(0, 0, 0, H);
        sea.addColorStop(0, "rgba(12,54,64,.35)");
        sea.addColorStop(1, "rgba(20,86,92,.55)");
        g.fillStyle = sea;
        g.fill();
        g.strokeStyle = "rgba(140,220,215,.10)";
        g.lineWidth = 1;
        g.stroke();
        // 经纬网格
        g.strokeStyle = "rgba(150,225,220,.055)";
        for (let lon = 75; lon <= 135; lon += 5) {
          g.beginPath();
          let started = false;
          for (let lat = 15; lat <= 55; lat += 2) {
            const q = project([...lcc(lon, lat), -2]);
            if (!q) { started = false; continue; }
            if (!started) { g.moveTo(q[0], q[1]); started = true; } else g.lineTo(q[0], q[1]);
          }
          g.stroke();
        }
        for (let lat = 15; lat <= 55; lat += 5) {
          g.beginPath();
          let started = false;
          for (let lon = 75; lon <= 135; lon += 2) {
            const q = project([...lcc(lon, lat), -2]);
            if (!q) { started = false; continue; }
            if (!started) { g.moveTo(q[0], q[1]); started = true; } else g.lineTo(q[0], q[1]);
          }
          g.stroke();
        }
      }
    }

    // 省份（由远及近）
    const active = state.region ? state.region.name : null;
    const items = [];
    for (const prov of state.provinces) {
      const c = project([prov.cx, prov.cy, prov.h]);
      if (!c) continue;
      items.push({ prov, depth: c[2], screen: c });
    }
    items.sort((a, b) => b.depth - a.depth);
    const maxH = state.maxH || 1;

    for (const item of items) {
      const prov = item.prov;
      const inRegion = !active || prov.region === active;
      const rgb = hexToRgb(prov.color);
      const heightT = clamp((prov.h - BASE_H) / Math.max(1, maxH - BASE_H), 0, 1);
      const fog = clamp((item.depth - 2600) / 7200, 0, 0.72);

      const wall = new Path2D();
      const top = new Path2D();
      const rings = state.animating || cam.dist > 3600 ? prov.rings.coarse : prov.rings.fine;
      for (const ring of rings) {
        // 顶面：相机空间裁剪后投影（放大到贴地也不会整块消失）
        const topPts = worldPolyScreen(ring, prov.h, true);
        if (topPts) screenPath(top, topPts, true);
        // 侧面：逐边裁剪，只画朝向相机的一侧
        for (let i = 0; i + 1 < ring.length; i++) {
          const a = ring[i], b = ring[i + 1];
          const ex = b[0] - a[0], ey = b[1] - a[1];
          const nx = ey, ny = -ex;                       // CCW 环 -> 外法线
          if (nx * (V.pos[0] - a[0]) + ny * (V.pos[1] - a[1]) <= 0) continue;
          const quad = worldPolyScreen([a, b, b, a], [prov.h, prov.h, 0, 0], true);
          if (quad) screenPath(wall, quad, true);
        }
      }
      const k = inRegion ? 1 : 0.34;
      const wallColor = shade(rgb, (0.34 + 0.22 * heightT) * k, active && inRegion ? 10 : 0);
      g.fillStyle = mixFog(wallColor, fog);
      g.fill(wall);

      // 顶面：大区色 + 轻微地形色（越高越接近雪线白），让高原一眼看出来
      let topColor = shade(rgb, (0.86 + 0.5 * heightT) * k, active && inRegion ? 18 : 0);
      if (heightT > 0.45) {
        const m = /rgb\((\d+),(\d+),(\d+)\)/.exec(topColor);
        if (m) {
          const t = (heightT - 0.45) * 0.9;
          topColor = `rgb(${Math.round(lerp(+m[1], 236, t))},` +
            `${Math.round(lerp(+m[2], 246, t))},${Math.round(lerp(+m[3], 255, t))})`;
        }
      }
      g.fillStyle = mixFog(topColor, fog * 0.75);
      g.fill(top);
      g.strokeStyle = active && inRegion
        ? "rgba(255,255,255,.34)" : "rgba(6,26,32,.5)";
      g.lineWidth = active && inRegion ? 1.1 : 0.7;
      g.stroke(top);
      prov._path = top;
    }

    // 山川湖河（示意层）
    drawNature(g, active);

    // 地平线雾
    const horizonY = project([cam.tx, cam.ty + 6000, 0]);
    const hy = horizonY ? clamp(horizonY[1], 0, H) : H * 0.3;
    const haze = g.createLinearGradient(0, Math.max(0, hy - H * 0.42), 0, Math.min(H, hy + H * 0.16));
    haze.addColorStop(0, "rgba(9,42,52,.92)");
    haze.addColorStop(0.55, "rgba(13,58,68,.45)");
    haze.addColorStop(1, "rgba(16,70,78,0)");
    g.fillStyle = haze;
    g.fillRect(0, 0, W, H);
    g.restore();
    baseDirty = false;
  }

  let fogColor = [16, 62, 72];
  function mixFog(color, amount) {
    if (amount <= 0.001) return color;
    const m = /rgb\((\d+),(\d+),(\d+)\)/.exec(color);
    if (!m) return color;
    const r = Math.round(lerp(+m[1], fogColor[0], amount));
    const g = Math.round(lerp(+m[2], fogColor[1], amount));
    const b = Math.round(lerp(+m[3], fogColor[2], amount));
    return `rgb(${r},${g},${b})`;
  }

  /* ---------------------------------------------------------- 山川湖河
   * 手绘示意数据（web/data/china.nature.json），高度按所在省份贴地，
   * 不追求写实，只求一眼看出中国的地形骨架。
   */
  function prepareNature(nature) {
    if (!nature) { state.nature = null; return; }
    const toWorld = (pt) => {
      const p = lcc(pt[0], pt[1]);
      return { x: p[0], y: p[1], h: 0 };
    };
    const rivers = (nature.rivers || []).map((r) => {
      const pts = r.points.map(toWorld);
      pts.forEach((p) => { p.h = heightAtWorld(p.x, p.y) + 2.5; });
      return { name: r.name, width: r.width || 1.4, pts };
    });
    const lakes = (nature.lakes || []).map((l) => {
      const pts = l.points.map(toWorld);
      pts.forEach((p) => { p.h = heightAtWorld(p.x, p.y) + 3.5; });
      return { name: l.name, pts };
    });
    const ranges = (nature.ranges || []).map((r) => {
      const pts = r.points.map(toWorld);
      pts.forEach((p) => { p.h = heightAtWorld(p.x, p.y) + 4.5; });
      return { name: r.name, snow: !!r.snow, pts };
    });
    state.nature = { rivers, lakes, ranges };
  }

  function drawNature(g, active) {
    const nat = state.nature;
    if (!nat) return;
    const dim = active ? 0.45 : 1;
    const scale = clamp(2600 / cam.dist, 0.55, 2.4);

    // 湖泊
    for (const lake of nat.lakes) {
      const pts = lake.pts.map((p) => [p.x, p.y]);
      const hs = lake.pts.map((p) => p.h);
      const screen = worldPolyScreen(pts, hs, true);
      if (!screen || screen.length < 3) continue;
      const path = new Path2D();
      screenPath(path, screen, true);
      g.fillStyle = `rgba(96,190,224,${0.72 * dim})`;
      g.fill(path);
      g.strokeStyle = `rgba(190,235,255,${0.5 * dim})`;
      g.lineWidth = 0.9;
      g.stroke(path);
    }

    // 河流：分段裁剪后描边（外侧一圈柔光是「水」的感觉）
    const strokeRivers = (width, color) => {
      g.save();
      g.strokeStyle = color;
      g.lineWidth = width;
      g.lineJoin = "round";
      g.lineCap = "round";
      for (const river of nat.rivers) {
        g.beginPath();
        for (let i = 0; i + 1 < river.pts.length; i++) {
          const a = river.pts[i], b = river.pts[i + 1];
          const seg = worldPolyScreen([[a.x, a.y], [b.x, b.y]], [a.h, b.h], false);
          if (!seg || seg.length < 2) continue;
          g.moveTo(seg[0][0], seg[0][1]);
          g.lineTo(seg[1][0], seg[1][1]);
        }
        g.stroke();
      }
      g.restore();
    };
    for (const river of nat.rivers) {
      strokeRivers(river.width * scale * 2.6, `rgba(70,170,210,${0.16 * dim})`);
    }
    for (const river of nat.rivers) {
      strokeRivers(Math.max(0.9, river.width * scale), `rgba(140,220,245,${0.85 * dim})`);
    }

    // 山脉：脊线 + 三角符号（经典地图画法）
    for (const range of nat.ranges) {
      const pts = range.pts.map((p) => [p.x, p.y]);
      const hs = range.pts.map((p) => p.h);
      const screen = worldPolyScreen(pts, hs, false);
      if (screen) {
        g.save();
        g.strokeStyle = `rgba(255,244,225,${0.30 * dim})`;
        g.lineWidth = 1.1;
        g.setLineDash([3, 3]);
        g.beginPath();
        g.moveTo(screen[0][0], screen[0][1]);
        for (let i = 1; i < screen.length; i++) g.lineTo(screen[i][0], screen[i][1]);
        g.stroke();
        g.restore();
      }
      // 沿脊线撒三角（按屏幕间距铺，避免挤在一起）
      const projected = [];
      for (let i = 0; i < pts.length; i++) {
        const q = project([pts[i][0], pts[i][1], hs[i]]);
        if (q) projected.push(q);
      }
      for (let i = 0; i + 1 < projected.length; i++) {
        const a = projected[i], b = projected[i + 1];
        const dist = Math.hypot(b[0] - a[0], b[1] - a[1]);
        const count = clamp(Math.round(dist / 26), 1, 26);
        for (let k = 0; k < count; k++) {
          const t = (k + 0.5) / count;
          const x = lerp(a[0], b[0], t), y = lerp(a[1], b[1], t);
          const depth = lerp(a[2], b[2], t);
          const size = clamp(1300 / depth, 0.5, 2.6) * 5.2;
          g.beginPath();
          g.moveTo(x, y - size);
          g.lineTo(x + size * 0.78, y + size * 0.42);
          g.lineTo(x - size * 0.78, y + size * 0.42);
          g.closePath();
          g.fillStyle = range.snow
            ? `rgba(246,251,255,${0.86 * dim})`
            : `rgba(214,196,166,${0.78 * dim})`;
          g.fill();
          // 背光面
          g.beginPath();
          g.moveTo(x, y - size);
          g.lineTo(x + size * 0.78, y + size * 0.42);
          g.lineTo(x, y + size * 0.42);
          g.closePath();
          g.fillStyle = `rgba(38,52,58,${0.30 * dim})`;
          g.fill();
        }
      }
    }
  }

  /* ---------------------------------------------------------- 主循环 */
  function resize() {
    const host = canvas.parentElement || canvas;
    const rect = host.getBoundingClientRect();
    W = Math.max(320, Math.round(rect.width));
    H = Math.max(320, Math.round(rect.height));
    dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    focal = (H / 2) / Math.tan(38 * D2R / 2);
    cx = W / 2;
    cy = H * 0.56;
    V = view();
    baseDirty = true;
  }

  function frame() {
    requestAnimationFrame(frame);
    if (!state.ready) return;
    // 切到别的页签时不做无谓渲染
    if (canvas.offsetParent === null) return;
    try {
      if (baseDirty) renderBase(state.animating ? 0.68 : 1);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      ctx.drawImage(baseCv, 0, 0, W, H);
      drawOverlay();
      layoutCards();
      state.frameError = null;
    } catch (err) {
      // 单帧出错不再让整个渲染循环静默死掉，把原因显示出来
      if (!state.frameError) {
        state.frameError = err;
        console.error("[地图漫游] 渲染出错", err);
        showError("地图渲染出错", (err && err.message) || String(err), false);
      }
    }
  }

  /* ---------------------------------------------------------- 覆盖层：路线与环线 */
  function worldOf(city) {
    const p = lcc(city.lon, city.lat);
    return [p[0], p[1]];
  }

  function polyline(points, opts) {
    const o = Object.assign({ width: 2, color: "rgba(255,255,255,.7)", dash: null, glow: 0, z: 40 }, opts);
    const runs = [];
    let run = [];
    for (const pt of points) {
      const q = project([pt[0], pt[1], o.z]);
      if (!q) { if (run.length > 1) runs.push(run); run = []; continue; }
      run.push(q);
    }
    if (run.length > 1) runs.push(run);
    if (!runs.length) return;
    const stroke = (color, width, dash) => {
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      if (dash) ctx.setLineDash(dash);
      for (const r of runs) {
        ctx.beginPath();
        ctx.moveTo(r[0][0], r[0][1]);
        for (let i = 1; i < r.length; i++) ctx.lineTo(r[i][0], r[i][1]);
        ctx.stroke();
      }
      ctx.restore();
    };
    if (o.glow) stroke(o.glow, o.width * 3.4, null);
    stroke(o.color, o.width, o.dash);
    return runs[0];
  }

  function drawOverlay() {
    const active = state.region ? state.region.name : null;
    const at = performance.now();
    // 经典环线
    for (const loop of state.data.loops) {
      if (!loop.stops || loop.stops.length < 2) continue;
      const pts = loop.stops.filter((s) => s.lon != null).map((s) => worldOf(s));
      if (pts.length < 2) continue;
      if (state.region) pts.push(pts[0]);
      const touching = !active || (loop.regions || []).includes(active);
      const isActive = state.activeLoop && state.activeLoop.id === loop.id;
      const alpha = isActive ? 1 : touching ? (active ? 0.5 : 0.34) : 0.1;
      const color = loop.plan_mode === "reference"
        ? `rgba(255,190,120,${alpha})` : `rgba(126,231,200,${alpha})`;
      polyline(pts, {
        color, width: isActive ? 3 : 1.8, z: 60 + (isActive ? 40 : 0),
        glow: isActive ? "rgba(126,231,200,.22)" : null,
        dash: isActive ? [9, 6] : null,
      });
    }
    // 选中序列
    if (state.selected.length > 1) {
      const pts = state.selected
        .map((s) => state.data.cityIndex[s.name])
        .filter((c) => c && c.lon != null)
        .map((c) => worldOf(c));
      if (pts.length > 1) {
        if (state.closed !== false) pts.push(pts[0]);
        const dash = [10, 7];
        dash[1] = 7 - (at / 46) % 7;
        polyline(pts, {
          color: "rgba(255,226,150,.92)", width: 2.6, z: 120,
          glow: "rgba(255,208,110,.20)", dash,
        });
      }
    }
  }

  /* ---------------------------------------------------------- 城市卡片 */
  /* ---------------------------------------------------------- 城市卡面
   * 没有真实照片时，用「城市海报」矢量图代替：按看点类型决定场景
   * （海岸 / 雪山湖泊 / 古建 / 城市天际线 / 沙丘草原），颜色取自所在大区。
   * 想换成真实照片，把图片放到 web/img/cities/<城市名>.jpg 即可自动使用。
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
  function mixHex(a, b, t) {
    const A = hexToRgb(a), B = hexToRgb(b);
    return `rgb(${Math.round(lerp(A[0], B[0], t))},${Math.round(lerp(A[1], B[1], t))},${Math.round(lerp(A[2], B[2], t))})`;
  }

  function cityArt(city) {
    const rand = rngOf(hashCode(city.name));
    const kinds = new Set(((city.highlights || []).map((x) => x.kind)));
    const tint = regionColor(city.region);
    const uid = "a" + hashCode(city.name).toString(36);
    const Wd = 70, Hd = 86;
    let scene = "mixed";
    if (kinds.has("海岛")) scene = "coast";
    else if (kinds.has("雪山") || kinds.has("湖泊") || kinds.has("自然")) scene = "mountain";
    else if (kinds.has("古镇") || kinds.has("古迹") || kinds.has("寺庙") || kinds.has("园林")) scene = "ancient";
    else if (kinds.has("街区") || kinds.has("夜景") || kinds.has("工业") || kinds.has("美食")) scene = "city";
    const sky1 = mixHex(tint, "#07222c", 0.45);
    const sky2 = mixHex(tint, "#04161e", 0.78);
    const far = mixHex(tint, "#062028", 0.66);
    const mid = mixHex(tint, "#04202a", 0.82);
    const near = "#03161d";
    const glow = mixHex(tint, "#ffffff", 0.35);
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
    // 太阳/月亮
    const sunX = 16 + rand() * 38, sunY = 20 + rand() * 12, sunR = 6 + rand() * 3;
    parts.push(`<circle cx="${sunX.toFixed(1)}" cy="${sunY.toFixed(1)}" r="${sunR.toFixed(1)}" fill="${glow}" opacity="0.55"/>`);

    const ridge = (y0, amp, color, opacity, n) => {
      let d = `M0 ${Hd} L0 ${y0}`;
      for (let i = 1; i <= n; i++) {
        const x = (Wd / n) * i;
        const y = y0 + (rand() - 0.5) * amp * 2;
        d += ` L${x.toFixed(1)} ${y.toFixed(1)}`;
      }
      d += ` L${Wd} ${Hd} Z`;
      parts.push(`<path d="${d}" fill="${color}" opacity="${opacity}"/>`);
    };
    const skyline = (y0, color, opacity, n, minH, maxH) => {
      let x = -4;
      let d = `M0 ${Hd} L0 ${y0}`;
      for (let i = 0; i < n; i++) {
        const w = 6 + rand() * 9;
        const hh = minH + rand() * (maxH - minH);
        d += ` L${x.toFixed(1)} ${(y0 - hh).toFixed(1)} L${(x + w).toFixed(1)} ${(y0 - hh).toFixed(1)}`;
        x += w + 1 + rand() * 3;
        if (x > Wd) break;
      }
      d += ` L${Wd} ${y0} L${Wd} ${Hd} Z`;
      parts.push(`<path d="${d}" fill="${color}" opacity="${opacity}"/>`);
    };

    if (scene === "coast") {
      parts.push(`<rect y="58" width="${Wd}" height="${Hd - 58}" fill="${mixHex(tint, "#04222c", 0.62)}"/>`);
      ridge(56, 3, far, 0.9, 5);
      parts.push(`<path d="M0 62 Q ${Wd * 0.3} 58 ${Wd * 0.55} 62 T ${Wd} 61 L${Wd} ${Hd} L0 ${Hd} Z" fill="${near}" opacity="0.75"/>`);
      for (let i = 0; i < 3; i++) {
        const y = 66 + i * 6;
        parts.push(`<path d="M4 ${y} Q ${Wd * 0.35} ${y - 2} ${Wd * 0.7} ${y + 1} T ${Wd - 4} ${y}" stroke="${glow}" stroke-opacity="${0.3 - i * 0.07}" stroke-width="1.2" fill="none"/>`);
      }
    } else if (scene === "mountain") {
      parts.push(`<path d="M0 ${Hd} L14 40 L26 60 L40 26 L54 56 L${Wd} ${Hd} Z" fill="${far}" opacity="0.85"/>`);
      parts.push(`<path d="M40 26 L34 38 L39 36 L44 42 L47 34 Z" fill="#ffffff" opacity="0.55"/>`);
      ridge(58, 4, mid, 0.95, 6);
      parts.push(`<rect y="72" width="${Wd}" height="${Hd - 72}" fill="${near}" opacity="0.9"/>`);
      parts.push(`<ellipse cx="${(20 + rand() * 30).toFixed(1)}" cy="76" rx="18" ry="3.2" fill="${glow}" opacity="0.22"/>`);
    } else if (scene === "ancient") {
      ridge(54, 3, far, 0.8, 5);
      for (let i = 0; i < 3; i++) {
        const x = 10 + i * 22 + rand() * 6, base = 74, hh = 16 + rand() * 8, w = 16 + rand() * 6;
        parts.push(`<rect x="${x.toFixed(1)}" y="${(base - hh).toFixed(1)}" width="${w.toFixed(1)}" height="${hh.toFixed(1)}" fill="${mid}"/>`);
        parts.push(`<path d="M${(x - 4).toFixed(1)} ${(base - hh).toFixed(1)} L${(x + w / 2).toFixed(1)} ${(base - hh - 6).toFixed(1)} L${(x + w + 4).toFixed(1)} ${(base - hh).toFixed(1)} Z" fill="${near}"/>`);
      }
      parts.push(`<rect y="74" width="${Wd}" height="${Hd - 74}" fill="${near}"/>`);
    } else if (scene === "city") {
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

  function cardHtml(city) {
    const kinds = [...new Set((city.highlights || []).map((h) => h.kind))]
      .slice(0, 3).map((k) => KIND_ICON[k] || "📍").join("");
    const railTag = city.rail ? ""
      : `<span class="cc-tag" title="无铁路客运，需飞机/包车">✈</span>`;
    // 注意：卡面图不能用内联 style 写（url("...") 里的双引号会截断 HTML 属性），
    // 统一在 buildCards 里用 CSSOM 赋值。
    return `<span class="cc-inner">
      <span class="cc-photo">${railTag}<span class="cc-badge" hidden></span></span>
      <span class="cc-name">${escapeHtml(city.name)}</span>
      <span class="cc-kinds">${kinds}</span>
    </span>`;
  }

  function regionColor(name) {
    const r = state.data && state.data.regions.find((x) => x.name === name);
    return r ? r.color : "#6ee7c8";
  }

  function escapeHtml(s) {
    const fn = TP().esc;
    if (fn) return fn(s);
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function buildCards(cities, opts) {
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
        photo.style.setProperty("--tint", regionColor(city.region));
        photo.style.backgroundImage = artUri(city);   // CSSOM 赋值，不经过 HTML 属性
      }
      el.addEventListener("mouseenter", () => showTip(city, el));
      el.addEventListener("focus", () => showTip(city, el));
      el.addEventListener("mouseleave", hideTip);
      el.addEventListener("blur", hideTip);
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
  }

  function clearCards() {
    $("map-cards").innerHTML = "";
    state.cards = new Map();
    state.cardList = [];
    hideTip();
  }

  function layoutCards() {
    const horizonLimit = H * 0.995;
    for (const item of state.cardList) {
      const q = project([item.world[0], item.world[1], (item.city.h || 0) + 40]);
      if (!q || q[1] > horizonLimit) { item.el.style.display = "none"; continue; }
      const pad = 120;
      if (q[0] < -pad || q[0] > W + pad) { item.el.style.display = "none"; continue; }
      item.el.style.display = "";
      // 卡片底边中点锚在城市位置（transform-origin 50% 100%，缩放不移动锚点）
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
    const active = state.region ? state.region.name : null;
    for (const item of state.loopLabels) {
      const loop = item.loop;
      const touching = !active || (loop.regions || []).includes(active);
      const isActive = state.activeLoop && state.activeLoop.id === loop.id;
      if (!touching && !isActive) { item.el.style.display = "none"; continue; }
      const q = project([item.world[0], item.world[1], 40]);
      if (!q) { item.el.style.display = "none"; continue; }
      item.el.style.display = "";
      item.el.style.transform = `translate3d(${(q[0]).toFixed(1)}px, ${(q[1]).toFixed(1)}px, 0) translate(-50%,-50%)`;
      item.el.style.zIndex = String(Math.max(1, 3000 - Math.round(q[2])));
      item.el.classList.toggle("active", !!isActive);
    }
  }

  function refreshBadges() {
    state.cards.forEach((el, name) => {
      const idx = state.selected.findIndex((s) => s.name === name);
      const badge = el.querySelector(".cc-badge");
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

  /* ---------------------------------------------------------- 悬停提示 */
  function showTip(city, el) {
    const tip = $("map-tip");
    const kinds = (city.highlights || []).map((h) =>
      `<span class="tip-kind">${KIND_ICON[h.kind] || "📍"} ${escapeHtml(h.name)}</span>`).join("");
    const stay = city.days >= 1 ? `建议 ${city.days} 天` : "建议半天";
    tip.innerHTML =
      `<div class="tip-head"><b>${escapeHtml(city.name)}</b>
        <span class="tip-score">${city.score}</span>
        <span class="tip-region">${escapeHtml(city.region)}·${escapeHtml(city.province)}</span></div>
      <div class="tip-intro">${escapeHtml(city.intro || "")}</div>
      <div class="tip-half"><span class="tip-label">半日游</span>${escapeHtml(city.halfday || "暂无半日建议")}</div>
      ${city.oneday ? `<div class="tip-day"><span class="tip-label">一日</span>${escapeHtml(city.oneday)}</div>` : ""}
      <div class="tip-kinds">${kinds}</div>
      <div class="tip-foot">${escapeHtml(stay)} · ${city.hours} 小时 · ${escapeHtml(city.best_season || "全年")}
        ${city.rail ? "" : " · 无铁路，需飞机/包车"}</div>`;
    tip.hidden = false;
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
  function hideTip() { $("map-tip").hidden = true; }

  /* ---------------------------------------------------------- 大区 */
  function provinceRegion(short) {
    const regions = (state.data && state.data.regions) || state.pendingRegions || [];
    const region = regions.find((r) => (r.provinces || []).includes(short));
    return region || null;
  }

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

  /** 全国视角的「精选」城市：评分最高的若干座，散布各大区，先给个可点的入口 */
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
    const p = worldOf(city);
    flyTo({ tx: p[0], ty: p[1], dist: Math.min(cam.dist, 1700), pitch: Math.max(cam.pitch, 24), duration: 900 });
    const el = state.cards.get(name);
    if (el) {
      el.classList.remove("flash");
      void el.offsetWidth;
      el.classList.add("flash");
    }
  }

  function setLegend(text) { $("map-legend").textContent = text; }

  /* ---------------------------------------------------------- 环线 */
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
      item.innerHTML =
        `<div class="loop-title">${escapeHtml(loop.name)}
           ${loop.cross_region ? '<span class="tag cross">跨大区</span>' : ""}
           ${loop.plan_mode === "reference" ? '<span class="tag ref">参考线路</span>' : ""}
         </div>
         <div class="loop-sub">${escapeHtml(loop.cities.join(" → "))}</div>
         <div class="loop-meta">${loop.days} 天 · ${escapeHtml(loop.season || "全年")}</div>`;
      item.addEventListener("mouseenter", () => highlightLoop(loop, true));
      item.addEventListener("mouseleave", () => highlightLoop(loop, false));
      item.addEventListener("click", () => focusLoop(loop));
      box.appendChild(item);
    });
    buildLoopLabels();
  }

  function lifecycleActive(loop) {
    return state.activeLoop && state.activeLoop.id === loop.id;
  }

  function highlightLoop(loop, on) {
    if (on) {
      const tag = $("map-legend");
      tag.dataset.prev = tag.dataset.prev || tag.textContent;
      tag.textContent = `${loop.name}：${loop.blurb}`;
    } else if ($("map-legend").dataset.prev) {
      $("map-legend").textContent = $("map-legend").dataset.prev;
      delete $("map-legend").dataset.prev;
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
        const p = worldOf(s);
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
      state.loopLabels.push({ loop, el, world: [sx / n, sy / n] });
    });
    layer.appendChild(frag);
  }

  function focusLoop(loop) {
    state.activeLoop = loop;
    renderLoops();
    const points = (loop.stops || []).filter((s) => s.lon != null).map((s) => worldOf(s));
    if (!points.length) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    points.forEach((p) => {
      x0 = Math.min(x0, p[0]); x1 = Math.max(x1, p[0]);
      y0 = Math.min(y0, p[1]); y1 = Math.max(y1, p[1]);
    });
    const pad = loop.cross_region ? 420 : 200;
    const rect = [x0 - pad, y0 - pad, x1 + pad, y1 + pad, 180];
    // 跨大区的环线：先把视角退到全国看清走向，再落到环线范围
    const targets = [];
    if (loop.cross_region) {
      targets.push(Object.assign(fitRect(state.chinaRect, { pitch: 30, mx: 0.72, my: 0.6 }),
        { duration: 780 }));
    }
    targets.push(Object.assign(fitRect(rect, {
      pitch: loop.cross_region ? 17 : 20,
      mx: 0.7, my: 0.6,
    }), { duration: loop.cross_region ? 1150 : 1000 }));
    flyTo(targets, { onDone: () => buildCards(loop.stops.map((s) => state.data.cityIndex[s.name]).filter(Boolean)) });
    setLegend(`${loop.name}（${loop.cities.join(" → ")}）· ${loop.blurb}`);
    showLoopDetail(loop);
  }

  function showLoopDetail(loop) {
    const panel = $("map-loops-panel");
    panel.classList.remove("collapsed");
    panel.querySelector(".panel-toggle").textContent = "–";
    let box = $("map-loop-detail");
    if (!box) {
      box = document.createElement("div");
      box.id = "map-loop-detail";
      box.className = "loop-detail";
      // 放在 panel-body 之外，避免列表重绘时被清掉
      panel.insertBefore(box, panel.querySelector(".panel-body"));
    }
    box.innerHTML =
      `<div class="ld-head">${escapeHtml(loop.name)}
        <button type="button" class="ld-close" title="关闭">✕</button></div>
       <div class="ld-blurb">${escapeHtml(loop.blurb)}</div>
       <div class="ld-meta">${loop.cities.length} 站 · ${loop.days} 天 · ${escapeHtml(loop.season || "全年")}
         ${loop.cross_region ? " · 跨大区" : ""}</div>
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
      setLegend(`已载入「${loop.name}」，可以在右侧改停留方式后点「规划乘车表」`);
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
  }

  function renderPicked() {
    const list = $("map-picked");
    $("map-picked-count").textContent = String(state.selected.length);
    list.innerHTML = "";
    state.selected.forEach((item, i) => {
      const city = state.data.cityIndex[item.name] || { name: item.name };
      const li = document.createElement("li");
      li.className = "picked-item";
      li.style.setProperty("--tint", regionColor(city.region || ""));
      const value = item.stay.mode === "nights" ? `nights:${item.stay.nights}` : item.stay.mode;
      li.innerHTML =
        `<span class="pi-no">${i + 1}</span>
         <span class="pi-name">${escapeHtml(item.name)}<em>${escapeHtml(city.region || "")}</em></span>
         <span class="pi-tools">
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
          if (act === "del") state.selected.splice(i, 1);
          else if (act === "up" && i > 0) {
            state.selected.splice(i - 1, 0, state.selected.splice(i, 1)[0]);
          } else if (act === "down" && i < state.selected.length - 1) {
            state.selected.splice(i + 1, 0, state.selected.splice(i, 1)[0]);
          } else if (act === "fly") { focusCity(item.name); return; }
          renderPicked();
          refreshBadges();
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

  /** 中间城市按地理最短路径粗排（起终点不动），给「按路线排序」按钮用 */
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
    // 最近邻 + 2-opt，够用且稳
    let order = middles.slice();
    const rest = order.slice();
    order = [];
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
    TP().setStatus && TP().setStatus("m-status", "规划中…（每段都在查真实车次，跨大区会慢一点）");
    $("map-results").innerHTML = "";
    $("map-guides").innerHTML = "";
    try {
      const res = await TP().postJSON("/api/roam/plan", planPayload());
      if (!res.ok) {
        TP().setStatus && TP().setStatus("m-status", res.error || "规划失败", true);
        return;
      }
      TP().setStatus && TP().setStatus("m-status", "");
      renderPlan(res);
    } catch (err) {
      TP().setStatus && TP().setStatus("m-status", "请求失败: " + err.message, true);
    } finally {
      go.disabled = false;
    }
  }

  function renderPlan(res) {
    const box = $("map-results");
    const guides = $("map-guides");
    const s = res.summary || {};
    const head = document.createElement("div");
    head.className = "summary roam-summary";
    head.innerHTML =
      `<b>${(res.order_cities || []).join(" → ")}</b>` +
      (res.closed ? " → 回到起点" : "") +
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

    // 每座城市怎么玩
    const gtitle = document.createElement("div");
    gtitle.className = "roam-subtitle";
    gtitle.textContent = "每座城市怎么玩";
    guides.appendChild(gtitle);
    const grid = document.createElement("div");
    grid.className = "guide-grid";
    (res.cities || []).forEach((c, i) => {
      const card = document.createElement("div");
      card.className = "guide-card";
      card.style.setProperty("--tint", regionColor(c.region));
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
           ${c.rail ? "" : " · 无铁路，需飞机/包车"}</div>
         ${c.note ? `<div class="g-note">${escapeHtml(c.note)}</div>` : ""}`;
      grid.appendChild(card);
    });
    guides.appendChild(grid);

    const rtitle = document.createElement("div");
    rtitle.className = "roam-subtitle";
    rtitle.textContent = "逐日乘车表";
    box.appendChild(rtitle);
    const dayCard = TP().dayCard;
    if (typeof dayCard === "function") {
      (res.legs || []).forEach((rec) => box.appendChild(dayCard(rec)));
    } else {
      (res.legs || []).forEach((rec) => {
        const d = document.createElement("div");
        d.className = "card";
        d.textContent = `D${rec.day_no} ${rec.date} ${rec.frm} → ${rec.to}`;
        box.appendChild(d);
      });
    }

    const foot = document.createElement("div");
    foot.className = "summary";
    foot.textContent = `共 ${res.total_days || s.total_days} 天`;
    box.appendChild(foot);

    const sug = res.suggestions || {};
    const chips = [];
    (sug.near_route || []).forEach((x) => chips.push(
      { ...x, why: `距路线 ${x.km} 公里` }));
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
          }
          setLegend(`已把「${city.name}」插进路线，重新点「规划乘车表」即可`);
        });
      });
      box.insertBefore(sbox, box.firstChild.nextSibling);
    }
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
      // 浏览器原生全屏更彻底（连地址栏一起让位）；失败也不影响 CSS 全屏
      try {
        const p = stage.requestFullscreen();
        if (p && p.catch) p.catch(() => { });
      } catch (err) { /* 忽略：保留 CSS 全屏 */ }
    } else if (!on && document.fullscreenElement && document.exitFullscreen) {
      try {
        const p = document.exitFullscreen();
        if (p && p.catch) p.catch(() => { });
      } catch (err) { /* 忽略 */ }
    }
    setTimeout(resize, 60);
    setTimeout(resize, 260);
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
      resize();
      setTimeout(resize, 200);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        const msg = escapeAction();
        if (msg) {
          e.preventDefault();
          setLegend(msg + "　（Esc 继续逐层退出：环线说明 → 环线高亮 → 所选城市 → 大区 → 全国）");
        }
      }
    });
  }

  /* ---------------------------------------------------------- 事件绑定 */
  function bindStage() {
    const stage = $("map-stage");
    const localX = (e) => e.clientX - canvas.getBoundingClientRect().left;
    const localY = (e) => e.clientY - canvas.getBoundingClientRect().top;

    stage.addEventListener("contextmenu", (e) => e.preventDefault());
    stage.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".city-card, .loop-label, .map-panel, .map-regions, .map-controls")) return;
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
        // 「抓住鼠标下面的地面」：把上一个光标位置的地面点搬到当前光标下
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
      e.preventDefault();
      const px = localX(e), py = localY(e);
      // 触控板捏合 / ctrl+滚轮 → 以光标为中心缩放
      if (e.ctrlKey || e.metaKey) { zoomAt(px, py, Math.exp(-e.deltaY * 0.01)); return; }
      if (e.shiftKey) { panByPixels(e.deltaY + e.deltaX, 0); return; }
      // 鼠标滚轮（步进大）→ 缩放；触控板双指滑动（步进小/带小数）→ 平移
      const mouseWheel = e.deltaMode !== 0 || Math.abs(e.deltaY) >= 40 || Math.abs(e.deltaX) >= 40;
      if (mouseWheel) zoomAt(px, py, Math.exp(-clamp(e.deltaY, -150, 150) * 0.0016));
      else panByPixels(e.deltaX, e.deltaY);
    }, { passive: false });
    stage.addEventListener("dblclick", (e) => {
      if (e.target.closest(".city-card, .map-panel")) return;
      zoomAt(localX(e), localY(e), 0.6);
    });
  }

  /** Esc 逐层退出：全屏 → 环线说明 → 环线高亮 → 已选城市 → 大区 → 全国 */
  function escapeAction() {
    if (isFull()) { toggleFullscreen(false); return "已退出全屏"; }
    const detail = $("map-loop-detail");
    if (detail) { detail.remove(); return "已关闭环线说明"; }
    if (state.activeLoop) { state.activeLoop = null; renderLoops(); return "已取消环线高亮"; }
    if (state.selected.length) {
      state.selected = [];
      renderPicked();
      refreshBadges();
      return "已清空所选城市";
    }
    if (state.region) { selectRegion(null); return "已返回全国视角"; }
    return "";
  }

  function handleStageClick(ev) {
    if (!baseCtx) return;
    const rect = canvas.getBoundingClientRect();
    const x = ev.clientX - rect.left;
    const y = ev.clientY - rect.top;
    for (const prov of state.provinces) {
      if (!prov._path) continue;
      let hit = false;
      try { hit = baseCtx.isPointInPath(prov._path, x, y); } catch (err) { hit = false; }
      if (!hit) continue;
      const region = provinceRegion(prov.short);
      if (region && (!state.region || state.region.name !== region.name)) {
        selectRegion(region);
      } else {
        setLegend(`${prov.name} · ${region ? region.name + " · " + region.blurb : "未分组"}`);
      }
      return;
    }
  }

  function bindControls() {
    $("map-home").addEventListener("click", () =>
      flyTo(Object.assign(fitRect(state.chinaRect, { pitch: 26, mx: 0.8, my: 0.7 }), { duration: 900 })));
    $("map-reset").addEventListener("click", () => {
      state.region = null;
      state.activeLoop = null;
      clearCards();
      renderRegionBar();
      renderLoops();
      const d = $("map-loop-detail");
      if (d) d.remove();
      flyTo(Object.assign(fitRect(state.chinaRect, { pitch: 20, mx: 0.8, my: 0.7 }), { duration: 900 }));
      setLegend("拖动平移 · 滚轮缩放 · 点大区或省份进入 · 点城市卡片编号入列");
    });
    $("map-pitch").addEventListener("input", (e) => setCamera({ pitch: Number(e.target.value) }));
    const left = $("map-rotate-left");
    const right = $("map-rotate-right");
    if (left) left.addEventListener("click", () => setCamera({ bearing: cam.bearing - 25 }));
    if (right) right.addEventListener("click", () => setCamera({ bearing: cam.bearing + 25 }));
    $("map-clear").addEventListener("click", () => {
      state.selected = [];
      renderPicked();
      refreshBadges();
    });
    $("map-optimize").addEventListener("click", optimizeOrder);
    $("m-closed").addEventListener("change", () => { state.closed = $("m-closed").checked; });
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
    window.addEventListener("resize", () => { resize(); togglePitchLabel(); });
    if (typeof ResizeObserver === "function") {
      try {
        new ResizeObserver(() => resize()).observe($("map-stage"));
      } catch (err) { /* 忽略 */ }
    }
  }

  function togglePitchLabel() { /* 预留：窗口变化时的界面微调 */ }

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
    if (btn) btn.addEventListener("click", () => {
      box.hidden = true;
      state.ready = false;
      state.loading = false;
      loadData();
    });
  }

  function clearError() {
    const box = $("map-error");
    if (box) { box.hidden = true; box.innerHTML = ""; }
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
      state.pendingRegions = roam.regions;   // prepareGeo 里要用大区着色
      prepareGeo(geo);
      prepareRoam(roam);
      try {
        prepareNature(natureResp && natureResp.ok ? await natureResp.json() : null);
      } catch (err) {
        state.nature = null;   // 山川层是可选装饰，缺了也能用
      }
      state.ready = true;
      state.loading = false;
      clearError();
      resize();
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

  function dp(points, tol) {
    if (points.length < 3) return points;
    const keep = new Array(points.length).fill(false);
    keep[0] = keep[points.length - 1] = true;
    const stack = [[0, points.length - 1]];
    while (stack.length) {
      const [a, b] = stack.pop();
      if (b <= a + 1) continue;
      const ax = points[a][0], ay = points[a][1], bx = points[b][0], by = points[b][1];
      const dx = bx - ax, dy = by - ay;
      const len = Math.hypot(dx, dy) || 1;
      let best = 0, bi = -1;
      for (let i = a + 1; i < b; i++) {
        const d = Math.abs((points[i][0] - ax) * dy - (points[i][1] - ay) * dx) / len;
        if (d > best) { best = d; bi = i; }
      }
      if (bi >= 0 && best > tol) { keep[bi] = true; stack.push([a, bi], [bi, b]); }
    }
    return points.filter((_, i) => keep[i]);
  }

  function prepareGeo(geo) {
    state.provinces = [];
    let maxH = 0;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const prov of geo.provinces) {
      const h = BASE_H + (ELEV[prov.short] || 0.2) * EXAG;
      maxH = Math.max(maxH, h);
      const coarse = [], fine = [];
      let px0 = Infinity, py0 = Infinity, px1 = -Infinity, py1 = -Infinity, sx = 0, sy = 0, n = 0;
      for (const ring of prov.rings) {
        const world = ring.map((p) => lcc(p[0], p[1]));
        // 统一成逆时针，保证侧壁外法线方向一致
        let area = 0;
        for (let i = 0; i < world.length - 1; i++) {
          area += world[i][0] * world[i + 1][1] - world[i + 1][0] * world[i][1];
        }
        if (area < 0) world.reverse();
        // 简化容差按环的大小自适应：澳门、香港这类小省不能被简化掉
        let ex0 = Infinity, ey0 = Infinity, ex1 = -Infinity, ey1 = -Infinity;
        for (const p of world) {
          ex0 = Math.min(ex0, p[0]); ey0 = Math.min(ey0, p[1]);
          ex1 = Math.max(ex1, p[0]); ey1 = Math.max(ey1, p[1]);
        }
        const extent = Math.max(ex1 - ex0, ey1 - ey0);
        const tolCoarse = clamp(extent / 16, 0.6, 7);
        const tolFine = clamp(extent / 60, 0.25, 2.2);
        let c = dp(world, tolCoarse);
        let f = dp(world, tolFine);
        if (c.length < 4) c = world;      // 兜底：太小就画原始环
        if (f.length < 4) f = world;
        if (c.length >= 3) coarse.push(c);
        if (f.length >= 3) fine.push(f);
        for (const p of world) {
          px0 = Math.min(px0, p[0]); py0 = Math.min(py0, p[1]);
          px1 = Math.max(px1, p[0]); py1 = Math.max(py1, p[1]);
          sx += p[0]; sy += p[1]; n++;
        }
      }
      if (!n) continue;
      const region = provinceRegion(prov.short);
      const item = {
        short: prov.short, name: prov.name, region: region ? region.name : "",
        color: region ? region.color : "#8fa6a3",
        h, rings: { coarse, fine }, bbox: [px0, py0, px1, py1],
        cx: sx / n, cy: sy / n,
      };
      state.provinces.push(item);
      x0 = Math.min(x0, px0); y0 = Math.min(y0, py0);
      x1 = Math.max(x1, px1); y1 = Math.max(y1, py1);
    }
    state.maxH = maxH;
    const pad = 420;
    state.chinaRect = [x0 - pad, y0 - pad, x1 + pad, y1 + pad, maxH];
    state.plate = [x0 - 900, y0 - 900, x1 + 900, y1 + 900];
  }

  function prepareRoam(roam) {
    state.data = {
      regions: roam.regions,
      cities: roam.cities,
      loops: roam.loops,
      cityIndex: {},
    };
    const provH = new Map(state.provinces.map((p) => [p.short, p.h]));
    roam.cities.forEach((c) => {
      c.h = provH.get(c.province) || BASE_H;
      state.data.cityIndex[c.name] = c;
    });
    document.title = "大交通换乘规划 · 地图漫游";
  }

  /* ---------------------------------------------------------- 对外 */
  function initCanvas() {
    canvas = $("map-canvas");
    if (!canvas) return false;
    ctx = canvas.getContext("2d");
    baseCv = document.createElement("canvas");
    baseCtx = baseCv.getContext("2d");
    const dateEl = $("m-date");
    if (dateEl && !dateEl.value) {
      dateEl.value = TP().defaultDate ? TP().defaultDate(5) : new Date().toISOString().slice(0, 10);
    }
    resize();
    V = view();
    return true;
  }

  window.DSHTravelMap = {
    onShow() {
      if (!state.ready && !state.loading) loadData();
      else if (state.ready) { resize(); }
    },
    /* 供无头冒烟测试与调试使用：一次性装载数据（不依赖网络） */
    __debug: {
      state, cam, loadData,
      lcc, project, fitRect, renderBase, drawOverlay, layoutCards,
      selectRegion, buildCards, focusLoop, clearCards, renderPicked,
      toggleFullscreen, isFull, prepareNature, escapeAction,
      groundPoint, zoomAt, panByPixels, drawNature,
      boot(geo, roamPayload, nature) {
        if (!canvas && !initCanvas()) return { error: "no canvas" };
        state.pendingRegions = roamPayload.regions;
        prepareGeo(geo);
        prepareRoam(roamPayload);
        prepareNature(nature || null);
        state.ready = true;
        resize();
        setCamera(Object.assign(fitRect(state.chinaRect, { pitch: 20, mx: 0.8, my: 0.7 }), {}));
        renderRegionBar();
        renderLoops();
        renderPicked();
        buildLoopLabels();
        buildCards(nationalPicks());
        renderBase(1);
        layoutCards();
        return {
          provinces: state.provinces.length,
          cities: state.data.cities.length,
          regions: state.data.regions.length,
          loops: state.data.loops.length,
          loopLabels: state.loopLabels.length,
          nationalCards: state.cardList.length,
          nature: state.nature
            ? { rivers: state.nature.rivers.length, lakes: state.nature.lakes.length, ranges: state.nature.ranges.length }
            : null,
          chinaRect: state.chinaRect,
          dist: cam.dist,
        };
      },
      toggleCity, renderLoops, showLoopDetail, planPayload,
      setSelected(names) {
        state.selected = names.map((n) => ({ name: n, stay: autoStay(state.data.cityIndex[n] || {}) }));
        renderPicked();
        refreshBadges();
      },
    },
  };

  document.addEventListener("DOMContentLoaded", () => {
    if (!initCanvas()) return;
    bindStage();
    bindControls();
    bindFullscreen();
    renderPicked();
    requestAnimationFrame(frame);
  });
})();
