# travel-planner 数据维护说明

> 一句话原则：**平时不用管；"换季/调图"后补一次基线；每次正式出行前，为出行日期准备当天（或同图期邻近）的时刻表。**

---

## 1. 数据全景与更新频率

| 数据 | 存在哪 | 变化频率 | 需要更新吗 |
|---|---|---|---|
| 车站表 stations | `stations` 表 | 很低（新线开通/更名，通常伴随调图） | 数月一次即可；发现"找不到某站"时先跑 `sync-stations` |
| 铁路时刻表 schedules | `schedules` 表（按日期分） | **每年约 2 次大调图**（一般 1 月上旬春运前、6 月中旬暑运前），另有零星调整与临客 | **出行前必须确认目标日期有数据**；调图后重新准备 |
| 航班数据 flights | `flights` + `flight_route_cache`（按 航线×日期） | 一年两个航季（3 月底/10 月底换季），日常班次基本稳定；**票价随时变** | 无需手动更：规划时按需自动抓取；要准票价则临行前重取 |
| 机场表 airports | `airports` | 随航班抓取自动增长 | 不用管 |
| 站坐标 geo_cache | `geo_cache` | 坐标基本不变 | 仅影响铁路票价估算精度，可跑一次 `prewarm_coords.py`（可选项） |
| 公交中转指引 transit_cache | `transit_cache` | 低（新地铁线开通后个别方案过时） | 大变化后可清缓存重取 |
| 高德/飞猪 key 状态 | meta / 控制台 | — | 高德欠费或认证失效时功能自动降级，留意提示 |

---

## 2. 铁路时刻表（唯一需要你主动管的）

### 最方便的日常用法

不想记住源日期时，直接让工具自动选择当前车次最多的基线：

```powershell
python scripts/maintain_data.py status
python scripts/maintain_data.py prepare 2026-09-20
```

一次准备多个日期，先预览再写入：

```powershell
python scripts/maintain_data.py copy 2026-06-17 2026-09-20 2026-09-21 --dry-run
python scripts/maintain_data.py copy 2026-06-17 2026-09-20 2026-09-21
```

目标日期已有数据时默认跳过/拒绝覆盖；确认要替换时显式加 `--overwrite`。这样不会因为重复点击或日期填错而误删已经爬取的结果。

### 什么时候必须更
- **国家调图之后**（一年通常 1 月、6 月各一次）→ 所有旧日期数据只能当参考；
- **每次正式出行前** → 给"出行日期"准备当天数据。

### 三种获取方式（按推荐排序）
```
# A. 导入开源每日数据集（最省事，全国 1 万+ 车次）
#    去 https://github.com/HerbertHe/cr-12306-train-info/releases 找最接近出行日期的
#    train_detail_YYYYMMDD.json（它是"该日期运行的全部车次"），下载到 data/import/ 后：
python scripts/import_dataset.py data/import/train_detail_20260617.json 2026-06-17

# B. 自己实时爬（最准；出行日期在 15 天预售期内推荐；耗时取决于车次数、网络和风控冷却）
python -m travel_planner.cli crawl-date 2026-09-20            # 首次
python -m travel_planner.cli crawl-date 2026-09-20 --resume   # 中断后续爬（风控冷却自动处理）

# C. 复制邻近日期（最快；同图期内可用；隔日/周末开行可能有误差）
python -m travel_planner.cli copy-date 2026-06-17 2026-07-15
```

旧 CLI 仍可使用；目标日期已有数据时请加 `--overwrite` 才会替换：

```powershell
python -m travel_planner.cli copy-date 2026-06-17 2026-07-15 --overwrite
```
> 说明：出行日期若在预售期外且新数据集还没出到那天，用 C（同图期复制）+ 临行前再用 A/B 复核。
> 判断是否"同图期"：调图日（约 1 月上旬 / 6 月中旬）前后分界，跨过调图日的数据不可靠。

### 日期是否齐全，一句话查看
```
python scripts/check_dates.py          # 每个日期：车次数 + 来源（导入/爬取/复制）
# 更方便的统一入口（推荐）
python scripts/maintain_data.py status
python scripts/maintain_data.py prepare 2026-09-20       # 自动选最近的完整日期
python scripts/maintain_data.py prepare 2026-09-20 2026-09-21  # 一次准备多个日期
python scripts/maintain_data.py copy 2026-09-20 2026-09-22 --dry-run  # 先预览
```
Web「数据」页签同样展示。缺日期的报错现在会自动列出已加载日期。

`maintain_data.py` 和 CLI 的复制操作默认保护已有日期，发现目标已有数据会显示“已跳过”；确认要用基准日覆盖时再加 `--overwrite`。旧的单日期写法仍可用：

```powershell
python -m travel_planner.cli copy-date 2026-06-17 2026-07-15
python -m travel_planner.cli copy-date 2026-06-17 2026-07-15 2026-07-16 --dry-run
python -m travel_planner.cli copy-date 2026-06-17 2026-07-15 --overwrite
```

---

## 3. 航班数据（基本免维护）

- 规划 `--mode mixed / air` 时按 航线×日期 **自动抓取并缓存**（飞猪 MCP，本地 SQLite）；
- 换季（3 月底、10 月底）后第一次查某航线即自动取到新班表；
- **价格**：缓存的是抓取当时的价格。临买票前想要准价，删掉该航线缓存让其重取：
```sql
-- 用 sqlite3 或本目录任意 python 连接 data/timetable.db 执行：
DELETE FROM flight_route_cache WHERE origin='北京' AND destination='上海' AND date='2026-09-20';
-- 下次 plan 会自动重新抓取
```
- 若飞猪共享接口长期失效（规划时会提示），备用方案：注册飞常准 AI 开放平台换数据源（接入点已预留）。

---

## 4. 可选的精度增强与缓存清理

```powershell
# 全国车站坐标预取（提升铁路票价估算精度；可选，跑一次约 20 分钟）
python scripts/prewarm_coords.py
python scripts/check_geo.py            # 查看坐标覆盖率

# 公交中转指引缓存（地铁线路图大改后想重新查询时）
# DELETE FROM transit_cache;  即可全部重取（走高德配额，个人 15 万次/月，够用）

# 空间回收：删除太久以前的日期数据
# DELETE FROM schedules WHERE date < '2026-01-01';
```

---

## 5. 城市资料与地图数据（基本免维护）

「地图漫游」用到的城市资料与地图几何，跟时刻表完全解耦，坏了也不影响规划。

