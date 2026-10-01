/* 大交通换乘规划 - 前端逻辑（原生 JS，无构建步骤） */
"use strict";

const $ = (id) => document.getElementById(id);

/* ---------------- 标签页 ---------------- */
document.querySelectorAll("header nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("header nav button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll("header nav button").forEach((b) => b.setAttribute("aria-selected", b === btn ? "true" : "false"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "data") loadData();
    // 地图漫游页：首次进入才加载地理数据与城市目录
    if (btn.dataset.tab === "map" && window.DSHTravelMap) window.DSHTravelMap.onShow();
    // 地球模式页：首次进入才加载世界城市 / 环线 / 世界之最
    if (btn.dataset.tab === "globe" && window.DSHGlobe) window.DSHGlobe.onShow();
    if (btn.dataset.tab === "globe" && window.DSHGlobe) window.DSHGlobe.onShow();
  });
});

/* ---------------- 跨页签跳转（环线三套入口 / 攻略↔动线 都用它） ----------------
 * 页签内容是切过去之后才加载的（地图要建场景、地球要建球），所以先切页签，
 * 等一会儿再滚到目标面板 —— 立刻滚会滚到一个还没渲染出来的空位置。 */
function gotoTab(tab, targetId, wait = 700) {
  const btn = document.querySelector(`header nav button[data-tab="${tab}"]`);
  if (btn) btn.click();
  if (!targetId) return Promise.resolve();
  return new Promise((resolve) => {
    setTimeout(() => {
      const el = $(targetId);
      if (el) {
        if (el.classList.contains("collapsed")) {           // 面板是收着的就先展开
          const toggle = document.querySelector(`.panel-toggle[data-target="${targetId}"]`);
          if (toggle) toggle.click();
        }
        el.scrollIntoView({ behavior: "smooth", block: "start" });
        el.classList.remove("jump-flash");
        void el.offsetWidth;
        el.classList.add("jump-flash");
        setTimeout(() => el.classList.remove("jump-flash"), 1800);
      }
      resolve(!!el);
    }, wait);
  });
}

/** 环线三套入口：国内周边（地图）/ 世界长线（地球）/ 京冀穷游（本页 Ultra） */
function setupLoopHubs() {
  const box = $("loop-hubs");
  if (!box) return;
  document.documentElement.dataset.loopHubs = "1";
  box.querySelectorAll("button.lh[data-goto-tab]").forEach((btn) => {
    btn.addEventListener("click", () => gotoTab(btn.dataset.gotoTab, btn.dataset.gotoTarget,
      btn.dataset.gotoTab === "globe" ? 1400 : 900));
  });
  const ultra = $("lh-ultra");
  if (ultra) ultra.addEventListener("click", () => {
    toggleUltra(true);
    setTimeout(() => jumpToSection("ultra-panel"), 120);
  });
}

/* ---------------- 工具 ---------------- */
function esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

function setStatus(id, text, isError) {
  const el = $(id);
  el.textContent = text || "";
  el.className = "status" + (isError ? " error" : "");
}

async function postJSON(url, payload) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  let data = null;
  try {
    data = await resp.json();
  } catch (e) {
    if (!resp.ok) throw new Error(`请求失败（HTTP ${resp.status}）`);
    throw new Error("服务器返回了无效响应");
  }
  if (!resp.ok) {
    const detail = data && (data.error || data.detail);
    throw new Error(detail || `请求失败（HTTP ${resp.status}）`);
  }
  return data;
}

/* ---------------- 日期默认值 ---------------- */
function defaultDate(offsetDays) {
  const d = new Date();
  // 不传参数就取今天：以前写成 d.getDate() + offsetDays，undefined 会算出
  // 「NaN-NaN-NaN」，日期框会是空的（路线卡里的出行日就这么空过）。
  d.setDate(d.getDate() + (Number(offsetDays) || 0));
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
$("p-date").value = defaultDate(3);
$("l-date").value = defaultDate(3);
$("c-to").value = defaultDate(7);

/* 自动把日期切到"车次最多的数据日期"，并提示数据日期与查询日期的关系 */
(async function pickBestDate() {
  try {
    const r = await fetch("/api/status");
    const d = await r.json();
    if (d.dates && d.dates.length) {
      let best = d.dates[0];
      d.dates.forEach((x) => { if (x.trains > best.trains) best = x; });
      $("p-date").value = best.date;
      $("l-date").value = best.date;
      setStatus("p-status",
        `数据提示：已自动选择数据最全的日期 ${best.date}（${best.trains} 车次）。` +
        `预填的示例环线需用 ${best.date}。查询其它日期前，请先到「数据」页把该日复制/导入。`);
    }
  } catch (e) { /* 忽略：保持今天+3 默认 */ }
})();

/* ---------------- 自动补全 ---------------- */
function attachAutocomplete(input, onSelect) {
  // 只创建一次包裹层，避免每次输入请求都把 input 嵌套进新的 .ac-wrap。
  const wrap = document.createElement("div");
  wrap.className = "ac-wrap";
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);

  let list = null;
  let timer = null;
  let requestId = 0;
  let activeIndex = -1;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 1) { close(); return; }
    timer = setTimeout(() => fetchSuggest(input, q), 250);
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
  input.addEventListener("keydown", (e) => {
    if (!list) return;
    const items = [...list.querySelectorAll(".ac-item")];
    if (!items.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      activeIndex = (activeIndex + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items.forEach((item, i) => item.classList.toggle("sel", i === activeIndex));
    } else if (e.key === "Enter" && activeIndex >= 0) {
      e.preventDefault();
      items[activeIndex].dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    } else if (e.key === "Escape") {
      close();
    }
  });

  function close() {
    if (list) { list.remove(); list = null; }
    activeIndex = -1;
  }
  async function fetchSuggest(input, q) {
    const currentRequest = ++requestId;
    try {
      const r = await fetch("/api/stations?q=" + encodeURIComponent(q));
      const data = await r.json();
      if (currentRequest !== requestId || input.value.trim() !== q) return;
      close();
      const items = (data.suggestions || []).slice(0, 10);
      if (!items.length) return;
      list = document.createElement("div");
      list.className = "ac-list";
      items.forEach((s) => {
        const it = document.createElement("div");
        it.className = "ac-item";
        it.innerHTML = `<span>${esc(s.name)}</span><span class="city">${esc(s.city)}</span>`;
        it.addEventListener("mousedown", (e) => {
          e.preventDefault();
          input.value = s.name === s.city ? s.city : s.name;
          input.dispatchEvent(new Event("change", { bubbles: true }));
          if (onSelect) onSelect(input.value, s);
          close();
        });
        list.appendChild(it);
      });
      wrap.appendChild(list);
    } catch (e) { /* 忽略 */ }
  }
}
attachAutocomplete($("p-from"));
attachAutocomplete($("p-to"));

function syncPlanModeFields() {
  const tour = $("p-mode").value === "tour";
  $("p-tourstay").disabled = !tour;
  $("p-withflights").disabled = !tour;
  const stayLabel = $("p-tourstay").closest("label");
  const flightLabel = $("p-withflights").closest("label");
  if (stayLabel) stayLabel.classList.toggle("muted", !tour);
  if (flightLabel) flightLabel.classList.toggle("muted", !tour);
  ["p-transfer-field", "p-objective-field", "p-slack-field", "p-buffer-field"]
    .forEach((id) => $(id)?.classList.toggle("tour-hidden", tour));
  ["p-maxdays-field", "p-city-count-field", "p-rest-days-field"]
    .forEach((id) => $(id)?.classList.toggle("tour-hidden", !tour));
}
$("p-mode").addEventListener("change", syncPlanModeFields);
syncPlanModeFields();

/* ---------------- 单程规划 ---------------- */
$("plan-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const go = $("p-go");
  go.disabled = true;
  setStatus("p-status", "规划中…（首次查询某航线会实时获取航班数据，约几秒）");
  $("p-results").innerHTML = "";
  try {
    const mode = $("p-mode").value;
    const payload = {
      frm: $("p-from").value.trim(),
      to: $("p-to").value.trim(),
      date: $("p-date").value,
      time: $("p-time").value,
      mode,
      max_transfers: parseInt($("p-transfers").value, 10),
      buffer_min: parseInt($("p-buffer").value, 10),
      objective: $("p-objective").value,
      slack_hours: parseInt($("p-slack").value, 10),
      tour_stay: $("p-tourstay").value,
      with_flights: mode === "tour" && $("p-withflights").checked,
      max_days: mode === "tour" ? (parseInt($("p-maxdays").value, 10) || 10) : null,
      tour_city_count: mode === "tour" ? (parseInt($("p-city-count").value, 10) || null) : null,
      rest_days: mode === "tour" ? (parseInt($("p-rest-days").value, 10) || 0) : 0,
      top: 10,
      links: $("p-links").checked,
      guide: $("p-guide").checked,
    };
    const res = await postJSON("/api/plan", payload);
    if (!res.ok) {
      setStatus("p-status", res.error || "规划失败", true);
      return;
    }
    setStatus("p-status", (res.notes || []).join("；"));
    if (res.tour) {
      renderTour(res);
    } else {
      renderPlan(res);
    }
    // 规划完顺手推荐相关路线（省钱/舒适经验），失败不影响主流程
    suggestRoutes([payload.frm, payload.to]);
  } catch (err) {
    setStatus("p-status", "请求失败: " + err.message, true);
  } finally {
    go.disabled = false;
  }
});

/** 规划页底部的「参考路线」：调 /api/routes/suggest，命中起点或终点城市 */
async function suggestRoutes(cities) {
  const box = $("p-routes");
  if (!box) return;
  const list = [...new Set((cities || []).map((c) => (c || "").trim()).filter(Boolean))];
  if (!list.length) { box.hidden = true; return; }
  try {
    const data = await fetch("/api/routes/suggest?cities=" + encodeURIComponent(list.join(",")))
      .then((r) => r.json());
    if (!data.ok || !data.routes.length) { box.hidden = true; return; }
    box.hidden = false;
    box.innerHTML =
      `<h3>相关路线参考（${esc(list.join(" / "))}）</h3>` +
      `<p class="hint">这些是网友走过的走法，价格是区间不是实时票价；上面算出来的方案才是可执行的。</p>`;
    const grid = document.createElement("div");
    grid.className = "rt-list";
    data.routes.forEach((rt) => grid.appendChild(routeCard(rt)));
    box.appendChild(grid);
  } catch (e) {
    box.hidden = true;
  }
}

function legIcon(kind) {
  return kind === "flight" ? "✈" : kind === "transfer" ? "🚇" : "🚄";
}

function legBlock(d, next) {
  const div = document.createElement("div");
  div.className = "leg " + d.kind;
  let html = `<div class="icon">${legIcon(d.kind)}</div>`;
  if (d.kind === "transfer") {
    html += `<div class="route"><div class="code">${esc(d.guide ? "公交中转" : "同城接驳")}</div>`;
    html += `<div>${esc(d.from)} → ${esc(d.to)}</div>`;
    if (d.guide) html += `<div class="guide">🚇 ${esc(d.guide)}</div>`;
    html += `</div>`;
  } else {
    html += `<div class="route"><div class="code">${esc(d.code)}</div>`;
    html += `<div>${esc(d.from)} → ${esc(d.to)}</div>`;
    if (d.wait) {
      const nxtName = next ? next.from : "";
      html += `<div class="sub">└─ ${esc(nxtName)} 换乘等待 ${esc(d.wait)}</div>`;
    }
    if (d.transfer_guide) html += `<div class="guide">🚇 中转: ${esc(d.transfer_guide)}</div>`;
    if (d.link) {
      html += `<a class="link" href="${esc(d.link)}" target="_blank" rel="noopener">🎫 去 12306 购票</a>`;
    }
    html += `</div>`;
  }
  html += `<div class="times">${esc(d.dep_text)}<br>${esc(d.arr_text)}</div>`;
  div.innerHTML = html;
  return div;
}

function statsText(j) {
  let s = `${j.rides} 趟 · 站内换乘 ${j.station_transfers} · 转场 ${j.city_transfers}`;
  if (j.price_text) s += ` · ${j.price_text}`;
  return s;
}

function renderPlan(res) {
  const box = $("p-results");
  const journeys = Array.isArray(res.journeys) ? res.journeys : [];
  if (!journeys.length) {
    box.innerHTML = `<div class="warn">${esc(res.diagnosis || "未找到可行方案：该日数据未覆盖此 OD，或换乘次数上限过低")}</div>`;
    renderCityGuides(res);
    return;
  }
  journeys.forEach((j, i) => {
    const card = document.createElement("div");
    card.className = "journey";
    const head = document.createElement("div");
    head.className = "j-head";
    head.innerHTML =
      `<span class="j-title">方案 ${i + 1}</span>` +
      `<span class="j-stats">${esc(statsText(j))}</span>` +
      `<span class="j-time">${esc(j.dep_cal || j.dep_text)} → ${esc(j.arr_cal || j.arr_text)}</span>` +
      `<span class="j-dur">全程 ${esc(j.duration_text)}</span>`;
    card.appendChild(head);
    j.legs.forEach((d, k) => card.appendChild(legBlock(d, j.legs[k + 1])));
    box.appendChild(card);
  });
  renderCityGuides(res);
}

/** 沿途城市资料：查询结果里出现过的城市（起点/终点/换乘点）都把攻略挂出来
 *
 *  这是「攻略与查询联动」的落点 —— 不再是「查询恰好命中攻略才弹」，
 *  而是任何一次查询都能一眼看到：这座城市建议留多久、半日/一日怎么走、
 *  换乘那点时间够不够出站，以及直达「城内动线」的入口。 */
function renderCityGuides(res) {
  const host = $("p-results");
  const data = res.guides || {};
  const cards = data.cities || [];
  if (!host || !cards.length) return;
  const section = document.createElement("section");
  section.className = "city-guides";
  section.innerHTML =
    `<h3>沿途城市资料（${cards.length} 座）</h3>
     <p class="hint">${esc(data.note || "")}</p>`;
  const list = document.createElement("div");
  list.className = "cg-list";
  cards.forEach((city) => list.appendChild(cityGuideCard(city)));
  section.appendChild(list);
  host.appendChild(section);
  bindGuideButtons(section);
}

/** 一座城市一张卡：看点、建议停留、换乘提示，加两个入口（城内动线 / 展开攻略） */
function cityGuideCard(city) {
  const el = document.createElement("div");
  el.className = "cg-card";
  const role = city.role ? `<span class="cg-role">${esc(city.role)}</span>` : "";
  const chips = (city.highlights || []).slice(0, 5)
    .map((h) => `<i class="cg-chip">${esc(h.name || h)}</i>`).join("");
  const layover = city.layover_hint
    ? `<div class="cg-layover">⏱ ${esc(city.layover_hint)}</div>` : "";
  el.innerHTML =
    `<div class="cg-head">${role}<b>${esc(city.name)}</b>
       <span class="cg-meta">${esc(city.province || city.region || "")}
         · 推荐 ${city.score} 分${city.action_hint ? " · " + esc(city.action_hint) : ""}</span>
     </div>
     <div class="cg-chips">${chips || `<i class="cg-chip">（还没有看点）</i>`}</div>
     ${layover}
     <div class="cg-body" hidden>
       ${city.halfday ? `<div class="cg-line"><b>半日</b>${esc(city.halfday)}</div>` : ""}
       ${city.oneday ? `<div class="cg-line"><b>一日</b>${esc(city.oneday)}</div>` : ""}
       ${city.food ? `<div class="cg-line"><b>吃</b>${esc(city.food)}</div>` : ""}
       ${city.note ? `<div class="cg-line cg-warn"><b>注意</b>${esc(city.note)}</div>` : ""}
     </div>
     <div class="trip-actions">
       <button type="button" class="ghost-sm gd-open" data-city="${esc(city.name)}">看城内动线</button>
       <button type="button" class="ghost-sm cg-toggle">展开攻略</button>
       <span class="hint">攻略来自城市资料库；换乘与门票以现场为准</span>
     </div>`;
  const body = el.querySelector(".cg-body");
  const btn = el.querySelector(".cg-toggle");
  btn.addEventListener("click", () => {
    body.hidden = !body.hidden;
    btn.textContent = body.hidden ? "展开攻略" : "收起攻略";
    el.classList.toggle("open", !body.hidden);
  });
  return el;
}

