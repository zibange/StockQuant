const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));

// ---------- sessionStorage + TTL 跨页共享 ----------
const _SCACHE = {
  _get(key) {
    try {
      const raw = sessionStorage.getItem(key);
      if (!raw) return null;
      const { v, t } = JSON.parse(raw);
      return t > Date.now() ? v : null;
    } catch { return null; }
  },
  _set(key, val, ttlMs) {
    try { sessionStorage.setItem(key, JSON.stringify({ v: val, t: Date.now() + ttlMs })); } catch {}
  },
  async fetch(url, ttlMs) {
    const hit = this._get(url);
    if (hit !== null) return hit;
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    this._set(url, data, ttlMs);
    return data;
  },
  invalidate(url) { sessionStorage.removeItem(url); }
};

const API = {
  buckets: () => fetch("/api/screener/buckets").then(r => r.json()),
  status:  () => fetch("/api/screener/status").then(r => r.json()),
  pick:    (p) => fetch("/api/screener/pick?" + new URLSearchParams(p)).then(r => r.json()),
  sync:    (body) => fetch("/api/screener/sync", {
              method: "POST", headers: {"Content-Type": "application/json"},
              body: JSON.stringify(body) }).then(r => r.json()),
  del:     (biz, codes) => {
              const params = new URLSearchParams({ biz });
              if (codes && codes.length) params.set("codes", codes.join(","));
              return fetch("/api/screener/cache?" + params, { method: "DELETE" }).then(r => r.json());
           },
  ping:    () => fetch("/api/screener/status").then(r => r.ok),
  freshness: () => fetch("/api/cache/freshness").then(r => r.json()),
};

let bucketChart = null;
let _freshnessCache = null;
const BUCKET_COLORS = ["#ef4444", "#f59e0b", "#10b981", "#3b82f6", "#8b5cf6", "#ec4899"];

// ========== Init (先 sessionStorage 秒显, 后台刷新) ==========
init();
async function init() {
  bindTabs();
  bindButtons();
  // 先尝试 sessionStorage 秒显
  const cachedBuckets = _SCACHE._get("/api/screener/buckets");
  const cachedStatus  = _SCACHE._get("/api/screener/status");
  const cachedFresh   = _SCACHE._get("/api/cache/freshness");
  if (cachedBuckets) _tryRenderBuckets(cachedBuckets);
  if (cachedStatus)  _tryRenderStatus(cachedStatus);
  if (cachedFresh)   _tryRenderFreshness(cachedFresh);
  // 后台刷新 (同时写入 sessionStorage)
  const ok = await checkConn();
  if (ok) {
    Promise.all([
      _SCACHE.fetch("/api/screener/buckets", 25000).then(_tryRenderBuckets).catch(()=>{}),
      _SCACHE.fetch("/api/screener/status", 25000).then(_tryRenderStatus).catch(()=>{}),
      _SCACHE.fetch("/api/cache/freshness", 25000).then(_tryRenderFreshness).catch(()=>{}),
    ]);
  }
}

function bindTabs() {
  $$(".tab").forEach(t => t.addEventListener("click", () => {
    $$(".tab").forEach(x => x.classList.remove("on"));
    t.classList.add("on");
    $$(".tab-pane").forEach(p => p.style.display = "none");
    $("#tab-" + t.dataset.tab).style.display = "";
    if (t.dataset.tab === "buckets") {
      if (bucketChart) { bucketChart.dispose(); bucketChart = null; }
      setTimeout(() => renderBucketChart(), 100);
    }
    if (t.dataset.tab === "cache") loadStatus();
  }));
}

function bindButtons() {
  $("#btnPick").addEventListener("click", runPick);
  $("#btnSync").addEventListener("click", runSync);
  $("#btnDelete").addEventListener("click", runDelete);

  // --- up_days slider <-> number 双向绑定 ---
  const slider = $("#upDaysSlider");
  const num = $("#upDaysNum");
  const badge = $("#upDaysVal");
  if (slider && num) {
    const sync = (v) => {
      v = Math.max(0, Math.min(10, parseInt(v) || 0));
      slider.value = v;
      num.value = v;
      badge.textContent = v;
    };
    slider.addEventListener("input", () => sync(slider.value));
    num.addEventListener("input", () => sync(num.value));
    sync(2);
  }
}