| 数据 | 存在哪 | 什么时候动它 |
|---|---|---|
| 城市旅游资料（运行时） | `data/tourism.db` 的 `city_tourism` 表 | 随时改；内置目录**只补空字段**，不覆盖你的编辑 |
| 内置城市目录 | `src/travel_planner/data/city_catalog.json` | 想加/删城市时（239 城 / 10 大区） |
| 大区划分 | `src/travel_planner/data/regions.json` | 极少：改大区名、配色、包含哪些省 |
| 经典环线 | `src/travel_planner/data/loop_presets.json` | 想加自己的线路时（26 条） |
| 省级边界面 | `web/data/china.geo.json` | 区划调整后重跑抓取脚本 |
| 山川湖河示意层 | `web/data/china.nature.json` | 想加河流/湖泊/山脉时直接编辑（手绘示意，非精确数据） |
| 3D 渲染库 | `web/vendor/three.module.min.js` | 基本不用动；升级步骤见本节末尾 |
| 城市中心点底稿 | `src/travel_planner/data/city_coords.json` | 随边界脚本一起更新 |
| 城市白天照 | `web/img/cities/<城市名>.jpg` | 239 城已全覆盖；换图直接覆盖同名文件 |
| 城市夜景照 | `web/img/cities/<城市名>@night.jpg` | 96 座夜游城市；「更适合晚上玩」的判定见 build_city_catalog.py 的 NIGHT_EXTRA |
| 照片清单 / 来源 | `web/img/cities/photos.json` · `credits.json` | 由抓图脚本自动生成，不用手改 |
| 完整攻略文案 | `data/city_guides/<大区>.json` | 想改攻略时改这里，再跑一次 build_city_catalog.py |
| 环线自驾资料 | `src/travel_planner/data/drive_presets.json` | 26 条环线的里程/公路/季节/提醒/取还车 + 租车价格模型与城市档位 |
| 车程缓存 | `timetable.db` 的 `drive_route_cache` | 高德驾车结果按城市对缓存；想重取加 `"refresh": true` 或删表 |

改一座城市「怎么玩」最快的办法：直接改数据库，网页与 CLI 立刻生效。

```powershell
# 用任意 SQLite 工具或 python 连接 data/tourism.db：
UPDATE city_tourism SET halfday_plan='上午拙政园—苏州博物馆（预约）—平江路'
WHERE city='苏州';
# 新增一座城市
INSERT OR REPLACE INTO city_tourism(city,recommendation_score,intro,halfday_plan,tags,region)
VALUES('某某','8.0','简介','半日路线','标签','江南');
```

改了 `src/travel_planner/data/city_catalog.json`（或 `data/city_content/*.json` 文案）之后重建目录：

```powershell
python scripts/build_city_catalog.py              # 合并文案+坐标+铁路可达性，并打印校验报告
python scripts/build_city_catalog.py --check-only  # 只校验不写入
# 重建后让 data/tourism.db 吸收新城市：目录签名变了会自动重新播种（不覆盖已有内容）
```

地图几何（省级轮廓）更新：

```powershell
python scripts/fetch_china_geo.py                 # 从 DataV.GeoAtlas 重新抓取并简化
python scripts/fetch_china_geo.py --min-island-area 0.003   # 保留厦门岛、澎湖这类小岛
```

3D 渲染库（three.js，已内置，一般不用动）：地图用 WebGL 渲染，库文件在 `web/vendor/three.module.min.js`。
要升级版本时重新下载同名文件即可（版本号写在文件内的 REVISION 常量里）：

```powershell
# 任选一个能访问的源（国内一般 jsDelivr / npmmirror 可用）
Invoke-WebRequest "https://registry.npmmirror.com/three/-/three-0.160.1.tgz" -OutFile "$env:TEMP\three.tgz"
tar -xzf "$env:TEMP\three.tgz" -C "$env:TEMP" package/build/three.module.min.js
Copy-Item "$env:TEMP\package\build\three.module.min.js" web\vendor\three.module.min.js -Force
node scripts/check_map.mjs                       # 升级后必须跑一遍冒烟测试
```

`web/legacy/map.canvas2d.js` 是本项目早期的 Canvas 2D 地图实现（不参与运行，留作参考/应急）。
如果浏览器没有 WebGL，页面会明确提示原因；开启硬件加速（chrome://gpu）后刷新即可。

城市照片（可选；维基媒体在国内通常直连不通，不通就用矢量海报，不影响使用）：

```powershell
python scripts/fetch_city_photos.py --only 杭州 苏州 敦煌      # 先试几个城市
python scripts/fetch_city_photos.py --limit 20                 # 只做前 20 个
python scripts/fetch_city_photos.py                            # 全量补齐（已存在的会跳过）
python scripts/fetch_city_photos.py --night-only --force       # 重抓夜景
```

抓取源是 360 图片搜索（Bing 图片兜底），图片统一裁成 960×640、JPEG q86（约 60-160 KB），
来源 URL 与标题记在 `web/img/cities/credits.json`（仅供个人本地使用）。
**换成自己的照片**：按 `<城市名>.jpg`（夜景 `<城市名>@night.jpg`）命名丢进 `web/img/cities/`，
脚本默认不覆盖已存在的文件；删掉照片即回到内置矢量海报。换图后 `photos.json`/`credits.json`
里的记录不影响显示（服务端按文件是否存在判断），URL 带 mtime 版本号，浏览器不会用旧缓存。

完整攻略文案在 `data/city_guides/<大区>.json`，结构为：

```json
{ "region": "江南", "guides": { "杭州": {
  "days": 2, "summary": "…", "itinerary": [{"day":"D1","title":"…","detail":"…"}],
  "transport": "…", "stay": "…", "eat": "…", "tips": ["…"], "budget": "…" } } }
```

改完跑 `python scripts/build_city_catalog.py` 合并进目录（会打印「完整攻略 N/239 城」），
重启服务后生效。数据库里 `city_tourism.guide` 非空时会优先于内置目录（想覆盖单城攻略改库即可）。

### 地球模式的数据（世界城市 / 环线 / 世界之最）

| 数据 | 存在哪 | 说明 |
|---|---|---|
| 世界城市文案 | `data/world_content/*.json`（可多份，按 name 合并） | 每城含坐标、时差、半日/一日、看点、美食、提示与完整攻略 |
| 世界经典环线 | `data/world_content/world_loops.json` + `scripts/extend_world_loops.py` | 20 条：陆地 14 + **海上 3**（地中海邮轮 / 波罗的海峡湾 / 南太平洋跳岛）+ **极地 3**（南极半岛 / 斯瓦尔巴 / 格陵兰冰峡湾）；每条带 `env` 与 `env_color`，`cities` 必须都能在城市文案里找到 |
| 海上/极地新城市攻略 | `scripts/extend_world_guides.py` | 乌斯怀亚、朗伊尔城、罗瓦涅米、摩尔曼斯克、南极半岛、努克 —— 补 `highlights`/`food`/`note`/`guide`（`extend_world_loops.py` 只写了简介，不补这份 `guide` 就是空的） |
| 世界之最 | `data/world_content/world_records.json` | 30 条，四类：自然 / 工程 / 人文 / 气候 |
| 运行时目录 | `src/travel_planner/data/world_catalog.json` | 由脚本生成，不要手改 |
| 世界照片 | `web/img/world/<城市名>.jpg`、`<城市名>@night.jpg`、`record_<id>.jpg` | 抓图脚本产出，可自己覆盖 |