/* ---------------- 大环线（状态驱动渲染：序号由位置决定，增删即整体重排） ---------------- */
let loopStops = [];       // [{name, stay: {mode, nights}}]
let loopClosed = true;    // 最后一行是"返回起点"（跟随第 1 行）

const STAY_OPTIONS = [
  { value: "transit", label: "纯中转（到站就走）" },
  { value: "halfday", label: "玩半天（约5小时后走）" },
  { value: "nights:1", label: "住 1 晚" },
  { value: "nights:2", label: "住 2 晚" },
  { value: "nights:3", label: "住 3 晚" },
];

function stayValue(s) {
  return s.mode === "nights" ? `nights:${s.nights}` : s.mode;
}

function renderStops() {
  const list = $("stop-list");
  list.innerHTML = "";
  loopStops.forEach((s, i) => {
    const isFirst = i === 0;
    const isLast = i === loopStops.length - 1 && loopClosed;
    const row = document.createElement("div");
    row.className = "stop-row";
    let stayHtml;
    if (isLast) {
      stayHtml = `<span class="stay-na">（返回起点）</span>`;
    } else {
      const opts = STAY_OPTIONS.map((o) =>
        `<option value="${o.value}" ${stayValue(s.stay) === o.value ? "selected" : ""}>${o.label}</option>`
      ).join("");
      stayHtml = `<label>停留 <select class="stop-stay" data-idx="${i}">${opts}</select></label>`;
    }
    row.innerHTML = `
      <span class="no">${i + 1}</span>
      <input type="text" class="stop-name" data-idx="${i}" autocomplete="off"
             placeholder="${isFirst ? "起点，如 昆明" : isLast ? "返回起点（自动跟随第 1 行）" : "途经点"}"
             value="${esc(s.name)}">
      ${stayHtml}
      <button type="button" class="x" data-idx="${i}" title="${isLast ? "返回起点行不可删除" : "删除此行"}" ${isLast ? "disabled" : ""}>✕</button>`;
    list.appendChild(row);
  });
  bindStopEvents();
}

function bindStopEvents() {
  const list = $("stop-list");
  list.querySelectorAll(".stop-name").forEach((el) => {
    el.addEventListener("input", () => {
      const i = Number(el.dataset.idx);
      loopStops[i].name = el.value;
      if (i === 0 && loopClosed) {
        // 回显到最后一行的"返回起点"
        const lastEl = list.querySelector(`.stop-name[data-idx="${loopStops.length - 1}"]`);
        if (lastEl) {
          loopStops[loopStops.length - 1].name = el.value;
          lastEl.value = el.value;
        }
      }
    });
    attachAutocomplete(el, (value) => {
      const i = Number(el.dataset.idx);
      loopStops[i].name = value;
      if (i === 0 && loopClosed) {
        loopStops[loopStops.length - 1].name = value;
        const lastEl = list.querySelector(`.stop-name[data-idx="${loopStops.length - 1}"]`);
        if (lastEl) lastEl.value = value;
      }
    });
  });
  list.querySelectorAll(".stop-stay").forEach((el) => {
    el.addEventListener("change", () => {
      const i = Number(el.dataset.idx);
      const v = el.value;
      loopStops[i].stay = v.startsWith("nights:")
        ? { mode: "nights", nights: parseInt(v.split(":")[1], 10) || 1 }
        : { mode: v, nights: 0 };
    });
  });
  list.querySelectorAll(".x").forEach((el) => {
    el.addEventListener("click", () => {
      const i = Number(el.dataset.idx);
      // 闭环的最后一行是自动返回起点，不能单独删除，否则会丢失闭环语义。
      if (loopClosed && i === loopStops.length - 1) return;
      if (loopStops.length <= 2) return;   // 至少保留起点与终点
      loopStops.splice(i, 1);
      if (loopClosed && loopStops.length) {
        loopStops[loopStops.length - 1].name = loopStops[0].name;
      }
      renderStops();
    });
  });
}

function initLoopForm() {
  loopStops = [
    { name: "昆明", stay: { mode: "nights", nights: 1 } },
    { name: "大理", stay: { mode: "nights", nights: 1 } },
    { name: "丽江", stay: { mode: "nights", nights: 1 } },
  ];
  loopClosed = $("l-closed").checked;
  if (loopClosed) {
    loopStops.push({ name: loopStops[0].name, stay: { mode: "transit", nights: 0 } });
  }
  renderStops();
}

$("add-stop").addEventListener("click", () => {
  if (loopClosed) {
    // 在"返回起点"行之前插入新中间点
    loopStops.splice(loopStops.length - 1, 0, { name: "", stay: { mode: "nights", nights: 1 } });
  } else {
    loopStops.push({ name: "", stay: { mode: "nights", nights: 1 } });
  }
  renderStops();
});

$("l-closed").addEventListener("change", () => {
  loopClosed = $("l-closed").checked;
  if (loopClosed) {
    loopStops.push({ name: loopStops[0] ? loopStops[0].name : "", stay: { mode: "transit", nights: 0 } });
  } else if (loopStops.length > 1) {
    // 取消闭合：最后一行变为普通途经点（保留其内容与停留设置）
    loopStops.splice(loopStops.length - 1, 1);
    if (!loopStops.length) loopStops.push({ name: "", stay: { mode: "nights", nights: 1 } });
  }
  renderStops();
});

function syncLoopTourFields() {
  const tour = $("l-tour").checked;
  ["l-maxdays", "l-city-count", "l-rest-days"].forEach((id) => {
    const el = $(id);
    if (el) el.closest("label")?.classList.toggle("tour-hidden", !tour);
  });
}
$("l-tour").addEventListener("change", syncLoopTourFields);
syncLoopTourFields();

// 地图选点完成后才展开规划表；Esc 可随时退回纯地图视图。
$("map-finish")?.addEventListener("click", () => {
  const stage = $("map-stage"), form = $("map-form");
  stage?.classList.add("selection-done"); form?.classList.remove("map-form-hidden");
  form?.scrollIntoView({ behavior: "smooth", block: "center" });
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const stage = $("map-stage"), form = $("map-form");
  if (stage?.classList.contains("selection-done")) { stage.classList.remove("selection-done"); form?.classList.add("map-form-hidden"); stage.scrollIntoView({ behavior: "smooth", block: "center" }); }
});

$("loop-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const go = $("l-go");
  go.disabled = true;
  setStatus("l-status", "规划中…（每段都会实时检查航班，首次较慢）");
  $("l-results").innerHTML = "";
  try {
    const filled = loopStops.filter((s) => s.name.trim() !== "");
    let stops = filled.map((s) => s.name.trim());
    let stayObjs = filled.map((s) => Object.assign({}, s.stay));
    if (stops.length < 2) {
      setStatus("l-status", "至少填写 2 个途经点", true);
      return;
    }
    const tour = $("l-tour").checked;
    if (tour) {
      // 游览模式：中间点（非起点、非"返回起点"行）一律玩半天
      for (let k = 1; k < stayObjs.length - (loopClosed ? 1 : 0); k++) {
        stayObjs[k] = { mode: "halfday", nights: 0 };
      }
    }
    const stays = stayObjs.slice(0, stops.length - 1);
    const maxDays = tour ? (parseInt($("l-maxdays").value, 10) || 10) : null;
    const payload = {
      date: $("l-date").value,
      stops,
      stays,
      free_order: $("l-free").checked,
      time: $("l-time").value,
      mode: $("l-mode").value,
      max_transfers: parseInt($("l-transfers").value, 10),
      buffer_min: parseInt($("l-buffer").value, 10),
      objective: $("l-objective").value,
      slack_hours: parseInt($("l-slack").value, 10),
      max_days: maxDays,
      tour_city_count: tour ? (parseInt($("l-city-count").value, 10) || null) : null,
      rest_days: tour ? (parseInt($("l-rest-days").value, 10) || 0) : 0,
      links: $("l-links").checked,
      guide: $("l-guide").checked,
    };
    const res = await postJSON("/api/loop", payload);
    if (!res.ok) {
      setStatus("l-status", res.error || "规划失败", true);
      return;
    }
    setStatus("l-status", "");
    renderLoop(res);
  } catch (err) {
    setStatus("l-status", "请求失败: " + err.message, true);
  } finally {
    go.disabled = false;
  }
});

function renderLoop(res) {
  const box = $("l-results");
  if (res.requested_city_count || res.rest_days) {
    const pref = document.createElement("div");
    pref.className = "summary preference-summary";
    pref.textContent = `游览城市 ${res.city_count || "未定"} 个` +
      (res.requested_city_count ? `（目标 ${res.requested_city_count} 个）` : "") +
      ` · 每个到达城市休整 ${res.rest_days || 0} 天`;
    box.appendChild(pref);
  }
  if (res.free_summary) {
    const fs = res.free_summary;
    const s = document.createElement("div");
    s.className = "summary";
    s.innerHTML = `顺序寻优：<b>${esc(fs.order.join(" → "))}</b>` +
      (fs.saved_delta_min != null && fs.saved_delta_min < 0
        ? `（相比输入顺序节省 ${esc(Math.round(-fs.saved_delta_min / 60 * 10) / 10)} 小时）` : "") +
      `<br><span style="font-size:12px">${esc(fs.note)}</span>`;
    box.appendChild(s);
  }
  if (res.budget) {
    const b = res.budget;
    const d = document.createElement("div");
    d.className = "warn";
    let html = `全程需 ${esc(String(res.total_days))} 天，超出预算（≤${b.max_days} 天）${b.over_by_days} 天。`;
    if (b.suggestions && b.suggestions.length) {
      html += " 删站建议：";
      html += b.suggestions.map((s) => `删「${esc(s.remove)}」→ ${s.days} 天`).join("；");
    }
    d.innerHTML = html;
    box.appendChild(d);
  }
  (res.warnings || []).forEach((w) => {
    const d = document.createElement("div");
    d.className = "warn";
    d.textContent = "警告：" + w;
    box.appendChild(d);
  });
  res.legs.forEach((rec) => box.appendChild(dayCard(rec)));
  if (res.total_days) {
    const t = document.createElement("div");
    t.className = "summary";
    t.textContent = `共 ${res.total_days} 天`;
    box.appendChild(t);
  }
}

function dayCard(rec) {
  const day = document.createElement("div");
  day.className = "card day-card";
  const head = document.createElement("div");
  head.className = "day-head";
  head.innerHTML = `D${rec.day_no} · ${esc(rec.date)} · ${esc(rec.frm)} → ${esc(rec.to)}` +
    (rec.stay != null ? `<span class="stay">停留: ${esc(rec.stay)}</span>` : "");
  day.appendChild(head);
  if (rec.city_tourism) {
    const info = rec.city_tourism;
    const tourism = document.createElement("div");
    tourism.className = "city-tourism";
    tourism.innerHTML = `<div class="tourism-meta"><b>城市推荐 ${esc(Number(info.recommendation_score || 0).toFixed(1))}</b>` +
      (info.tags ? ` · ${esc(info.tags)}` : "") + `</div>` +
      (info.intro ? `<div>${esc(info.intro)}</div>` : "") +
      (info.halfday_plan ? `<div class="tourism-plan">半日建议：${esc(info.halfday_plan)}</div>` : "");
    day.appendChild(tourism);
  }
  if (rec.journey) {
    const j = rec.journey;
    const sub = document.createElement("div");
    sub.className = "j-head";
    sub.innerHTML =
      `<span class="j-stats">${esc(statsText(j))}</span>` +
      `<span class="j-time">${esc(j.dep_cal || j.dep_text)} → ${esc(j.arr_cal || j.arr_text)}</span>` +
      `<span class="j-dur">全程 ${esc(j.duration_text)}</span>`;
    day.appendChild(sub);
    j.legs.forEach((d, k) => day.appendChild(legBlock(d, j.legs[k + 1])));
  } else {
    const err = document.createElement("div");
    err.className = "warn";
    err.textContent = "!! " + (rec.error || "未找到方案");
    day.appendChild(err);
  }
  return day;
}

function renderTour(res) {
  const box = $("p-results");
  const t = res.tour;
  const s = document.createElement("div");
  s.className = "summary";
  s.innerHTML = `串城链：<b>${esc(t.chain.join(" → "))}</b>` +
    `<br><span style="font-size:12px">绿皮直达基线 ¥${esc(Math.round(t.baseline))} · 3倍闸门 ¥${esc(Math.round(t.gate))}` +
    (t.total_price != null ? ` · 本次全程约¥${esc(Math.round(t.total_price))}` : "") +
    (t.with_flights ? " · 本次含飞机" : (t.flight_requested ? " · 允许飞机但本次未采用" : " · 纯绿皮")) +
    `<br><span style="font-size:12px">城市 ${esc(t.city_count || t.chain.length)} 个` +
    (t.rest_days ? ` · 每个城市休整 ${esc(t.rest_days)} 天` : "") + `</span>`;
  box.appendChild(s);
  (t.warnings || []).forEach((w) => {
    const d = document.createElement("div");
    d.className = "warn";
    d.textContent = "⚠ " + w;
    box.appendChild(d);
  });
  if (t.budget) {
    const d = document.createElement("div");
    d.className = "warn";
    d.textContent = `全程 ${t.total_days} 天，超过最多 ${t.budget.max_days} 天的预算 ${t.budget.over_by_days} 天。`;
    box.appendChild(d);
  }
  t.legs.forEach((rec) => box.appendChild(dayCard(rec)));
  if (t.total_days) {
    const d = document.createElement("div");
    d.className = "summary";
    d.textContent = `共 ${t.total_days} 天`;
    box.appendChild(d);
  }
}

/* ---------------- 数据台：自动检查 + 一键执行 ---------------- */
initLoopForm();
let maintPlan = null;          // 上一次生成的维护计划，供「执行」按钮用
let crawlTimer = null;         // 爬取任务轮询
let crawlPollSeq = 0;
let crawlPollDate = null;
let crawlAbort = null;

const SOURCE_BADGE = {
  import: ["导入", "good"], crawl: ["真爬", "info"], focus: ["定向", "info"],
  copy: ["拷贝", "muted"], sparse: ["车次少", "warn"], missing: ["无数据", "bad"],
};