async function checkConn() {
  const el = $("#conn");
  try {
    const ok = await API.ping();
    el.className = "st ok";
    el.innerHTML = "API <b>●</b> 就绪";
    return ok;
  } catch {
    el.className = "st err";
    el.innerHTML = "API <b>●</b> 离线";
    return false;
  }
}

// ========== Buckets ==========
async function loadBuckets(data) {
  data = data || await API.buckets();
  const checks = $("#bucketChecks");
  checks.innerHTML = "";
  data.buckets.forEach((b, i) => {
    const excluded = b.excluded_by_default;
    const label = document.createElement("label");
    if (excluded) label.classList.add("excluded");
    label.innerHTML = `
      <input type="checkbox" value="${b.id}" ${excluded ? "" : "checked"} data-excluded="${excluded}">
      <span>${b.id} ${b.name}</span>
      <span class="count">${b.cached_count}</span>
    `;
    checks.appendChild(label);
  });

  // legend
  const legend = $("#bucketLegend");
  legend.innerHTML = "";
  data.buckets.forEach((b, i) => {
    const range = b.lo + "亿 - " + (b.hi_label || "∞") + "亿";
    const div = document.createElement("div");
    div.className = "item";
    div.innerHTML = `
      <span class="swatch" style="background:${BUCKET_COLORS[i]}"></span>
      <span class="bid">${b.id}</span>
      <span class="bname">${b.name}${b.excluded_by_default ? '<span class="excluded-tag">默认排除</span>' : ''}</span>
      <span class="brange">${range}</span>
      <span class="bcnt ${b.cached_count === 0 ? "zero" : ""}">${b.cached_count}</span>
    `;
    legend.appendChild(div);
  });
}

function _tryRenderBuckets(d) { loadBuckets(d); }

async function renderBucketChart() {
  const data = await API.buckets();
  if (!bucketChart) {
    bucketChart = echarts.init($("#bucketChart"));
    window.addEventListener("resize", () => bucketChart && bucketChart.resize());
  }
  bucketChart.setOption({
    tooltip: { trigger: "axis", formatter: p => {
      const d = p[0];
      const b = data.buckets[d.dataIndex];
      return `<b>${b.id} ${b.name}</b><br/>缓存股票: <b>${d.value}</b><br/>范围: ${b.lo}-${b.hi_label || "∞"}亿${b.excluded_by_default ? "<br/><span style='color:#dc2626'>⚠️ 默认排除</span>" : ""}`;
    }},
    grid: { left: 50, right: 30, top: 30, bottom: 50 },
    xAxis: {
      type: "category",
      data: data.buckets.map(b => `${b.id} ${b.name}`),
      axisLabel: { interval: 0, rotate: 20 }
    },
    yAxis: { type: "value", name: "缓存股票数", splitLine: { lineStyle: { color: "#e2e8f0" } } },
    series: [{
      type: "bar",
      data: data.buckets.map((b, i) => ({
        value: b.cached_count,
        itemStyle: {
          color: BUCKET_COLORS[i],
          opacity: b.excluded_by_default ? 0.4 : 0.9,
          borderRadius: [4, 4, 0, 0]
        }
      })),
      barWidth: "50%",
      label: { show: true, position: "top", color: "#334155", fontWeight: 600 }
    }]
  });
}

function refreshBuckets() {
  loadBuckets();
  renderBucketChart();
}