```powershell
python scripts/build_world_catalog.py     # 合并 + 校验（坐标范围、攻略天数与行程条数、环线引用、分类）
python scripts/extend_world_loops.py --dry-run   # 看会加哪些环线/城市（环境色 + 海上极地）
python scripts/extend_world_guides.py --dry-run  # 看会给哪几座新城市补完整攻略
python scripts/fetch_world_photos.py      # 抓照片（世界城市白天/夜景 + 世界之最），已存在的会跳过
python scripts/fetch_world_photos.py --only 东京 巴黎 珠穆朗玛峰   # 只补几张
python -m unittest tests.test_world -v    # 世界数据与行程单的单测
node scripts/check_globe.mjs              # 地球模式前端冒烟测试（假 globe.gl，不需要 WebGL）
node scripts/check_globe_env.mjs          # 真浏览器：环线环境色 + 球面线条可见度（含截图）
node scripts/check_contrast.mjs           # 真浏览器：逐页签算 WCAG 对比度（含 Ultra 面板、地球环线列表、下拉选项）
node scripts/shot_ui.mjs                  # 只截图：跳转条 / 跳转落点 / 隐藏图标前后 / 摊开的下拉选项
node scripts/check_ultra.mjs              # 真浏览器：环线 Ultra 版按钮 → 面板 → 卡片 → 筛选 → 大环线 → 城内动线
```

**配色两条硬规矩**（都踩过，改前端时别破）：

1. **填充色与文字色分开**：`--primary`（#0d6a63）只用于按钮底与边框，文字一律用 `--primary-ink`（#0a544e）。
   青绿当小字号文字用，白底上只有 5.4 的对比度 —— 达标但费眼，用户的原话是「蓝色字体看不清」。
2. **原生下拉的选项要自己带颜色**：深色工具栏上的 `select` 是浅色字（在深底上才对），
   但展开的选项列表是白底，选项会继承那个浅色 → 白底白字，看着像没渲染出来。
   所以 `select option` 必须显式写死 `color` + `background`；对比度探针里有专项检查 + 自检（塞回旧样式必须被抓到）。

改完文案要跑一次 `build_world_catalog.py`（它会重写 `world_catalog.json`），
然后重启服务或刷新页面即可（目录按 mtime 自动重载）。世界行程单只给距离/飞行时间/时差/天数，
**不含机票价**——世界航班票价必须实时查询。

地球页签的交互约定（改 `web/globe3d.js` 前先看这段）：

- **布局是三栏，且两栏在地球外面**：`web/index.html` 里 `.globe-shell` 是 grid —— 左栏「世界经典环线」，
  中栏 `.globe-stage`（3D 窗口，内含 `#globe-viz` 与城市卡），右栏「世界之最 + 已选城市」；
  两栏落在页面白底上，栏宽由 `--globe-side`（默认 286px）控制。地球占满可视宽度（`.globe-band` 做
  breakout，和地图页签同款），所以两栏正好在左右留白里。**面板不能放回 `.globe-stage` 里**
  （曾经压在地球上，也遮住城市卡）；测试 `test_globe_panels_sit_outside_the_3d_stage` 会盯着这一点。
  **卡片坐标是相对 `#globe-viz` 算的**，所以 `.map-cards` 必须留在 `#globe-viz` 内。
  侧栏面板用浅色皮肤（`.globe-col .map-panel` 覆盖了深色那套），窄于 1240px 时两栏回到地球下面。
- **全屏**：工具栏「⛶ 全屏」给 `.globe-stage` 加 `.full`（原生全屏不可用时用 CSS 兜底），
  `.globe-shell:has(.globe-stage.full)` 会让两栏 `display:none`；进/出全屏、切页签后都要调
  `fitCanvas()` 重新量一次画布（globe.gl 只按容器宽高渲染，0 或旧尺寸会变形）。
- **经度字段**：目录与数据库里叫 `lon`，globe.gl 的点位属性叫 `lng`。
  `worldmap.city_card()/record_card()` 会同时给出 `lon` 与 `lng`，前端统一读 `lng`；
  改接口字段时两边要一起改，否则点位与城市卡片会静默落到 `undefined`（曾经出过一次）。
- **卡片定位**：`worldOf()` 必须复刻 three-globe 的坐标换算
  `world = R·(cosφcosθ, sinφ, −cosφsinθ)`（东经/北纬为正），屏幕坐标走 globe.gl 的
  `toScreenPosition()`；两者不同源就会「卡片与点错位」。
- **正反面判定**：不能用投影矩阵，地球背面的点也会拿到合法屏幕坐标。判据是几何半球：
  点的世界坐标与相机位置的点积 > 0 才朝向镜头（`isFront()`）。
- **视角**：点环线 / 点世界之最 / 点城市列表的「◎」都会调 `pointOfView()` 飞过去，
  高度按城市跨度取（`fitView()`，约 40° 跨度对应 1.0 个球半径）；跨 ±180° 的环线用
  `lonWindow()` 取最紧凑的经度区间，否则跨太平洋环线会被算成 350° 宽。
  相机飞行会先关掉自转（按钮文案同步），Esc 依次退出：弹层 → 全屏 → 世界之最 → 环线 → 已选城市 → 全球视角。

### 省钱 / 舒适 / 巧思路线库（`data/route_content/*.json` + `routes.py`）

收的是「有人真走过、值得一说的走法」：什么时候坐夜车、哪段买硬座、住城外更划算，
以及**别出心裁**的那类（稍微多花钱、但顺路多玩几个地方）。
**价格是公开攻略的区间，不是实时票价** —— 每条都有 `source`、`confidence` 与全局 `updated`（口径日期）。
规划页出结果后会按城市推荐相关路线；数据台里可按城市/分类/主题筛选。

```powershell
python scripts/build_route_catalog.py           # 合并素材 + 校验 + 写运行时目录
python scripts/build_route_catalog.py --check   # 只校验（改完素材先跑这个）
python scripts/build_route_catalog.py --check --include-staging   # 连暂存区一起校验
python -m unittest tests.test_routes -v         # 加载/打分/筛选/落地核实的单测（31 项）
```