async function loadData() {
  try {
    const [ov, st] = await Promise.all([
      fetch("/api/maintain/overview").then((r) => r.json()),
      fetch("/api/status").then((r) => r.json()).catch(() => ({})),
    ]);
    const h = ov.health || {};
    $("data-summary").innerHTML =
      `车站 ${ov.stations} 个 · 机场 ${ov.airports} 个（静态主数据 ${st.airport_master || 0}）· ` +
      `旅游资料 ${ov.tourism_cities || 0} 个城市 · ` +
      `时刻表 <b>${h.dates || 0}</b> 天（导入/真爬 <b>${h.truthful || 0}</b> · 拷贝 ${h.copies || 0}）· ` +
      `航班缓存 ${st.flight_routes_cached || 0} 条 · 高德 key：${st.amap_key_set ? "已配置 ✓" : "未配置（CLI: set-amap-key）"}`;
    $("flight-data-hint").textContent =
      `基准日：${ov.base ? ov.base.date + "（" + ov.base.label + "，" + ov.base.trains + " 趟）" : "无"}` +
      ` · 今天 ${ov.today} · 12306 预售窗口约 ${ov.crawl_window_days} 天 · 少于 ${ov.sparse_below} 趟视为异常`;
    renderHealth(ov.health);
    renderDates(ov.dates);
    renderCredibility(ov, st);
    const sel = $("c-from");
    sel.innerHTML = "";
    ov.dates.forEach((x) => {
      const o = document.createElement("option");
      o.value = x.date;
      o.textContent = `${x.date}（${x.trains} 车次 · ${x.label}）`;
      sel.appendChild(o);
    });
    if (!$("m-start").value && ov.base) $("m-start").value = ov.base.date;
    // 真爬那张卡的日期默认跟基准日走，并顺手看一眼「这天有没有没爬完的进度」
    if ($("crawl-date") && !$("crawl-date").value && ov.base) $("crawl-date").value = ov.base.date;
    if ($("crawl-date")) loadCrawlProgress($("crawl-date").value);
    renderJobs(ov.jobs || []);
  } catch (e) {
    setStatus("c-status", "加载数据失败: " + e.message, true);
  }
  loadRoutes();
  loadTrips();
  loadPois();
  pollTripBuildIfRunning();
  pollPoiHarvestIfRunning();
}

function renderHealth(h) {
  const box = $("health-list");
  if (!box) return;
  box.innerHTML = "";
  (h.items || []).forEach((it) => {
    const div = document.createElement("div");
    div.className = "health-item tone-" + (it.tone || "ok");
    div.innerHTML =
      `<span class="hi-dot"></span><span class="hi-text">${esc(it.text)}</span>` +
      `<span class="hi-hint">${esc(it.hint || "")}</span>`;
    const fix = fixButton(it);
    if (fix) div.appendChild(fix);
    box.appendChild(div);
  });
}

/** 数据可信度：把散在几张卡里的覆盖率收成一张表，每行给一个「去看」。
 *
 * 之前要判断「这份数据能不能信」得来回看三处（体检清单 / 城内点位命中率 / 各卡片第一行），
 * 现在一行一类，缺口行标黄。
 */
async function renderCredibility(ov, status) {
  const box = $("cred-grid");
  if (!box) return;
  const h = (ov && ov.health) || {};
  const st = status || {};
  const rows = [];
  rows.push({
    name: "时刻表",
    val: `${h.dates || 0} 天 · 真实来源 <b>${h.truthful || 0}</b> 天 · 拷贝 ${h.copies || 0} 天`,
    note: (h.copies ? "拷贝日只反映基准日车次，重要出行日建议真爬" : "没有拷贝日"),
    gap: !!h.copies,
    go: ["去看日期", () => jumpToSection("data-dates")],
  });
  // 下面三项是异步来的，先占位再补
  rows.push({ name: "城内点位", val: "读取中…", note: "", gap: false, id: "cred-poi" });
  rows.push({ name: "城市攻略", val: "读取中…", note: "", gap: false, id: "cred-guide" });
  rows.push({ name: "路线库", val: "读取中…", note: "", gap: false, id: "cred-routes" });
  rows.push({
    name: "世界数据",
    val: "读取中…", note: "", gap: false, id: "cred-world",
  });
  box.innerHTML = rows.map((r) => `
    <div class="cred-row${r.gap ? " is-gap" : ""}"${r.id ? ` id="${r.id}"` : ""}>
      <span class="cr-name">${esc(r.name)}</span>
      <span class="cr-val">${r.val}</span>
      ${r.note ? `<span class="cr-note">${esc(r.note)}</span>` : ""}
      ${r.go ? `<button type="button" class="go" data-cred="${esc(r.name)}">${esc(r.go[0])}</button>` : ""}
    </div>`).join("");
  rows.filter((r) => r.go).forEach((r) => {
    const btn = box.querySelector(`button.go[data-cred="${r.name}"]`);
    if (btn) btn.addEventListener("click", r.go[1]);
  });

  // 三项异步覆盖：城内点位（命中率）、路线库（条数与口径）、世界数据（城/环线/之最）
  const fill = async (id, val, note, gap, go) => {
    const row = $(id);
    if (!row) return;
    row.querySelector(".cr-val").innerHTML = val;
    if (note) {
      const n = document.createElement("span");
      n.className = "cr-note";
      n.textContent = note;
      row.appendChild(n);
    }
    row.classList.toggle("is-gap", !!gap);
    if (go) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "go";
      btn.textContent = go[0];
      btn.addEventListener("click", go[1]);
      row.appendChild(btn);
    }
  };
  try {
    const poi = await fetch("/api/poi?limit=1").then((r) => r.json());
    const s = poi.stats || {};
    const pct = Number(s.rate || 0);
    await fill("cred-poi",
      `命中 <b>${s.hit || 0}</b> / ${s.targets || s.named || 0} 个看点（<b>${pct.toFixed ? pct.toFixed(1) : pct}%</b>）`,
      s.miss ? `未命中 ${s.miss} 个（不猜坐标）` : "全部命中",
      pct > 0 && pct < 99,
      ["去看点位", () => jumpToSection("data-poi")]);
  } catch (e) {
    await fill("cred-poi", "读取失败", e.message, true);
  }
  try {
    // 攻略覆盖率要问目录（239 城），不能拿数据库里 city_tourism 的行数充数
    const c = await fetch("/api/cities?limit=1").then((r) => r.json());
    const total = c.catalog_total || 0;
    const withGuide = c.with_guide || 0;
    await fill("cred-guide",
      `<b>${withGuide}</b> / ${total} 城有完整攻略`,
      "动线按攻略散文对齐坐标，没攻略的城查不出动线",
      total > 0 && withGuide < total,
      ["去查动线", () => jumpToSection("data-guide")]);
  } catch (e) {
    await fill("cred-guide", "读取失败", e.message, true);
  }
  // 路线库与世界数据并行取：串行 await 会让最后两行多等两三秒，看着一直停在「读取中…」
  await Promise.all([
    fetch("/api/routes?limit=1").then((r) => r.json()).then((rt) => {
      const s = rt.stats || {};
      return fill("cred-routes",
        `库内 <b>${s.routes || 0}</b> 条（单向 ${s.oneway || 0} / 环线 ${s.loops || 0}）· 口径 <b>${esc(rt.updated || "?")}</b>`,
        "价格为公开攻略区间，不是实时票价", false,
        ["去路线库", () => jumpToSection("data-routes")]);
    }).catch((e) => fill("cred-routes", "读取失败", e.message, true)),
    fetch("/api/world/map").then((r) => r.json()).then((w) => {
      const s = w.stats || {};
      return fill("cred-world",
        `<b>${s.cities || 0}</b> 城 · <b>${s.loops || 0}</b> 条环线 · <b>${s.records || 0}</b> 条世界之最`,
        "世界机票必须实时查，这里只给距离/时间/天数", false,
        ["去地球模式", () => gotoTab("globe", "globe-loops-panel", 1400)]);
    }).catch((e) => fill("cred-world", "读取失败", e.message, true)),
  ]);
}

/** 体检项 → 一键修按钮（动作都在后端 dry-run 预览 + 二次确认） */
function fixButton(item) {
  if (!item.fix) return null;
  const map = {
    delete_sparse: ["清理异常日期", async () => {
      const dates = (item.text.match(/\d{4}-\d{2}-\d{2}/g) || []);
      if (!dates.length) return;
      if (!window.confirm(`删除这几个日期的时刻表？\n${dates.join("  ")}\n\n之后查询会自动从基准日补一份。`)) return;
      for (const d of dates) {
        const res = await postJSON("/api/maintain/run", { action: "delete_date", date: d, dry_run: false });
        setStatus("c-status", res.ok ? `已删除 ${d}（${res.deleted} 行）` : res.error, !res.ok);
      }
      loadData();
    }],
    refetch_flights: ["清掉缺价缓存", async () => {
      const pre = await postJSON("/api/maintain/run", { action: "clear_flight_cache", dry_run: true });
      if (!pre.count) { setStatus("c-status", "没有需要清理的航线缓存"); return; }
      if (!window.confirm(`清掉这 ${pre.count} 条航线的缓存，下次查询重新取价？\n${pre.routes.join("、")}`)) return;
      const res = await postJSON("/api/maintain/run", { action: "clear_flight_cache", dry_run: false });
      setStatus("c-status", `已清理 ${res.count} 条航线缓存，下次查询会重新取价`);
      loadData();
    }],
    copies: ["检查缺口", () => planFill()],
    prewarm_coords: null,          // 需要跑脚本，页面只提示
  };
  const spec = map[item.fix];
  if (!spec) return null;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "ghost-sm hi-fix";
  btn.textContent = spec[0];
  btn.addEventListener("click", spec[1]);
  return btn;
}

function renderDates(dates) {
  const tbody = $("data-table").querySelector("tbody");
  tbody.innerHTML = "";
  dates.forEach((x) => {
    const [label, tone] = SOURCE_BADGE[x.source] || [x.label || x.source, "muted"];
    const tr = document.createElement("tr");
    tr.className = "src-" + (x.source || "unknown");
    tr.innerHTML =
      `<td>${esc(x.date)}</td><td>${x.trains}</td>` +
      `<td><span class="src-badge ${tone}" title="${esc(x.copied_from ? "来自 " + x.copied_from : x.detail)}">${esc(label)}</span></td>` +
      `<td class="row-actions"></td>`;
    const cell = tr.querySelector(".row-actions");
    cell.appendChild(rowBtn("真爬", `真爬 ${x.date}（耗时取决于限速与车次数）`, () => startCrawl(x.date)));
    cell.appendChild(rowBtn("删", `删掉 ${x.date} 的时刻表`, async () => {
      const pre = await postJSON("/api/maintain/run", { action: "delete_date", date: x.date, dry_run: true });
      if (!window.confirm(`删掉 ${x.date} 的 ${pre.rows} 行时刻表？之后查询会自动从基准日补一份。`)) return;
      const res = await postJSON("/api/maintain/run", { action: "delete_date", date: x.date, dry_run: false });
      setStatus("c-status", res.ok ? `已删除 ${x.date}` : res.error, !res.ok);
      loadData();
    }));
    tbody.appendChild(tr);
  });
}

function rowBtn(text, title, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "ghost-sm";
  b.textContent = text;
  b.title = title;
  b.addEventListener("click", onClick);
  return b;
}

/** 检查缺口 → 生成计划（只读）→ 页面上确认才执行 */
async function planFill() {
  const start = $("m-start").value, end = $("m-end").value;
  if (!start || !end) { setStatus("c-status", "先选起止日期", true); return; }
  try {
    const res = await postJSON("/api/maintain/plan", { start, end });
    if (!res.ok) { setStatus("c-status", res.error, true); return; }
    maintPlan = res;
    renderMaintainPlan(res);
  } catch (e) {
    setStatus("c-status", "生成计划失败: " + e.message, true);
  }
}

/** 数据台的「缺口维护计划」渲染。
 *  注意别叫 renderPlan：规划页签已经有一个同名函数了，
 *  两个同名函数声明会被后一个覆盖（hoisting），于是单程规划一按就报
 *  `Cannot read properties of undefined (reading 'length')` —— 真出过这个事故。 */
function renderMaintainPlan(plan) {
  const box = $("m-plan");
  box.hidden = false;
  box.innerHTML = "";
  if (!plan.steps.length) {
    box.innerHTML = `<div class="plan-head">这个区间没有缺口，都是可用数据。</div>`;
    return;
  }
  const head = document.createElement("div");
  head.className = "plan-head";
  head.innerHTML = `计划：<b>${plan.summary.copy}</b> 天拷贝 · <b>${plan.summary.crawl}</b> 天真爬 · ` +
    `<b>${plan.summary.keep}</b> 天不用动 · 基准日 ${plan.base ? esc(plan.base.date) : "无"}`;
  box.appendChild(head);
  plan.steps.forEach((s) => {
    const row = document.createElement("div");
    row.className = "plan-row action-" + s.action;
    const label = { copy: "拷贝", overwrite: "覆盖", crawl: "真爬", keep: "不动", skip: "跳过",
                    invalid: "日期错", blocked: "缺基准日" }[s.action] || s.action;
    row.innerHTML = `<code>${esc(s.date)}</code><span class="pa">${esc(label)}</span>` +
      `<span class="pr">${esc(s.reason || "")}</span>`;
    if (s.action === "crawl") row.appendChild(rowBtn("开始真爬", "", () => startCrawl(s.date)));
    box.appendChild(row);
  });
  const buttons = document.createElement("div");
  buttons.className = "plan-actions";
  const todo = plan.steps.filter((s) => s.action === "copy" || s.action === "overwrite");
  if (todo.length) {
    const run = document.createElement("button");
    run.type = "button";
    run.className = "primary";
    run.textContent = `执行计划（${todo.length} 天）`;
    run.addEventListener("click", () => applyPlan(todo));
    buttons.appendChild(run);
  }
  box.appendChild(buttons);
  box.insertAdjacentHTML("beforeend",
    `<p class="hint">${esc(plan.note || "")}</p>`);
}

async function applyPlan(steps) {
  const pre = await postJSON("/api/maintain/run", { action: "apply", steps, dry_run: true });
  const will = (pre.done || []).map((x) => x.date).join("、") || "（无）";
  if (!window.confirm(`要写库的日期：${will}\n\n拷贝只复用车次与停站时刻，隔日开行与临时调图体现不出来。确认执行？`)) return;
  const res = await postJSON("/api/maintain/run", { action: "apply", steps, dry_run: false });
  const done = (res.done || []).filter((x) => x.status === "copied").length;
  setStatus("c-status", `已补齐 ${done} 天（来源已标记为拷贝）`);
  loadData();
}

/** 真爬：必须先确认；带目标城市时走定向爬取（只爬相关 OD） */
async function startCrawl(date) {
  if (!date) date = $("crawl-date").value;
  if (!date) { setStatus("c-status", "先选日期", true); return; }
  const cities = ($("crawl-cities").value || "").split(/[\s,，、]+/).filter(Boolean);
  const pairs = Number($("crawl-pairs").value) || 320;
  const scale = cities.length
    ? `定向爬取：只爬 ${cities.join("、")} 相关的 OD（最多 ${pairs} 对）`
    : "全国爬取：6006 个 OD 对，耗时较长";
  if (!window.confirm(
    `真爬 ${date}？\n\n${scale}\n\n` +
    `· 期间别关服务；同一时刻只能跑一个爬取任务；\n` +
    `· 进度逐对落盘，断电/关窗口都能接着爬（勾着「接着上次爬」）；\n` +
    `· 遇限速会自动降速冷却，耗时取决于实际车次数与服务端限制；\n` +
    `· 会向 12306 发大量请求，注意别太频繁。`)) return;
  try {
    const res = await postJSON("/api/crawl/start", {
      date, delay: Number($("crawl-delay").value) || 1.0, resume: $("crawl-resume").checked,
      cities, max_pairs: pairs,
    });
    if (!res.ok) { setStatus("c-status", res.error, true); return; }
    setStatus("c-status", `已开始爬取 ${date}：${res.note || ""}`);
    if (res.warning) window.alert(res.warning);
    renderResume(res.progress);
    pollCrawl(date);
  } catch (e) {
    setStatus("c-status", "启动失败: " + e.message, true);
  }
}