// ========== Pick ==========
async function runPick() {
  const btn = $("#btnPick");
  btn.disabled = true; btn.textContent = "选股中...";
  const hint = $("#pickHint");
  hint.className = "hint"; hint.textContent = "";

  const selected = $$("#bucketChecks input:checked").map(c => c.value);
  const force = $("#forceUpdate").checked;

  const params = {
    top_n: $("#topN").value,
    buckets: selected.join(","),
    exclude_st: $("#excludeST").checked,
    min_amount_wan: $("#minAmount").value,
    min_list_days: $("#minList").value,
    kdj_window: $("#kdjWindow").value,
    up_days: $("#upDaysSlider").value,
  };

  if (selected.length === 0) {
    hint.className = "hint err"; hint.textContent = "请至少选择一个市值分桶";
    btn.disabled = false; btn.textContent = "🚀 开始选股";
    return;
  }

  // --- Stale check: 缓存过期提示 ---
  if (!force && _freshnessCache && _freshnessCache.status === "stale") {
    const f = _freshnessCache;
    const ok = confirm(
      `📦 日线缓存落后 ${f.gap_days} 天\n\n` +
      `  本地最新: ${f.storage_latest}\n` +
      `  通达信最新: ${f.tdx_latest}\n\n` +
      `是否先自动同步最新日线再选股？(预计 1-2 分钟)`
    );
    if (!ok) {
      hint.className = "hint"; hint.textContent = "已跳过同步, 将用过期数据选股";
    } else {
      params.auto_sync = "true";
      hint.className = "hint"; hint.textContent = "🔄 缓存过期, 先同步日线...";
    }
  }

  let result;
  try {
    if (force) {
      hint.className = "hint"; hint.textContent = "先刷新日线...";
      await API.sync({ biz: "kline", kline_count: params.kdj_window, force: true });
    }
    result = await API.pick(params);
  } catch (e) {
    hint.className = "hint err"; hint.textContent = "请求失败: " + e.message;
    btn.disabled = false; btn.textContent = "🚀 开始选股";
    return;
  }

  if (!result.ok) {
    hint.className = "hint err"; hint.textContent = result.error || "选股失败";
    btn.disabled = false; btn.textContent = "🚀 开始选股";
    return;
  }

  renderPickResult(result);
  btn.disabled = false; btn.textContent = "🚀 开始选股";
}

function renderPickResult(r) {
  _pickResults = r.results || [];

  // --- 动态表头: J↑N天 ---
  const cfg = r.strategy_cfg || {};
  const upDays = cfg.up_days ?? 2;
  const thJUp = $("#thJUp");
  if (thJUp) {
    thJUp.textContent = upDays <= 0 ? "J↑(关闭)" : `J↑${upDays}天`;
  }

  const stats = $("#resultStats");
  const miss = r.cache_miss || 0;
  const hit = r.cache_hit || 0;
  let html = `
    <span>候选 <b>${r.total_candidates}</b></span>
    <span>缓存命中 <b class="ok">${hit}</b></span>
    <span>缓存缺失 <b class="${miss ? 'warn' : 'ok'}">${miss}</b></span>
    <span>KDJ 命中 <b class="ok">${r.match_count}</b></span>
    <span>耗时 <b>${r.elapsed_seconds}</b>s</span>
  `;
  if (r.cache_freshness) {
    const f = r.cache_freshness;
    if (f.status === "fresh") {
      html += `<span class="ok">🟢 K线最新 ${f.storage_latest}</span>`;
    } else if (f.status === "stale") {
      html += `<span class="warn">🟡 落后 ${f.gap_days} 天 (本地 ${f.storage_latest} / TDX ${f.tdx_latest})</span>`;
    }
    renderFreshBadge(f);
  }
  if (r.note) html += `<span class="warn">⚠️ ${r.note}</span>`;
  stats.innerHTML = html;

  const tbody = $("#resultBody");
  const box = $("#kdjChartBox");
  if (!r.results || r.results.length === 0) {
    tbody.innerHTML = `<tr><td colspan="16" class="empty">未找到符合条件的股票</td></tr>`;
    if (box) box.style.display = "none";
    return;
  }
  tbody.innerHTML = _renderTableRows(_pickResults);
  // 默认选中第一行
  selectResultRow(0, tbody.querySelector("tr[data-idx='0']"));

  // 绑定表头排序 click（只绑一次）
  if (!window._sortedBound) {
    document.querySelectorAll("#resultTable th[data-sort]").forEach(th => {
      th.addEventListener("click", () => _sortBy(th.dataset.sort, th));
    });
    window._sortedBound = true;
  }
}

