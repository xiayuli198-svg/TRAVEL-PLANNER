/* 地球模式（globe.gl）—— 世界城市卡 + 世界经典环线 + 世界之最
 *
 * 数据来自 /api/world/map（世界城市 / 环线 / 世界之最），照片在 web/img/world/。
 * 交互与「地图漫游」保持一致：悬停出卡片、点卡片编号入列、点环线飞过去并铺开城市、
 * 点世界之最飞过去看详情、右侧列顺序、生成世界行程单（飞行距离/时间/时差，不含票价）。
 * 原有的自转开关与「高德 3D」按钮保留。
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const GLOBE_R = 100;                 // three-globe 的球半径（世界单位）
  const KIND_ICON = {
    博物馆: "🏛", 古迹: "🏯", 自然: "🏞", 街区: "🏙", 古镇: "🏮", 美食: "🍜",
    夜景: "🌃", 演出: "🎭", 海岛: "🏝", 雪山: "🏔", 湖泊: "🌊", 寺庙: "🛕",
    园林: "🌿", 工业: "🏭", 温泉: "♨", 边境: "🛂", 主题乐园: "🎡",
  };
  const CAT_COLOR = { 自然: "#7ee081", 工程: "#ffcf6b", 人文: "#b39dff", 气候: "#7fd7f5" };

  const state = {
    data: null,
    ready: false,
    loading: false,
    globeError: "",      // 建球失败的原因（页面底部的信息条会显示它）
    fitTimer: 0,         // 画布尺寸兜底定时器
    active: [],          // 已选城市名（有序）
    cards: new Map(),    // name -> element
    cardList: [],
    recordEls: [],
    showRecords: true,
    activeLoop: null,
    filter: "全部",
    tipTimer: null,
    showIcons: true,     // 「显示/隐藏图标」：城市卡与数值卡是 DOM 浮层，收起来只看球与航线
  };
  let g = null;
  let rafId = 0;

  const esc = (s) => {
    const fn = window.TP && window.TP.esc;
    if (fn) return fn(s);
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  };
  const info = (text) => { const el = $("globe-info"); if (el) el.textContent = text || ""; };

  /* ---------------------------------------------------------- 加载数据 */
  async function loadWorld() {
    if (state.loading || state.ready) return;
    state.loading = true;
    info("正在加载世界城市与世界之最…");
    try {
      const resp = await fetch("/api/world/map");
      if (!resp.ok) throw new Error(`/api/world/map 返回 HTTP ${resp.status}`);
      const data = await resp.json();
      if (!data || !data.cities || !data.cities.length) {
        throw new Error("世界城市数据为空：先运行 python scripts/build_world_catalog.py 生成 world_catalog.json");
      }
      state.data = {
        cities: data.cities,
        loops: data.loops || [],
        records: data.records || [],
        cityIndex: {},
      };
      data.cities.forEach((c) => { state.data.cityIndex[c.name] = c; });
      state.ready = true;
      state.loading = false;
      buildScene();
      renderLoopSelect();
      renderLoops();
      renderRecords();
      renderPicked();
      // 建球失败时别把错误信息盖掉：错误比「已载入 N 座城市」重要得多
      if (state.globeError) {
        info("3D 地球初始化失败：" + state.globeError);
      } else {
        info(`已载入 ${data.cities.length} 座世界城市 · ${(data.loops || []).length} 条经典环线 · ` +
          `${(data.records || []).length} 条世界之最`);
      }
    } catch (err) {
      state.loading = false;
      console.error("[地球模式] 加载失败", err);
      info("世界数据加载失败：" + err.message);
    }
  }

  /* ---------------------------------------------------------- 场景 */
  function buildScene() {
    const host = $("globe-viz");
    if (!host) return;
    if (typeof window.Globe !== "function") {
      info("3D 地球库未加载，请检查 /static/vendor/globe.gl.min.js");
      return;
    }
    if (g) {
      // 已经建过：只更新数据
      updatePoints();
      if (!state.cardList.length) buildCards(state.data.cities);
      return;
    }
    try {
      g = window.Globe()(host)
        .backgroundColor("#020812")
        .globeImageUrl("/static/data/earth-blue-marble.jpg")
        .bumpImageUrl("/static/data/earth-topology.png")
        .showAtmosphere(true)
        .atmosphereColor("#56c9ff")
        .atmosphereAltitude(0.18)
        .pointLat("lat").pointLng("lng")
        .pointAltitude((d) => (d.record ? 0.06 : 0.022))
        .pointRadius((d) => (d.record ? 0.28 : 0.34))
        .pointColor((d) => pointColor(d))
        .pointLabel("name")
        .pathPoints("coords")
        .pathPointLat((p) => p[0]).pathPointLng((p) => p[1])
        .pathPointAlt(() => 0.004)          // 注意是 pathPointAlt；没有 pathAltitude 这个方法
        .pathColor((p) => p.color || "#ffd36b")
        .pathStroke((p) => p.stroke || 1.1)
        .ringsData([])
        .ringLat("lat").ringLng("lng")
        .ringColor((d) => (t) => `rgba(255,207,107,${Math.max(0, 1 - t)})`)
        .ringMaxRadius(4.5)
        .ringPropagationSpeed(1.6)
        .ringRepeatPeriod(900)
        .onPointClick((p) => { if (!p.record) toggleCity(p.name); });
      g.controls().autoRotate = true;
      g.controls().autoRotateSpeed = 0.35;
      g.controls().minDistance = 160;
      g.controls().maxDistance = 900;
      state.globeError = "";
      // 自检：链上用到的方法名在真实球实例上必须都存在
      // （假球什么都接受，所以「方法名写错 → 整条链抛错 → 地球没建」这种事故要靠它兜住）
      const wanted = ["backgroundColor", "globeImageUrl", "bumpImageUrl", "showAtmosphere", "atmosphereColor",
        "atmosphereAltitude", "pointLat", "pointLng", "pointAltitude", "pointRadius", "pointColor",
        "pointLabel", "pathPoints", "pathPointLat", "pathPointLng", "pathPointAlt", "pathColor", "pathStroke",
        "ringsData", "ringLat", "ringLng", "ringColor", "ringMaxRadius", "ringPropagationSpeed",
        "ringRepeatPeriod", "pointsData", "pathsData", "onPointClick", "width", "height", "pointOfView",
        "controls", "camera", "viewOffset"];
      g._missingApi = wanted.filter((m) => typeof g[m] !== "function");
      updatePoints();
      if (window.ResizeObserver) {
        new ResizeObserver(fitCanvas).observe(host);       // 切页签 / 进全屏后自动跟上
      }
      window.addEventListener("resize", fitCanvas);
      // 兜底：ResizeObserver 不保证回调，首次布局也常晚于建球，
      // 所以建球后补几次 + 定时兜住（fitCanvas 内部先比尺寸，没变立刻返回，开销可忽略）
      [120, 400, 1000, 2200].forEach((ms) => setTimeout(fitCanvas, ms));
      if (state.fitTimer) clearInterval(state.fitTimer);
      state.fitTimer = setInterval(fitCanvas, 400);
      g.width(host.clientWidth || 900).height(host.clientHeight || 560);
      // 一开始就铺满全世界的城市卡（没选环线时）——卡片靠 layoutCards 每帧定位
      buildCards(state.data.cities);
      startCardLoop();
    } catch (err) {
      g = null;                              // 半成品球不要留着，否则后面每个函数都会炸
      state.globeError = err.message;
      console.error("[地球模式] 初始化失败", err);
      info("3D 地球初始化失败：" + err.message);
    }
  }

  function pointColor(d) {
    if (d.record) {
      if (state.activeRecord === d.id) return "#ffcf6b";
      return CAT_COLOR[d.category] || "#b39dff";
    }
    if (state.active.includes(d.name)) return "#ffd36b";
    if (state.activeLoop && (state.activeLoop.cities || []).includes(d.name)) return "#7fd7f5";
    return "#7ee7c8";
  }

  function updatePoints() {
    if (!g || !state.data) return;
    const pts = state.data.cities.map((c) => ({ name: c.name, lat: c.lat, lng: c.lon, record: false }));
    if (state.showRecords) {
      state.data.records.forEach((r) => {
        if (state.filter !== "全部" && r.category !== state.filter) return;
        const lon = recLon(r);
        pts.push({ name: r.name, lat: r.lat, lng: lon, record: true, id: r.id, category: r.category });
      });
    }
    g.pointsData(pts);
    g.ringsData(state.showRecords
      ? state.data.records.filter((r) => state.filter === "全部" || r.category === state.filter)
      : []);
  }

  /** 世界之最的经度字段（lon 为主，lng 兼容旧数据） */
  function recLon(r) {
    return r.lon != null ? r.lon : r.lng;
  }

  /* ---------------------------------------------------------- 卡片定位
   * 定位与「正反面」判定必须和 three-globe 自己的坐标换算完全一致，
   * 否则会出现「背面城市显示、正面城市消失」的错位。
   * three-globe：world = R * (cos φ cos θ, sin φ, -cos φ sin θ)
   * （φ=纬度弧度，θ=经度弧度，东经/北纬为正，球心在原点，与 toScreenPosition 同一套）
   */
  function startCardLoop() {
    if (rafId) cancelAnimationFrame(rafId);
    const step = () => {
      rafId = requestAnimationFrame(step);
      if (!g || !state.ready) return;
      if ($("globe-viz") && $("globe-viz").offsetParent === null) return;   // 切走页签不计算
      layoutCards();
    };
    rafId = requestAnimationFrame(step);
  }

  /** 经纬度 → 世界坐标：**先问 globe.gl 自己**，拿不到才用公式兜底。
   *
   * 踩过的坑：这里原来自算 `x=R·cosφ·cosθ, y=R·sinφ, z=-R·cosφ·sinθ`，
   * 而 three-globe 内部用的是极角写法
   * `φ=(90-lat), θ=(90-lng), x=R·sinφ·cosθ, y=R·cosφ, z=R·sinφ·sinθ`
   * —— 两套公式**不等价**（cos/sin 对调、z 号相反）。后果是正反面判反、投影算出 NaN，
   * 81 张城市卡片一张都不显示。现在直接用 `getCoords`（它内部就是同一套换算）。
   */
  function worldOf(lat, lng, altitude = 0) {
    if (g && typeof g.getCoords === "function") {
      try {
        const p = g.getCoords(lat, lng, altitude);
        if (p && isFinite(p.x) && isFinite(p.y) && isFinite(p.z)) {
          return { x: p.x, y: p.y, z: p.z };
        }
      } catch (err) { /* 球还没就绪，走下面的公式 */ }
    }
    const radius = (g && typeof g.getGlobeRadius === "function" && g.getGlobeRadius()) || GLOBE_R;
    const phi = ((90 - lat) * Math.PI) / 180, theta = ((90 - lng) * Math.PI) / 180;
    const r = radius * (1 + altitude), sinPhi = Math.sin(phi);
    return { x: r * sinPhi * Math.cos(theta), y: r * Math.cos(phi), z: r * sinPhi * Math.sin(theta) };
  }

  /** 世界坐标 → 屏幕像素
   *
   *  三级兜底，每一级都真踩过：
   *  1. `g.getScreenCoords(size, x, y, z)` —— 签名是 `{width,height}` + 三个分量（不是 w,h,x,y）；
   *  2. 这个 vendor 包里它**会算出 NaN**（实测 x/y 都是 NaN，但同一台相机手算就没问题），
   *     所以自己按标准 MVP 算：clip = P·V·world → NDC → 像素；
   *  3. 都没有就返回 null（卡片自己藏起来，不会跑到 (0,0) 堆成一坨）。
   */
  function screenOf(lat, lng) {
    if (!g) return null;
    const host = $("globe-viz");
    if (!host) return null;
    const size = { width: host.clientWidth, height: host.clientHeight };
    if (!size.width || !size.height) return null;
    const w = worldOf(lat, lng);
    try {
      if (typeof g.getScreenCoords === "function") {
        const p = g.getScreenCoords(size, w.x, w.y, w.z);
        if (p && isFinite(p.x) && isFinite(p.y)) return p;
      }
      const cam = g.camera && g.camera();
      if (cam && cam.projectionMatrix && cam.matrixWorldInverse) {
        const P = cam.projectionMatrix.elements, V = cam.matrixWorldInverse.elements;
        const mul = (m, v) => {
          const o = [0, 0, 0, 0];
          for (let r = 0; r < 4; r++) {
            o[r] = m[r] * v[0] + m[4 + r] * v[1] + m[8 + r] * v[2] + m[12 + r] * v[3];
          }
          return o;
        };
        const view = mul(V, [w.x, w.y, w.z, 1]);
        const clip = mul(P, view);
        if (clip[3]) {
          const x = ((clip[0] / clip[3]) + 1) * size.width / 2;
          const y = -((clip[1] / clip[3]) - 1) * size.height / 2;
          if (isFinite(x) && isFinite(y)) return { x, y };
        }
      }
    } catch (err) { /* 相机还没就绪 */ }
    return null;
  }

  /** 该点是否朝向镜头（背面的卡片要藏起来）
   * 判据用几何量：点落在镜头方向的半球内。注意不能用 toScreenPosition/投影矩阵，
   * 它们对地球背面的点也会给出屏幕坐标，必须自己判。
   */
  function isFront(lat, lng) {
    if (!g) return false;
    const cam = g.camera && g.camera();
    if (!cam || !cam.position) return true;
    const w = worldOf(lat, lng);
    const c = cam.position;
    const len = Math.hypot(w.x, w.y, w.z) || 1;
    const d = Math.hypot(c.x, c.y, c.z) || 1;
    return (w.x * c.x + w.y * c.y + w.z * c.z) / (len * d) > 0;
  }

  /** 相机朝向：three.js 的 local -Z 翻到世界坐标（与 isFront 同源，供自检用） */
  function cameraForward() {
    const cam = g && g.camera && g.camera();
    if (!cam || !cam.position) return null;
    const c = cam.position;
    const d = Math.hypot(c.x, c.y, c.z) || 1;
    return { x: -c.x / d, y: -c.y / d, z: -c.z / d };
  }

  function layoutCards() {
    if (!state.showIcons) return;      // 图标收起来了：不投影也不排版，省掉每帧的开销
    const host = $("globe-viz");
    if (!host) return;
    const rect = host.getBoundingClientRect();
    const place = (el, p) => {
      if (!p) { el.style.display = "none"; return; }
      if (!isFront(p.lat, p.lon)) { el.style.display = "none"; return; }
      const s = screenOf(p.lat, p.lon);
      if (!s) { el.style.display = "none"; return; }
      const x = s.x, y = s.y;
      if (x < -140 || x > rect.width + 140 || y < -80 || y > rect.height + 80) {
        el.style.display = "none";
        return;
      }
      el.style.display = "";
      const depth = Math.hypot(x - rect.width / 2, y - rect.height / 2);
      // 远端的卡片别缩得太狠：全球视角下 81 座城市也要能看出是卡片
      const k = clamp(1.25 - depth / Math.max(rect.width, rect.height), 0.78, 1.15);
      if (!el._w0) { el._w0 = el.offsetWidth || 92; el._h0 = el.offsetHeight || 116; }
      el.style.transform =
        `translate3d(${(x - el._w0 / 2).toFixed(1)}px, ${(y - el._h0).toFixed(1)}px, 0) scale(${k.toFixed(3)})`;
      el.style.zIndex = String(Math.max(1, 3000 - Math.round(depth)));
      el.style.opacity = String(clamp(1.3 - depth / 900, 0.75, 1));
    };
    state.cardList.forEach((it) => place(it.el, it.city));
    state.recordEls.forEach((it) => place(it.el, it.rec));
  }

  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  /* ---------------------------------------------------------- 视角（相机飞行） */
  const HOME_VIEW = { lat: 22, lng: 20, altitude: 2.2 };

  /** 把一组经度沿 ±180° 劈开取最紧凑的一段，避免跨太平洋环线被算成 350° */
  function lonWindow(lons) {
    const list = lons.slice().sort((a, b) => a - b);
    const n = list.length;
    if (!n) return { min: 0, max: 0 };
    let bestGap = -1, bestIdx = 0;
    for (let i = 0; i < n; i++) {
      const next = i === n - 1 ? list[0] + 360 : list[i + 1];
      const gap = next - list[i];
      if (gap > bestGap) { bestGap = gap; bestIdx = i; }
    }
    const start = list[(bestIdx + 1) % n];
    const end = list[bestIdx];
    const min = start > end ? start - 360 : start;      // 起点可能比终点大 360
    return { min, max: min + (360 - bestGap) };
  }

  /** 一组点 → 取景机位：中心经纬度 + 高度（跨度越大站得越高） */
  function fitView(points, minAlt, maxAlt) {
    if (!points || !points.length) {
      return { lat: HOME_VIEW.lat, lng: HOME_VIEW.lng, altitude: HOME_VIEW.altitude, span: 0 };
    }
    const latMin = Math.min(...points.map((p) => p.lat)), latMax = Math.max(...points.map((p) => p.lat));
    const win = lonWindow(points.map((p) => p.lon));
    const latMid = (latMin + latMax) / 2;
    let lonMid = (win.min + win.max) / 2;
    lonMid = ((((lonMid + 180) % 360) + 360) % 360) - 180;      // 归一到 [-180,180]
    const span = Math.max(latMax - latMin, win.max - win.min);
    // 高度标定：约 40° 跨度用 1.0 个球半径的距离（13° 左右的本地环线仍看得清城市卡）
    const altitude = clamp(span / 40 - 0.05, minAlt == null ? 0.9 : minAlt, maxAlt == null ? 2.4 : maxAlt);
    return { lat: latMid, lng: lonMid, altitude, span };
  }

  function currentAlt() {
    if (!g || !g.camera) return 2.2;
    const cam = g.camera();
    if (!cam || !cam.position) return 2.2;
    return Math.hypot(cam.position.x, cam.position.y, cam.position.z) / GLOBE_R - 1;
  }

  /** 缩放得越狠飞得越久一点，避免「瞬移」感 */
  function flightDuration(alt) {
    const jump = Math.abs((alt || 1) - currentAlt());
    return Math.round(clamp(900 + jump * 1100, 900, 2200));
  }

  /** 打开 / 收起自转（相机飞行时先停下，按钮文案跟着改） */
  function setAutoRotate(on) {
    if (!g || typeof g.controls !== "function") return;
    const c = g.controls();
    if (!c) return;
    c.autoRotate = !!on;
    const btn = $("globe-auto");
    if (btn) btn.textContent = c.autoRotate ? "暂停自转" : "开始自转";
  }

  /** 飞向一个坐标；命中卡片时让它闪一下，明确「已定位到哪儿」 */
  function flyToPoint(lat, lng, altitude, dur, names) {
    if (!g || !isFinite(lat) || !isFinite(lng)) return;
    setAutoRotate(false);
    const alt = altitude || 1.15;
    g.pointOfView({ lat, lng, altitude: alt }, dur || flightDuration(alt));
    // 环线按名字精确高亮；单点定位按坐标就近高亮（城市与同坐标的世界之最都会闪）
    if (names && names.length) names.forEach((n) => flashCard(state.cards.get(n)));
    else flashCardsAt(lat, lng);
  }

  /** 与目标点重合（0.6° 以内）的卡片高亮：环线的一串、或世界之最的单张 */
  function flashCardsAt(lat, lng) {
    const near = (p) => Math.abs((p.lat || 0) - lat) < 0.6 && Math.abs(((p.lon || 0) - lng + 540) % 360 - 180) < 0.6;
    state.cardList.forEach((it) => {
      if (near({ lat: it.city.lat, lon: it.city.lon })) flashCard(it.el);
    });
    state.recordEls.forEach((it) => {
      const lon = recLon(it.rec);
      if (near({ lat: it.rec.lat, lon })) flashCard(it.el);
    });
  }

  function flashCard(el) {
    if (!el) return;
    el.classList.remove("globe-focus");
    void el.offsetWidth;                       // 重新触发动画
    el.classList.add("globe-focus");
    if (el._focusTimer) clearTimeout(el._focusTimer);
    el._focusTimer = setTimeout(() => el.classList.remove("globe-focus"), 2400);
  }

  function flyTo(name, altitude) {
    const city = state.data && state.data.cityIndex[name];
    if (!city) return;
    flyToPoint(city.lat, city.lon, altitude || 1.15);
  }

  /** 把一组城市装进画面（环线定位：整条线的卡片一起闪） */
  function flyToCities(cities, minAlt, maxAlt) {
    if (!cities || !cities.length) return null;
    const view = fitView(cities, minAlt, maxAlt);
    flyToPoint(view.lat, view.lng, view.altitude, null, cities.map((c) => c.name));
    return view;
  }

  function flyHome() {
    if (!g) return;
    setAutoRotate(false);
    g.pointOfView(HOME_VIEW, 900);
  }

  /** 当前正面可见的卡片名（看不到卡片时先用它在控制台自检） */
  function frontNames(points) {
    return (points || []).filter((p) => isFront(p.lat, p.lon)).map((p) => p.name);
  }

  /* ---------------------------------------------------------- 尺寸与全屏 */
  /**
   * 把画布对齐到容器当前尺寸。
   * 坑：globe.gl 的 width/height 是 triggerUpdate:false 的 prop，改完要等下一次 digest 才生效，
   * 中间若有异常或根本没能触发，画布就会停在旧尺寸（表现为「球偏到右边/被裁掉」）。
   * 所以这里除了通知 globe.gl，还直接把 canvas 的样式与绘制缓冲尺寸写成实际像素。
   */
  function fitCanvas() {
    const host = $("globe-viz");
    if (!g || !host) return;
    const rect = host.getBoundingClientRect();
    const w = Math.round(rect.width) || host.clientWidth;
    const h = Math.round(rect.height) || host.clientHeight;
    if (w <= 0 || h <= 0) return;
    const box = w + "x" + h;
    if (host._fittedBox === box) return;            // 尺寸没变就别折腾 WebGL（每帧都会调）
    host._fittedBox = box;
    const dpr = window.devicePixelRatio || 1;
    const cv = host.querySelector("canvas");
    const scene = host.querySelector(".scene-container");
    /* 顺序很关键：globe.gl 的 width/height 是 triggerUpdate:false 的 prop，
       底层 treemap 的 onChange 会用 window.innerWidth/innerHeight 兜底，
       所以先把 DOM 尺寸写实，再通知 globe.gl，它才不会把画布改回窗口大小。 */
    if (scene) {
      scene.style.width = w + "px";
      scene.style.height = h + "px";
    }
    if (cv) {
      cv.style.width = w + "px";
      cv.style.height = h + "px";
      cv.width = Math.floor(w * dpr);
      cv.height = Math.floor(h * dpr);
    }
    g.width(w).height(h);
    if (typeof g.viewOffset === "function") g.viewOffset([0, 0]);   // 球心别被任何偏移推走
    const cam = g.camera();
    if (cam && Math.abs(cam.aspect - w / h) > 0.001) {
      cam.aspect = w / h;
      cam.updateProjectionMatrix();
    }
    // globe.gl 的 digest 可能在之后把画布改回窗口尺寸，下一帧再核对一次
    requestAnimationFrame(() => {
      const c2 = host.querySelector("canvas");
      if (!c2) return;
      if (c2.style.width !== w + "px" || c2.style.height !== h + "px") {
        c2.style.width = w + "px";
        c2.style.height = h + "px";
        if (c2.width !== Math.floor(w * dpr)) c2.width = Math.floor(w * dpr);
        if (c2.height !== Math.floor(h * dpr)) c2.height = Math.floor(h * dpr);
      }
      const s2 = host.querySelector(".scene-container");
      if (s2 && s2.style.width !== w + "px") { s2.style.width = w + "px"; s2.style.height = h + "px"; }
      const c = g.camera();
      if (c && Math.abs(c.aspect - w / h) > 0.001) { c.aspect = w / h; c.updateProjectionMatrix(); }
    });
  }

  function isFull() {
    const stage = $("globe-stage");
    return !!(document.fullscreenElement || (stage && stage.classList.contains("full")));
  }

  /** 全屏只留地球：左右两栏由 CSS 让位（.globe-stage.full） */
  function toggleFullscreen(want) {
    const stage = $("globe-stage");
    if (!stage) return;
    const on = want === undefined ? !isFull() : !!want;
    stage.classList.toggle("full", on);
    document.body.classList.toggle("map-locked", on);
    const btn = $("globe-full");
    if (btn) btn.textContent = on ? "⛶ 退出全屏" : "⛶ 全屏";
    if (on && stage.requestFullscreen) {
      try {
        const p = stage.requestFullscreen();
        if (p && p.catch) p.catch(() => { /* 原生全屏不可用就用 CSS 兜底 */ });
      } catch (err) { /* 同上 */ }
    } else if (!on && document.fullscreenElement && document.exitFullscreen) {
      try {
        const p = document.exitFullscreen();
        if (p && p.catch) p.catch(() => { });
      } catch (err) { /* 忽略 */ }
    }
    setTimeout(() => { fitCanvas(); fitCanvas(); }, 80);
    setTimeout(fitCanvas, 300);
  }

  /* ---------------------------------------------------------- 卡片渲染 */
  function buildCards(cities) {
    const layer = $("globe-cards");
    if (!layer) return;
    layer.querySelectorAll(".city-card").forEach((el) => el.remove());
    state.cards = new Map();
    state.cardList = [];
    const frag = document.createDocumentFragment();
    cities.forEach((city, i) => {
      const el = document.createElement("button");
      el.type = "button";
      el.className = "city-card";
      el.dataset.city = city.name;
      el.style.animationDelay = Math.min(i * 40, 900) + "ms";
      el.innerHTML = cardHtml(city);
      const photo = el.querySelector(".cc-photo");
      if (photo) {
        photo.style.setProperty("--tint", areaColor(city.area));
        photo.style.backgroundImage = artUri(city);
        if (city.photo && typeof Image === "function") {
          const probe = new Image();
          probe.onerror = () => { photo.style.backgroundImage = artUri(city); };
          probe.src = city.photo;
        }
      }
      el.addEventListener("mouseenter", () => showTip(city, el));
      el.addEventListener("mouseleave", () => hideTipSoon());
      el.addEventListener("click", (e) => { e.stopPropagation(); toggleCity(city.name); });
      const gb = el.querySelector(".cc-guide");
      if (gb) {
        gb.addEventListener("pointerdown", (e) => e.stopPropagation());
        gb.addEventListener("click", (e) => { e.stopPropagation(); hideTip(); openCityGuide(city.name); });
      }
      frag.appendChild(el);
      state.cards.set(city.name, el);
      state.cardList.push({ city, el });
    });
    layer.appendChild(frag);
    refreshBadges();
  }

  function clearCards() {
    const layer = $("globe-cards");
    if (layer) layer.querySelectorAll(".city-card").forEach((el) => el.remove());
    state.cards = new Map();
    state.cardList = [];
    hideTip();
  }

  function cardHtml(city) {
    const kinds = [...new Set((city.highlights || []).map((h) => h.kind))]
      .slice(0, 3).map((k) => KIND_ICON[k] || "📍").join("");
    const moon = city.night ? `<span class="cc-moon" title="更适合夜游：配的是夜景照片">🌙</span>` : "";
    const guide = city.has_guide
      ? `<button type="button" class="cc-guide" title="看 ${esc(city.name)} 的完整攻略">📖</button>` : "";
    return `<span class="cc-inner">
      <span class="cc-photo">${moon}${guide}<span class="cc-badge" hidden></span></span>
      <span class="cc-name">${esc(city.name)}</span>
      <span class="cc-kinds">${kinds}</span>
    </span>`;
  }

  function areaColor(area) {
    const map = {
      东亚: "#6fb1ff", 东南亚: "#7ee081", 南亚: "#e2b04a", 中亚: "#c9a06a",
      中东: "#ffb066", 欧洲: "#b39dff", 北美: "#7fd7f5", 拉美: "#ff9f7a",
      非洲: "#ffd479", 大洋洲: "#6ee7c8",
    };
    return map[area] || "#6ee7c8";
  }

  /** 环境色 → 画在球面上的颜色。
   *
   * 环境色是给「陆地/浅色背景」用的（沙漠土黄 #d9a441、海洋深蓝 #2f6f9f），
   * 直接拿来画在近黑的球面上，深蓝那条线会糊进海面里。这里只提亮度、不动色相，
   * 所以「沙漠=黄、海洋=蓝、极地=冰蓝」的辨识度不变，线却看得见了。
   * 列表里的圆点与标签仍然用原始环境色 —— 那里是白底，用原色才准。
   */
  function globeStroke(hex) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || "").trim());
    if (!m) return hex || "#7fd7f5";
    const n = parseInt(m[1], 16);
    let r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    let h = 0, s = 0;
    const l = (max + min) / 2 / 255;
    if (max !== min) {
      const d = (max - min) / 255;
      s = l > 0.5 ? d / (2 - max / 255 - min / 255) : d / ((max + min) / 255);
      if (max === r) h = ((g - b) / (max - min) + (g < b ? 6 : 0)) / 6;
      else if (max === g) h = ((b - r) / (max - min) + 2) / 6;
      else h = ((r - g) / (max - min) + 4) / 6;
    }
    const target = Math.max(l, 0.66);      // 球面是深色的，线至少要有这个亮度
    const keepS = Math.max(s * 0.85, 0.35);
    const hue2rgb = (p, q, t) => {
      if (t < 0) t += 1;
      if (t > 1) t -= 1;
      if (t < 1 / 6) return p + (q - p) * 6 * t;
      if (t < 1 / 2) return q;
      if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
      return p;
    };
    const q = target < 0.5 ? target * (1 + keepS) : target + keepS - target * keepS;
    const p = 2 * target - q;
    r = Math.round(hue2rgb(p, q, h + 1 / 3) * 255);
    g = Math.round(hue2rgb(p, q, h) * 255);
    b = Math.round(hue2rgb(p, q, h - 1 / 3) * 255);
    const hex2 = (v) => v.toString(16).padStart(2, "0");
    return `#${hex2(r)}${hex2(g)}${hex2(b)}`;
  }

  function artUri(city) {
    if (city.photo) return `url("${city.photo}")`;
    return `url("data:image/svg+xml,${encodeURIComponent(cityPoster(city))}")`;
  }

  /** 没有照片时的矢量海报（与地图模式同一套思路，按大区配色 + 城市首字） */
  function cityPoster(city) {
    const tint = areaColor(city.area);
    const ch = (city.name || "?").slice(0, 1);
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 70 86" width="70" height="86">
      <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="${tint}" stop-opacity="0.85"/>
        <stop offset="1" stop-color="#05202a"/></linearGradient></defs>
      <rect width="70" height="86" fill="url(#g)"/>
      <path d="M0 86 L14 52 L26 66 L40 40 L54 62 L70 86 Z" fill="#05202a" opacity="0.75"/>
      <text x="35" y="45" font-size="26" text-anchor="middle" fill="#eafff9" opacity="0.9"
        font-family="sans-serif">${esc(ch)}</text>
    </svg>`;
  }

  function refreshBadges() {
    state.cards.forEach((el, name) => {
      const idx = state.active.indexOf(name);
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

  /* ---------------------------------------------------------- 悬停提示 */
  function showTip(city, el) {
    const tip = $("globe-tip");
    if (!tip) return;
    if (state.tipTimer) { clearTimeout(state.tipTimer); state.tipTimer = null; }
    const kinds = (city.highlights || []).map((h) =>
      `<span class="tip-kind">${KIND_ICON[h.kind] || "📍"} ${esc(h.name)}</span>`).join("");
    const picked = state.active.includes(city.name);
    tip.innerHTML =
      `<div class="tip-head"><b>${esc(city.name)}</b>
        <span class="tip-score">${city.score}</span>
        <span class="tip-region">${esc(city.country)} · ${esc(city.area)}</span></div>
      <div class="tip-intro">${esc(city.intro || "")}</div>
      <div class="tip-half"><span class="tip-label">半日游</span>${esc(city.halfday || "暂无半日建议")}</div>
      ${city.oneday ? `<div class="tip-day"><span class="tip-label">一日</span>${esc(city.oneday)}</div>` : ""}
      <div class="tip-kinds">${kinds}</div>
      <div class="tip-foot">建议 ${city.days} 天 · ${esc(city.best_season || "全年")}
        ${city.night ? " · 🌙 夜景照" : ""}${city.has_guide ? ` · 📖 ${city.guide_days || ""} 天完整攻略` : ""}</div>
      <div class="tip-actions">
        <button type="button" class="tip-btn primary-sm" data-act="guide"${city.has_guide ? "" : " disabled"}>📖 看完整攻略</button>
        <button type="button" class="tip-btn" data-act="pick">${picked ? "✓ 移出路线" : "＋ 加入路线"}</button>
      </div>`;
    tip.hidden = false;
    tip.dataset.city = city.name;
    if (!tip.dataset.bound) {
      tip.dataset.bound = "1";
      tip.addEventListener("mouseenter", () => {
        if (state.tipTimer) { clearTimeout(state.tipTimer); state.tipTimer = null; }
      });
      tip.addEventListener("mouseleave", hideTip);
      tip.addEventListener("click", (e) => {
        const btn = e.target.closest(".tip-btn");
        if (!btn) return;
        e.stopPropagation();
        const name = tip.dataset.city;
        if (btn.dataset.act === "guide") { hideTip(); openCityGuide(name); }
        else if (btn.dataset.act === "pick") {
          toggleCity(name);
          const card = state.cards.get(name);
          const item = state.cardList.find((x) => x.el === card);
          if (item) showTip(item.city, card);
        }
      });
    }
    // 位置：卡片上方/下方都行，靠近就够（有缓冲不会断）
    const stage = $("globe-stage").getBoundingClientRect();
    const r = el.getBoundingClientRect();
    const tw = tip.offsetWidth || 300, th = tip.offsetHeight || 200;
    let left = clamp(r.left - stage.left + r.width / 2, tw / 2 + 8, stage.width - tw / 2 - 8);
    let top = r.top - stage.top - 8;
    top = top - th < 8 ? r.bottom - stage.top + 10 : top - th;
    tip.style.left = left + "px";
    tip.style.top = Math.max(8, top) + "px";
  }

  function hideTipSoon() {
    if (state.tipTimer) clearTimeout(state.tipTimer);
    state.tipTimer = setTimeout(() => {
      state.tipTimer = null;
      const t = $("globe-tip");
      if (t) t.hidden = true;
    }, 260);
  }
  function hideTip() {
    if (state.tipTimer) { clearTimeout(state.tipTimer); state.tipTimer = null; }
    const t = $("globe-tip");
    if (t) t.hidden = true;
  }

  /* ---------------------------------------------------------- 选择与路线 */
  function toggleCity(name) {
    const idx = state.active.indexOf(name);
    if (idx >= 0) state.active.splice(idx, 1);
    else {
      if (state.active.length >= 12) { info("一次最多 12 站，先生成行程单或删掉几站"); return; }
      state.active.push(name);
    }
    renderPicked();
    refreshBadges();
    updatePaths();
    updatePoints();
  }

  function updatePaths() {
    if (!g || !state.data) return;
    const paths = [];
    if (state.active.length > 1) {
      for (let i = 0; i < state.active.length - 1; i++) {
        const a = state.data.cityIndex[state.active[i]], b = state.data.cityIndex[state.active[i + 1]];
        if (a && b) paths.push({ coords: [[a.lat, a.lon], [b.lat, b.lon]], color: "#ffd36b", stroke: 1.3 });
      }
    }
    if (state.activeLoop) {
      const cs = (state.activeLoop.cities || []).map((n) => state.data.cityIndex[n]).filter(Boolean);
      // 航线颜色跟着**环境**走：沙漠土黄、海洋深蓝、极地冰蓝（后端按 env 给 env_color）；
      // 球面太深，深色环境色要提亮一档才看得见（见 globeStroke）
      const envColor = globeStroke(state.activeLoop.env_color || "#7fd7f5");
      for (let i = 0; i < cs.length - 1; i++) {
        paths.push({
          coords: [[cs[i].lat, cs[i].lon], [cs[i + 1].lat, cs[i + 1].lon]],
          color: envColor, stroke: state.activeLoop ? 1.8 : 1.1,
        });
      }
      if (cs.length > 2) {
        paths.push({
          coords: [[cs[cs.length - 1].lat, cs[cs.length - 1].lon], [cs[0].lat, cs[0].lon]],
          color: envColor, stroke: 0.8, dash: true,
        });
      }
    }
    if (state.activeRecord && state.data.cityIndex[state.nearestCityOfRecord]) {
      const r = state.data.records.find((x) => x.id === state.activeRecord);
      const c = state.data.cityIndex[state.nearestCityOfRecord];
      if (r && c) paths.push({ coords: [[c.lat, c.lon], [r.lat, recLon(r)]], color: "#ffcf6b", stroke: 0.9 });
    }
    g.pathsData(paths);
  }

  function renderPicked() {
    const list = $("globe-picked-list");
    const count = $("globe-picked-count");
    if (count) count.textContent = String(state.active.length);
    if (!list) return;
    list.innerHTML = "";
    state.active.forEach((name, i) => {
      const city = state.data.cityIndex[name] || { name };
      const li = document.createElement("li");
      li.className = "picked-item";
      li.style.setProperty("--tint", areaColor(city.area));
      li.innerHTML =
        `<span class="pi-no">${i + 1}</span>
         <span class="pi-name">${esc(name)}<em>${esc(city.country || "")}</em></span>
         <span class="pi-tools">
           <button type="button" class="pi-btn" data-act="guide" title="完整攻略">📖</button>
           <button type="button" class="pi-btn" data-act="up" title="上移">↑</button>
           <button type="button" class="pi-btn" data-act="down" title="下移">↓</button>
           <button type="button" class="pi-btn" data-act="fly" title="飞过去">◎</button>
           <button type="button" class="pi-btn danger" data-act="del" title="移除">✕</button>
         </span>`;
      li.querySelectorAll(".pi-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
          const act = btn.dataset.act;
          if (act === "guide") { flyTo(name); openCityGuide(name); return; }
          if (act === "del") state.active.splice(i, 1);
          else if (act === "up" && i > 0) state.active.splice(i - 1, 0, state.active.splice(i, 1)[0]);
          else if (act === "down" && i < state.active.length - 1) {
            state.active.splice(i + 1, 0, state.active.splice(i, 1)[0]);
          } else if (act === "fly") { flyTo(name); return; }
          renderPicked();
          refreshBadges();
          updatePaths();
          updatePoints();
        });
      });
      list.appendChild(li);
    });
    const hint = $("globe-picked-hint");
    if (hint) hint.hidden = state.active.length > 0;
  }

  /* ---------------------------------------------------------- 环线 */
  function renderLoopSelect() {
    const sel = $("globe-loop-select");
    if (!sel) return;
    const loops = (state.data && state.data.loops) || [];
    sel.innerHTML = "";
    if (!loops.length) {
      // 世界数据还没到：说清楚在等什么，别让人以为「只有不选」
      sel.add(new Option(state.ready ? "（环线加载失败）" : "（正在载入环线…）", ""));
      return;
    }
    sel.add(new Option("（不选）", ""));
    loops.forEach((l) => sel.add(new Option(`${l.name}（${l.days} 天）`, l.id)));
    sel.onchange = () => {
      const loop = loops.find((x) => x.id === sel.value);
      selectLoop(loop || null, { home: !loop });
      syncLoopAddButton(loop || null);
    };
  }

  /** 工具栏上的「加进路线」按钮：选中环线后直接可点，不必先找详情面板 */
  function syncLoopAddButton(loop) {
    const btn = $("globe-loop-add");
    if (!btn) return;
    btn.disabled = !loop;
    btn.title = loop ? `把「${loop.name}」的 ${loop.cities.length} 座城市按顺序放进路线`
                     : "先在上面选一条环线";
  }

  function addLoopToRoute(loop) {
    if (!loop) return false;
    state.active = (loop.cities || []).filter((n) => state.data.cityIndex[n]);
    renderPicked();
    refreshBadges();
    updatePaths();
    updatePoints();
    info(`已载入「${loop.name}」（${state.active.length} 站），点右侧「生成行程单」看飞行距离与时差`);
    return true;
  }

  /** 选中一条环线：城市卡只留这条线的城市，镜头飞过去把整条线装进画面 */
  function selectLoop(loop, opts) {
    state.activeLoop = loop;
    if (!loop) {
      renderLoops();
      updatePaths();
      updatePoints();
      clearCards();
      buildCards(state.data.cities);
      if (opts && opts.home) flyHome();       // 下拉框选「不选」时顺手回到全球视角
      return;
    }
    renderLoops();
    const cs = (loop.cities || []).map((n) => state.data.cityIndex[n]).filter(Boolean);
    buildCards(cs);
    updatePaths();
    updatePoints();
    const view = flyToCities(cs, 1.1, 2.3);
    info(`${loop.name}｜${loop.cities.join(" → ")}｜${loop.days} 天 · ${loop.season}｜` +
      `${loop.km} 公里 · ${loop.transport}` +
      (view ? `｜视角已对准这条线（约 ${Math.round(view.span)}° 跨度）` : ""));
    if (!opts || opts.detail !== false) showLoopDetail(loop);
  }

  function renderLoops() {
    const box = $("globe-loops");
    if (!box) return;
    box.innerHTML = "";
    state.data.loops.forEach((loop) => {
      const item = document.createElement("div");
      item.className = "loop-item" + (state.activeLoop && state.activeLoop.id === loop.id ? " active" : "");
      const envColor = loop.env_color || "#6b7f95";
      item.innerHTML =
        `<div class="loop-title"><i class="loop-env" style="background:${envColor}"
            title="${esc(loop.env_label || "")}"></i>${esc(loop.name)}
           ${loop.cross_region ? '<span class="tag cross">跨洲</span>' : ""}
           ${loop.env ? `<span class="tag env" style="border-color:${envColor}">${esc(loop.env)}</span>` : ""}
         </div>
         <div class="loop-sub">${esc(loop.cities.join(" → "))}</div>
         <div class="loop-meta">${loop.days} 天 · ${esc(loop.season || "全年")} · ${loop.km} 公里</div>`;
      item.addEventListener("click", () => {
        const sel = $("globe-loop-select");
        if (sel) sel.value = loop.id;
        selectLoop(loop);
      });
      box.appendChild(item);
    });
  }

  function showLoopDetail(loop) {
    const panel = $("globe-loops-panel");
    panel.classList.remove("collapsed");
    let box = $("globe-loop-detail");
    if (!box) {
      box = document.createElement("div");
      box.id = "globe-loop-detail";
      box.className = "loop-detail";
      panel.insertBefore(box, panel.querySelector(".panel-body"));
    }
    box.innerHTML =
      `<div class="ld-head">${esc(loop.name)}
         <button type="button" class="ld-close" title="关闭">✕</button></div>
       <div class="ld-blurb">${esc(loop.blurb || "")}</div>
       <div class="ld-meta">${loop.cities.length} 站 · ${loop.days} 天 · ${loop.km} 公里 · ${esc(loop.transport || "")}</div>
       <ul class="ld-tips">${(loop.tips || []).map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
       <button type="button" class="primary ld-use">把这条环线放进路线</button>`;
    box.querySelector(".ld-close").addEventListener("click", () => box.remove());
    box.querySelector(".ld-use").addEventListener("click", () => addLoopToRoute(loop));
  }

  /** 显示 / 隐藏图标：城市卡与世界之最数值卡都在 #globe-cards 这一层。
   * 收起来只剩球、航线与点位 —— 看全球走向时不被上百张卡片糊住。 */
  function setIcons(show) {
    state.showIcons = !!show;
    const layer = $("globe-cards");
    if (layer) layer.hidden = !state.showIcons;
    const btn = $("globe-icons-toggle");
    if (btn) btn.textContent = state.showIcons ? "隐藏图标" : "显示图标";
    hideTip();
    if (state.showIcons) layoutCards();     // 重新显示时立刻排一次版，不然要等下一帧
  }

  /* ---------------------------------------------------------- 世界之最 */
  function renderRecords() {
    const filterBox = $("globe-records-filter");
    const box = $("globe-records");
    if (!box) return;
    const cats = ["全部", ...new Set(state.data.records.map((r) => r.category))];
    if (filterBox && !filterBox.dataset.built) {
      filterBox.dataset.built = "1";
      cats.forEach((cat) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "rec-chip" + (cat === state.filter ? " active" : "");
        b.textContent = cat;
        b.addEventListener("click", () => {
          state.filter = cat;
          filterBox.querySelectorAll(".rec-chip").forEach((x) => x.classList.toggle("active", x.textContent === cat));
          renderRecords();
          updatePoints();
        });
        filterBox.appendChild(b);
      });
    }
    box.innerHTML = "";
    const list = state.data.records.filter((r) => state.filter === "全部" || r.category === state.filter);
    list.forEach((rec) => {
      const item = document.createElement("div");
      item.className = "rec-item" + (state.activeRecord === rec.id ? " active" : "");
      item.innerHTML =
        `<div class="rec-name"><span class="rec-cat">${esc(rec.category)}</span>${esc(rec.name)}
           <span class="rec-value">${esc(rec.value)}</span></div>
         <div class="rec-sub">${esc(rec.country)} · ${esc((rec.blurb || "").slice(0, 30))}…</div>`;
      item.addEventListener("click", () => focusRecord(rec));
      box.appendChild(item);
    });
    renderRecordCards(list);
  }

  function renderRecordCards(list) {
    const layer = $("globe-cards");
    if (!layer) return;
    state.recordEls.forEach((x) => x.el.remove());
    state.recordEls = [];
    if (!state.showRecords) return;
    const frag = document.createDocumentFragment();
    list.forEach((rec, i) => {
      const el = document.createElement("button");
      el.type = "button";
      el.className = "record-card";
      el.style.animationDelay = Math.min(i * 35, 800) + "ms";
      el.innerHTML = `<span class="rc-inner">
        <span class="rc-photo" style="background-image:${rec.photo ? `url("${rec.photo}")` : "none"}">
          <span class="rc-tag">${esc(rec.category)}</span>
          <span class="rc-value">${esc(rec.value)}</span>
        </span>
        <span class="rc-name">${esc(rec.name)}</span>
      </span>`;
      el.addEventListener("click", (e) => { e.stopPropagation(); focusRecord(rec); });
      frag.appendChild(el);
      state.recordEls.push({ rec, el });
    });
    layer.appendChild(frag);
  }

  /** 点一条世界之最：先飞过去看它在哪，再弹出介绍 */
  function focusRecord(rec, opts) {
    state.activeRecord = rec.id;
    state.nearestCityOfRecord = rec.nearest_city;
    renderRecords();
    updatePoints();
    updatePaths();
    flyToPoint(rec.lat, recLon(rec), 1.05);
    info(`${rec.name}｜${rec.category}｜${rec.value}｜${rec.country}｜视角已飞过去`);
    if (!opts || opts.guide !== false) openRecordGuide(rec.id);
  }

  /* ---------------------------------------------------------- 详情弹层 */
  function guideShell(heroHtml, bodyHtml) {
    const layer = $("globe-guide");
    const panel = $("gg-panel");
    if (!layer || !panel) return null;
    layer.hidden = false;
    panel.innerHTML = `${heroHtml}<div class="cg-body">${bodyHtml}</div>`;
    const close = panel.querySelector(".cg-close");
    if (close) close.addEventListener("click", closeGuide);
    return panel;
  }

  function closeGuide() {
    const layer = $("globe-guide");
    if (layer) layer.hidden = true;
  }

  async function openCityGuide(name) {
    const panel = guideShell("", `<div class="gg-loading">正在加载「${esc(name)}」的攻略…</div>`);
    if (!panel) return;
    try {
      const resp = await fetch("/api/world/city?name=" + encodeURIComponent(name));
      const data = await resp.json();
      if (!data.ok) throw new Error(data.error || "没有这座城市");
      const c = data.city, gu = data.guide || {};
      const kinds = (c.highlights || []).map((h) =>
        `<span class="cg-kind">${KIND_ICON[h.kind] || "📍"} ${esc(h.name)}</span>`).join("");
      const hero = `<div class="cg-hero" id="gg-hero">
          <button type="button" class="cg-close" title="关闭（Esc）">✕</button>
          <div class="cg-title"><b>${esc(c.name)}</b>
            <span class="cg-score">${c.score}</span>
            <span class="cg-where">${esc(c.country)} · ${esc(c.area)}${c.night ? " · 🌙 更适合夜游" : ""}${
              c.tz != null ? ` · 时差 ${c.tz > 0 ? "+" : ""}${c.tz} 小时` : ""}</span></div>
        </div>`;
      const body =
        `<div class="cg-tags">${(c.tags || []).map((t) => `<span class="cg-tag">${esc(t)}</span>`).join("")}</div>
         <div class="cg-lead">${esc(c.intro || "")}</div>
         <div class="cg-sec"><h4>半日路线</h4><div class="cg-lead">${esc(c.halfday || "—")}</div></div>
         ${c.oneday ? `<div class="cg-sec"><h4>一日路线</h4><div class="cg-lead">${esc(c.oneday)}</div></div>` : ""}
         ${kinds ? `<div class="cg-sec"><h4>看点</h4><div class="cg-kinds">${kinds}</div></div>` : ""}
         <div class="cg-sec"><h4>完整攻略${gu.days ? ` · 建议 ${gu.days} 天` : ""}</h4>
           <div class="cg-itinerary">${(gu.itinerary || []).map((d) => `
             <div class="cg-day"><div class="cg-day-head">${esc(d.day)}${d.title ? " · " + esc(d.title) : ""}</div>
               <div class="cg-day-body">${esc(d.detail)}</div></div>`).join("")
        || `<div class="cg-note">这座城市暂时没有更详细的多日攻略。</div>`}</div></div>
         <div class="cg-sec"><h4>实用信息</h4>
           <div class="cg-row"><span class="cg-label">交通</span>${esc(gu.transport || "—")}</div>
           <div class="cg-row"><span class="cg-label">住宿</span>${esc(gu.stay || "—")}</div>
           <div class="cg-row"><span class="cg-label">吃</span>${esc(gu.eat || c.food || "—")}</div>
           <div class="cg-row"><span class="cg-label">预算</span>${esc(gu.budget || "—")}</div>
           <div class="cg-row"><span class="cg-label">季节</span>${esc(c.best_season || "全年")}</div>
         </div>
         ${(gu.tips || []).length ? `<div class="cg-sec"><h4>提示</h4>
           <ul class="cg-tips">${gu.tips.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></div>` : ""}
         ${c.note ? `<div class="cg-note">${esc(c.note)}</div>` : ""}
         <div class="cg-actions">
           <button type="button" class="primary" id="gg-toggle">加入 / 移出路线</button>
           <button type="button" class="ghost-sm" id="gg-fly">在地球上定位</button>
         </div>
         <div class="cg-note">攻略为个人整理的行程建议；签证、门票与航班以官方渠道为准。</div>`;
      const panel2 = guideShell(hero, body);
      const h = panel2.querySelector("#gg-hero");
      if (h && c.photo) h.style.backgroundImage = `url("${c.photo}")`;
      const t = panel2.querySelector("#gg-toggle");
      if (t) {
        t.textContent = state.active.includes(c.name) ? "从路线里移除" : "加入路线";
        t.addEventListener("click", () => {
          toggleCity(c.name);
          t.textContent = state.active.includes(c.name) ? "从路线里移除" : "加入路线";
        });
      }
      const f = panel2.querySelector("#gg-fly");
      if (f) f.addEventListener("click", () => { closeGuide(); flyTo(c.name); });
    } catch (err) {
      guideShell("", `<div class="gg-loading">加载失败：${esc(err.message)}</div>`);
    }
  }

  async function openRecordGuide(id) {
    try {
      const resp = await fetch("/api/world/record?id=" + encodeURIComponent(id));
      const data = await resp.json();
      if (!data.ok) throw new Error(data.error || "没有这条记录");
      const r = data.record;
      const hero = `<div class="cg-hero" id="gg-hero">
          <button type="button" class="cg-close" title="关闭（Esc）">✕</button>
          <div class="cg-title"><b>${esc(r.name)}</b>
            <span class="cg-score">${esc(r.value)}</span>
            <span class="cg-where">${esc(r.category)} · ${esc(r.country)}</span></div>
        </div>`;
      const body =
        `<div class="cg-lead">${esc(r.blurb)}</div>
         <div class="cg-sec"><h4>怎么去</h4><div class="cg-lead">${esc(r.visit)}</div></div>
         <div class="cg-sec"><h4>实用信息</h4>
           <div class="cg-row"><span class="cg-label">最近城市</span>${esc(r.nearest_city || "—")}</div>
           <div class="cg-row"><span class="cg-label">最佳季节</span>${esc(r.best_season || "全年")}</div>
           <div class="cg-row"><span class="cg-label">坐标</span>${r.lat}, ${recLon(r)}</div>
         </div>
         <div class="cg-actions">
           ${state.data.cityIndex[r.nearest_city]
             ? `<button type="button" class="primary" id="gg-add-nearest">把「${esc(r.nearest_city)}」加入路线</button>` : ""}
           <button type="button" class="ghost-sm" id="gg-fly-record">在地球上定位</button>
         </div>
         <div class="cg-note">数值为公开资料的常见口径；出行前请以官方信息为准。</div>`;
      const panel = guideShell(hero, body);
      const h = panel.querySelector("#gg-hero");
      if (h && r.photo) h.style.backgroundImage = `url("${r.photo}")`;
      const add = panel.querySelector("#gg-add-nearest");
      if (add) add.addEventListener("click", () => { toggleCity(r.nearest_city); });
      const f = panel.querySelector("#gg-fly-record");
      // 弹层里只定位、不再弹一次介绍
      if (f) f.addEventListener("click", () => { closeGuide(); focusRecord(r, { guide: false }); });
    } catch (err) {
      guideShell("", `<div class="gg-loading">加载失败：${esc(err.message)}</div>`);
    }
  }

  /* ---------------------------------------------------------- 行程单 */
  async function planWorld() {
    const host = $("globe-plan");
    if (!host) return;
    if (state.active.length < 2) {
      info("至少选 2 座城市才能生成行程单");
      return;
    }
    host.innerHTML = `<div class="status">正在生成世界行程单…</div>`;
    try {
      const data = await window.TP.postJSON("/api/world/plan", {
        cities: state.active, closed: state.active.length > 2,
      });
      if (!data.ok) { host.innerHTML = `<div class="warn">${esc(data.error || "生成失败")}</div>`; return; }
      const s = data.summary;
      const box = document.createElement("div");
      box.innerHTML =
        `<div class="summary roam-summary"><b>${data.order.join(" → ")}</b>
           <br><span class="dim">${s.city_count} 城 · ${s.areas.join("/")} · 大圆距离 ${s.total_km} 公里 ·
           预计飞行 ${s.flight_hours} 小时 · 建议游览 ${s.visit_days} 天 + 转场 ${s.travel_days} 天 =
           约 <b>${s.total_days} 天</b>${s.tz_span ? ` · 时差跨度 ${s.tz_span[1] - s.tz_span[0]} 小时` : ""}</span></div>`;
      (data.warnings || []).forEach((w) => {
        const d = document.createElement("div");
        d.className = "warn";
        d.textContent = "⚠ " + w;
        box.appendChild(d);
      });
      const title = document.createElement("div");
      title.className = "roam-subtitle";
      title.textContent = "分段飞行";
      box.appendChild(title);
      data.legs.forEach((leg) => {
        const card = document.createElement("div");
        card.className = "card";
        card.innerHTML =
          `<div class="world-day"><span class="wd-km">${leg.km} 公里</span>
             <span>${esc(leg.frm)} → ${esc(leg.to)}</span>
             <span>预计飞行 ${esc(leg.flight_text)}</span>
             ${leg.tz_shift ? `<span class="wd-tz">时差 ${leg.tz_shift > 0 ? "+" : ""}${leg.tz_shift} 小时</span>` : ""}
             <span class="wd-note">${leg.same_country ? "同一国家，可考虑陆路/高铁" : "跨国航段"}</span></div>`;
        box.appendChild(card);
      });
      const guideTitle = document.createElement("div");
      guideTitle.className = "roam-subtitle";
      guideTitle.textContent = "每座城市怎么玩";
      box.appendChild(guideTitle);
      const grid = document.createElement("div");
      grid.className = "guide-grid";
      (data.cities || []).forEach((c, i) => {
        const card = document.createElement("div");
        card.className = "guide-card";
        card.style.setProperty("--tint", areaColor(c.area));
        const kinds = (c.highlights || []).map((h) =>
          `<span class="g-kind">${KIND_ICON[h.kind] || "📍"} ${esc(h.name)}</span>`).join("");
        card.innerHTML =
          `<div class="g-head"><span class="g-no">${i + 1}</span><b>${esc(c.name)}</b>
             <span class="g-score">${c.score}</span></div>
           <div class="g-intro">${esc(c.intro || "")}</div>
           <div class="g-row"><span class="g-label">半日</span>${esc(c.halfday || "—")}</div>
           ${c.oneday ? `<div class="g-row"><span class="g-label">一日</span>${esc(c.oneday)}</div>` : ""}
           <div class="g-kinds">${kinds}</div>
           <div class="g-foot">建议 ${c.days} 天 · ${esc(c.best_season || "全年")} · ${esc(c.country)}</div>
           <div class="g-more"><button type="button" class="g-more-btn">需要更多攻略？看完整攻略 ▾</button></div>`;
        card.querySelector(".g-more-btn").addEventListener("click", () => openCityGuide(c.name));
        grid.appendChild(card);
      });
      box.appendChild(grid);
      host.innerHTML = "";
      host.appendChild(box);
      info("行程单已生成：世界机票必须实时查询，这里只给距离、飞行时间、时差与天数建议");
    } catch (err) {
      host.innerHTML = `<div class="warn">生成失败：${esc(err.message)}</div>`;
    }
  }

  /* ---------------------------------------------------------- 事件绑定 */
  function bind() {
    const home = $("globe-home");
    if (home) home.addEventListener("click", flyHome);
    const auto = $("globe-auto");
    if (auto) auto.addEventListener("click", () => {
      if (!g) return;
      setAutoRotate(!g.controls().autoRotate);
    });
    const clear = $("globe-clear");
    if (clear) clear.addEventListener("click", () => {
      state.active = [];
      renderPicked();
      refreshBadges();
      updatePaths();
      updatePoints();
    });
    const toggle = $("globe-records-toggle");
    if (toggle) toggle.addEventListener("click", () => {
      state.showRecords = !state.showRecords;
      toggle.textContent = state.showRecords ? "隐藏世界之最" : "显示世界之最";
      renderRecordCards(state.data.records.filter((r) => state.filter === "全部" || r.category === state.filter));
      updatePoints();
    });
    const iconsBtn = $("globe-icons-toggle");
    if (iconsBtn) iconsBtn.addEventListener("click", () => setIcons(!state.showIcons));
    const plan = $("globe-plan-go");
    if (plan) plan.addEventListener("click", planWorld);
    // 工具栏上的「加进路线」：选中环线后一键把整条线的城市按顺序放进去
    const loopAdd = $("globe-loop-add");
    if (loopAdd) loopAdd.addEventListener("click", () => {
      const sel = $("globe-loop-select");
      const loop = sel && state.data ? (state.data.loops || []).find((x) => x.id === sel.value) : null;
      if (!addLoopToRoute(loop)) info("先在上面的「经典环线」里选一条，再点这里加进路线");
    });
    // 世界数据是异步来的：载入完成后刷新下拉框与按钮状态
    const ready = setInterval(() => {
      if (state.ready) {
        clearInterval(ready);
        renderLoopSelect();
        syncLoopAddButton(state.activeLoop);
      }
    }, 300);
    const full = $("globe-full");
    if (full) full.addEventListener("click", () => toggleFullscreen());
    document.addEventListener("fullscreenchange", () => {
      const stage = $("globe-stage");
      if (!stage) return;
      const on = !!document.fullscreenElement;
      stage.classList.toggle("full", on);
      document.body.classList.toggle("map-locked", on);
      const btn = $("globe-full");
      if (btn) btn.textContent = on ? "⛶ 退出全屏" : "⛶ 全屏";
      fitCanvas();
      setTimeout(fitCanvas, 300);
    });
    const amap = $("globe-amap");
    if (amap) amap.addEventListener("click", showAmap);
    document.querySelectorAll("#tab-globe .panel-toggle").forEach((btn) => {
      btn.addEventListener("click", () => {
        const panel = $(btn.dataset.target);
        panel.classList.toggle("collapsed");
        btn.textContent = panel.classList.contains("collapsed") ? "+" : "–";
      });
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      const layer = $("globe-guide");
      if (layer && !layer.hidden) { closeGuide(); return; }
      const stage = $("globe-stage");
      if (stage && stage.classList.contains("full")) { toggleFullscreen(false); return; }
      if (state.activeRecord) {
        state.activeRecord = null;
        state.nearestCityOfRecord = null;
        renderRecords();
        updatePaths();
        updatePoints();
        return;
      }
      if (state.activeLoop) { selectLoop(null, { detail: false }); return; }
      if (state.active.length) {
        state.active = [];
        renderPicked();
        refreshBadges();
        updatePaths();
        updatePoints();
        info("已清空路线");
        return;
      }
      flyHome();
    });
  }

  /* ---------------------------------------------------------- 高德 3D（保留原有功能） */
  const AMAP_KEY = "7913f5e8e44f41933f72f18b5e9320d";
  function showAmap() {
    const box = $("amap-info");
    if (!box) return;
    box.hidden = false;
    box.textContent = "正在加载高德 3D 地形…";
    if (!AMAP_KEY) return;
    if (window.AMap) { mountAmap(); return; }
    const sc = document.createElement("script");
    sc.src = "https://webapi.amap.com/maps?v=2.0&key=" + encodeURIComponent(AMAP_KEY);
    sc.onload = mountAmap;
    sc.onerror = () => { box.textContent = "高德 JS API 加载失败，请检查 Key、域名白名单或网络。"; };
    document.head.appendChild(sc);
  }

  function mountAmap() {
    const host = $("globe-viz");
    if (!host || !window.AMap) return;
    host.innerHTML = "";
    const map = new window.AMap.Map(host, {
      zoom: 4, center: [105, 35], viewMode: "3D", pitch: 48, showBuildingBlock: true, terrain: true,
    });
    state.active.forEach((name) => {
      const c = state.data && state.data.cityIndex[name];
      if (!c) return;
      new window.AMap.Marker({ position: [c.lon, c.lat], title: name }).setMap(map);
    });
    const box = $("amap-info");
    if (box) box.textContent = "高德 3D 地形模式 · 已显示当前路线城市；返回全球视角请刷新页面。";
  }

  /* ---------------------------------------------------------- 对外 */
  window.DSHGlobe = {
    onShow() {
      setTimeout(() => {
        loadWorld();
        // 切回本页签时补一次尺寸：globe.gl 只按容器宽高渲染，0×0 会变成一片空白
        if (!g) return;
        const w = $("globe-viz").clientWidth, h = $("globe-viz").clientHeight;
        if (w > 0 && h > 0) g.width(w).height(h);
      }, 60);
    },
    __debug: {
      state, bind,
      loadWorld, selectLoop, focusRecord, planWorld, toggleCity, renderPicked,
      openCityGuide, openRecordGuide, closeGuide, renderRecords, renderLoops,
      buildScene, buildCards, layoutCards, updatePoints, updatePaths, fitCanvas,
      flyTo, flyHome, flyToCities, setAutoRotate, frontNames,
      setIcons,
      toggleFullscreen, isFull, fitCanvas,
      fitView, lonWindow, worldOf, isFront: (lat, lng) => isFront(lat, lng),
      screenOf,                       // 投影自检用：返回 null 说明卡片会全被藏起来
      /** 当前正面可见的卡片名（看不到卡片时先用它在控制台自检） */
      visibleCards: () => state.cardList
        .filter((it) => it.el.style.display !== "none")
        .map((it) => it.city.name),
      get globe() { return g; },
    },
  };

  window.addEventListener("DOMContentLoaded", () => setTimeout(() => {
    bind();
    if ($("tab-globe") && $("tab-globe").classList.contains("active")) loadWorld();
  }, 300));
})();