/** 爬取进度横幅：从**数据库账本**读，所以断电/关窗口/重启服务之后照样看得见。
 *
 * 以前只有内存里的任务状态，进程一重启页面就什么都不知道了 —— 用户只知道「中断了」，
 * 不知道爬到哪、还剩多少、能不能接着爬，于是只能从头再爬一遍。
 */
async function loadCrawlProgress(date) {
  const box = $("crawl-resume-box");
  if (!box) return null;
  const day = date || $("crawl-date").value || "";
  if (!day) { box.hidden = true; return null; }
  try {
    const data = await fetch("/api/crawl/progress?date=" + encodeURIComponent(day))
      .then((r) => r.json());
    renderResume(data);
    return data;
  } catch (e) {
    box.hidden = true;
    return null;
  }
}

function renderResume(p) {
  const box = $("crawl-resume-box");
  if (!box) return;
  // 进度账本里没有记录，但磁盘上留着旧格式断点：也要如实说出来（别装作没爬过）
  if ((!p || !p.planned || (!p.done && !p.running && !p.failed))) {
    if (p && p.legacy) {
      box.hidden = false;
      box.className = "resume-box is-done";
      box.innerHTML =
        `<div class="rb-head"><b>${esc(p.date)} 有旧断点残留</b>` +
        `<span class="rb-when">${esc((p.legacy.files || []).join("、"))}</span></div>` +
        `<div class="rb-text">${esc(p.legacy.text)}</div>` +
        `<div class="rb-actions"><button type="button" class="ghost-sm" id="crawl-resume-go">` +
        `按 OD 重新核对这一天</button>` +
        `<span class="hint">勾着「接着上次爬」点开始即可；已入库的车次不会重复抓。</span></div>`;
      const go = box.querySelector("#crawl-resume-go");
      if (go) go.addEventListener("click", () => {
        $("crawl-date").value = p.date;
        $("crawl-resume").checked = true;
        startCrawl(p.date);
      });
      return;
    }
    box.hidden = true;
    return;
  }
  const finished = p.state === "done" && !p.resumable;
  const pct = p.percent == null ? 0 : p.percent;
  box.hidden = false;
  box.className = "resume-box" + (finished ? " is-done" : "");
  box.innerHTML =
    `<div class="rb-head"><b>${esc(p.date)} 的爬取进度</b>` +
    `<span class="rb-when">${p.updated_at ? "最后更新 " + esc(p.updated_at) : ""}` +
    `${p.state ? " · 状态 " + esc(p.state) : ""}</span></div>` +
    `<div class="rb-text">${esc(p.note_text || (finished ? "上次已爬完" : "有未完成的进度"))}` +
    `　已完成 <b>${p.done}</b>/${p.planned} 对 OD，剩余 <b>${p.remaining}</b>` +
    (p.failed ? `，失败 ${p.failed}` : "") + `</div>` +
    `<div class="rb-bar"><i style="width:${pct}%"></i></div>` +
    (p.running ? `<div class="rb-fail">有 ${p.running} 对停在「进行中」—— 那是断电时正在做的一对，续爬会重新做它。</div>` : "") +
    `<div class="rb-actions">` +
    `<button type="button" class="ghost-sm" id="crawl-resume-go">${finished ? "再核对一遍（跳过已完成的）" : (p.remaining ? `接着爬剩下的 ${p.remaining} 对` : "继续补齐未入库车次")}</button>` +
    `<button type="button" class="ghost-sm" id="crawl-resume-detail">看逐对明细</button>` +
    `<span class="hint">已发现的 ${p.discovered} 趟车里，${p.ingested} 趟已入库，不会重复抓。</span>` +
    `</div>`;
  const go = box.querySelector("#crawl-resume-go");
  if (go) go.addEventListener("click", () => {
    $("crawl-date").value = p.date;
    $("crawl-resume").checked = true;
    startCrawl(p.date);
  });
  const detail = box.querySelector("#crawl-resume-detail");
  if (detail) detail.addEventListener("click", () => {
    const box2 = $("crawl-preview-box");
    box2.hidden = false;
    const rows = (p.rows || []).slice(0, 60);
    box2.innerHTML =
      `<div class="plan-head">${esc(p.date)} 逐对明细（最近 ${rows.length} 条 / 共 ${p.done + p.failed + p.running} 条）</div>` +
      rows.map((r) => `<div class="plan-row"><code>${esc(r.od)}</code>` +
        `<span class="pr">${esc(r.status)}${r.trains ? ` · 新发现 ${r.trains} 趟` : ""}` +
        `${r.tries ? ` · 试过 ${r.tries} 次` : ""} · ${esc(r.updated_at)}</span></div>`).join("") +
      `<p class="hint">done=已完成；failed=失败（续爬会重试）；running=尚未确认完成，续爬会重做。</p>`;
  });
}

/** 预览定向爬取会爬哪些 OD（只读，不启动任务） */
async function previewCrawl() {
  const cities = ($("crawl-cities").value || "").split(/[\s,，、]+/).filter(Boolean);
  const box = $("crawl-preview-box");
  box.hidden = false;
  if (!cities.length) {
    box.innerHTML = `<div class="plan-head">没填目标城市：将按<b>全国</b>爬取 6006 个 OD 对，耗时取决于车次数与限速冷却。</div>`;
    return;
  }
  box.innerHTML = `<div class="plan-head">正在计算…</div>`;
  try {
    const res = await postJSON("/api/crawl/plan", {
      date: $("crawl-date").value || "2030-01-01",
      cities, max_pairs: Number($("crawl-pairs").value) || 320,
    });
    if (!res.ok) { box.innerHTML = `<div class="plan-head">${esc(res.error || "计算失败")}</div>`; return; }
    const f = res.focus;
    box.innerHTML =
      `<div class="plan-head">${esc(res.describe)}</div>` +
      `<div class="plan-row"><code>车站</code><span class="pr">` +
        Object.entries(f.stations || {}).map(([c, cs]) => `${esc(c)}: ${cs.join("/")}`).join("　") +
      `</span></div>` +
      `<div class="plan-row"><code>前几对</code><span class="pr">${res.sample.map(esc).join("　")}</span></div>` +
      `<p class="hint">勾了「断点续爬」时，之前成功的 OD 与已入库车次会自动跳过，失败项会重试。</p>`;
  } catch (e) {
    box.innerHTML = `<div class="plan-head">计算失败：${esc(e.message)}</div>`;
  }
}

/** 建议爬哪些城市：最近查过的 + 可用枢纽（「主动学习」的入口） */
async function suggestCities() {
  const box = $("crawl-preview-box");
  box.hidden = false;
  try {
    const res = await fetch("/api/crawl/targets").then((r) => r.json());
    const cities = (res.targets || []).join(" ");
    if (cities) $("crawl-cities").value = cities;
    box.innerHTML =
      `<div class="plan-head">${esc(res.describe || "")}</div>` +
      `<div class="plan-row"><code>建议目标</code><span class="pr">${(res.targets || []).map(esc).join("、") || "（无）"}</span></div>` +
      `<p class="hint">已填入上面的「目标城市」框；点「预览要爬哪些 OD」能看到具体范围。</p>`;
  } catch (e) {
    box.innerHTML = `<div class="plan-head">加载失败：${esc(e.message)}</div>`;
  }
}

function renderJobs(list) {
  const running = list.find((j) => j.state === "running");
  if (running) pollCrawl(running.date);
}

function pollCrawl(date) {
  if (crawlPollDate === date) return;
  if (crawlTimer) clearTimeout(crawlTimer);
  if (crawlAbort) crawlAbort.abort();
  crawlPollDate = date;
  const seq = ++crawlPollSeq;
  let failures = 0;
  let showedDisconnect = false;
  const tick = async () => {
    if (seq !== crawlPollSeq) return;
    const controller = new AbortController();
    crawlAbort = controller;
    const timeout = setTimeout(() => controller.abort(), 10000);
    let keepPolling = true;
    try {
      const response = await fetch("/api/crawl/status?date=" + encodeURIComponent(date),
        { signal: controller.signal, cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const res = await response.json();
      if (!res.ok) throw new Error(res.error || "读取进度失败");
      if (seq !== crawlPollSeq) return;
      if (showedDisconnect) setStatus("c-status", "连接已恢复，继续显示爬取进度");
      showedDisconnect = false;
      failures = 0;
      const job = res.job;
      if (!job) {
        keepPolling = false;
        setStatus("c-status", "后台任务已不在运行（服务可能已重启），可按保存的断点接着爬", true);
        loadCrawlProgress(date);
        return;
      }
      renderCrawl(job);
      if (job.state !== "running") {
        keepPolling = false;
        setStatus("c-status", job.state === "done"
          ? `爬取完成：入库 ${job.stats.ingested || 0} 趟（耗时 ${Math.round((job.stats.seconds || 0) / 60)} 分钟）`
          : (job.state === "partial" ? `部分完成：入库 ${job.stats.ingested || 0} 趟，还有 ${job.stats.failures || 0} 项失败，可接着爬补齐`
            : (job.state === "cancelled" ? "已取消（断点已保存）" : "爬取失败：" + job.error)),
          job.state === "failed");
        loadData();
      }
    } catch (e) {
      if (seq !== crawlPollSeq) return;
      failures += 1;
      showedDisconnect = true;
      setStatus("c-status", "进度连接暂时中断，正在自动重连；后台任务不会因页面断线而取消", true);
    } finally {
      clearTimeout(timeout);
      if (seq === crawlPollSeq) {
        crawlAbort = null;
        if (keepPolling) {
          // 完成本次请求后才安排下一次，慢响应不会堆积并覆盖新状态。
          crawlTimer = setTimeout(tick, Math.min(30000, 1500 * 2 ** Math.min(failures, 5)));
        } else {
          crawlTimer = null;
          crawlPollDate = null;
        }
      }
    }
  };
  tick();
}

function renderCrawl(job) {
  const box = $("crawl-progress");
  const log = $("crawl-log");
  box.hidden = false;
  const pct = job.percent == null ? 0 : job.percent;
  const eta = job.eta == null ? "" : ` · 预计还需 ${Math.round(job.eta / 60)} 分钟`;
  box.innerHTML =
    `<div class="cp-head"><b>${esc(job.date)}</b> · ${esc(job.phase)} · ` +
    `${job.state === "running" ? "进行中" : job.state}</div>` +
    `<div class="cp-bar"><i style="width:${pct}%"></i></div>` +
    `<div class="cp-meta">${job.done}/${job.total || "?"}（${pct}%）· 已用 ${Math.round(job.elapsed / 60)} 分钟${eta}` +
    (job.extra && job.extra.ingested != null ? ` · 已入库 ${job.extra.ingested}` : "") +
    (job.extra && job.extra.delay != null ? ` · 当前间隔 ${Number(job.extra.delay).toFixed(2)} 秒` : "") +
    (job.extra && job.extra.retry_in > 0 ? ` · ${esc(job.extra.reason || "等待恢复")}，约 ${Math.ceil(job.extra.retry_in)} 秒后重试（可取消）` : "") + `</div>`;
  if (job.logs && job.logs.length) {
    log.hidden = false;
    log.textContent = job.logs.join("\n");
    log.scrollTop = log.scrollHeight;
  }
}

$("m-refresh").addEventListener("click", loadData);
$("m-plan-fill").addEventListener("click", planFill);
$("crawl-preview").addEventListener("click", previewCrawl);
$("crawl-suggest").addEventListener("click", suggestCities);
$("crawl-start").addEventListener("click", () => startCrawl($("crawl-date").value));
$("crawl-date").addEventListener("change", () => loadCrawlProgress($("crawl-date").value));
$("crawl-cancel").addEventListener("click", async () => {
  const res = await postJSON("/api/crawl/cancel", {});
  setStatus("c-status", res.ok ? "已请求取消，当前车次结束后停下（进度已落盘，可以接着爬）" : res.error, !res.ok);
});

$("c-go").addEventListener("click", async () => {
  setStatus("c-status", "复制中…");
  try {
    const payload = {
      from_date: $("c-from").value,
      to_date: $("c-to").value,
    };
    let res = await postJSON("/api/copy-date", payload);
    // 目标日期已有数据时先保护现有结果，再由用户明确确认覆盖。
    if (!res.ok && res.target_exists && window.confirm(
      `${$("c-to").value} 已有 ${res.target_trains || 0} 趟车次，确定覆盖并重新复制吗？`
    )) {
      setStatus("c-status", "正在覆盖并复制…");
      res = await postJSON("/api/copy-date", {...payload, overwrite: true});
    }
    if (!res.ok) {
      setStatus("c-status", res.error, true);
      return;
    }
    const suffix = res.overwritten ? "（已覆盖原数据）" : "";
    setStatus("c-status", `已复制 ${res.rows} 行到 ${$("c-to").value}${suffix}`);
    loadData();
  } catch (e) {
    setStatus("c-status", "失败: " + e.message, true);
  }
});

/* ---------------- 省钱/舒适/巧思路线库（参考数据） ---------------- */
let routeFilter = { category: "", theme: "", clever: false, kind: "" };
let routeView = { items: [], shown: 0, total: 0, hasMore: false };

async function loadRoutes(append) {
  const box = $("rt-list");
  if (!box) return;
  const params = new URLSearchParams({
    city: $("rt-city").value.trim(), sort: $("rt-sort").value,
    category: routeFilter.category, theme: routeFilter.theme,
    clever: routeFilter.clever ? "巧思" : "",
    kind: routeFilter.kind || $("rt-kind").value,
  });
  const size = Math.max(6, Number($("rt-page").value) || 12);
  if (!append) {
    params.set("limit", String(size));
    box.innerHTML = `<div class="status">正在读取路线库…</div>`;
  } else {
    params.set("limit", String(size));
    params.set("skip", String(routeView.items.length));
    const btn = $("rt-more");
    if (btn) { btn.disabled = true; btn.textContent = "载入中…"; }
  }
  try {
    const data = await fetch("/api/routes?" + params).then((r) => r.json());
    if (!data.ok) { box.innerHTML = `<div class="status error">${esc(data.error || "读取失败")}</div>`; return; }
    renderRouteFilters(data.filters || {});
    routeView.total = data.total;
    routeView.hasMore = data.has_more;
    routeView.items = append ? routeView.items.concat(data.routes) : data.routes;
    const count = $("rt-count");
    if (count) {
      const s = data.stats || {};
      count.textContent =
        `共 ${data.total} 条符合条件（库内 ${s.routes || 0} 条：单向 ${s.oneway || 0} / 环线 ${s.loops || 0}，` +
        `巧思 ${s.clever_routes || 0} 条）· 口径 ${data.updated}` +
        (data.has_more ? ` · 已显示 ${routeView.items.length} 条` : "");
    }
    if (!routeView.items.length) {
      box.innerHTML = `<div class="status">没有符合条件的路线（试试清掉筛选或换个城市）</div>`;
      if ($("rt-more")) $("rt-more").hidden = true;
      return;
    }
    box.innerHTML = "";
    routeView.items.forEach((rt, i) => box.appendChild(routeCard(rt, i)));
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = `口径 ${data.updated}｜${data.note}`;
    box.appendChild(note);
    const btn = $("rt-more");
    if (btn) {
      btn.hidden = !data.has_more;
      btn.disabled = false;
      btn.textContent = `载入更多（还有 ${Math.max(0, data.total - routeView.items.length)} 条）`;
    }
  } catch (e) {
    box.innerHTML = `<div class="status error">读取路线库失败：${esc(e.message)}</div>`;
  }
}

function renderRouteFilters(filters) {
  const box = $("rt-filters");
  if (!box) return;
  box.innerHTML = "";
  const chip = (text, active, onClick) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "rec-chip" + (active ? " active" : "");
    b.textContent = text;
    b.addEventListener("click", onClick);
    return b;
  };
  // 分类很多（几十个）时全铺出来太乱：只显示前 12 个，其余靠搜索框
  (filters.categories || []).slice(0, 12).forEach((cat) => box.appendChild(chip(
    cat, routeFilter.category === cat,
    () => { routeFilter.category = routeFilter.category === cat ? "" : cat; loadRoutes(); })));
  (filters.themes || []).slice(0, 8).forEach((th) => box.appendChild(chip(
    th, routeFilter.theme === th,
    () => { routeFilter.theme = routeFilter.theme === th ? "" : th; loadRoutes(); })));
  box.appendChild(chip("只看巧思", routeFilter.clever, () => {
    routeFilter.clever = !routeFilter.clever;
    if (routeFilter.clever && $("rt-sort").value === "value") $("rt-sort").value = "clever";
    loadRoutes();
  }));
  box.appendChild(chip("只看环线", routeFilter.kind === "loop", () => {
    routeFilter.kind = routeFilter.kind === "loop" ? "" : "loop";
    $("rt-kind").value = routeFilter.kind;
    loadRoutes();
  }));
}

/**
 * 路线卡：默认**折叠成一行摘要**（几百条也不至于刷屏），点标题展开细节。
 * 展开状态记在 dataset 上，重新渲染（切筛选）不会乱。
 */
function routeCard(rt, index) {
  const el = document.createElement("div");
  el.className = "rt-card tier-" + (rt.tier || "观景") + (rt.kind === "loop" ? " is-loop" : "");
  el.dataset.rid = rt.id;
  const segs = (rt.segments || []).map((s) => `
    <div class="rt-seg"><span class="rt-mode">${esc(s.mode || "")}</span>
      <span class="rt-path">${esc(s.frm)} → ${esc(s.to)}</span>
      <span class="rt-num">${s.hours ? s.hours + " 小时" : ""}</span>
      <span class="rt-price">${esc(s.price || "")}</span>
      ${s.note ? `<span class="rt-note">${esc(s.note)}</span>` : ""}</div>`).join("");
  const list = (arr) => (arr || []).map((t) => `<li>${esc(t)}</li>`).join("");
  let clever = "";
  if ((rt.clever_score || 0) >= 45) {
    const base = rt.cost_baseline;
    const extra = (base != null && rt.cost_low != null) ? rt.cost_low - base : null;
    clever = `<div class="rt-clever">
        <div class="rt-clever-head">💡 巧思 ${rt.clever_score}
          ${(rt.clever_tags || []).map((t) => `<i>${esc(CLEVER_TAG_LABEL[t] || t)}</i>`).join("")}</div>
        ${extra != null && extra > 0
          ? `<div class="rt-extra">比直达多花约 <b>${extra}</b> 元${rt.extra_value ? `，换来：${esc(rt.extra_value)}` : ""}</div>`
          : (extra != null && rt.extra_value ? `<div class="rt-extra">不多花钱就多看了：${esc(rt.extra_value)}</div>` : "")}
        ${(rt.smart_tips || []).length ? `<ul>${list(rt.smart_tips)}</ul>` : ""}
      </div>`;
  } else if (rt.extra_value) {
    clever = `<div class="rt-clever"><div class="rt-extra">${esc(rt.extra_value)}</div></div>`;
  }
  const kindTag = rt.kind === "loop"
    ? `<span class="rt-kind loop">环线</span>` : `<span class="rt-kind">单向</span>`;
  el.innerHTML = `
    <div class="rt-head" role="button" tabindex="0" title="点一下展开/收起详情">
      <span class="rt-caret">▸</span>
      <b>${esc(rt.name)}</b>
      ${kindTag}
      <span class="rt-tier">${esc(rt.tier || "")}</span>
      <span class="rt-inline">${esc(rt.from_city)} → ${esc(rt.to_city)} ·
        ${rt.total_hours ? rt.total_hours + "h" : ""} ·
        ${rt.cost_low}-${rt.cost_high} 元</span>
      <span class="rt-scores">
        <span class="rt-score" title="省钱指数：每小时花费越低越高">省钱 ${rt.value_score}</span>
        <span class="rt-score comfort" title="舒适指数">舒适 ${rt.comfort_score}</span>
        <span class="rt-score clever" title="巧思指数：别出心裁的程度">巧思 ${rt.clever_score || 0}</span>
      </span>
    </div>
    <div class="rt-body" hidden>
      <div class="rt-summary">${esc(rt.summary)}</div>
      <div class="rt-meta">
        <span>${(rt.modes || []).join(" / ")}</span>
        <span>${esc(rt.best_season || "")}</span>
        <span>${(rt.themes || []).map((t) => `<i>${esc(t)}</i>`).join("")}</span>
      </div>
      ${clever}
      ${segs ? `<div class="rt-segs">${segs}</div>` : ""}
      <div class="rt-tips">
        <div><h5>怎么省</h5><ul>${list(rt.save_tips)}</ul></div>
        <div><h5>怎么舒服</h5><ul>${list(rt.comfort_tips)}</ul></div>
      </div>
      ${(rt.fit_for || []).length ? `<div class="rt-fit">适合：${rt.fit_for.map(esc).join("、")}</div>` : ""}
      ${rt.watch_out ? `<div class="rt-warn">注意：${esc(rt.watch_out)}</div>` : ""}
      <div class="rt-actions">
        <label class="rt-date">出行日 <input type="date" class="rt-when" value="${esc(defaultDate())}"></label>
        <label class="rt-date">每站停留
          <select class="rt-stay">
            <option value="">自动（按素材时长）</option>
            <option value="transit">只中转</option>
            <option value="one_night">住一晚</option>
            <option value="two_nights">住两晚</option>
          </select>
        </label>
        <button type="button" class="primary rt-trip">排成行程（用真实时刻表）</button>
        <button type="button" class="ghost-sm rt-concrete">只看逐段核实</button>
      </div>
      <div class="rt-trip-box" hidden></div>
      <div class="rt-concrete-box" hidden></div>
      <div class="rt-foot">来源：${esc(rt.source || "公开攻略")} · 可信度 ${esc(rt.confidence || "")}
        ${rt.source_url ? ` · <a href="${esc(rt.source_url)}" target="_blank" rel="noreferrer">原始链接</a>` : ""}</div>
    </div>`;
  const head = el.querySelector(".rt-head");
  const body = el.querySelector(".rt-body");
  const toggle = (open) => {
    body.hidden = !open;
    el.classList.toggle("open", open);
    el.querySelector(".rt-caret").textContent = open ? "▾" : "▸";
  };
  head.addEventListener("click", () => toggle(body.hidden));
  head.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(body.hidden); }
  });
  if (index < 3 && !$("rt-city").value && !routeFilter.category && !routeFilter.theme) toggle(true);
  const tripBtn = el.querySelector(".rt-trip");
  if (tripBtn) tripBtn.addEventListener("click", () => showTrip(rt, el));
  const btn = el.querySelector(".rt-concrete");
  if (btn) btn.addEventListener("click", () => showConcrete(rt, el));
  return el;
}