function _renderTableRows(list) {
  return list.map((s, i) => `
    <tr data-idx="${i}" onclick="selectResultRow(${i}, this)">
      <td>${i + 1}</td>
      <td><b>${s.code}</b></td>
      <td>${s.name || "-"}</td>
      <td><span class="tag tag-${s.bucket}">${s.bucket}</span></td>
      <td>${s.float_mv ?? "-"}</td>
      <td>${s.K ?? "-"}</td>
      <td>${s.D ?? "-"}</td>
      <td><b>${s.J ?? "-"}</b></td>
      <td>${s.jk_gap ?? "-"}</td>
      <td>${s.J_slope ?? "-"}</td>
      <td>${s.vol_ratio ?? "-"}</td>
      <td>${s.chg_pct != null ? s.chg_pct + "%" : "-"}</td>
      <td class="${s.cond_J_gt_K ? 'cond-y' : 'cond-n'}">${s.cond_J_gt_K ? '✓' : '✗'}</td>
      <td class="${s.cond_J_gt_D ? 'cond-y' : 'cond-n'}">${s.cond_J_gt_D ? '✓' : '✗'}</td>
      <td class="${s.cond_K_gt_D ? 'cond-y' : 'cond-n'}">${s.cond_K_gt_D ? '✓' : '✗'}</td>
      <td class="${s.cond_J_up ? 'cond-y' : 'cond-n'}">${s.cond_J_up ? '✓' : '✗'}</td>
    </tr>
  `).join("");
}

let _sortState = { key: null, dir: 1 };  // dir: 1=升序, -1=降序
function _sortBy(key, thEl) {
  if (!_pickResults || _pickResults.length === 0) return;

  // 同列切换方向
  if (_sortState.key === key) {
    _sortState.dir *= -1;
  } else {
    _sortState.key = key;
    _sortState.dir = -1;  // 默认降序（数值大的排前）
  }

  _pickResults.sort((a, b) => {
    let va = a[key], vb = b[key];
    // null 排末尾
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === "number" && typeof vb === "number") {
      return (va - vb) * _sortState.dir;
    }
    return String(va).localeCompare(String(vb)) * _sortState.dir;
  });

  // 重渲染
  const tbody = $("#resultBody");
  tbody.innerHTML = _renderTableRows(_pickResults);
  selectResultRow(0, tbody.querySelector("tr[data-idx='0']"));

  // 表头视觉反馈
  document.querySelectorAll("#resultTable th[data-sort]").forEach(t => {
    t.classList.remove("sort-asc", "sort-desc");
    if (t === thEl) {
      t.classList.add(_sortState.dir === 1 ? "sort-asc" : "sort-desc");
    }
  });
}

let _pickResults = null;
let _kdjChart = null;

function selectResultRow(idx, trEl) {
  // 缓存结果集以便反复点击
  if (!_pickResults) return;
  const s = _pickResults[idx];
  if (!s || !s.kdj_series) return;

  // 切换 active 高亮
  document.querySelectorAll("#resultBody tr").forEach(r => r.classList.remove("active"));
  if (trEl) trEl.classList.add("active");

  renderKdjChart(s);
}

function renderKdjChart(s) {
  const box = $("#kdjChartBox");
  const title = $("#kdjChartTitle");
  if (!box || !s.kdj_series) return;
  box.style.display = "block";
  title.textContent = `${s.code} ${s.name || ""}  (K=${s.K} D=${s.D} J=${s.J})`;

  const el = $("#kdjChart");
  if (_kdjChart) _kdjChart.dispose();
  _kdjChart = echarts.init(el);

  const k = s.kdj_series.K;
  const d = s.kdj_series.D;
  const j = s.kdj_series.J;
  const dates = s.kdj_series.dates;
  const lastIdx = k.length - 1;

  _kdjChart.setOption({
    tooltip: {
      trigger: "axis",
      formatter: (p) => {
        const x = p[0].axisValue;
        let rows = p.map(it => {
          const v = it.data == null ? "-" : it.data;
          return `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${it.color};margin-right:6px"></span>${it.seriesName}: <b>${v}</b>`;
        });
        return `${x}<br/>${rows.join("<br/>")}`;
      },
    },
    legend: { top: 0, textStyle: { fontSize: 12 } },
    grid: { left: 50, right: 20, top: 35, bottom: 35 },
    xAxis: {
      type: "category",
      data: dates,
      boundaryGap: false,
      axisLabel: { color: "#475569", fontSize: 10, interval: Math.floor(dates.length / 8) },
      axisLine: { lineStyle: { color: "#cbd5e1" } },
    },
    yAxis: {
      type: "value",
      name: "KDJ",
      splitLine: { lineStyle: { color: "#e2e8f0" } },
    },
    series: [
      {
        name: "K", type: "line", smooth: true, symbol: "none",
        lineStyle: { width: 2, color: "#2563eb" },
        itemStyle: { color: "#2563eb" },
        data: k,
        markPoint: {
          symbol: "circle", symbolSize: 8,
          data: [{ coord: [lastIdx, k[lastIdx]], name: "now", value: k[lastIdx] }],
          label: { formatter: p => p.value, fontSize: 10, color: "#2563eb" },
        },
      },
      {
        name: "D", type: "line", smooth: true, symbol: "none",
        lineStyle: { width: 2, color: "#ea580c" },
        itemStyle: { color: "#ea580c" },
        data: d,
        markPoint: {
          symbol: "circle", symbolSize: 8,
          data: [{ coord: [lastIdx, d[lastIdx]], name: "now", value: d[lastIdx] }],
          label: { formatter: p => p.value, fontSize: 10, color: "#ea580c" },
        },
      },
      {
        name: "J", type: "line", smooth: true, symbol: "none",
        lineStyle: { width: 2.5, color: "#9333ea" },
        itemStyle: { color: "#9333ea" },
        data: j,
        markPoint: {
          symbol: "circle", symbolSize: 10,
          data: [{ coord: [lastIdx, j[lastIdx]], name: "now", value: j[lastIdx] }],
          label: { formatter: p => p.value, fontSize: 11, color: "#9333ea", fontWeight: "bold" },
        },
      },
    ],
  });
  setTimeout(() => _kdjChart && _kdjChart.resize(), 50);
}