**三个独立评分维度**：

| 维度 | 算法 | 用途 |
|---|---|---|
| `value_score` 省钱 | 「每小时花费」为主 + 时间惩罚 + 舒适度小幅加成（±8） | 找性价比 |
| `comfort_score` 舒适 | 作者给（0-100） | 找舒服的 |
| `clever_score` 巧思 | `clever_tags` 白名单逐项加分 + `clever_extra`，上限 100 | **专救「多花点钱但顺路多玩」** |

为什么巧思要单列：同一段北京→拉萨，多花 300 元在西安下车逛两天、或在西宁停一晚看青海湖，
**性价比分反而更低**（更贵更久），只按性价比排序会把它们沉底 —— 这正是「三倍阈值」想解决的问题。
白名单与分值：`extra_stops` 顺路多玩 30、`detour_worth` 绕路值得 22、`night_move` 夜车 22、
`cheap_upgrade` 同价升级 20、`shoulder` 错峰 16、`combo_ticket` 联程接续 16、`local_trick` 本地经验 16、
`season_window` 抓窗口 14。**巧思 ≥70 必须写 `smart_tips`、≥60 必须写 `cost_baseline` + `extra_value`**，校验器强制。

省钱分标定按真实价位：普速硬座 5-10 元/小时 → 80-100；普速硬卧 15-25 → 70-85；
高铁 50-120 → 45-75；机票 100-200 → 45 上下。踩过两次坑：① 只按「总价÷时长」线性折算，
1200 元/40 小时算出 89 分（长行程被奖励）；② 阈值定太低，把高铁全打到 10 分以下。

**素材字段**（`data/route_content/*.json`，可多份按 id 合并）：

| 字段 | 说明 |
|---|---|
| `id/name/from_city/to_city/category/tier/themes` | 基本信息；`tier` 是 省钱/舒适/观景/打卡 |
| `segments[]` | `{frm,to,mode,hours,price,note}`；`mode` 限 train/rail/high_speed/bus/car/boat/fly/walk/metro |
| `alternatives` | 分段是**多种走法**（如桂林→阳朔有动车/大巴/游船）时置 `true`，校验器不按「分段相加」核对时长 |
| `loop` | 环线（起终点同城）必须置 `true`，否则被当成填错 |
| `total_hours/cost_low/cost_high` | 最快走法的耗时；最省～最贵区间 |
| `cost_baseline/extra_value` | 直达基线价 + 「多花的钱换到了什么」（巧思路线必填） |
| `clever_tags/clever_extra/smart_tips` | 巧思标签（白名单）+ 额外加分 + 「妙在哪」 |
| `save_tips/comfort_tips/fit_for/watch_out/best_season` | 页面正文 |
| `source/source_url/confidence` | 来源与可信度（high/medium/low），**必填** |

**收集流程（子代理 → 先审后收）**：

1. 子代理只写 `data/route_content/_staging/*.json`（按主题分工：顺路多玩 / 夜车与窗口期 /
   同价升级与本地经验 / 错峰与环线 / 各城市窍门）；
2. `build_route_catalog.py --check --include-staging` 校验；**暂存区不允许覆盖已审素材**（同 id 直接报错）；
3. `python scripts/promote_staged_routes.py` 合并成正式素材 `smart_routes.json`，
   原始文件归档到 `_staging/_archive/`（留底可追溯）；
4. 再跑不带 `--include-staging` 的构建。

**网页一键导入（`route_import.py` + 数据台）**：粘贴或选一个 JSON → 先校验（同一套规则）→
写进暂存区 → 点「把暂存区正式化」上线。接口：`POST /api/routes/import`（`apply=false` 只校验）、
`GET /api/routes/staged`、`POST /api/routes/promote`。导入会自动补缺坐标（见下）。

**规模与展示**：库内 202 条素材（目录 212 条，其中 **分段串线 30 条** —— 见下）时，页面**分页 + 折叠**：
首屏 12 条（`/api/routes?limit=12`），每张卡默认只显示一行摘要（名称 / 单向环线 / 三档评分 /
城市 / 时长 / 价格区间），点标题展开细节，另有「全部展开/收起」「载入更多」。
`kind=loop|oneway` 可单独筛，`sort=clever` 按巧思排。

**分段串线（大动脉分段走法）**：这一类就是用户说的「直达 20 多小时，不如拆成几段一路玩过去」——
一条长途直达拆成 4-8 段、每段 2-8 小时，覆盖 5-9 个城市。现有 30 条，覆盖京沪、京广、京九、
陇海—兰新、京昆/宝成—成昆、沪昆、京哈/哈大、沿海、青甘/川西/滇西九条走向，北京 16 条 + 河北 14 条。
写法约束（校验器强制）：`segments` 按顺序排且 `total_hours` ≈ 各段之和（`alternatives` 为 true 时才不算和）、
`name` 必须是「主题·起终点」形式、价格给区间并在 `source` 里写口径日期、**不许写车次号与精确时刻**。
成对的「慢串 / 快串」「高铁 / 夜车硬卧」是故意的：正好对应页面的性价比与巧思两种排序。

**路线 → 一键变行程（`concrete.plan_trip` + `POST /api/routes/trip`）**：这是与规划页**同一个引擎**，
所以它能排出素材没写、但时刻表里确实存在的换乘方案：

1. `route_stops()` 把分段里的地名抽成停靠序列 —— **站名归一成城市**（南昌西→南昌），
   环线去掉重复的尾站；
2. `stays_for()` 决定每站住几晚：页面可传 `stay_mode`（`transit`/`one_night`/`two_nights`），
   不传就按 `total_hours` 推断（≤12h 一日游不过夜 / ≤36h 一晚 / 更长两晚），
   `note` 里写「住/过夜」的站点至少一晚；
3. `do_loop(free_order=False)` 排行程 —— **不重排顺序**，因为参考路线的顺序本身就是它的价值；
4. 返回按天铺开的车次（`days[].journey.legs[]`：车次号/发到/席别/票价）+ 合计天数与票价。

边界处理（都实测过）：
- 分段里有**没有铁路站的小镇**（黑马河、北极村）→ 明确报「这些地点在车站表里找不到」，不让引擎报含糊错；
- 同城选站类条目（只涉及一个城市）→ 报「排不出行程」并说明原因；
- 那一天**没有数据**时，引擎会自动从基准日拷一份来算 —— 这时会带一条醒目警告
  「下面的车次是从基准日 X 自动拷贝来的，**不是当天的真实车次**」（最容易误导人的地方）。