/** 路线 → 排成行程：调规划引擎，出可执行的乘车表 */
async function showTrip(rt, el) {
  const box = el.querySelector(".rt-trip-box");
  const when = (el.querySelector(".rt-when") || {}).value || defaultDate();
  const stay = (el.querySelector(".rt-stay") || {}).value || "";
  box.hidden = false;
  box.innerHTML = `<div class="status">正在用 ${esc(when)} 的时刻表排行程…</div>`;
  try {
    const data = await postJSON("/api/routes/trip", { id: rt.id, date: when, stay_mode: stay });
    if (!data.ok) {
      box.innerHTML = `<div class="status error">${esc(data.error || "排行程失败")}
        ${(data.unknown_stops || []).length ? `<br>这些点没有铁路站：${data.unknown_stops.map(esc).join("、")}` : ""}</div>`;
      return;
    }
    const s = data.summary || {};
    box.innerHTML =
      `<div class="cc-head">${esc(data.route.name)}｜${esc(when)}｜停靠 ${data.stops.map(esc).join(" → ")}
        ${data.closed ? "（环线）" : ""}｜共 ${s.days} 天 / ${s.rides} 段车` +
      `${data.ground_count ? ` / ${data.ground_count} 段地面接驳（估算）` : ""}` +
      `${s.total_fare ? ` / 合计约 ${s.total_fare} 元` : ""}</div>` +
      (data.days || []).map((day) => {
        const rides = ((day.journey || {}).legs || []).map((l) =>
          `<span class="cc-train">${esc(l.code || "")} ${esc(l.kind || "")} ${esc(l.dep_text || "")}→${esc(l.arr_text || "")}${l.fare ? " ¥" + l.fare : ""}</span>`).join("");
        const ground = (day.ground || []).map((g) =>
          `<span class="cc-ground" title="${esc(g.note || "")}">地面 · ${esc(g.anchor)}→${esc(g.stop)}
            ${g.hours ? g.hours + "h" : ""}${g.price ? " " + esc(g.price) : ""}</span>`).join("");
        return `<div class="trip-day"><b>第 ${day.day_no} 天</b>
          <span class="trip-path">${esc(day.frm)} → ${esc(day.to)}</span>
          <span class="trip-stay">${esc(day.stay || day.frm + " 出发")}</span>
          <div class="trip-rides">${rides || "（这一天没有车次）"}${ground}</div></div>`;
      }).join("") +
      (data.warnings || []).map((w) => `<div class="rt-warn">⚠ ${esc(w)}</div>`).join("") +
      `<div class="trip-actions">
        <button type="button" class="primary rt-save-trip">存进「我的行程」</button>
        <span class="rt-save-note hint"></span>
      </div>` +
      `<div class="rt-foot">${esc(data.note)}</div>`;
    const saveBtn = box.querySelector(".rt-save-trip");
    bindGuideButtons(box);
    if (saveBtn) saveBtn.addEventListener("click", async () => {
      const noteEl = box.querySelector(".rt-save-note");
      saveBtn.disabled = true;
      noteEl.textContent = "正在存档…";
      try {
        const res = await postJSON("/api/trips/save", { id: rt.id, date: when, stay_mode: stay });
        if (res.ok) {
          noteEl.textContent = `已存档：${(res.saved || {}).name || ""}`;
          loadTrips();
        } else {
          noteEl.textContent = res.error || "存档失败";
        }
      } catch (e) {
        noteEl.textContent = "存档失败：" + e.message;
      }
      saveBtn.disabled = false;
    });
  } catch (e) {
    box.innerHTML = `<div class="status error">排行程失败：${esc(e.message)}</div>`;
  }
}

/** 把参考路线落到某一天：调接口拿真实车次/车程，逐段标「已核实 / 待自查」 */
async function showConcrete(rt, el) {
  const box = el.querySelector(".rt-concrete-box");
  const when = (el.querySelector(".rt-when") || {}).value || defaultDate();
  box.hidden = false;
  box.innerHTML = `<div class="status">正在用 ${esc(when)} 的时刻表核实…</div>`;
  try {
    const data = await postJSON("/api/routes/concrete", { id: rt.id, date: when });
    if (!data.ok) { box.innerHTML = `<div class="status error">${esc(data.error || "核实失败")}</div>`; return; }
    const rows = data.segments.map((s) => {
      const tag = s.verified ? `<span class="cc-ok">已核实</span>`
        : (s.status === "manual" ? `<span class="cc-manual">需自查</span>`
          : `<span class="cc-bad">没查到</span>`);
      let detail = "";
      if (s.options && s.options.length) {
        detail = s.options.map((o) => `<span class="cc-train">${esc(o.code)} ${esc(o.dep)}→${esc(o.arr)}（${o.hours}h）</span>`).join("");
      } else if (s.km) {
        detail = `<span class="cc-train">${s.km} 公里 · ${s.hours} 小时${s.tolls ? " · 过路费约 " + s.tolls + " 元" : ""}${s.approximate ? "（含估算路段）" : ""}</span>`;
      }
      return `<div class="cc-seg">${tag}<span class="cc-mode">${esc(s.mode)}</span>
        <span class="cc-path">${esc(s.frm)} → ${esc(s.to)}</span>
        <span class="cc-plan">素材：${esc(s.plan_price || "")}${s.plan_hours ? ` / ${s.plan_hours}h` : ""}</span>
        ${detail}
        ${s.note ? `<div class="cc-note">${esc(s.note)}</div>` : ""}</div>`;
    }).join("");
    box.innerHTML =
      `<div class="cc-head">${esc(when)} 的真实情况：${data.summary.verified}/${data.summary.segments} 段已核实
        · 数据来源 ${esc(data.data_label)}</div>` + rows +
      (data.warnings || []).map((w) => `<div class="rt-warn">⚠ ${esc(w)}</div>`).join("") +
      `<div class="rt-foot">${esc(data.note)}</div>`;
  } catch (e) {
    box.innerHTML = `<div class="status error">核实失败：${esc(e.message)}</div>`;
  }
}

/** 巧思标签的中文名（对应后端 routes.CLEVER_WEIGHTS 白名单） */
const CLEVER_TAG_LABEL = {
  extra_stops: "顺路多玩", detour_worth: "绕路值得", night_move: "夜车省时",
  cheap_upgrade: "同价升级", shoulder: "错峰出行", combo_ticket: "联程接续",
  local_trick: "本地经验", season_window: "抓窗口期",
};

$("rt-go").addEventListener("click", () => loadRoutes());
$("rt-sort").addEventListener("change", () => loadRoutes());
$("rt-kind").addEventListener("change", () => { routeFilter.kind = ""; loadRoutes(); });
$("rt-city").addEventListener("keydown", (e) => { if (e.key === "Enter") loadRoutes(); });
$("rt-more").addEventListener("click", () => loadRoutes(true));
$("rt-expand-all").addEventListener("click", () => {
  document.querySelectorAll("#rt-list .rt-card").forEach((el) => {
    el.querySelector(".rt-body").hidden = false;
    el.classList.add("open");
    el.querySelector(".rt-caret").textContent = "▾";
  });
});
$("rt-collapse-all").addEventListener("click", () => {
  document.querySelectorAll("#rt-list .rt-card").forEach((el) => {
    el.querySelector(".rt-body").hidden = true;
    el.classList.remove("open");
    el.querySelector(".rt-caret").textContent = "▸";
  });
});

/* ---------------- 一键导入路线素材 ---------------- */
/* ---------------- 环线 Ultra 版：整条穷游环线（北京 / 河北出发） ----------------
 * 不另开界面：点按钮就把 Ultra 列表铺在环线页签里，再点一次收起。
 * 数据是「几天走完的整条线」，和单点走法的路线库互补 —— 环线出行直接看这个。 */