// ========== Cache: Status ==========
let _klRowChart = null;

async function loadStatus(data) {
  const kpi = $("#statusKpi");
  kpi.innerHTML = "加载中...";
  try {
    data = data || await API.status();
    if (!data || !data.ok || !data.cache) { kpi.innerHTML = "暂无数据"; return; }
    const mv = data.cache.market_value || {total:0, fresh:0, stale:0};
    const kl = data.cache.kline || {total:0, root:"", min_rows:0, max_rows:0, avg_rows:0, median_rows:0, buckets:{}};
    const kb = kl.buckets || {};
    kpi.innerHTML = `
      <span class="kpi-row head"><span>项目</span><span>数量</span></span>
      <span class="kpi-row data"><span>市值缓存 (DuckDB)</span><span>${mv.total}</span></span>
      <span class="kpi-row data ok"><span>市值新鲜</span><span>${mv.fresh}</span></span>
      <span class="kpi-row data warn"><span>市值待更新</span><span>${mv.stale}</span></span>
      <span class="kpi-row data"><span>日线缓存 (Parquet)</span><span>${kl.total}</span></span>
      <span class="kpi-row data"><span>日线根数 min/max/avg</span><span>${kl.min_rows} / ${kl.max_rows} / ${kl.avg_rows}</span></span>
      <span class="kpi-row data"><span>日线根数 median</span><span>${kl.median_rows}</span></span>
      <span class="kpi-row data"><span>日线文件根目录</span><span style="font-family:monospace;font-size:11px">${kl.root}</span></span>
    `;
    renderKlRowChart(kb);
  } catch (e) {
    kpi.innerHTML = "加载失败: " + e.message;
  }
}
function _tryRenderStatus(d) { loadStatus(d); }

function renderKlRowChart(b) {
  const el = $("#klRowCountChart");
  if (!el) return;
  if (_klRowChart) _klRowChart.dispose();
  _klRowChart = echarts.init(el);
  const total = (b.lt20 || 0) + (b.bet20_59 || 0) + (b.bet60_119 || 0) + (b.ge120 || 0);
  const option = {
    tooltip: {
      trigger: "axis",
      formatter: (p) => {
        const x = p[0].axisValue;
        const v = p[0].value;
        const pct = total > 0 ? ((v / total) * 100).toFixed(1) : 0;
        return `${x}<br/>股票数: <b>${v}</b> (${pct}%)`;
      },
    },
    grid: { left: 60, right: 20, top: 30, bottom: 40 },
    xAxis: {
      type: "category",
      data: ["< 20 行", "20-59 行", "60-119 行", "≥ 120 行"],
      axisLabel: { color: "#475569" },
      axisLine: { lineStyle: { color: "#cbd5e1" } },
    },
    yAxis: {
      type: "value",
      name: "股票数",
      splitLine: { lineStyle: { color: "#e2e8f0" } },
    },
    series: [{
      type: "bar",
      barWidth: "55%",
      itemStyle: {
        color: (p) => {
          const colors = ["#dc2626", "#f59e0b", "#2563eb", "#16a34a"];
          return colors[p.dataIndex];
        },
        borderRadius: [4, 4, 0, 0],
      },
      label: {
        show: true,
        position: "top",
        formatter: (p) => p.value,
        fontSize: 12,
        color: "#334155",
      },
      data: [b.lt20 || 0, b.bet20_59 || 0, b.bet60_119 || 0, b.ge120 || 0],
    }],
  };
  _klRowChart.setOption(option);
  setTimeout(() => _klRowChart && _klRowChart.resize(), 50);
}