坑位（这一轮新踩的）：
- **`stats()` 被重复定义**：新版本写在前面、旧版本留在文件尾，后者静默覆盖前者，
  页面上「单向/环线条数」就是空的。已删重复；`tests/test_routes.py` 里有 kind 计数断言兜住。
- **`web.py` 用了 `json` 没 import**：导入接口直接 500。已加，并顺手跑了一遍
  「模块里用到但没导入的常见内置名」扫描。
- **省份包围盒太粗**：长白山被查到新疆、东兴市被查到南宁 —— 同省不同角落照样错。
  解法是 `HINTS` 里加精确参考点 + 80 公里偏差校验；过滤只能丢弃不能纠正，
  所以少量关键点仍靠 `fix_route_coords.py` 人工核定。
- **子代理改完文件落到了归档路径**（`_archive/smart_east_china.json` 比已合并版本新）：
  合并前先 diff 一遍归档版，把修正合并回正式素材，然后把该文件改名
  `*.applied.json` 标记「已合并」，避免下次正式化再撞车（否则会报「与已审素材冲突」）。
- **城市名降级查找有三档**：精确 → 去尾 1-3 字（北京西/上海虹桥）→ **前缀匹配**
  （大理 → 大理白族自治州、伊犁 → 伊犁哈萨克自治州）。第三档取最短候选，
  且候选至少 2 个字（否则「上海」会被「上」抢走）。
- **境外城市高德查不到**（它是境内服务，且会被省份粗筛拒掉）：`fill_route_coords.py` 的
  `MANUAL` 里核定常用坐标，`OVERSEAS` 集合跳过省份校验。部分景点名高德直接报
  `ENGINE_RESPONSE_DATA_ERROR`（亚龙湾、鄱阳北、防川），也只能核定。
- **坐标文件要定期精简**：`python scripts/fix_route_coords.py --prune` 删掉不再被任何路线引用的条目。
  修剪时必须把 `_staging/_archive` 与 `src/.../data` 一起扫 —— 正式化之后素材都在归档目录里，
  只扫顶层会把仍在用的坐标误删（踩过一次）。
- **合并前先 diff 归档目录**：子代理可能在你合并之后又改过文件（写过 `_archive/xxx.json` 的修正版），
  把它当新素材重新走一遍流程，并把该文件改名 `*.applied.json` 标记已合并，避免下次撞车。

**落地核实（`concrete.py` + `POST /api/routes/concrete`）**：把参考路线落到某一天，逐段核实 ——
火车段查本地时刻表（车次号/发到点/历时）、自驾段走高德驾车（里程/耗时/过路费）、
大巴/船/飞机标成「需自查」（没有公开可查的班次，**不假装知道**）。每段三态 `ok / no_data / manual`，
页面分开显示；环线（起终点同城）不会当成两地车程去问高德。

坑位：
- 县市级目的地（敦煌、阳朔、格尔木、集宁、额济纳、篁岭…）在 DataV 市级目录里**没有**坐标：
  `python scripts/fill_route_coords.py` 先查本地 239 城目录，缺的用高德**地理编码**补齐
  （与驾车规划同一个 key，只换 endpoint），结果写 `city_coords_routes.json`。
- **高德对景点名会跨省张冠李戴**（实测：黄龙九寨→陕西、雪乡→西藏、柳园→广东、莫高窟→深圳、
  北极村→青海、青石嘴→广东）。所以脚本加了 ① `HINTS` 约束城市、② 省份包围盒粗筛、
  ③ 偏离参考点即丢弃；剩下少量关键点用 `scripts/fix_route_coords.py` 人工核定。
- 高德 Web 服务有 QPS 限制（`CUQPS_HAS_EXCEEDED_THE_LIMIT`）：脚本按 1s/2s/3s 退避重试；
  批量补坐标用 `--delay 1.2` 更稳。
- 城市名要归一：素材里会出现「北京西」「上海虹桥」「西宁（环湖）」「宏村/汤口」。
  `routes._clean_city()` 去括号/斜杠/「站」；`routes.coord_of()` 再做「逐步去掉尾部 1-3 字」的
  降级查找（长度不足 2 的候选一律不接受，避免把「上海」配到「上」）。
- 坐标文件两种格式：`city_coords.json` 是对象数组（`name`/`short`/`lon`/`lat`），
  `city_coords_extra.json` 与 `city_coords_routes.json` 是 `{"cities": [...]}`；`routes._load_coords()` 都吃。
- 素材改了必须重跑构建脚本，`tests/test_routes.py` 会核对「素材条数 == 目录条数」，忘记跑会红。
- 构建/补坐标脚本开头把 stdout/stderr 切成 UTF-8：Windows 控制台默认 GBK，打印 `✓` 会 UnicodeEncodeError。

### 环线 Ultra 版（`data/route_content/ultra_loops.json` + `ultra.py`）

现有路线库偏「单点走法 + 攻略」，适合单程；**整条环线出行**要的是另一份：几天走完、每天住哪花多少、
哪里能省。这份数据集只收北京出发与河北出发（河北任意城市都收）的**穷游环线**，64 条，覆盖 12 个出发地。

| 字段 | 说明 |
|---|---|
| `id/name/origin/origin_province/days` | 出发地必须写明省份（北京 / 河北），页面按它分组筛选 |
| `scale` | `large` = 大环线（跨省、≥8 天、≥6 城、有 `hub_line`）；`small` = 省内/周边几站。老素材不写就按 small 算 |
| `hub_line` | 走的是哪条大动脉（京沪线 / 京广线 / 京九线 / 陇海—兰新线 / 沪昆线 / 京哈线 / 杭深沿海线…） |
| `budget_low/budget_high/transport[]/cities[]/regions[]` | 总价区间（公开攻略区间，**不是实时票价**）、交通方式、途径城市 |
| `itinerary[]` | `{day,city,title,detail}` —— 逐日安排：哪天在哪、住哪、花多少、怎么走 |
| `highlights/save_tips/comfort_tips/fit_for/best_season/watch_out` | 看点 / 怎么省 / 怎么舒服点 / 适合谁 / 季节 / 注意什么 |
| `source/source_url/confidence` | 来源与可信度（high/medium/low）；**低可信度必须写明不确定在哪** |

**大环线的判定标准**（用户给的那把尺子，构建器会强制）：
「张家口到杭州，直达 20 多小时，可以先到济南再到南京，然后到无锡，再到苏州，去上海，最后到杭州」——
**把一条长直达拆成若干「每段 2-8 小时」的落地点，一路串下去，最后回到出发地**。
所以 `scale: "large"` 必须同时满足：≥8 天、≥6 城、首尾闭环、写明 `hub_line`。