let ultraOpen = false;
let ultraView = { items: [], total: 0 };
// 筛选条件是连着改的（出发地 → 天数 → 预算），请求会并发回来；
// 谁最后回来谁写页面，慢的那个会把旧结果盖上去，看着像「筛选没生效」。
// 所以给每次请求编号，只认最新一次的返回。
let ultraSeq = 0;

async function loadUltra() {
  const box = $("ultra-list");
  if (!box) return;
  const seq = ++ultraSeq;
  const params = new URLSearchParams({
    origin: $("ultra-origin").value || "",
    days_max: String(Number($("ultra-days").value) || 0),
    budget_max: String(Number($("ultra-budget").value) || 0),
    sort: $("ultra-sort").value || "value",
    scale: ($("ultra-scale") || {}).value || "",
  });
  box.innerHTML = `<div class="status">正在读取环线 Ultra…</div>`;
  try {
    const data = await fetch("/api/ultra?" + params).then((r) => r.json());
    if (seq !== ultraSeq) return;          // 已经有更新的筛选了，这次结果丢掉
    if (!data.ok) { box.innerHTML = `<div class="status error">${esc(data.error || "读取失败")}</div>`; return; }
    ultraView.items = data.loops || [];
    ultraView.total = data.total || 0;
    const sel = $("ultra-origin");
    if (sel && !sel.dataset.built) {
      sel.dataset.built = "1";
      sel.innerHTML = `<option value="">全部（${data.stats.loops} 条）</option>` +
        (data.origins || []).map((o) =>
          `<option value="${esc(o.name)}">${esc(o.name)}（${o.count}${o.province === "北京" ? "" : " · 河北"}）</option>`).join("");
    }
    const s = data.stats || {};
    const count = $("ultra-count");
    if (count) {
      count.textContent =
        `共 ${data.total} 条（北京 ${s.beijing || 0} · 河北 ${s.hebei || 0}，覆盖 ${s.origins || 0} 个出发地` +
        (s.large ? ` · 含大环线 ${s.large} 条` : "") + `）· ` +
        `${s.days ? s.days[0] + "-" + s.days[1] : "?"} 天 · 总价 ${s.budget ? s.budget[0] + "-" + s.budget[1] : "?"} 元 · ` +
        `日均 ${s.per_day ? s.per_day[0] + "-" + s.per_day[1] : "?"} 元 · 口径 ${data.updated}`;
    }
    if (!ultraView.items.length) {
      box.innerHTML = `<div class="status">没有符合条件的环线（放宽天数或预算试试）</div>`;
      return;
    }
    box.innerHTML = "";
    ultraView.items.forEach((item, i) => box.appendChild(ultraCard(item, i)));
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = data.note || "";
    box.appendChild(note);
  } catch (e) {
    box.innerHTML = `<div class="status error">读取环线 Ultra 失败：${esc(e.message)}</div>`;
  }
}

/** Ultra 卡：一行摘要 + 展开逐日安排（默认前 2 条展开，方便直接看质量） */
function ultraCard(item, index) {
  const el = document.createElement("div");
  el.className = "rt-card ultra-card";
  const conf = { high: ["可信度 高", "good"], medium: ["可信度 中", "info"], low: ["可信度 低", "warn"] }[item.confidence]
    || ["可信度 未知", "muted"];
  el.innerHTML =
    `<div class="rt-head" tabindex="0">
       <span class="rt-caret">▸</span>
       <b>${esc(item.name)}</b>
       <span class="rt-kind ${item.scale === "large" ? "scale-large" : ""}">${esc(item.scale_label || "小环线")}</span>
       <span class="rt-kind loop">${esc(item.origin)}出发</span>
       <span class="rt-kind">${item.days} 天</span>
       <span class="rt-kind">日均约 ¥${item.per_day}</span>
       <span class="rt-inline">${esc(item.cities.join(" → "))}</span>
     </div>
     <div class="ultra-meta">
       <span class="src-badge ${conf[1]}">${conf[0]}</span>
       ${item.hub_line ? `<span>走 ${esc(item.hub_line)}</span>` : ""}
       <span>预算 ¥${item.budget_low}-${item.budget_high}</span>
       <span>${esc((item.transport || []).join(" / "))}</span>
       <span>${esc(item.best_season || "")}</span>
     </div>
     <div class="rt-summary">${esc(item.summary)}</div>
     <div class="rt-body">
       <div class="ultra-chips">${(item.highlights || []).map((h) => `<i class="cg-chip">${esc(h)}</i>`).join("")}</div>
       <div class="ultra-days"><div class="status">展开时读取逐日安排…</div></div>
     </div>
     <div class="rt-foot">来源：${esc(item.source || "公开攻略整理")}
       ${item.source_url ? ` · <a href="${esc(item.source_url)}" target="_blank" rel="noreferrer">原始链接</a>` : ""}</div>`;
  const head = el.querySelector(".rt-head");
  const body = el.querySelector(".rt-body");
  const caret = el.querySelector(".rt-caret");
  let loaded = false;
  const toggle = async (open) => {
    body.hidden = !open;
    caret.textContent = open ? "▾" : "▸";
    if (!open || loaded) return;
    loadUltraDetail(item.id, el);
    loaded = true;
  };
  body.hidden = true;
  head.addEventListener("click", () => toggle(body.hidden));
  head.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(body.hidden); }
  });
  if (index < 2) toggle(true);
  return el;
}

async function loadUltraDetail(id, el) {
  const box = el.querySelector(".ultra-days");
  try {
    const data = await fetch("/api/ultra/detail?id=" + encodeURIComponent(id)).then((r) => r.json());
    if (!data.ok) { box.innerHTML = `<div class="status error">${esc(data.error || "取不到")}</div>`; return; }
    const loop = data.loop;
    box.innerHTML = (loop.itinerary || []).map((day) =>
      `<div class="trip-day"><b>${esc(day.day)}</b>
         <span class="trip-path">${esc(day.city || "")}</span>
         <span class="trip-stay">${esc(day.title || "")}</span>
         <div class="ultra-detail">${esc(day.detail || "")}</div></div>`).join("") +
      `<div class="ultra-notes">
         ${(loop.save_tips || []).length ? `<div><h5>怎么省</h5><ul>${loop.save_tips.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></div>` : ""}
         ${(loop.comfort_tips || []).length ? `<div><h5>怎么舒服点</h5><ul>${loop.comfort_tips.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></div>` : ""}
         ${(loop.fit_for || []).length ? `<div><h5>适合谁</h5><ul>${loop.fit_for.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></div>` : ""}
       </div>` +
      (loop.watch_out ? `<div class="rt-warn">⚠ ${esc(loop.watch_out)}</div>` : "") +
      `<div class="trip-actions">
         <button type="button" class="ghost-sm ud-flow" data-city="${esc((loop.cities || [])[0] || "")}">
           看出发城市的城内动线</button>
         <span class="hint">逐日安排来自公开攻略；车次与票价以 12306 为准</span>
       </div>`;
    bindGuideButtons(box);
  } catch (e) {
    box.innerHTML = `<div class="status error">读取失败：${esc(e.message)}</div>`;
  }
}

/** 切换 Ultra 面板：打开时把普通环线结果收起来，只留这份高质量环线清单 */
function toggleUltra(force) {
  const panel = $("ultra-panel");
  const results = $("l-results");
  if (!panel) return;
  ultraOpen = force === undefined ? !ultraOpen : !!force;
  panel.hidden = !ultraOpen;
  if (results) results.hidden = ultraOpen;
  const btn = $("l-ultra");
  if (btn) btn.textContent = ultraOpen ? "收起 Ultra 版" : "环线 Ultra 版";
  if (ultraOpen) {
    panel.scrollIntoView({ block: "start", behavior: "smooth" });
    loadUltra();
  }
}

if ($("l-ultra")) {
  document.documentElement.dataset.ultraWired = "1";
  $("l-ultra").addEventListener("click", () => toggleUltra());
  $("ultra-close").addEventListener("click", () => toggleUltra(false));
  $("ultra-go").addEventListener("click", () => loadUltra());
  $("ultra-origin").addEventListener("change", () => loadUltra());
  $("ultra-sort").addEventListener("change", () => loadUltra());
  if ($("ultra-scale")) $("ultra-scale").addEventListener("change", () => loadUltra());
  $("ultra-days").addEventListener("change", () => loadUltra());
  $("ultra-budget").addEventListener("change", () => loadUltra());
}

/* ---------------- 我的行程：把路线排成乘车表并存档 ---------------- */
let tripView = { items: [], total: 0, hasMore: false, page: 10 };
let tripTimer = null;

async function loadTrips(append) {
  const box = $("tp-list");
  if (!box) return;
  const params = new URLSearchParams({
    order: "created", limit: String(tripView.page),
    skip: String(append ? tripView.items.length : 0),
  });
  if (!append) box.innerHTML = `<div class="status">正在读取存档…</div>`;
  try {
    const data = await fetch("/api/trips?" + params).then((r) => r.json());
    if (!data.ok) { box.innerHTML = `<div class="status error">${esc(data.error || "读取失败")}</div>`; return; }
    tripView.total = data.total;
    tripView.hasMore = data.has_more;
    tripView.items = append ? tripView.items.concat(data.trips) : data.trips;
    const s = data.stats || {};
    const cov = data.coverage || {};
    const count = $("tp-count");
    if (count) {
      const last = s.last_build || {};
      count.textContent =
        `已存档 ${s.trips || 0} 份（环线 ${s.loops || 0} / 单向 ${s.oneway || 0}，共 ${s.days || 0} 天、` +
        `${s.rides || 0} 段车` +
        (s.partial ? `，其中 ${s.partial} 份含 ${s.ground || 0} 段地面接驳` : "") + `）· ` +
        `路线库覆盖 ${cov.saved || 0}/${cov.routes || 0} 条` +
        `（排不出 ${cov.skipped || 0} 条 · 还没排 ${cov.pending || 0} 条）` +
        (last.finished ? ` · 上次批量建档 ${last.finished}` : "");
    }
    renderTripSkips(cov.skips || [], cov.skip_groups || []);
    if (!tripView.items.length) {
      box.innerHTML = `<div class="status">还没有存档：先在路线卡里点「排成行程（用真实时刻表）」再点「存进我的行程」，
        或者在上面选好出行日直接「把整库排成行程并存档」。</div>`;
      if ($("tp-more")) $("tp-more").hidden = true;
      return;
    }
    box.innerHTML = "";
    tripView.items.forEach((t) => box.appendChild(tripCard(t)));
    const btn = $("tp-more");
    if (btn) {
      btn.hidden = !data.has_more;
      btn.textContent = `载入更多（还有 ${Math.max(0, data.total - tripView.items.length)} 份）`;
    }
  } catch (e) {
    box.innerHTML = `<div class="status error">读取存档失败：${esc(e.message)}</div>`;
  }
}

function renderTripSkips(skips, groups) {
  const box = $("tp-skips");
  if (!box) return;
  if (!skips.length) { box.hidden = true; return; }
  box.hidden = false;
  const summary = (groups || []).map((g) => `${esc(g.label)} ${g.count} 条`).join(" · ");
  box.innerHTML =
    `<div class="imp-head">这 ${skips.length} 条排不出城际乘车表${summary ? `：${summary}` : ""}。
      它们不是坏数据，而是「不是城际路线」或「还没爬到那天的车次」——下面逐条写了原因，
      条目仍留在路线库里当参考。</div>` +
    skips.map((s) => `<div class="imp-row bad"><code>${esc(s.route_id)}</code>
      <span class="imp-name">${esc(s.name || "")}</span>
      <span class="imp-kind">${esc(s.fail_label || "")}</span>
      <div class="imp-issues">${esc(s.reason || "")}</div></div>`).join("");
}

/** 存档卡：默认一行摘要，点开取完整乘车表（详情按需请求，列表不至于很重） */
function tripCard(t) {
  const el = document.createElement("div");
  el.className = "trip-card" + (t.kind === "loop" ? " is-loop" : "");
  el.dataset.tripId = String(t.id);
  el.innerHTML =
    `<div class="trip-head" tabindex="0">
      <span class="trip-caret">▸</span>
      <b>${esc(t.name)}</b>
      <span class="trip-meta">${esc(t.date)} · ${t.day_count} 天 · ${t.ride_count} 段车` +
      `${t.fare ? ` · 约 ¥${t.fare}` : ""} · ${esc(t.kind_label)}` +
      `${t.ground_count ? ` · <span class="cc-ground">含 ${t.ground_count} 段地面接驳（估算）</span>` : ""}` +
      `${t.data_label ? ` · ${esc(t.data_label)}` : ""}</span>
    </div>
    <div class="trip-stops">${(t.stops || []).map(esc).join(" → ")}</div>
    <div class="trip-body" hidden></div>`;
  const body = el.querySelector(".trip-body");
  const head = el.querySelector(".trip-head");
  const caret = el.querySelector(".trip-caret");
  let loaded = false;
  const toggle = async (open) => {
    body.hidden = !open;
    caret.textContent = open ? "▾" : "▸";
    if (!open || loaded) return;
    body.innerHTML = `<div class="status">正在取行程…</div>`;
    try {
      const data = await fetch("/api/trips/detail?id=" + encodeURIComponent(t.id)).then((r) => r.json());
      if (!data.ok) { body.innerHTML = `<div class="status error">${esc(data.error || "取不到")}</div>`; return; }
      const plan = (data.trip || {}).plan || {};
      body.innerHTML = tripPlanHtml(plan) + tripRowActions(t, el);
      bindTripRowActions(el);
      bindGuideButtons(body);
      loaded = true;
    } catch (e) {
      body.innerHTML = `<div class="status error">取行程失败：${esc(e.message)}</div>`;
    }
  };
  head.addEventListener("click", () => toggle(body.hidden));
  head.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(body.hidden); }
  });
  return el;
}

/** 一份行程的乘车表 HTML（存档卡与「排成行程」共用同一套排版规则）
 *  地面接驳段（景区/小镇）单独用一种颜色标出来，并写明「照素材估算、需自查」。 */
function tripPlanHtml(plan) {
  const days = (plan.days || []).map((day) => {
    const rides = ((day.journey || {}).legs || []).map((l) =>
      `<span class="cc-train">${esc(l.code || "")} ${esc(l.kind || "")} ${esc(l.dep_text || "")}→${esc(l.arr_text || "")}${l.fare ? " ¥" + l.fare : ""}</span>`).join("");
    const ground = (day.ground || []).map((g) =>
      `<span class="cc-ground" title="${esc(g.note || "")}">地面 · ${esc(g.anchor)}→${esc(g.stop)}
        ${g.hours ? g.hours + "h" : ""}${g.price ? " " + esc(g.price) : ""}</span>`).join("");
    return `<div class="trip-day"><b>第 ${day.day_no} 天</b>
      <span class="trip-path">${esc(day.frm)} → ${esc(day.to)}</span>
      <span class="trip-stay">${esc(day.stay || day.frm + " 出发")}</span>
      <div class="trip-rides">${rides || "（这一天没有车次）"}${ground}</div>
      ${day.to ? `<div class="gd-open-row"><button type="button" class="ghost-sm gd-open"
        data-city="${esc(day.city || day.to)}">看这天的城内动线</button>
        <span class="hint">用攻略里的 D1/D2 动线补上「到了以后怎么玩」</span></div>` : ""}</div>`;
  }).join("");
  return `<div class="cc-head">${esc(plan.date || "")}｜停靠 ${(plan.stops || []).map(esc).join(" → ")}
      ${plan.closed ? "（环线）" : ""}｜停留 ${esc(plan.stay_mode || "auto")}` +
    (plan.ground_count ? `｜含 ${plan.ground_count} 段地面接驳（估算）` : "") + `</div>` + days +
    (plan.warnings || []).map((w) => `<div class="rt-warn">⚠ ${esc(w)}</div>`).join("") +
    `<div class="rt-foot">${esc(plan.note || "")}</div>`;
}