// ========== Freshness ==========
async function loadFreshness(data) {
  const badge = $("#freshBadge");
  if (!badge) return;
  badge.className = "fresh-badge";
  badge.textContent = "⏳ 检查中...";
  try {
    data = data || await API.freshness();
    const f = data.freshness || {};
    renderFreshBadge(f);
  } catch (e) {
    badge.className = "fresh-badge error";
    badge.textContent = "⚠️ 检查失败";
  }
}
function _tryRenderFreshness(d) { loadFreshness(d); }

function renderFreshBadge(f) {
  if (f) _freshnessCache = f;
  const badge = $("#freshBadge");
  if (!badge || !f || !f.status) return;
  const st = f.status;
  badge.className = "fresh-badge " + st;
  if (st === "fresh") {
    badge.textContent = `🟢 K线最新 ${f.storage_latest || ""}`;
    badge.title = f.message || "";
  } else if (st === "stale") {
    const gap = f.gap_days || 0;
    badge.textContent = `🟡 落后 ${gap} 天 (本地 ${f.storage_latest} / TDX ${f.tdx_latest})`;
    badge.title = "点击缓存管理执行同步，或同步全部";
    badge.onclick = () => {
      const el = document.querySelector('[data-tab="cache"]');
      if (el) el.click();
    };
  } else {
    badge.textContent = "🔴 状态异常";
    badge.title = f.message || "";
  }
}

// ========== Cache: Sync ==========
// ========== Run Sync (with SSE progress) ==========
function startProgress(prefix, button, textBusy, textDone) {
  const box = $(`#${prefix}Progress`);
  const label = $(`#${prefix}ProgLabel`);
  const pct = $(`#${prefix}ProgPct`);
  const fill = $(`#${prefix}ProgFill`);
  const sub = $(`#${prefix}ProgSub`);
  box.style.display = "block";
  fill.className = "progress-fill";
  fill.style.width = "0%";
  label.textContent = "准备中...";
  pct.textContent = "0%";
  sub.textContent = "";
  button.disabled = true;
  button.textContent = textBusy;

  return {
    box, label, pct, fill, sub, button, textDone,
    setPct(p, info) {
      pct.textContent = p + "%";
      fill.style.width = p + "%";
      if (info) {
        if (info.stage) label.textContent = stageLabel(info.stage);
        const parts = [];
        if (info.msg) parts.push(info.msg);
        if (info.updated != null) parts.push(`更新${info.updated}`);
        if (info.saved != null) parts.push(`保存${info.saved}`);
        if (info.skipped != null) parts.push(`跳过${info.skipped}`);
        if (info.errors != null && info.errors > 0) parts.push(`⚠${info.errors}`);
        if (info.done != null && info.total != null && info.total !== "all")
          parts.push(`${info.done}/${info.total}`);
        if (info.elapsed != null) parts.push(`⏱${info.elapsed}s`);
        sub.textContent = parts.join("  ");
      }
    },
    done() {
      fill.classList.add("done");
      pct.textContent = "100%";
      fill.style.width = "100%";
    },
    error(msg) {
      fill.classList.add("err");
      fill.style.width = pct.textContent;
      sub.textContent = "❌ " + msg;
    },
    resetButton() {
      button.disabled = false;
      button.textContent = textDone;
    },
    hide() {
      setTimeout(() => { box.style.display = "none"; }, 3000);
    }
  };
}

function stageLabel(stage) {
  const map = {
    mv: "市值同步", kline: "日线同步", all: "全量同步",
    delete_mv: "删除市值", delete_kline: "删除日线",
    mv_done: "市值完成", kline_done: "日线完成",
  };
  return map[stage] || stage;
}