```powershell
python scripts/build_ultra_loops.py            # 校验 + **增量合并** _staging/ultra_loops_*.json，输入归档
python scripts/build_ultra_loops.py --check    # 只校验（暂存区空时就体检已收录的那批）
python scripts/check_ultra_library.py          # 独立体检：闭环 / 逐日条数 / 重复收口日 / 车次号 / 预算口径
python scripts/check_ultra_staging_sync.py     # 暂存区那份和正式文件是否一致（归档前先跑，别拿旧版覆盖新版）
python scripts/fix_ultra_budgets.py --dry-run  # 修正被压低的预算（见下）
python -m unittest tests.test_ultra -v         # 13 项：字段完整性 / 大小环线 / 天数与逐日条数 / 预算区间 / 车次号
node scripts/check_ultra.mjs                   # 真浏览器：按钮 → 面板 → 卡片 → 筛选 → 大环线 → 城内动线
```

**合并是「增量」的**：脚本先把已收录的 `ultra_loops.json` 读回来，再把暂存区并进去。
早期版本只读暂存区，第二次合并会把上一批素材整批冲掉（34 条 → 30 条），暂存区 id 与已收录撞车时直接报错。

**别为了让测试变绿去改数据**：有一次子代理把 14 天海南线压到 3600-5200 元（314 元/天）来迁就
当时「小环线日均 320」的测试上限，而公开攻略口径的往返机票 + 岛上 14 天要 4200-6000 元。
正确做法是**测试按大小环线分开定上限**（现在 large 480 / small 320），数据回到真实区间
（`scripts/fix_ultra_budgets.py` 就是干这个的，改的是数据不是测试）。

**校验器会扫车次号与时刻写法**（`G1234`、`D1`、`08:15` 这种），命中就报错 —— 因为这份数据明确
**不写车次号与班次时刻**（那是时刻表引擎的活，写死在攻略里就会过期）。扫描前先剥掉 `day` 标签、
`source` 文本与 URL，否则 `D1` 与网址片段会全部误报（踩过）。**注意 detail 正文里也别写「D9」**
（会被当成 D 字头列车），改写「返程当天 / 第 9 天」。

页面交互（改 `web/app.js` 里 `loadUltra` 一段前先看）：

- **不另开界面**：`#l-ultra` 按钮 → `#ultra-panel` 展开，同时把 `#l-results`（普通环线结果）`hidden`，
  再点一次收起。开关状态在按钮文案上（「环线 Ultra 版」↔「收起 Ultra 版」）。
- **筛选请求要编号**：出发地 / 天数 / 预算 / 范围四个控件连着改会并发发请求，**慢的旧响应会盖掉新结果**
  （表现为「筛选没生效」）。`loadUltra()` 用一个 `ultraSeq` 令牌，只认最新一次的返回。
- **详情里的「看城内动线」按钮类名是 `.ud-flow`**，由 `bindGuideButtons()` 与 `.gd-open` 一起绑定；
  曾经只绑 `.gd-open`，于是那个按钮点了毫无反应。

### 断点续爬：进度按 OD 成员落盘（`crawl_progress.py`）

**用户的原话是「中断了没有接着爬的功能吧，得重新爬」** —— 之所以像没有，是两个原因凑一起：
进程一重启（断电/关窗口）内存里的任务就没了，页面**什么痕迹都看不到**；而旧断点只记「第几对 OD」，
换个爬取范围就对不上号。现在两件事都修了。

| 账本（SQLite） | 记什么 |
|---|---|
| `crawl_pairs(date, od, status, trains, tries, updated_at)` | **按 OD 成员**记账：开爬前写 `running`，做完写 `done`/`failed` 并立刻 commit |
| `crawl_runs(date, planned, state, discovered, ingested, started_at/updated_at/finished_at, note)` | 这一天的总账，页面据此显示「已完成 596/6006，剩余 5410」 |

要点（都是踩过才写下来的）：

- **按成员、不按序号**：全国爬到第 596 对中断，之后换「定向爬取」（另一份列表）续爬也不会错位跳空
  或重复爬 —— 旧实现 `pairs[596:]` 会切出空列表，跑完报「入库 0 趟」，看着就像没续爬。
- **先记账再请求**：断电时正在做的那一对留在 `running`，续爬会**重试**它（没确认完成就该重来）。
- **旧格式断点只在能安全翻译时迁移**：`crawl_state_<date>.txt` 里只有序号；全国爬取的 OD 列表是
  确定性的（枢纽城市名排序后嵌套枚举），所以能翻译成成员记账；定向爬取的列表每次重算，翻译就是错的，
  宁可不迁移。页面与 CLI 会把「有旧断点残留」如实说出来，不装作没爬过。
- **面包屑文件原子写**：`crawl_state_<date>.txt` 现在只是给人看的一行字（临时文件 + `replace`），
  写坏了也不影响续爬；`discovered_<date>.jsonl` 追加式，读到半行就跳过。
- **界面**：数据台「真爬某一天」下面有一条进度横幅（来自数据库，不是内存），显示已完成/剩余/停在哪、
  失败几对、已发现多少趟、已入库多少趟，带「接着爬剩下的 N 对」「看逐对明细」两个按钮；
  没有成员账但有旧断点时，显示一条灰色提示 + 「按 OD 重新核对这一天」。

```powershell
python -m travel_planner.cli crawl-progress 2026-10-10 --rows 5   # 看进度与逐对明细
python -m travel_planner.cli crawl-date 2026-10-10 --resume        # 接着爬（自动只补没做完的）
python -m unittest tests.test_crawl_resume -v                      # 11 项：中断/断电/换范围/旧断点迁移
python scripts/dev_probe_crawl_resume.py                           # 手工走一遍中断→续爬（假 client，不联网）
python scripts/purge_probe_crawl.py --date 2099-01-01              # 清理探测留下的假日期（探针踩过一次）
```

⚠️ **写浏览器探针时注意**：前端 `startCrawl` 调用的是模块内的 `postJSON`，在页面里替换
`window.TP.postJSON` **拦不住**它 —— 探针点一下「开始真爬」就会真的向 12306 发请求。
`dev_probe_data.mjs` 里的做法是把 `window.confirm` 打桩成返回 `false`，让流程停在确认框上。

### 报告书与验证流水线（`collect_evidence.py` → `build_report.py`）

改完东西怎么「一次说清现在到底行不行」：

```powershell
python -X utf8 scripts/collect_evidence.py     # 单测 + 9 个浏览器探针 + 6 项数据体检 → data/report_evidence.json
python -X utf8 scripts/build_report.py         # 读证据 + 直接问运行时 → 项目报告书.html
python -X utf8 scripts/check_report.py         # 报告体检（结构/自包含/正文无占位）
node scripts/shot_report.mjs                   # 渲染截图确认排版
python -X utf8 scripts/show_report_section.py  # 打印某几节纯文本，便于快速核对
```