/** 行程里的「城内动线」按钮：把停留日接到那座城的攻略动线上。
 *
 * 注意：Ultra 详情里的按钮类名是 `.ud-flow`，一开始只绑了 `.gd-open`，
 * 于是那条「看出发城市的城内动线」点了毫无反应 —— 两个选择器一起绑。
 */
function bindGuideButtons(root) {
  (root || document).querySelectorAll(".gd-open, .ud-flow").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => openGuideFor(btn.dataset.city || ""));
  });
}

function tripRowActions(t, el) {
  return `<div class="trip-actions">
    <button type="button" class="ghost-sm tp-rename">改名</button>
    <button type="button" class="ghost-sm tp-del">删掉这份存档</button>
    <span class="hint">存档时间 ${esc(t.created_at || "")}</span>
  </div>`;
}

function bindTripRowActions(el) {
  const del = el.querySelector(".tp-del");
  if (del) del.addEventListener("click", async () => {
    const id = Number(el.dataset.tripId || 0);
    if (!window.confirm("删掉这份存档？（路线库不受影响）")) return;
    const res = await postJSON("/api/trips/delete", { id });
    if (!res.ok) { setStatus("tp-status", res.error || "删除失败", true); return; }
    setStatus("tp-status", "已删除");
    loadTrips();
  });
  const ren = el.querySelector(".tp-rename");
  if (ren) ren.addEventListener("click", async () => {
    const id = Number(el.dataset.tripId || 0);
    const now = el.querySelector(".trip-head b").textContent;
    const name = window.prompt("给这份行程起个名字", now);
    if (!name) return;
    const res = await postJSON("/api/trips/rename", { id, name });
    setStatus("tp-status", res.ok ? "已改名" : (res.error || "改名失败"), !res.ok);
    loadTrips();
  });
}

async function startTripBuild() {
  const date = $("tp-date").value || defaultDate();
  const stay = $("tp-stay").value || "";
  const replace = $("tp-replace").checked;
  if (!window.confirm(
    `把路线库里的每条路线都按 ${date} 排成行程并存档？\n\n` +
    `· 只用本地时刻表，不联网、不碰 12306；\n` +
    `· ${replace ? "会覆盖同一天的旧存档" : "已有存档的会跳过，只补没排过的"}；\n` +
    `· 排不出来的条目会单独列出来，不会静默丢掉。`)) return;
  setStatus("tp-status", "正在启动后台任务…");
  try {
    const res = await postJSON("/api/trips/build", { date, stay_mode: stay, replace });
    if (!res.ok) { setStatus("tp-status", res.error, true); return; }
    setStatus("tp-status", "已在后台开跑（可以切页签，跑完列表会刷新）");
    pollTripBuild();
  } catch (e) {
    setStatus("tp-status", "启动失败：" + e.message, true);
  }
}

function pollTripBuild() {
  if (tripTimer) clearInterval(tripTimer);
  const tick = async () => {
    try {
      const res = await fetch("/api/trips/build/status").then((r) => r.json());
      const job = res.job;
      if (!job) return;
      renderTripJob(job);
      if (job.state !== "running") {
        clearInterval(tripTimer);
        tripTimer = null;
        setStatus("tp-status", job.state === "done"
          ? `建档完成：新增 ${job.saved} 份，跳过 ${job.skipped} 份，排不出 ${job.failed} 条`
          : (job.state === "cancelled" ? "已取消（已存档的保留）" : "建档失败：" + job.error),
          job.state === "failed");
        loadTrips();
      }
    } catch (e) { /* 下一轮再试 */ }
  };
  tick();
  tripTimer = setInterval(tick, 1500);
}

function renderTripJob(job) {
  const box = $("tp-progress");
  const log = $("tp-log");
  if (!box) return;
  box.hidden = false;
  const pct = job.percent == null ? 0 : job.percent;
  const eta = job.eta == null ? "" : ` · 预计还需 ${job.eta} 秒`;
  box.innerHTML =
    `<div class="cp-head"><b>${esc(job.date)}</b> · ${esc(job.phase)} · ` +
    `${job.state === "running" ? "进行中" : job.state}</div>` +
    `<div class="cp-bar"><i style="width:${pct}%"></i></div>` +
    `<div class="cp-meta">${job.done}/${job.total || "?"}（${pct}%）· 新增 ${job.saved} · 跳过 ${job.skipped} · ` +
    `排不出 ${job.failed}${eta}</div>`;
  if (log && job.logs && job.logs.length) {
    log.hidden = false;
    log.textContent = job.logs.join("\n");
    log.scrollTop = log.scrollHeight;
  }
}

async function exportTrips() {
  try {
    const data = await fetch("/api/trips/export").then((r) => r.json());
    if (!data.ok) { setStatus("tp-status", data.error || "导出失败", true); return; }
    const blob = new Blob([data.json], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `我的行程_${defaultDate()}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    setStatus("tp-status", `已导出 ${(data.stats || {}).trips || 0} 份行程`);
  } catch (e) {
    setStatus("tp-status", "导出失败：" + e.message, true);
  }
}

/** 页面刷新/切页签后，如果后台还在建档，继续把进度显示出来。
 *  跑完的任务不主动弹进度条（上次建档时间在统计行里），免得一进数据台就占地方。 */
async function pollTripBuildIfRunning() {
  try {
    const res = await fetch("/api/trips/build/status").then((r) => r.json());
    if (res.job && res.job.state === "running") {
      renderTripJob(res.job);
      pollTripBuild();
    }
  } catch (e) { /* 没跑就算了 */ }
}

if ($("tp-build")) {
  document.documentElement.dataset.tpWired = "1";   // 探针用它确认这块绑定真的跑到了
  $("tp-build").addEventListener("click", startTripBuild);
  $("tp-refresh").addEventListener("click", () => loadTrips());
  $("tp-export").addEventListener("click", exportTrips);
  $("tp-more").addEventListener("click", () => loadTrips(true));
  $("tp-cancel").addEventListener("click", async () => {
    const res = await postJSON("/api/trips/build/cancel", {});
    setStatus("tp-status", res.ok ? "已请求取消，当前这条排完就停" : res.error, !res.ok);
  });
  if (!$("tp-date").value) $("tp-date").value = defaultDate();
}

/* ---------------- 城内点位（只读预览，先不画地图） ---------------- */
let poiView = { items: [], total: 0, hasMore: false };
let poiTimer = null;

async function loadPois(append) {
  const box = $("poi-list");
  if (!box) return;
  const page = Math.max(6, Number($("poi-page").value) || 12);
  const params = new URLSearchParams({
    city: $("poi-city").value.trim(),
    status: $("poi-only-miss").checked ? "miss" : "",
    limit: String(page), skip: String(append ? poiView.items.length : 0),
  });
  if (!append) box.innerHTML = `<div class="status">正在读取点位…</div>`;
  try {
    const data = await fetch("/api/poi?" + params).then((r) => r.json());
    if (!data.ok) { box.innerHTML = `<div class="status error">${esc(data.error || "读取失败")}</div>`; return; }
    poiView.total = data.total;
    poiView.hasMore = data.skip + data.pois.length < data.total;
    poiView.items = append ? poiView.items.concat(data.pois) : data.pois;
    const s = data.stats || {};
    const count = $("poi-count");
    if (count) {
      count.textContent =
        `名单 ${s.targets || 0} 个看点（${s.cities || 0} 城）：命中 ${s.hit || 0}` +
        `（${s.rate || 0}%）· 未命中 ${s.miss || 0} · 还没查 ${s.pending || 0}` +
        (s.cities_with_hit ? ` · 有命中的城市 ${s.cities_with_hit}/${s.cities}` : "") +
        (s.last_harvest && s.last_harvest.total
          ? ` · 上次补查 ${s.last_harvest.total} 个（命中 ${s.last_harvest.hit}）` : "");
    }
    if (!poiView.items.length) {
      box.innerHTML = `<div class="status">${s.targets
        ? "还没有点位数据：点上面的「按名单补坐标」开始（1 个名字 1 次请求，约 900 个）"
        : "城市目录里没有看点"}</div>`;
      if ($("poi-more")) $("poi-more").hidden = true;
      return;
    }
    box.innerHTML = "";
    poiView.items.forEach((p) => box.appendChild(poiRow(p)));
    const more = $("poi-more");
    if (more) {
      more.hidden = !poiView.hasMore;
      more.textContent = `载入更多（还有 ${Math.max(0, poiView.total - poiView.items.length)} 个）`;
    }
  } catch (e) {
    box.innerHTML = `<div class="status error">读取点位失败：${esc(e.message)}</div>`;
  }
}

/** 一行点位：命中的给坐标/类型/评分/距离，未命中的把「差在哪」照实写出来 */
function poiRow(p) {
  const el = document.createElement("div");
  el.className = "rt-card poi-row" + (p.status === "ok" ? "" : " poi-miss");
  const stars = p.rating ? ` ★${p.rating}` : "";
  const geom = p.has_coord ? `${Number(p.lat).toFixed(4)}, ${Number(p.lon).toFixed(4)}` : "无坐标";
  el.innerHTML =
    `<div class="rt-head">
      <span class="rt-caret">·</span>
      <b>${esc(p.name)}</b>
      <span class="rt-kind ${p.status === "ok" ? "loop" : ""}">${p.status === "ok" ? "已补坐标" : "未命中"}</span>
      <span class="rt-inline">${esc(p.city)} · ${esc(geom)}</span>
    </div>
    <div class="poi-detail">` +
    (p.status === "ok"
      ? `高德返回：<b>${esc(p.poi_name)}</b> · ${esc(p.kind || "")}${esc(stars)}` +
        `${p.distance_km != null ? ` · 距市中心 ${p.distance_km} 公里` : ""}` +
        `${p.address ? ` · ${esc(p.address)}` : ""}`
      : `没收到：${esc(p.why || "没有返回结果")}`) +
    `${p.note ? ` · ${esc(p.note)}` : ""}</div>`;
  return el;
}

async function startPoiHarvest() {
  const refresh = $("poi-refresh").checked;
  const onlyMiss = $("poi-only-miss").checked;
  if (!window.confirm(
    "按名单逐个查坐标？\n\n" +
    (onlyMiss ? "· 这次只重查上次「未命中」的那些；\n"
              : "· 名单 = 城市目录里约 900 个看点，1 个名字 1 次高德请求（不加类型过滤）；\n") +
    "· 已查过的会跳过（" + (refresh ? "这次勾了「重查」，会重查一遍" : "不重复查") + "）；\n" +
    "· 查不到的记「未命中」，不猜坐标；\n" +
    "· 名义上几分钟，可以随时取消，已查到的都留着。")) return;
  setStatus("poi-status", "正在启动…");
  try {
    const res = await postJSON("/api/poi/harvest", { refresh, only_miss: onlyMiss });
    if (!res.ok) { setStatus("poi-status", res.error, true); return; }
    setStatus("poi-status", res.note || "已在后台开跑");
    pollPoiHarvest();
  } catch (e) {
    setStatus("poi-status", "启动失败：" + e.message, true);
  }
}

function pollPoiHarvest() {
  if (poiTimer) clearInterval(poiTimer);
  const tick = async () => {
    try {
      const res = await fetch("/api/poi/harvest/status").then((r) => r.json());
      const job = res.job;
      if (!job) return;
      renderPoiJob(job);
      if (job.state !== "running") {
        clearInterval(poiTimer);
        poiTimer = null;
        setStatus("poi-status", job.state === "done"
          ? `补坐标完成：命中 ${job.hit} · 未命中 ${job.miss} · 失败 ${job.failed}`
          : (job.state === "cancelled" ? "已取消（已查到的都留着，下次接着跑）" : "失败：" + job.error),
          job.state === "failed");
        loadPois();
      }
    } catch (e) { /* 下一轮再试 */ }
  };
  tick();
  poiTimer = setInterval(tick, 1500);
}

function renderPoiJob(job) {
  const box = $("poi-progress");
  const log = $("poi-log");
  if (!box) return;
  box.hidden = false;
  const pct = job.percent == null ? 0 : job.percent;
  const eta = job.eta == null ? "" : ` · 预计还需 ${job.eta} 秒`;
  box.innerHTML =
    `<div class="cp-head">${esc(job.phase)} · ${job.state === "running" ? "进行中" : job.state}</div>` +
    `<div class="cp-bar"><i style="width:${pct}%"></i></div>` +
    `<div class="cp-meta">${job.done}/${job.total || "?"}（${pct}%）· 命中 ${job.hit} · ` +
    `未命中 ${job.miss} · 失败 ${job.failed}${eta}</div>`;
  if (log && job.logs && job.logs.length) {
    log.hidden = false;
    log.textContent = job.logs.join("\n");
    log.scrollTop = log.scrollHeight;
  }
}

/** 页面刷新/切页签后，任务还在跑就继续显示进度 */
async function pollPoiHarvestIfRunning() {
  try {
    const res = await fetch("/api/poi/harvest/status").then((r) => r.json());
    if (res.job && res.job.state === "running") {
      renderPoiJob(res.job);
      pollPoiHarvest();
    }
  } catch (e) { /* 没跑就算了 */ }
}

async function exportPois() {
  try {
    const data = await fetch("/api/poi/export").then((r) => r.json());
    if (!data.ok) { setStatus("poi-status", data.error || "导出失败", true); return; }
    const blob = new Blob([data.json], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `城内点位_${defaultDate()}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    setStatus("poi-status", `已导出 ${(data.stats || {}).hit || 0} 个已补坐标的看点`);
  } catch (e) {
    setStatus("poi-status", "导出失败：" + e.message, true);
  }
}

if ($("poi-run")) {
  document.documentElement.dataset.poiWired = "1";   // 探针用它确认这块绑定真的跑到了
  $("poi-run").addEventListener("click", startPoiHarvest);
  $("poi-go").addEventListener("click", () => loadPois());
  $("poi-only-miss").addEventListener("change", () => loadPois());
  $("poi-city").addEventListener("keydown", (e) => { if (e.key === "Enter") loadPois(); });
  $("poi-more").addEventListener("click", () => loadPois(true));
  $("poi-export").addEventListener("click", exportPois);
  $("poi-cancel").addEventListener("click", async () => {
    const res = await postJSON("/api/poi/harvest/cancel", {});
    setStatus("poi-status", res.ok ? "已请求取消，当前这个查完就停" : res.error, !res.ok);
  });
}

/* ---------------- 城市停留动线（B+：散文 → 带坐标的动线） ---------------- */
let guideCity = "";

async function loadGuideCity(city) {
  const box = $("guide-days");
  if (!box) return;
  const name = (city || $("guide-city").value || "").trim();
  if (!name) { setStatus("guide-status", "先填一座城市", true); return; }
  guideCity = name;
  $("guide-city").value = name;
  setStatus("guide-status", `正在对齐「${name}」的动线…（首次打开会自动抓 4 页 POI 池）`);
  box.innerHTML = "";
  try {
    const data = await fetch("/api/guide/city?city=" + encodeURIComponent(name)).then((r) => r.json());
    if (!data.ok) { setStatus("guide-status", data.error || "取不到", true); return; }
    setStatus("guide-status", data.pool_built
      ? `已抓 POI 池 ${data.pool_built.added} 条（池子共 ${data.pool} 个点位）`
      : `池子 ${data.pool} 个点位`);
    const s = $("guide-summary");
    if (s) {
      const drawable = (data.days || []).filter((d) => d.drawable).length;
      s.textContent = `${data.city}：${data.days.length} 天动线（建议 ${data.days_count} 天），` +
        `对上 ${data.matched_total} 个带坐标的点位 · 能画线的 ${drawable} 天 · ` +
        `POI 池 ${data.pool} 个 · ${data.summary || ""}`;
    }
    box.innerHTML = "";
    (data.days || []).forEach((d) => box.appendChild(guideDayCard(d)));
    if (!(data.days || []).length) {
      box.innerHTML = `<div class="status">这座城市还没有攻略动线（城市目录里没有 guide）</div>`;
    }
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = data.note || "";
    box.appendChild(note);
  } catch (e) {
    setStatus("guide-status", "失败：" + e.message, true);
  }
}

/** 一天一张卡：先看对齐结果，点「算这段路」才联网查市内腿并画图 */
function guideDayCard(day) {
  const el = document.createElement("div");
  el.className = "rt-card guide-day" + (day.drawable ? " is-loop" : "");
  const chips = (day.stops || []).map((s, i) =>
    `<span class="guide-stop" title="${esc(s.kind || "")} ${esc(s.address || "")}">
      <b>${i + 1}</b> ${esc(s.name)}</span>`).join("");
  el.innerHTML =
    `<div class="rt-head">
      <span class="rt-caret">▸</span>
      <b>${esc(day.day)} · ${esc(day.title)}</b>
      <span class="rt-kind ${day.drawable ? "loop" : ""}">${day.matched} 个点位</span>
      <span class="rt-inline">${day.drawable ? "可以画动线" : "点位太少，只有文字"}</span>
    </div>
    <div class="guide-detail" hidden>${esc(day.detail)}</div>
    <div class="guide-stops" hidden>${chips || "（这一天没对上带坐标的点位）"}</div>
    <div class="trip-actions">
      <button type="button" class="ghost-sm gd-legs"${day.drawable ? "" : " disabled"}>
        算这段路（联网查一次）</button>
      <span class="hint gd-note"></span>
    </div>
    <div class="gd-box" hidden></div>`;
  const head = el.querySelector(".rt-head");
  const caret = el.querySelector(".rt-caret");
  const detail = el.querySelector(".guide-detail");
  const stops = el.querySelector(".guide-stops");
  const legBtn = el.querySelector(".gd-legs");
  const box = el.querySelector(".gd-box");
  const noteEl = el.querySelector(".gd-note");
  head.addEventListener("click", () => {
    const open = detail.hidden;
    detail.hidden = !open;
    stops.hidden = !open;
    caret.textContent = open ? "▾" : "▸";
  });
  legBtn.addEventListener("click", async () => {
    legBtn.disabled = true;
    noteEl.textContent = "正在查…";
    try {
      const url = `/api/guide/day?city=${encodeURIComponent(guideCity)}` +
        `&day=${encodeURIComponent(day.day)}&live=true`;
      const data = await fetch(url).then((r) => r.json());
      if (!data.ok) { noteEl.textContent = data.error || "取不到"; legBtn.disabled = false; return; }
      box.hidden = false;
      box.innerHTML = guideDayHtml(data);
      noteEl.textContent = "已算（下次直接读缓存）";
    } catch (e) {
      noteEl.textContent = "失败：" + e.message;
    }
    legBtn.disabled = false;
  });
  return el;
}

/** 一天的实际内容：示意图 + 点位顺序 + 每段怎么走（含「不是时刻表」的口径） */
function guideDayHtml(data) {
  const legs = (data.legs || []).map((l, i) => {
    const a = (data.stops || [])[i] || {}, b = (data.stops || [])[i + 1] || {};
    const how = l.minutes == null
      ? `<span class="cc-manual">没取到</span>`
      : `<span class="cc-train">${l.mode === "walk" ? "步行" : "公交/地铁"} ` +
        `${l.minutes} 分钟${l.walk_m ? ` · 步行 ${l.walk_m} 米` : ""}</span>`;
    const lines = (l.lines || []).map((ln) =>
      `<span class="cc-train">${esc(ln.line || "")} ${esc(ln.from || "")}→${esc(ln.to || "")}</span>`).join("");
    return `<div class="cc-seg"><span class="cc-mode">${i + 1}→${i + 2}</span>
      <span class="cc-path">${esc(a.name || "")} → ${esc(b.name || "")}</span>
      ${how}${lines}
      <span class="cc-plan">直线 ${l.straight_km ?? "-"} 公里${l.cached ? " · 读缓存" : ""}</span>
      ${l.note ? `<div class="cc-note">${esc(l.note)}</div>` : ""}</div>`;
  }).join("");
  return `<div class="cc-head">${esc(data.city)} · ${esc(data.day)} ${esc(data.title || "")}
      ｜点位 ${data.stops.length} 个</div>` +
    (data.svg || "") +
    `<div class="guide-legend">图：按经纬度摆位的示意图 · 非导航</div>` + legs +
    `<div class="rt-foot">${esc(data.note || "")}</div>`;
}

async function rebuildGuidePool() {
  const city = ($("guide-city").value || "").trim();
  if (!city) { setStatus("guide-status", "先填一座城市", true); return; }
  const pages = Math.max(1, Math.min(5, Number($("guide-pages").value) || 1));
  if (!window.confirm(`重抓「${city}」的 POI 池？（4 类 × ${pages} 页 = ${4 * pages} 次请求）`)) return;
  setStatus("guide-status", "正在抓 POI 池…");
  try {
    const res = await postJSON("/api/guide/pool", { city, pages, refresh: true });
    setStatus("guide-status", res.ok ? res.note : res.error, !res.ok);
    if (res.ok) loadGuideCity(city);
  } catch (e) {
    setStatus("guide-status", "失败：" + e.message, true);
  }
}

if ($("guide-go")) {
  document.documentElement.dataset.guideWired = "1";
  $("guide-go").addEventListener("click", () => loadGuideCity());
  $("guide-city").addEventListener("keydown", (e) => { if (e.key === "Enter") loadGuideCity(); });
  $("guide-pool").addEventListener("click", rebuildGuidePool);
  // 反方向的那条路：这里只带动线，完整攻略（散文版）在地图页签的城市弹层里
  const full = $("guide-full");
  if (full) full.addEventListener("click", () => {
    const city = ($("guide-city").value || guideCity || "").trim();
    if (!city) { setStatus("guide-status", "先填一座城市", true); return; }
    openCityGuide(city);
  });
}

/** 切到地图页签并打开某座城的完整攻略弹层（数据台 → 攻略 的反向入口） */
function openCityGuide(city) {
  gotoTab("map", null, 0).then(() => {
    setTimeout(() => {
      const dbg = window.DSHTravelMap && window.DSHTravelMap.__debug;
      if (!dbg || !dbg.openGuide) {
        setStatus("guide-status", "地图页还没准备好，稍后再点一次", true);
        return;
      }
      dbg.openGuide(city);
      setStatus("guide-status", `已在地图页打开「${city}」的完整攻略（Esc 关闭弹层）`);
    }, 900);
  });
}

/** 行程里某一天的「城内动线」：切到数据台并把城市填好（把停留内容接上进行程） */
function openGuideFor(city) {
  const tab = document.querySelector('button[data-tab="data"]');
  if (tab) tab.click();
  loadGuideCity(city);
}

/* ---------------- 数据台「直达」跳转条 ----------------
 * 数据台有九张卡，「城内点位」「城市停留动线」在很下面 —— 手动滑要滑半天。
 * 这条吸顶导航点一下滚到目标卡并闪一下边框；城市框还能直接跳过去并开始查动线。 */
function jumpToSection(id) {
  const el = $(id);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
  el.classList.remove("jump-flash");
  void el.offsetWidth;                       // 重新触发动画（连点同一个也闪）
  el.classList.add("jump-flash");
  if (el._jumpTimer) clearTimeout(el._jumpTimer);
  el._jumpTimer = setTimeout(() => el.classList.remove("jump-flash"), 1800);
}

async function fillJumpCities() {
  const list = $("jump-city-list");
  if (!list || list.dataset.built) return;
  list.dataset.built = "1";
  try {
    const data = await fetch("/api/cities").then((r) => r.json());
    if (!data.ok) return;
    list.innerHTML = (data.cities || []).map((n) => `<option value="${esc(n)}"></option>`).join("");
  } catch (e) { /* 候选填不上不影响手输 */ }
}

function setupDataJump() {
  const bar = $("data-jump");
  if (!bar) return;
  document.documentElement.dataset.jumpWired = "1";
  bar.querySelectorAll("button.dj").forEach((btn) => {
    btn.addEventListener("click", () => jumpToSection(btn.dataset.jump));
  });
  const top = $("jump-top");
  if (top) top.addEventListener("click", () => {
    const intro = document.querySelector("#tab-data .section-intro") || $("data-jump");
    intro.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  const go = $("jump-city-go");
  const input = $("jump-city");
  const run = () => {
    const city = (input.value || "").trim();
    jumpToSection("data-guide");
    if (!city) { input.focus(); return; }
    loadGuideCity(city);
  };
  if (go) go.addEventListener("click", run);
  if (input) {
    input.addEventListener("focus", fillJumpCities);
    input.addEventListener("input", fillJumpCities);      // 只靠 focus 会漏（直接点候选也一样）
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
    // 候选只有 239 个名字（约 3 KB），页面起来就顺手拉一次，别等用户去点输入框
    setTimeout(fillJumpCities, 1200);
  }
  // 沿页面滚动时把「当前在看的卡」在跳转条上标出来（长页面里不至于迷路）
  const cards = [...document.querySelectorAll("#tab-data .card[id]")];
  if (cards.length && "IntersectionObserver" in window) {
    const obs = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const id = entry.target.id;
        bar.classList.toggle("at-bottom", id === "data-debug");
        bar.querySelectorAll("button.dj").forEach((b) => {
          const on = b.dataset.jump === id;
          b.classList.toggle("active", on);
          if (on) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current");
        });
      });
    }, { rootMargin: "-45% 0px -45% 0px" });
    cards.forEach((c) => obs.observe(c));
  }
}