function runTaskWithSSE({ submit, streamPrefix }, progress) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let es = null;

    submit()
      .then(r => {
        if (!r.ok) { reject(new Error(r.error || "提交失败")); return; }
        const tid = r.task_id;
        es = new EventSource(streamPrefix + "/" + tid + "/stream");

        es.addEventListener("progress", (e) => {
          try {
            const d = JSON.parse(e.data);
            progress.setPct(d.pct, d.info);
          } catch(err){}
        });
        es.addEventListener("done", (e) => {
          if (settled) return; settled = true;
          try { const d = JSON.parse(e.data); resolve(d.result); } catch(err){ resolve(null); }
          es.close();
          progress.done(); progress.resetButton(); progress.hide();
        });
        es.addEventListener("error", (e) => {
          if (settled) return; settled = true;
          try { const d = JSON.parse(e.data); reject(new Error(d.error || "任务失败")); }
          catch(err) { reject(new Error("任务失败")); }
          es.close(); progress.error("任务失败"); progress.resetButton();
        });
        es.onerror = () => {
          if (settled) { try { es.close(); } catch(err){} return; }
          if (es.readyState === EventSource.CLOSED) {
            settled = true; reject(new Error("SSE 连接关闭"));
            progress.error("连接已关闭"); progress.resetButton();
          }
        };
      })
      .catch((err) => { settled = true; reject(err); });
  });
}

async function runSync() {
  const body = {
    biz: $("#syncBiz").value,
    codes: $("#syncCodes").value.split(",").map(s=>s.trim()).filter(Boolean),
    force: $("#forceSync").checked,
    kline_count: parseInt($("#klineCount").value) || 120,
  };
  const btn = $("#btnSync");
  const hint = $("#syncHint");
  hint.className = "hint"; hint.textContent = "";

  const prog = startProgress("sync", btn, "同步中...", "⬇️ 同步缓存");

  try {
    const r = await runTaskWithSSE({
      submit: () => fetch("/api/screener/sync", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify(body),
      }).then(r => r.json()),
      streamPrefix: "/api/screener/task",
    }, prog);
    if (r) {
      const parts = [];
      if (r.mv) parts.push(`市值: ${r.mv.updated}/${r.mv.total} (${r.mv.seconds}s)`);
      if (r.kline) parts.push(`日线: ${r.kline.saved}/${r.kline.total} (${r.kline.seconds}s)`);
      if (r.seconds != null && !parts.length) parts.push(`${r.seconds}s`);
      hint.className = "hint ok"; hint.textContent = "✅ " + (parts.join("  ") || "完成");
    } else {
      hint.className = "hint ok"; hint.textContent = "✅ 完成";
    }
  } catch (e) {
    hint.className = "hint err"; hint.textContent = "❌ " + e.message;
    prog.error(e.message);
    prog.resetButton();
  }
  loadBuckets();
  loadStatus();
}

// ========== Cache: Delete ==========
async function runDelete() {
  const codes = $("#delCodes").value.split(",").map(s => s.trim()).filter(Boolean);
  const biz = $("#delBiz").value;

  const msg = biz === "all" ? "全部（市值+日线）" : (biz === "kline" ? "日线" : "市值");
  const countLabel = codes.length ? `（${codes.length} 只: ${codes.slice(0,3).join(",")}${codes.length>3?",...":""}）` : "（全量）";
  if (!confirm(`确定要删除 ${msg} 缓存${countLabel}吗？此操作不可恢复！`)) {
    return;
  }

  const btn = $("#btnDelete");
  const hint = $("#delHint");
  hint.className = "hint"; hint.textContent = "";
  const prog = startProgress("del", btn, "删除中...", "🗑️ 删除");

  const params = new URLSearchParams({ biz });
  if (codes.length) params.set("codes", codes.join(","));

  try {
    await runTaskWithSSE({
      submit: () => fetch("/api/screener/cache?" + params, { method: "DELETE" }).then(r => r.json()),
      streamPrefix: "/api/screener/task",
    }, prog);
    hint.className = "hint ok"; hint.textContent = "✅ 已删除";
  } catch (e) {
    hint.className = "hint err"; hint.textContent = "❌ " + e.message;
    prog.error(e.message);
    prog.resetButton();
  }
  loadBuckets();
  loadStatus();
}