规矩只有一条：**报告里的数字必须来自脚本**（证据 JSON 或运行时），不许手写。
`collect_evidence.py` 会把每条探针的原始输出整段存下来，报告用 `<details>` 折叠展示。

这条流水线自己抓到过两个真问题（都值得记着）：

- **探针假通过**：`check_globe.mjs` 里写成了 `dbg.globe.pointsData().length` —— 假球的
  `pointsData(d)` 是 setter，不带参数调用返回 `undefined`，两边都 `undefined` 于是断言永远成立，
  打印还露出「点位 undefined」。改成读内部数组 `_points` 并加上 `pointsBefore > 0` 才真的在测。
  `check_report.py` 把正文里的 `undefined/TODO/None` 当错误，就是为了逼出这类事。
- **数据重名**：路线库里有两条都叫「京沪夜车·睡一觉到上海」（一条讲怎么选、一条写死 Z284 走法），
  页面上并排像重复条目。`fix_route_duplicate_names.py` 改名；`build_route_catalog.py` 现在把
  **名称重复**也当校验错误（以前只查 id）。

报告是**快照**：跑 `build_report.py` 时的数据规模与爬取进度都定格在当时。真爬在跑时生成的报告会显示
「running N/6006」—— 那是如实反映，不是错误。相应地，有探针断言依赖「当前没有爬取任务在跑」
（例如「进度条默认收起」）：`dev_probe_data.mjs` 会先问 `/api/crawl/status`，有任务就按「有任务」判。

### 定向爬取（`od_focus.py`）：别再每次都扫全国 6006 个 OD
固定 78 个枢纽两两组合 = **6006 个 OD 对**，其中绝大多数航段跟你这次的行程无关；实际耗时受车次数、网络和服务端限流影响，不宜按固定时长估算。
时刻表本身定位就是「参考」，所以**够用的子图**比**全国全集**划算：

```powershell
python -m travel_planner.cli crawl-date 2026-10-01 -f 北京 上海 --dry-run   # 先看会爬哪些 OD
python -m travel_planner.cli crawl-date 2026-10-01 -f 北京 上海             # 定向爬取；具体耗时随车次数和限流变化
python -m travel_planner.cli crawl-date 2026-10-01 -f 北京 上海 --focus-pairs 150
```
网页端：数据台「真爬某一天」里填**目标城市**（留空＝全国），旁边有「预览要爬哪些 OD」和
「建议爬哪些城市」。接口：`POST /api/crawl/plan`（只读预览）、`POST /api/crawl/start`（`ods` 可精确指定
`BJP:SHH`）、`GET /api/crawl/targets`。

优先顺序：① 目标城市之间直达 → ② 目标城市 ↔ 全国枢纽（转车用）→ ③ 同城各站互连。
单城参与组合的车站取 3 个，**主站优先**（北京站排在郊区车场前）。

**主动学习**：每次 `/api/plan`、`/api/loop` 成功都会把城市写进 `search_log`，
`GET /api/crawl/targets` 据此把「你常走的方向」排在前面 —— 这是「注意力放在你会走的方向」，
不是训练模型。

踩过的坑：
- `search_log` 的列名**不能叫 `to`**（SQLite 关键字，`near "to": syntax error`），而且这个错被
  `except sqlite3.Error` 吞了整整一轮；现在列名是 `dest/travel_date`，并且
  `DSH_STRICT_LOG=1` 时写失败会直接抛错。
- `CREATE TABLE IF NOT EXISTS` **不会修旧结构**，早期建错的表要 `ALTER TABLE` 补列（已加）。
- 建表/写入都必须 `with conn`：连接默认没开 autocommit，DDL 不提交就随连接一起丢。
- `sqlite3.Connection` **不允许挂自定义属性**，缓存要放模块级字典（`_BUSIEST_CACHE/_COUNTS_CACHE`）。
  车站排序若按全日期做相关子查询会卡到超时，改成「基准日一次分组 + 字典查」后从 9 秒降到 0 秒。
- 定向爬完的日期车次天然偏少，分类上单独一档 `focus`（徽章「定向」），**不要**和
  「车次少＝调试遗留」混在一起；`route_for_date` 会说明「只覆盖目标方向，其余自动拷贝补上」。
- 对已有数据的日期做定向爬取，新旧车次会并存（总数看着像拼起来的）：接口会返回 `warning`，
  页面弹提示。想只留本次结果，先删掉这一天再爬。

### 数据台：自动检查 + 一键执行（`maintenance.py` / `jobs.py`）
约定：**检查自动，写库与真爬都要人点**。服务启动、页面刷新都不会自己爬 12306。

| 文件 | 作用 |
|---|---|
| `src/travel_planner/maintenance.py` | 来源判定 / 维护计划 / 一键执行 / 清理（只读优先，写库动作都支持 dry-run） |
| `src/travel_planner/jobs.py` | 后台爬取任务（进度、日志、取消、断点续爬），同一时刻最多一个 |
| `scripts/dev_probe_data.mjs` | 真浏览器探针：核对数据台是否真的渲染出体检清单/徽章/计划 |
| `scripts/maintain_data.py` | 命令行版：`status` / `prepare` / `copy --overwrite --dry-run` |

**数据来源分档**（同一张表里长得一样，只有 meta 能区分，页面据此上色）：

| 档位 | 判据 | 含义 |
|---|---|---|
| 导入 | `import:<日期>` | 公开数据集，最接近真实时刻表 |
| 真爬 | `crawl:<日期>:stats`（不是 copied） | 真的爬过 12306 |
| 拷贝 | `copy:<日期>:from`，或 `crawl:*:stats` 为 `copied from …` | **隔日/周末开行与临时调图体现不出来** |
| 车次少 | 车次数 < 1000 | 多半是调试留下的局部线路 |
| 无数据 | 没有行 | 查询时会自动从基准日拷一份 |

改这块时的注意点：
- `copy_dates(..., via=…)` 会写 `copy:<日期>:from`；**别把复制的数据写成 `crawl:*`**，否则页面会把它当真爬。
- `classify_date` 对「车次 < 1000」的日期一律判 `sparse`，不再猜来源——这类日期要提醒重导/重爬。
- `overview()/health()` 用 `_table_count/_count` 兜底：老库或测试库缺表时不该整块报错。
- 接口：`GET /api/maintain/overview`、`POST /api/maintain/plan`（只读）、`POST /api/maintain/run`（默认 `dry_run=true`）、
  `POST /api/crawl/start` / `GET /api/crawl/status` / `POST /api/crawl/cancel`、`GET /api/maintain/route?date=`。