async function importRoutes(apply) {
  const text = ($("imp-text").value || "").trim();
  if (!text) { setStatus("imp-status", "先粘贴或选择素材内容", true); return; }
  setStatus("imp-status", apply ? "正在导入…" : "正在校验…");
  try {
    const data = await postJSON("/api/routes/import", {
      data: text, apply: !!apply, promote: $("imp-promote").checked,
    });
    const box = $("imp-report");
    box.hidden = false;
    if (!data.ok) {
      setStatus("imp-status", data.error || "失败", true);
      renderImportReport(box, data);
      return;
    }
    const problemCount = (data.problems || []).length;
    setStatus("imp-status", apply
      ? `已导入 ${data.routes_count || data.count} 条到暂存区${data.promote && data.promote.ok ? "，并已正式化上线" : ""}`
      : (problemCount ? `校验未通过：${problemCount} 个问题` : "校验通过，可以导入"),
      !apply && problemCount > 0);
    renderImportReport(box, data);
    if (apply) { await refreshStaged(); loadRoutes(); }
  } catch (e) {
    setStatus("imp-status", "失败：" + e.message, true);
  }
}

function renderImportReport(box, data) {
  const coords = data.coords || {};
  const rows = (data.routes || []).map((r) => {
    const bad = (r.problems || []).length;
    return `<div class="imp-row ${bad ? "bad" : "ok"}">
      <span class="imp-mark">${bad ? "✗" : "✓"}</span>
      <code>${esc(r.id || "(无 id)")}</code>
      <span class="imp-name">${esc(r.name || "")}</span>
      <span class="imp-kind">${r.kind === "loop" ? "环线" : "单向"}</span>
      <span class="imp-scores">省钱 ${r.value_score ?? "-"} · 巧思 ${r.clever_score || 0}</span>
      ${bad ? `<div class="imp-issues">${r.problems.map(esc).join("<br>")}</div>` : ""}
    </div>`;
  }).join("");
  box.innerHTML =
    `<div class="imp-head">共 ${data.count ?? (data.routes || []).length} 条 ·
     问题 ${(data.problems || []).length} 个
     ${coords.note ? `· 坐标：${esc(coords.note)}` : ""}
     ${data.file ? `· 已写入 <code>${esc(data.file)}</code>` : ""}</div>` +
    rows +
    (data.next ? `<p class="hint">${esc(data.next)}</p>` : "");
}

async function refreshStaged() {
  const box = $("imp-staged-box");
  if (!box) return;
  try {
    const data = await fetch("/api/routes/staged").then((r) => r.json());
    box.hidden = false;
    if (!data.files || !data.files.length) {
      box.innerHTML = `<div class="imp-head">暂存区是空的（导入或子代理产出会出现在这里）·
        已归档 ${data.archived || 0} 个文件</div>`;
      return;
    }
    box.innerHTML = `<div class="imp-head">暂存区 ${data.total} 条待正式化</div>` +
      data.files.map((f) => `<div class="imp-row ok"><code>${esc(f.file)}</code>
        <span class="imp-name">${esc(f.collector || "")}</span>
        <span class="imp-scores">${f.routes} 条</span></div>`).join("") +
      `<p class="hint">${esc(data.note)}</p>`;
  } catch (e) {
    box.hidden = false;
    box.innerHTML = `<div class="status error">读暂存区失败：${esc(e.message)}</div>`;
  }
}

$("imp-check").addEventListener("click", () => importRoutes(false));
$("imp-apply").addEventListener("click", () => {
  if (!window.confirm("导入到暂存区？\n\n校验通过才写入；之后点「把暂存区正式化」才会出现在路线库里。")) return;
  importRoutes(true);
});
$("imp-staged").addEventListener("click", refreshStaged);
$("imp-promote-btn").addEventListener("click", async () => {
  if (!window.confirm("把暂存区里的路线合并进正式素材？\n\n原文件会被归档到 _staging/_archive 留底。")) return;
  setStatus("imp-status", "正在正式化…");
  try {
    const data = await postJSON("/api/routes/promote", {});
    setStatus("imp-status", data.ok ? `已正式化 ${data.promoted} 条，库内现有 ${data.stats?.routes ?? "?"} 条` : data.error, !data.ok);
    await refreshStaged();
    loadRoutes();
  } catch (e) {
    setStatus("imp-status", "正式化失败：" + e.message, true);
  }
});
$("imp-file").addEventListener("change", async (e) => {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  try {
    $("imp-text").value = await file.text();
    setStatus("imp-status", `已读入 ${file.name}（${(file.size / 1024).toFixed(1)} KB），点「先校验」看看`);
  } catch (err) {
    setStatus("imp-status", "读文件失败：" + err.message, true);
  }
});

if ($("data-jump")) setupDataJump();
if ($("loop-hubs")) setupLoopHubs();

/* ---------------- 供 map.js 复用的公共能力 ---------------- */
window.TP = {
  esc, setStatus, postJSON, defaultDate,
  dayCard, legBlock, statsText,
  // 攻略 ↔ 动线 的双向入口：地图弹层里的「看城内动线」调过来（避免两边各渲染一遍同一份内容）
  openCityFlow: openGuideFor,
  gotoTab,
};