- 后台爬取走 `crawl.crawl_date(..., progress_cb=, cancel_cb=)`；取消后断点在
  `data/crawl_state_<日期>.txt` 与 `discovered_<日期>.jsonl`，勾「断点续爬」可接着跑。
- 单测：`python -m unittest tests.test_maintenance -v`（来源判定、计划只读、覆盖留指纹、任务不自动起）。

```powershell
python -m unittest tests.test_maintenance -v      # 数据台逻辑
node scripts/dev_probe_data.mjs                   # 真浏览器：数据台是否渲染正常
node scripts/dev_probe_data.mjs 1024 720
```

### 自驾与租车（`drive_presets.json`）
- 车程走高德**驾车路径规划**（Web服务 key 即 `meta.amap_key`；没配 key 或配额用尽会退回
  「大圆距离 × 道路系数 + 地形车速」的估算，并在结果里标注「含估算路段」）。
  结果按城市对缓存在 `drive_route_cache`：想强制重取就把对应行的 `failed` 置 0 删掉，或用 API 的 `refresh`。
- 环线自驾资料（总里程、建议天数、公路、季节与封路、提醒、取还车建议）在 `drive_presets.json` 的
  `loops` 里，键就是 `loop_presets.json` 的环线 id；`city_tier_overrides` 决定各城租车档位
  （A 一线热门 / B 省会热门 / C 普通地级市 / D 偏远车少），`rental_model` 是价格模型：
  日租区间、异地还车每公里单价、保险区间、油价与百公里油耗、平台列表。
- **租车实时报价**：国内平台（神州/一嗨/携程/飞猪/租租车）没有免费公开 API，本项目只做区间估算；
  如果你有聚合数据 / 阿里云市场那类付费租车接口的 key，可以按 `drive.rental_model()` 与
  `drive.rental_quote()` 的返回结构接进去（替换 `platforms` 与合计值即可），前端不用改。
- 单日驾驶上限默认 6.5 小时（前端可选，接口参数 `max_drive_hours`），超限会在 `warnings` 里列出具体段。

排查地图问题时：网页第 3 页签底部会显示当前状态；如果提示缺少 `web/data/china.geo.json`，
跑一次上面的抓取脚本即可（文件已随仓库提供，正常不需要重跑）。

改过 `web/map.js` 之后建议跑一次无头冒烟测试（需要 Node 18+，用最小 DOM 桩真正渲染一遍底图、
选大区、点环线，能抓出浏览器里才暴露的错）：

```powershell
node scripts/check_map.mjs
node scripts/check_globe.mjs              # 地球页签：点位/卡片定位/正反面/相机飞行
python -m unittest discover -s tests      # 城市目录/环线/停留映射的单测
```

`check_globe.mjs` 里的假 globe.gl 会跟着 `pointOfView()` 真的移动相机（含 lookAt 的基向量），
所以「卡片全部消失」「飞错方向」「正反面判反」这类问题不用开浏览器就能测出来。

**改了前端要记得升版号**：`web/index.html` 里 `/static/style.css?v=12`、`app.js?v=12`、`map.js?v=12`、
`globe3d.js?v=12` 的 `?v=` 是给浏览器用的缓存钥匙，改了 css/js 就把数字加一，否则浏览器可能继续用旧文件
（典型症状：页签内容是新的，样式却是旧的）。`/` 返回的 HTML 已设置 `no-cache`，所以刷新页面一定拿到最新外壳。

地图本身出问题时（一片空白/提示框报错）：页面上的红色提示会写明原因（缺几何数据、接口 404、
数据结构不对等）；接口 404 通常意味着后端进程还是旧代码，重启 `python -m travel_planner.web` 即可。

**别用 PowerShell 的 `Get-Content -Raw` / `Set-Content` 改这些文件（血的教训）**：这台机器的
`pwsh` 默认按 GBK 读文件，UTF-8 的 `index.html` 会被读成乱码再写回，中文永久损坏（GBK→UTF-8
的往返不可逆，`?` 会吃掉字符，救不回来，只能重建文件）。改文本请用编辑器工具，或明确走 .NET：

```powershell
$p = "web\index.html"
$t = [System.IO.File]::ReadAllText((Resolve-Path $p), [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText((Resolve-Path $p), $t.Replace('旧','新'), [System.Text.UTF8Encoding]::new($false))
```

存盘后顺手确认没写进 BOM：`[System.IO.File]::ReadAllBytes($p)[0..2]` 不该是 `239,187,191`。

### 想用真浏览器核对前端时（`scripts/dev_probe.mjs`）

假 DOM 桩查不出「方法名写错」「画布尺寸不对」这类问题，所以留了一个用 Edge 无头模式 + DevTools 协议
的探针：真的打开页面、点环线、读相机坐标与几何尺寸。

```powershell
node scripts/dev_probe.mjs            # 默认 1440x900
node scripts/dev_probe.mjs 1024 720   # 指定窗口尺寸
```

它会打印：三栏与地球窗口的 x/宽度、右栏有没有被视口切掉、canvas 是否等于容器、相机 aspect 是否等于
容器宽高比，以及点世界之最/环线之后相机坐标有没有变、高亮卡片有几张。**改地球页签的布局或尺寸后跑一次。**

---

## 6. 例行体检清单（建议每次出行前过一遍，2 分钟）

1. `python scripts/check_dates.py` —— 确认出行日期在列、车次数量正常（全国约 1 万+；若只有几百说明只有局部线路）；
2. `plan 出发地 到达地 出行日期` 试查一次 —— 不报"未找到地点/无数据"；
3. 需要准确机票价：出行前 1-3 天再查一次该航线（或删缓存重取）；
4. 想更稳的换乘方案：表单把「换乘预留」调 30-60 分钟。

---

## 7. 各数据源的天然风险（都是"半官方"渠道）

| 渠道 | 风险 | 对策 |
|---|---|---|
| 12306 接口爬取 | 风控限流或网络中断 | 已内置指数退避、风控冷却、降速恢复与按 OD 断点续爬；遵循服务端限流，不保证固定完成时间 |
| HerbertHe 数据集 | 第三方，更新频率看其 Actions 是否正常 | 以 Releases 页最新为准；必要时改用爬取 |
| 飞猪 MCP | 半官方共享 key，可能失效/限额 | 已缓存降载；规划会提示"仅铁路规划"；备份=飞常准 |
| 高德 | 个人配额 15 万次/月；未认证=0 配额 | 确保完成个人认证；欠费时指引功能自动降级为默认接驳时间 |

合规提醒：所有抓取均为个人查询用途、限速访问；请勿放大并发或商用。
