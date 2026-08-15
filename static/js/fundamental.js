// Last modified: 2026-08-13 01:07:39
let traceData = null;       // 溯源数据 (懒加载)
let allFields = [];         // 字段元数据 (懒加载, 全量轻量)
let cc = '';                // 当前选中股票代码 (未选时为空)
let ccName = '';            // 当前选中股票名称 (搜索选择时传入, 画像回退用)
let selFinFields = new Set(['FN1','FN4','FN6','FN210','FN230','FN232','FN242','FN319']);
let selGpFields = new Set(['GP01_1','GP03_1','GP06_1','GP16_1']);

// 左侧"已同步股票"列表分页状态
const STOCK_PAGER = {page:1, size:10};
// 七大数据类型 展示顺序 + 短标签 + 徽标样式类
const BIZ_ORDER = ['basic','financial','gpjy','chip','l2','shareholder','mainbusi'];
const BIZ_SHORT = {basic:'基础', financial:'财务', gpjy:'交易', chip:'筹码', l2:'L2', shareholder:'股东', mainbusi:'主营'};

// 分页状态: 每个 Tab 独立 page/page_size
const PAGER = {
  fin:  {page:1, size:10},
  long: {page:1, size:10},
  gp:   {page:1, size:10},
  sh:   {page:1, size:10},
  mb:   {page:1, size:10},
  fld:  {page:1, size:100},
};
const PAGE_SIZES = [10, 30, 50, 100];

const TAB_LABEL = {overview:'📋 总览画像',financial:'📈 财务宽表',long:'🔍 财务长表',gpjy:'📊 交易专业数据',shareholder:'🏛 股东明细',mainbusi:'🏭 主营构成',fields:'🗂 字段元数据',trace:'🔗 数据溯源'};
function $(id){return document.getElementById(id)}
function debounce(fn,t=250){let h;return(...a)=>{clearTimeout(h);h=setTimeout(()=>fn(...a),t)}}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function fmt(v){if(v===null||v===undefined||v===''||v==='-')return '-';const n=Number(v);if(isNaN(n))return esc(v);return Number.isInteger(n)?n.toLocaleString():n.toLocaleString(undefined,{maximumFractionDigits:2})}
function fmtBig(v){if(v===null||v===undefined)return '-';const n=Number(v);if(isNaN(n))return esc(v);const a=Math.abs(n);if(a>=1e8)return (n/1e8).toFixed(2)+'亿';if(a>=1e4)return (n/1e4).toFixed(2)+'万';return n.toLocaleString()}

// ---------- 分页控件 ----------
function pagerHTML(key, total, page, size){
  const totalPages = Math.max(1, Math.ceil(total/size));
  const cur = Math.min(page, totalPages);
  return `<div class="pager">
    <span class="pager-info">共 ${total} 条 · ${totalPages} 页</span>
    <span class="pager-size">每页
      <select onchange="PAGER['${key}'].size=+this.value;PAGER['${key}'].page=1;${key==='fin'?'loadFinancial(cc)':key==='long'?'loadLong(cc)':key==='gp'?'loadGpjy(cc)':key==='sh'?'loadShareholder(cc)':'renderFields()'}">
        ${PAGE_SIZES.map(s=>`<option value="${s}" ${s===size?'selected':''}>${s}</option>`).join('')}
      </select> 条
    </span>
    <span class="pager-nav">
      <button ${cur<=1?'disabled':''} onclick="PAGER['${key}'].page=${cur-1};${key==='fin'?'loadFinancial(cc)':key==='long'?'loadLong(cc)':key==='gp'?'loadGpjy(cc)':key==='sh'?'loadShareholder(cc)':'renderFields()'}">‹ 上一页</button>
      <span class="pager-cur">${cur} / ${totalPages}</span>
      <button ${cur>=totalPages?'disabled':''} onclick="PAGER['${key}'].page=${cur+1};${key==='fin'?'loadFinancial(cc)':key==='long'?'loadLong(cc)':key==='gp'?'loadGpjy(cc)':key==='sh'?'loadShareholder(cc)':'renderFields()'}">下一页 ›</button>
    </span>
  </div>`;
}

// ---------- 股票搜索 ----------
const q=$('q'),sug=$('sug');
q.addEventListener('input',debounce(()=>{
  const kw=q.value.trim();
  if(kw.length<1){sug.style.display='none';return}
  fetch('/api/search?q='+encodeURIComponent(kw)).then(r=>r.json()).then(l=>{
    if(!l.length){sug.style.display='none';return}
    sug.innerHTML=l.filter(x=>x.type==='stock'||x.type==='index').map(x=>
      `<div onclick="pick('${x.code}','${x.name}')"><div class="nm"><b>${esc(x.name)}</b></div><span class="cd">${x.code}</span></div>`).join('');
    sug.style.display='block';
  });
}));
document.addEventListener('click',e=>{if(!e.target.closest('.sb'))sug.style.display='none'});
// Enter: 直接选中首个搜索结果, 并在页面展示其基本面信息
q.addEventListener('keydown',e=>{
  if(e.key==='Enter'){
    const first=sug.querySelector('div[onclick]');
    if(first){first.click();sug.style.display='none'}
    else{const kw=q.value.trim();if(kw.length)sug.style.display='none'}
  }
});
function pick(code,name){sug.style.display='none';q.value=name||code;ccName=name||'';loadStock(code)}

// ---------- 加载股票 ----------
function loadStock(code){
  cc=code;
  showTab('overview');
  document.querySelectorAll('.stock-list .it').forEach(x=>x.classList.toggle('sel',x.dataset.code===code));
  $('tab-overview').innerHTML='<div class="empty">正在加载 '+esc(code)+' 基本面数据...</div>';
  fetch('/api/fundamental/profile?code='+encodeURIComponent(code)).then(r=>r.json()).then(p=>{
    renderOverview(p);
    loadFinancial(code);
    loadLong(code);
    loadGpjy(code);
    loadShareholder(code);
    loadMainbusi(code);
  }).catch(e=>showErr('overview','加载失败: '+e.message));
}
function loadFinancial(code){
  const {page,size}=PAGER.fin;
  const fSel=[...selFinFields].join(',');
  fetch(`/api/fundamental/financial?code=${encodeURIComponent(code)}&format=wide&page=${page}&page_size=${size}&fields=${encodeURIComponent(fSel)}`).then(r=>r.json()).then(d=>{
    ensureFields(()=>renderFinancial(d));
  }).catch(()=>{});
}
function loadLong(code){
  const {page,size}=PAGER.long;
  const rd=$('longRd'); const rdVal=rd?rd.value:'';
  fetch(`/api/fundamental/financial?code=${encodeURIComponent(code)}&format=long${rdVal?'&report_date='+rdVal:''}&page=${page}&page_size=${size}`).then(r=>r.json()).then(d=>{
    renderLong(d, code);
  }).catch(()=>{});
}
function loadGpjy(code){
  const {page,size}=PAGER.gp;
  fetch(`/api/fundamental/gpjy?code=${encodeURIComponent(code)}&page=${page}&page_size=${size}`).then(r=>r.json()).then(d=>{
    ensureFields(()=>renderGpjy(d));
  }).catch(()=>{});
}
function loadShareholder(code){
  const {page,size}=PAGER.sh;
  const ht=$('shType'); const htVal=ht?ht.value:'';
  const rd=$('shRd'); const rdVal=rd?rd.value:'';
  const qs=`code=${encodeURIComponent(code)}${htVal?'&holder_type='+htVal:''}${rdVal?'&report_date='+rdVal:''}&page=${page}&page_size=${size}`;
  fetch(`/api/fundamental/shareholder?${qs}`).then(r=>r.json()).then(d=>{
    renderShareholder(d, code);
  }).catch(()=>{});
}
function loadMainbusi(code){
  const rd=$('mbRd'); const rdVal=rd?rd.value:'';
  const qs=`code=${encodeURIComponent(code)}${rdVal?'&report_date='+rdVal:''}`;
  fetch(`/api/fundamental/mainbusi?${qs}`).then(r=>r.json()).then(d=>{
    renderMainbusi(d, code);
  }).catch(()=>{});
}

// ---------- 总览画像 ----------
function renderOverview(p){
  const info=p.info||{}, more=p.more||{};
  const nm=info.name||ccName||p.code;
  const kpi=`<div class="k"><div class="l">证券名称</div><div class="v">${esc(nm)}</div><div class="s">${p.code}</div></div>
    <div class="k"><div class="l">行业</div><div class="v" style="font-size:15px">${esc(info.industry||'-')}</div><div class="s">地域 ${esc(info.region||'-')}</div></div>
    <div class="k"><div class="l">动态PE</div><div class="v">${fmt(more.pe_dyna)}</div><div class="s">PB ${fmt(more.pb_mrq)} · 股息率 ${fmt(more.dy_ratio)}%</div></div>
    <div class="k"><div class="l">总市值(亿)</div><div class="v">${fmt(more.total_mv)}</div><div class="s">流通 ${fmt(more.float_mv)}</div></div>
    <div class="k"><div class="l">当日涨幅%</div><div class="v">${fmt(more.zaf)}</div><div class="s">换手 ${fmt(more.hsl)}% · 量比 ${fmt(more.lb)}</div></div>`;
  $('kpi').innerHTML=kpi;

  const infoRows=[['上市日期',info.list_date],['总股本(万股)',fmt(info.total_share)],['流通股本(万股)',fmt(info.float_share)],['HS种类',info.hs_kind],['ST',info.is_st?'是':'否'],['退市板',info.is_quit?'是':'否']];
  const moreRows=[['行情日期',more.hq_date],['52周高',fmt(more.his_high)],['52周低',fmt(more.his_low)],['涨停价',fmt(more.zt_price)],['跌停价',fmt(more.dt_price)],['Beta',fmt(more.beta)],['主力净流入(万)',fmt(more.zjl)]];
  const fin = p.financial && p.financial.length ? p.financial[p.financial.length-1] : {};
  const finRows=[['报告期',fin.report_date],['基本EPS',fmt(fin.FN1)],['营收',fmtBig(fin.FN230)],['归母净利',fmtBig(fin.FN232)],['股东户数',fmt(fin.FN242)],['ROE%',fmt(fin.FN6)],['资产负债率%',fmt(fin.FN210)]];
  const finDates=(p.financial_dates||[]);
  $('tab-overview').innerHTML=`
    <div class="grid2">
      <div class="card"><h4>公司基础信息 <span class="src">get_stock_info</span></h4>
        <div class="info-grid">${infoRows.map(([k,v])=>`<div class="row"><span class="k2">${k}</span><span class="v2">${v==null?'-':esc(String(v))}</span></div>`).join('')}</div></div>
      <div class="card"><h4>市场估值信息 <span class="src">get_more_info</span></h4>
        <div class="info-grid">${moreRows.map(([k,v])=>`<div class="row"><span class="k2">${k}</span><span class="v2">${v==null?'-':esc(String(v))}</span></div>`).join('')}</div></div>
    </div>
    <div class="card"><h4>最新报告期财务摘要 <span class="src">get_financial_data · ${finDates.length}期</span></h4>
      <div class="info-grid">${finRows.map(([k,v])=>`<div class="row"><span class="k2">${k}</span><span class="v2">${v==null?'-':esc(String(v))}</span></div>`).join('')}</div>
      <div style="margin-top:12px;font-size:11px;color:#64748b">报告期序列: ${finDates.slice(0,12).join(' → ')}${finDates.length>12?' …':''}</div>
    </div>`;
}

// ---------- 财务宽表 (分页) ----------
function renderFinancial(d){
  const rows=d.rows||[];
  const total=d.total||0, page=d.page||1, size=d.page_size||10;
  const fieldChips=allFields.filter(f=>f.category==='financial').map(f=>f.field_code);
  const chipHtml=(fieldChips).map(f=>
    `<span class="field-chip ${selFinFields.has(f)?'sel':''}" onclick="toggleFinField('${f}')">${f}</span>`).join('');
  const showFields=fieldChips.filter(f=>selFinFields.has(f));
  const html=`
    <div class="card">
      <div class="toolbar">
        <span style="font-size:12px;color:#64748b">已选字段 (${showFields.length}):</span>
        <button class="btn" onclick="selAllFin()">全选</button>
        <button class="btn" onclick="selDefaultFin()">默认</button>
        <button class="btn" onclick="selClearFin()">清空</button>
        <span style="flex:1"></span>
        <span style="font-size:11px;color:#94a3b8">点击下方字段标签切换</span>
      </div>
      <div style="max-height:70px;overflow:auto;margin-bottom:10px">${chipHtml}</div>
    </div>
    <div class="card"><h4>财务数据宽表 <span class="src">report_date × FN字段</span></h4>
      ${pagerHTML('fin', total, page, size)}
      ${rows.length?`<div class="tbl-wrap"><table><thead><tr><th>报告期</th><th>公告日</th>${showFields.map(f=>`<th title="${f}">${f}</th>`).join('')}</tr></thead>
      <tbody>${rows.map(r=>`<tr><td class="mono">${r.report_date}</td><td class="mono">${r.announce_date||'-'}</td>${showFields.map(f=>`<td class="num">${fmt(r[f])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`
      :'<div class="empty">暂无财务数据 — 在右上角搜索股票，或先在 /api/fundamental/sync 触发同步</div>'}
    </div>`;
  $('tab-financial').innerHTML=html;
}
function toggleFinField(f){selFinFields.has(f)?selFinFields.delete(f):selFinFields.add(f);loadFinancial(cc)}
function selAllFin(){selFinFields=new Set(allFields.filter(x=>x.category==='financial').map(x=>x.field_code));loadFinancial(cc)}
function selDefaultFin(){selFinFields=new Set(['FN1','FN4','FN6','FN210','FN230','FN232','FN242','FN319']);loadFinancial(cc)}
function selClearFin(){selFinFields=new Set();loadFinancial(cc)}

// ---------- 财务长表 (分页) ----------
function renderLong(d, code){
  const rows=d.rows||[];
  const total=d.total||0, page=d.page||1, size=d.page_size||10;
  // 报告期下拉需全量列表 (轻量, 按 code 缓存)
  const cacheKey='_longDates_'+code;
  let longDates=(window[cacheKey])||[];
  const curRd=$('longRd')?$('longRd').value:'';
  if(!longDates.length){
    fetch(`/api/fundamental/financial?code=${encodeURIComponent(code)}&format=long&page=1&page_size=100&all_dates=1`).then(r=>r.json()).then(x=>{
      window[cacheKey]=x.dates||[];
      if($('longRd'))$('longRd').innerHTML=
        `<option value="" ${curRd===''?'selected':''}>全部</option>`+
        `${(x.dates||[]).map(dd=>`<option value="${dd}" ${dd===curRd?'selected':''}>${dd}</option>`).join('')}`;
    }).catch(()=>{});
  }
  $('tab-long').innerHTML=`
    <div class="card">
      <div class="toolbar">
        <label style="font-size:12px;color:#64748b">报告期:</label>
        <select id="longRd" onchange="PAGER.long.page=1;loadLong('${code}')">
          <option value="" ${curRd===''?'selected':''}>全部</option>
          ${longDates.map(x=>`<option value="${x}" ${x===curRd?'selected':''}>${x}</option>`).join('')}
        </select>
        <span style="flex:1"></span>
        <span style="font-size:11px;color:#94a3b8">字段级明细（含中文名/来源接口）</span>
      </div>
    </div>
    <div class="card"><h4>财务长表（EAV 明细）</h4>
      ${pagerHTML('long', total, page, size)}
      ${rows.length?`<div class="tbl-wrap"><table><thead><tr><th>字段代码</th><th>字段中文名</th><th>报告期</th><th>公告日期</th><th>数值</th><th>来源接口</th></tr></thead>
      <tbody>${rows.map(r=>`<tr><td class="mono">${r.field_code}</td><td>${esc(r.field_name||r.field_code)}</td><td class="mono">${r.report_date}</td><td class="mono">${r.announce_date||'-'}</td><td class="num">${fmt(r.value)}</td><td class="mono" style="color:#64748b">get_financial_data</td></tr>`).join('')}</tbody></table></div>`
      :'<div class="empty">暂无数据</div>'}
    </div>`;
}

// ---------- GP 交易专业数据 (分页) ----------
function renderGpjy(d){
  const rows=d.rows||[];
  const total=d.total||0, page=d.page||1, size=d.page_size||10;
  const fields=d.fields||[];
  const fieldMap={};
  (allFields).forEach(f=>{
    if(f.category==='gpjy')fieldMap[f.field_code]=f.field_name;
  });
  // 已废弃硬编码, 中文名统一来自 field_meta (fundamental_fields.GP_NAME)
  const gpName=fieldMap;
  const chipHtml=fields.map(f=>{
    const base=f.replace(/_\d+$/,'');
    const label=`${f} · ${gpName[base]||''}`;
    return `<span class="field-chip ${selGpFields.has(f)?'sel':''}" onclick="toggleGpField('${f}')">${label}</span>`;
  }).join('');
  const show=fields.filter(f=>selGpFields.has(f));
  const html=`
    <div class="card">
      <div class="toolbar">
        <span style="font-size:12px;color:#64748b">GP 字段 (${fields.length}):</span>
        <button class="btn" onclick="selDefaultGp()">默认</button>
        <button class="btn" onclick="selClearGp()">清空</button>
      </div>
      <div style="max-height:120px;overflow:auto">${chipHtml}</div>
    </div>
    <div class="card"><h4>股票交易专业数据 <span class="src">get_gpjy_value</span></h4>
      ${pagerHTML('gp', total, page, size)}
      ${show.length&&rows.length?`<div class="tbl-wrap"><table><thead><tr><th>日期</th>${show.map(f=>`<th title="${f}">${f}</th>`).join('')}</tr></thead>
      <tbody>${rows.map(r=>`<tr><td class="mono">${r.trade_date}</td>${show.map(f=>`<td class="num">${fmt(r[f])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`
      :'<div class="empty">${rows.length?\'请选择GP字段\':\'暂无GP数据 — 通过 /api/fundamental/sync (biz=gpjy) 同步\'}</div>'}
    </div>`;
  $('tab-gpjy').innerHTML=html;
}
function toggleGpField(f){selGpFields.has(f)?selGpFields.delete(f):selGpFields.add(f);loadGpjy(cc)}
function selDefaultGp(){selGpFields=new Set(['GP01_1','GP03_1','GP06_1','GP16_1']);loadGpjy(cc)}
function selClearGp(){selGpFields=new Set();loadGpjy(cc)}

// ---------- 股东明细 (十大股东/十大流通股东, 分页) ----------
const SH_TYPE = {gd:'十大股东', ltgd:'十大流通股东'};
function renderShareholder(d, code){
  const rows=d.rows||[];
  const total=d.total||0, page=d.page||1, size=d.page_size||10;
  const dates=d.dates||[];
  const shSelType=$('shType')?$('shType').value:'';
  const shSelDate=$('shRd')?$('shRd').value:'';
  $('tab-shareholder').innerHTML=`
    <div class="card">
      <div class="toolbar">
        <label style="font-size:12px;color:#64748b">股东类型:</label>
        <select id="shType" onchange="PAGER.sh.page=1;loadShareholder('${code}')">
          <option value="">全部</option>
          <option value="gd" ${shSelType==='gd'?'selected':''}>十大股东</option>
          <option value="ltgd" ${shSelType==='ltgd'?'selected':''}>十大流通股东</option>
        </select>
        <label style="font-size:12px;color:#64748b;margin-left:10px">报告期:</label>
        <select id="shRd" onchange="PAGER.sh.page=1;loadShareholder('${code}')">
          <option value="">全部</option>
          ${dates.map(x=>`<option value="${x}" ${x===shSelDate?'selected':''}>${x}</option>`).join('')}
        </select>
        <span style="flex:1"></span>
        <span style="font-size:11px;color:#94a3b8">download_file(down_type=1) · 十大股东/十大流通股东明细</span>
      </div>
    </div>
    <div class="card"><h4>🏛 股东明细 <span class="src">shareholder_facts</span></h4>
      ${pagerHTML('sh', total, page, size)}
      ${rows.length?`<div class="tbl-wrap"><table><thead><tr><th>报告期</th><th>类型</th><th>排名</th><th>股东名称</th><th>持股数量(股)</th><th>持股比例(%)</th></tr></thead>
      <tbody>${rows.map(r=>`<tr><td class="mono">${r.report_date}</td><td>${SH_TYPE[r.holder_type]||r.holder_type}</td><td class="num">${r.rank}</td><td>${esc(r.holder_name)}</td><td class="num">${fmtBig(r.shares)}</td><td class="num">${fmt(r.pct)}</td></tr>`).join('')}</tbody></table></div>`
      :'<div class="empty">暂无股东明细 — 通过 /api/fundamental/sync (biz=shareholder) 同步</div>'}
    </div>`;
}

// ---------- 主营构成 (按产品/按行业/按地区) ----------
const MB_DIM = {'按产品(项目)':'按产品', '按行业':'按行业', '按地区':'按地区'};
function renderMainbusi(d, code){
  const rows=d.rows||[];
  const total=d.total||0;
  const dates=d.dates||[];
  const profile=d.profile||null;
  const mbSelDate=$('mbRd')?$('mbRd').value:'';
  $('tab-mainbusi').innerHTML=`
    <div class="card">
      <div class="toolbar">
        <label style="font-size:12px;color:#64748b">报告期:</label>
        <select id="mbRd" onchange="loadMainbusi('${code}')">
          <option value="">全部 (${dates.length} 期)</option>
          ${dates.map(x=>`<option value="${x}" ${x===mbSelDate?'selected':''}>${x}</option>`).join('')}
        </select>
        ${profile?`<span class="mb-profile">🏷 ${esc(profile.product_name||'—')} · ${esc(profile.business_desc||'—')}</span>`:''}
        <span style="flex:1"></span>
        <span style="font-size:11px;color:#94a3b8">download_file(down_type=5) · 主营构成明细</span>
      </div>
    </div>
    <div class="card"><h4>🏭 主营构成 <span class="src">mainbusi_facts</span> ${total?'· '+total+' 条':''}</h4>
      ${rows.length?`<div class="tbl-wrap"><table><thead><tr>
        <th>报告期</th><th>维度</th><th>构成项目</th><th>主营收入(元)</th><th>收入比例(%)</th>
        <th>主营成本</th><th>成本比例(%)</th><th>毛利</th><th>利润比例(%)</th><th>毛利率(%)</th>
      </tr></thead>
      <tbody>${rows.map(r=>`<tr>
        <td class="mono">${r.report_date}</td>
        <td>${MB_DIM[r.dim_type]||esc(r.dim_type)}</td>
        <td>${esc(r.item_name)}</td>
        <td class="num">${fmtBig(r.revenue)}</td>
        <td class="num">${fmt(r.revenue_pct)}</td>
        <td class="num">${fmtBig(r.cost)}</td>
        <td class="num">${fmt(r.cost_pct)}</td>
        <td class="num">${fmtBig(r.profit)}</td>
        <td class="num">${fmt(r.profit_pct)}</td>
        <td class="num">${fmt(r.profit_rate)}</td>
      </tr>`).join('')}</tbody></table></div>`
      :'<div class="empty">暂无主营构成 — 通过 /api/fundamental/sync (biz=mainbusi) 同步</div>'}
    </div>`;
}

// ---------- 字段元数据 (分页) ----------
function renderFields(){
  const {page,size}=PAGER.fld;
  fetch(`/api/fundamental/fields?page=${page}&page_size=${size}`).then(r=>r.json()).then(d=>{
    const list=d.fields||[], total=d.total||0;
    const cats={};
    list.forEach(f=>{(cats[f.category]=cats[f.category]||[]).push(f)});
    const catLabel={financial:'专业财务 (FN)',gpjy:'交易专业 (GP)',stock_more:'估值信息 (stock_more)'};
    let html=`<div class="card">${pagerHTML('fld', total, page, size)}</div>`;
    Object.entries(cats).forEach(([cat,arr])=>{
      html+=`<div class="card"><h4>${catLabel[cat]||cat} <span class="src">${arr.length} 字段 · ${arr[0]?arr[0].source_api:'-'}</span></h4>
        <div class="tbl-wrap" style="max-height:400px"><table><thead><tr><th>字段代码</th><th>中文名</th><th>来源接口</th></tr></thead>
        <tbody>${arr.map(f=>`<tr><td class="mono">${f.field_code}</td><td>${esc(f.field_name)}</td><td class="mono" style="color:#64748b">${f.source_api}</td></tr>`).join('')}</tbody></table></div></div>`;
    });
    $('tab-fields').innerHTML=html||'<div class="empty">无字段元数据</div>';
  }).catch(()=>{$('tab-fields').innerHTML='<div class="empty">加载失败</div>'});
}

// ---------- 溯源 ----------
function renderTrace(){
  if(!traceData){$('tab-trace').innerHTML='<div class="empty">加载中...</div>';return}
  const s=traceData.summary, logs=traceData.recent_logs||[], tables=traceData.tables||[];
  const flow=traceData.data_flow||[];
  const flowCss=['tdx','client','store','api','fe'];
  const flowHtml=flow.map((n,i)=>
    `<div class="flow-node ${flowCss[i]||''}"><div class="fn">${i+1}. ${n.step}</div><div class="fd">${n.detail}</div><div class="fi">${n.iface}</div></div>`)
    .join('<div class="flow-arrow">→</div>');
  // 迷你纵向流 (侧边栏)
  $('flowMini').innerHTML=flow.map((n,i)=>`<div style="font-size:11px;color:#64748b"><b style="color:#1e293b">${i+1}.${n.step}</b><br><span style="font-size:10px;color:#94a3b8;font-family:monospace">${n.iface}</span></div>`).join('<div style="color:#94a3b8;text-align:center;font-size:10px">↓</div>');
  const statBoxes=[
    ['数据总量',(s.financial_facts+s.gpjy_facts||0).toLocaleString(),'financial+gpjy 记录'],
    ['财务记录',(s.financial_facts||0).toLocaleString(),'FN 长表'],
    ['交易记录',(s.gpjy_facts||0).toLocaleString(),'GP 长表'],
    ['股东明细',(s.shareholder_facts||0).toLocaleString(),'十大股东/十大流通股东'],
    ['主营构成',(s.mainbusi_facts||0).toLocaleString(),'按产品/按行业/按地区'],
    ['股票覆盖',(traceData.codes||[]).length+' 只',(traceData.codes||[]).slice(0,6).join(' ')],
    ['库大小',s._size_kb?(s._size_kb/1024).toFixed(1)+' MB':'0','fundamental.duckdb'],
  ];
  const logBadge=l=>l.status==='ok'?'<span class="badge ok">OK</span>':l.status==='error'?'<span class="badge err">ERR</span>':'<span class="badge warn">'+esc(l.status)+'</span>';
  $('tab-trace').innerHTML=`
    <div class="card"><h4>🔗 数据链路（溯源）</h4>
      <div class="flow-wrap">${flowHtml}</div>
    </div>
    <div class="trace-grid">${statBoxes.map(([t,v,d])=>`<div class="trace-box"><div class="tb">${t}</div><div class="tn">${v}</div><div class="td">${d}</div></div>`).join('')}</div>
    <div class="grid2">
      <div class="card"><h4>📦 库表结构 <span class="src">fundamental.duckdb</span></h4>
        <div class="tbl-wrap" style="max-height:320px"><table><thead><tr><th>表名</th><th>列数</th><th>行数</th></tr></thead>
        <tbody>${tables.map(t=>`<tr><td class="mono">${t.name}</td><td>${t.columns}</td><td class="num">${t.rows.toLocaleString()}</td></tr>`).join('')}</tbody></table></div>
      </div>
      <div class="card"><h4>📝 更新日志 <span class="src">update_log</span></h4>
        <div class="tbl-wrap" style="max-height:320px"><table class="log-table"><thead><tr><th>时间</th><th>业务</th><th>范围</th><th>状态</th><th>详情</th></tr></thead>
        <tbody>${logs.map(l=>`<tr><td class="mono" style="font-size:10px">${l.finished_at}</td><td class="mono">${l.biz}</td><td>${esc(l.scope)}</td><td>${logBadge(l)}</td><td style="color:#64748b">${esc(l.detail)}</td></tr>`).join('')}</tbody></table></div>
      </div>
    </div>
    <div class="card"><h4>🗂 已同步股票</h4>
      ${(traceData.codes||[]).map(c=>`<span class="code-pill" onclick="pick('${c}','')">${c}</span>`).join('')||'<div class="empty">暂无</div>'}
    </div>`;
}

// ---------- Tab 切换 ----------
function showTab(name){
  document.querySelectorAll('.tabs .tab').forEach(x=>x.classList.toggle('on',x.dataset.tab===name));
  document.querySelectorAll('.tab-pane').forEach(p=>p.style.display='none');
  const pane=$('tab-'+name);
  if(pane)pane.style.display='block';
  if(name==='trace')loadTrace();
  if(name==='fields')renderFields();
}
document.querySelectorAll('.tabs .tab').forEach(t=>{
  t.addEventListener('click',()=>showTab(t.dataset.tab));
});

// ---------- 初始化 ----------
function showErr(tab,msg){$('tab-'+tab).innerHTML='<div class="empty">'+esc(msg)+'</div>'}
async function init(){
  // 连通性 (异步检测, 不阻塞左侧列表加载; admin/status 冷缓存扫描较慢)
  (async()=>{
    try{
      const r=await fetch('/api/admin/status');const d=await r.json();
      $('conn').innerHTML='API <b>●</b> 就绪 · TQ '+(d.tdx_connected?'<b>●</b>':'<b style="color:#f87171">○</b>');
    }catch(e){$('conn').innerHTML='API <b style="color:#f87171">●</b> 不可达'}
  })();
  // 进入页面只加载左侧"已同步股票"列表 (轻量分页, 立即可用),
  // 选中具体股票后才从后端调取对应数据; 字段元数据/溯源按需懒加载
  loadSynced();
  loadAutoSyncConfig();
}
init();

// ---------- 已同步股票列表 (左侧, 轻量分页) ----------
function loadSynced(){
  const {page,size}=STOCK_PAGER;
  fetch(`/api/fundamental/synced?page=${page}&page_size=${size}`).then(r=>r.json()).then(d=>{
    renderSynced(d);
  }).catch(()=>{
    $('stockList').innerHTML='<div class="empty" style="padding:20px">加载失败</div>';
    $('stockPager').style.display='none';
  });
}
function renderSynced(d){
  const items=d.items||[], total=d.total||0;
  $('syncedCount').textContent=total?`(${total})`:'';
  const list=$('stockList');
  if(!items.length){
    list.innerHTML='<div class="empty" style="padding:20px">暂无已同步股票<br>点击右上角"＋ 添加股票 / 同步数据"</div>';
    $('stockPager').style.display='none';
    return;
  }
  list.innerHTML=items.map(it=>{
    const tags=BIZ_ORDER.map(b=>{
      const on=it.types.includes(b);
      return `<span class="st-tag ${on?'on b-'+b:''}" title="${on?('已同步 '+BIZ_SHORT[b]):(BIZ_SHORT[b]+' 未同步')}">${BIZ_SHORT[b]}</span>`;
    }).join('');
    return `<div class="it" data-code="${esc(it.code)}">
      <div class="it-top"><span class="n">${esc(it.name||it.code)}</span><span class="c">${esc(it.code)}</span></div>
      <div class="it-tags">${tags}</div>
    </div>`;
  }).join('');
  list.querySelectorAll('.it').forEach(el=>{
    const nm=el.querySelector('.n')?el.querySelector('.n').textContent:'';
    el.onclick=()=>pick(el.dataset.code,nm);
  });
  // 保持选中高亮
  if(cc)list.querySelectorAll('.it').forEach(x=>x.classList.toggle('sel',x.dataset.code===cc));
  // 分页
  const totalPages=Math.max(1,Math.ceil(total/STOCK_PAGER.size));
  const cur=Math.min(STOCK_PAGER.page,totalPages);
  $('stockPager').innerHTML=`
    <span class="pager-info">共 ${total} 只 · ${totalPages} 页</span>
    <span class="pager-nav">
      <button ${cur<=1?'disabled':''} onclick="STOCK_PAGER.page=${cur-1};loadSynced()">‹ 上一页</button>
      <span class="pager-cur">${cur} / ${totalPages}</span>
      <button ${cur>=totalPages?'disabled':''} onclick="STOCK_PAGER.page=${cur+1};loadSynced()">下一页 ›</button>
    </span>`;
  $('stockPager').style.display='flex';
}

// ---------- 懒加载: 字段元数据 (财务/交易 Tab 需要) ----------
function ensureFields(cb){
  if(allFields.length){if(cb)cb();return}
  fetch('/api/fundamental/fields?all=1').then(r=>r.json()).then(d=>{
    allFields=d.fields||[];
    if(cb)cb();
  }).catch(()=>{if(cb)cb()});
}

// ---------- 懒加载: 数据溯源 ----------
function loadTrace(){
  if(!traceData){
    $('tab-trace').innerHTML='<div class="empty">加载中...</div>';
    fetch('/api/fundamental/trace').then(r=>r.json()).then(d=>{
      traceData=d;renderTrace();
    }).catch(()=>{$('tab-trace').innerHTML='<div class="empty">溯源数据加载失败</div>'});
    return;
  }
  renderTrace();
}

// ============================================================
//  股票基本面数据同步（前端触发）
// ============================================================
const BIZ_LABELS = {basic:'基础信息',financial:'专业财务',gpjy:'交易专业数据',chip:'筹码指标',l2:'L2 扩展',shareholder:'股东明细',mainbusi:'主营构成'};
const BIZ_DESC = {
  basic:'get_stock_info + get_more_info',
  financial:'FN1-584 全历史长表 (每票较慢)',
  gpjy:'融资融券/龙虎榜/陆股通/涨停等 GP 系列',
  chip:'MCST/CYS/ASR/SCR/CYC 筹码指标',
  l2:'分档成交/主力净额/封单额 L2 扩展日线',
  shareholder:'download_file → 十大股东/十大流通股东',
  mainbusi:'download_file → 主营构成 (按产品/按行业/按地区)'
};

function openSync(){
  $('syncModal').style.display='flex';
  $('syncCodes').value=cc||'';
  updateCodePreview();
  $('syncProgress').style.display='none';
  $('progLog').innerHTML='';
  $('progFill').className='prog-fill';
  $('progFill').style.width='0%';
  $('progClose').style.display='none';
  $('btnSyncRun').disabled=false;
  $('btnSyncRun').textContent='▶ 开始同步';
}
function closeSync(){
  if($('btnSyncRun').disabled){
    if(!confirm('正在同步中，确定关闭吗？（后端会继续执行，但前端看不到进度）'))return;
  }
  $('syncModal').style.display='none';
}

// ----- 代码格式化 -----
function normalizeCode(raw){
  let c=String(raw||'').trim().toUpperCase();
  if(!c)return null;
  // 已有后缀
  if(/\.(SH|SZ|BJ|HK|US|CSI|CFF|SHF|DCE|CZC|INE|GFE|SHO|SZO|OF|NQ|CNI|HI|HG|QHZ|CFFO|CZCO|DCEO|SHFO|GFEO)$/i.test(c))return c;
  // 纯数字 6 位
  if(/^\d{6}$/.test(c)){
    const n=parseInt(c,10);
    // 上交所
    if(n>=600000&&n<=699999)return c+'.SH';
    // 深交所
    if((n>=0&&n<=399999)||(n>=100000&&n<=499999)){
      // 001xxx / 000xxx / 300xxx / 002xxx / 003xxx 都是 SZ
      return c+'.SZ';
    }
    // 北交所
    if((n>=430000&&n<=439999)||(n>=830000&&n<=839999))return c+'.BJ';
    // 无法判定 — 原样返回（后续会在预览里标红）
    return c;
  }
  // 其他格式原样返回
  return c;
}
function parseCodes(text){
  const list=String(text||'').split(/[\s,，;；\n\r\t]+/).map(normalizeCode).filter(Boolean);
  // 去重保序
  const seen=new Set(), out=[];
  for(const c of list){
    if(!seen.has(c)){seen.add(c);out.push(c)}
  }
  return out;
}
function validateCode(c){
  // 已带后缀 → OK；纯数字 6 位但未匹配规则 → bad
  if(/\.(SH|SZ|BJ|HK|US|CSI|CFF|SHF|DCE|CZC|INE|GFE|SHO|SZO|OF|NQ|CNI|HI|HG|QHZ|CFFO|CZCO|DCEO|SHFO|GFEO)$/i.test(c))return true;
  if(/^\d{6}$/.test(c)){
    const n=parseInt(c,10);
    if((n>=600000&&n<=699999)||(n>=0&&n<=399999)||(n>=100000&&n<=499999)||(n>=430000&&n<=439999)||(n>=830000&&n<=839999))return true;
    return false;  // 无法判定市场归属
  }
  // 其他允许（如指数代码 000300.CSI 等，已被 normalizeCode 处理过）
  return true;
}
function updateCodePreview(){
  const codes=parseCodes($('syncCodes').value);
  const pv=$('syncCodesPreview');
  if(!codes.length){pv.innerHTML='<span style="color:#94a3b8">尚未输入代码</span>';return codes}
  const ok=[], bad=[];
  codes.forEach(c=>validateCode(c)?ok.push(c):bad.push(c));
  let html=`<span style="color:#1e293b;font-weight:600">共 ${codes.length} 个</span>`;
  if(ok.length)html+=ok.map(c=>`<span class="pv-ok">${c}</span>`).join('');
  if(bad.length)html+=bad.map(c=>`<span class="pv-bad">${c}(无法识别市场)</span>`).join('');
  pv.innerHTML=html;
  return codes;
}
$('syncCodes').addEventListener('input',updateCodePreview);

// ----- 进度条 -----
function setProgress(pct,title){
  $('progFill').style.width=Math.min(100,Math.max(0,pct))+'%';
  if(title)$('progTitle').textContent=title;
}
function appendLog(msg,cls){
  const log=$('progLog');
  const time=new Date().toLocaleTimeString('zh-CN',{hour12:false});
  const div=document.createElement('div');
  div.className=cls||'';
  div.textContent=`[${time}] ${msg}`;
  log.appendChild(div);
  log.scrollTop=log.scrollHeight;
}

// ----- SSE 进度流 -----
function connectSSE(taskId, bizLabel, onProgress, onDone, onError){
  const es=new EventSource(`/api/screener/task/${taskId}/stream`);
  es.addEventListener('progress', e=>{
    const d=JSON.parse(e.data);
    onProgress(d);
  });
  es.addEventListener('done', e=>{
    const d=JSON.parse(e.data);
    es.close();
    onDone(d);
  });
  es.addEventListener('error', e=>{
    let d={error:'SSE 连接异常'};
    try{d=JSON.parse(e.data)}catch(_){}
    es.close();
    onError(d);
  });
  return es;
}

// ----- 同步执行 -----
async function runSync(){
  const codes=updateCodePreview();
  if(!codes.length){alert('请先输入股票代码');return}
  const bad=codes.filter(c=>!validateCode(c));
  if(bad.length&&!confirm(`有 ${bad.length} 个代码无法识别市场归属，是否继续？`))return;

  const bizList=[...document.querySelectorAll('.biz-item input:checked')].map(c=>c.value);
  if(!bizList.length){alert('请至少选择一个同步类型');return}

  const force=$('syncForce').checked;
  $('btnSyncRun').disabled=true;
  $('btnSyncRun').textContent='⏳ 同步中...';
  $('syncProgress').style.display='block';
  $('progClose').style.display='none';
  $('progFill').className='prog-fill';
  $('progFill').style.width='0%';
  $('progLog').innerHTML='';

  appendLog(`目标 ${codes.length} 只股票 × ${bizList.length} 种业务`);
  appendLog(`业务顺序: ${bizList.map(b=>BIZ_LABELS[b]).join(' → ')}`);

  const taskResults={};
  let hasError=false;

  function updateOverallProgress(){
    const totalPct=Object.values(taskResults).reduce((s,r)=>s+(r.pct||0),0);
    const overall=totalPct/bizList.length;
    const activeBiz=bizList.find(b=>!taskResults[b]||taskResults[b].status==='running');
    const parts=bizList.map(b=>{
      const r=taskResults[b];
      if(!r)return `${BIZ_LABELS[b]}:待提交`;
      if(r.status==='running')return `${BIZ_LABELS[b]}:${r.pct}%`;
      if(r.status==='done')return `${BIZ_LABELS[b]}:✓`;
      if(r.status==='error')return `${BIZ_LABELS[b]}:✗`;
      return `${BIZ_LABELS[b]}:${r.pct}%`;
    });
    setProgress(overall, parts.join(' | '));
  }

  for(const biz of bizList){
    const body={codes,biz,force,async:true};
    if(biz==='chip'){
      const v=document.querySelector(`.biz-param[data-biz="chip"] .param-num`);
      if(v)body.days=+v.value;
    }else if(biz==='l2'){
      const v=document.querySelector(`.biz-param[data-biz="l2"] .param-num`);
      if(v)body.count=+v.value;
    }else if(biz==='shareholder'){
      const v=document.querySelector(`.biz-param[data-biz="shareholder"] .param-num`);
      if(v)body.years=+v.value;
    }else if(biz==='mainbusi'){
      const v=document.querySelector(`.biz-param[data-biz="mainbusi"] .param-num`);
      if(v)body.years=+v.value;
    }

    taskResults[biz]={status:'submitting',pct:0};
    updateOverallProgress();

    try{
      appendLog(`────────── ${BIZ_LABELS[biz]} ──────────`);
      const resp=await fetch('/api/fundamental/sync',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(body)
      });
      const data=await resp.json();
      if(!resp.ok){
        hasError=true;
        taskResults[biz]={status:'error',pct:0,error:data.error||`HTTP ${resp.status}`};
        appendLog(`❌ ${BIZ_LABELS[biz]} 提交失败: ${taskResults[biz].error}`,'log-err');
        updateOverallProgress();
        continue;
      }
      const tid=data.task_id;
      appendLog(`已提交 ${BIZ_LABELS[biz]} 任务 (${tid}), 等待进度...`);

      await new Promise((resolve)=>{
        const es=connectSSE(tid, BIZ_LABELS[biz],
          (d)=>{
            taskResults[biz]={status:'running',pct:d.pct||0,info:d.info};
            const info=d.info||{};
            if(info.msg&&(info.msg.includes('开始同步')||info.msg.includes('完成'))){
              appendLog(`[${BIZ_LABELS[biz]}] ${info.msg}`);
            }
            if(info.total_records){
              const elapsed=info.elapsed?`${info.elapsed}s`:'';
              appendLog(`[${BIZ_LABELS[biz]}] 进度 ${d.pct}% · ${d.done||0}/${d.total||0} · ${info.total_records}条 ${elapsed}`);
            }
            updateOverallProgress();
          },
          (d)=>{
            taskResults[biz]={status:'done',pct:100,result:d.result};
            es.close();
            const r=d.result||{};
            const stats=[];
            if(r.total_records!=null)stats.push(`${r.total_records}条`);
            if(r.updated!=null)stats.push(`更新 ${r.updated}`);
            if(r.skipped!=null)stats.push(`跳过 ${r.skipped}`);
            if(r.errors!=null)stats.push(`错误 ${r.errors}`);
            if(r.per_code)stats.push(`${Object.keys(r.per_code).length}只股票`);
            if(r.unsupported)stats.push(`${r.unsupported}只不支持`);
            const msg=stats.length?stats.join(' · '):(r.ok?'OK':'失败');
            appendLog(`✅ ${BIZ_LABELS[biz]} 完成 — ${msg}`,
              r.errors>0?'log-warn':'log-ok');
            updateOverallProgress();
            resolve();
          },
          (d)=>{
            hasError=true;
            taskResults[biz]={status:'error',pct:0,error:d.error};
            appendLog(`❌ ${BIZ_LABELS[biz]} 失败: ${d.error}`,'log-err');
            updateOverallProgress();
            resolve();
          }
        );
      });
    }catch(err){
      hasError=true;
      taskResults[biz]={status:'error',pct:0,error:err.message};
      appendLog(`❌ ${BIZ_LABELS[biz]} 异常: ${err.message}`,'log-err');
      updateOverallProgress();
    }
  }

  setProgress(100, hasError?'部分完成':'全部完成 ✓');
  $('progFill').className='prog-fill '+(hasError?'err':'done');
  $('btnSyncRun').disabled=false;
  $('btnSyncRun').textContent='🔄 重新同步';
  $('progClose').style.display='inline';
  $('progClose').textContent='✕ 关闭';
  $('progClose').onclick=()=>{$('syncModal').style.display='none';refreshAll()};
  appendLog(`━━━ 同步完成 (${new Date().toLocaleTimeString('zh-CN',{hour12:false})}) ━━━`,hasError?'log-warn':'log-ok');

  refreshAll();
}

function refreshAll(){
  loadSynced();
  if(traceData||document.querySelector('.tab.on')?.dataset.tab==='trace'){
    traceData=null;
    loadTrace();
  }
}

// ============================================================
//  自动同步面板
// ============================================================
let _autoSyncConfig=null;

const AS_BIZ_LABELS={stock_basic:'基础',financial:'财务',gpjy:'交易',chip:'筹码',l2:'L2',shareholder:'股东',mainbusi:'主营'};

async function loadAutoSyncConfig(){
  try{
    const r=await fetch('/api/fundamental/auto-sync/config');
    _autoSyncConfig=await r.json();
    renderAutoSyncPanel();
  }catch(e){
    console.error('加载自动同步配置失败',e);
  }
}

function renderAutoSyncPanel(){
  if(!_autoSyncConfig)return;
  const cfg=_autoSyncConfig;
  $('asEnabled').checked=!!cfg.enabled;
  $('asOnStartup').checked=!!cfg.on_startup;
  $('asDelay').value=cfg.delay_seconds||3;

  const bizInputs=document.querySelectorAll('#asBizList .as-biz-item input');
  bizInputs.forEach(inp=>{
    const b=inp.dataset.biz;
    inp.checked=!!(cfg.biz&&cfg.biz[b]);
  });

  const enabled=!!cfg.enabled;
  const bizList=$('asBizList');
  if(enabled){bizList.classList.remove('disabled')}else{bizList.classList.add('disabled')}

  const hasBiz=Object.values(cfg.biz||{}).some(Boolean);
  $('asRunBtn').disabled=!enabled||!hasBiz;

  const lastInfo=$('asLastInfo');
  if(cfg.last_run){
    const r=cfg.last_result||{};
    const parts=Object.entries(r).map(([b,v])=>{
      const total=v.total_records||v.updated||0;
      const errs=v.errors||0;
      return `${AS_BIZ_LABELS[b]||b}:${total}条${errs?`(错${errs})`:''}`;
    });
    lastInfo.innerHTML=`<div>上次: ${cfg.last_run}</div><div>${parts.join(' · ')}</div>`;
  }else{
    lastInfo.innerHTML='<div style="color:#94a3b8">尚未执行过</div>';
  }
}

function getAutoSyncFormData(){
  const biz={};
  document.querySelectorAll('#asBizList .as-biz-item input').forEach(inp=>{
    biz[inp.dataset.biz]=inp.checked;
  });
  return{
    enabled:$('asEnabled').checked,
    on_startup:$('asOnStartup').checked,
    delay_seconds:parseInt($('asDelay').value)||3,
    biz
  };
}

$('asEnabled')?.addEventListener('change',()=>{
  const enabled=$('asEnabled').checked;
  $('asBizList').classList.toggle('disabled',!enabled);
  const hasBiz=[...document.querySelectorAll('#asBizList .as-biz-item input')].some(i=>i.checked);
  $('asRunBtn').disabled=!enabled||!hasBiz;
});
document.querySelectorAll('#asBizList .as-biz-item input').forEach(inp=>{
  inp.addEventListener('change',()=>{
    const enabled=$('asEnabled').checked;
    const hasBiz=[...document.querySelectorAll('#asBizList .as-biz-item input')].some(i=>i.checked);
    $('asRunBtn').disabled=!enabled||!hasBiz;
  });
});

$('asSaveBtn')?.addEventListener('click',async()=>{
  const data=getAutoSyncFormData();
  try{
    const r=await fetch('/api/fundamental/auto-sync/config',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(data)
    });
    const d=await r.json();
    if(d.ok){
      _autoSyncConfig=d.config;
      renderAutoSyncPanel();
      $('asSaveBtn').textContent='✓ 已保存';
      setTimeout(()=>{$('asSaveBtn').textContent='保存设置'},1500);
    }
  }catch(e){
    alert('保存失败: '+e.message);
  }
});

$('asRunBtn')?.addEventListener('click',async()=>{
  const data=getAutoSyncFormData();
  if(!data.enabled){alert('请先启用自动同步');return}
  const hasBiz=Object.values(data.biz).some(Boolean);
  if(!hasBiz){alert('请至少选择一种同步业务');return}

  $('asRunBtn').disabled=true;
  $('asRunBtn').textContent='同步中...';

  try{
    const r=await fetch('/api/fundamental/auto-sync/run',{
      method:'POST',headers:{'Content-Type':'application/json'}
    });
    const d=await r.json();
    if(!r.ok||!d.ok){
      alert(d.error||'执行失败');
      $('asRunBtn').disabled=false;
      $('asRunBtn').textContent='立即执行';
      return;
    }

    openSync();
    $('syncProgress').style.display='block';
    $('progClose').style.display='none';
    $('progFill').className='prog-fill';
    $('progFill').style.width='0%';
    $('progLog').innerHTML='';
    appendLog(`自动同步已启动: ${d.biz.map(b=>AS_BIZ_LABELS[b]||b).join(', ')}`);
    appendLog(`任务ID: ${d.task_id}`);

    const es=connectSSE(d.task_id,'自动同步',
      (evt)=>{
        const info=evt.info||{};
        if(info.msg)appendLog(`[${evt.stage}] ${info.msg}`);
        const stats=[];
        if(info.total_records)stats.push(`${info.total_records}条`);
        if(info.elapsed)stats.push(`${info.elapsed}s`);
        if(stats.length)appendLog(`进度 ${evt.pct}% · ${evt.done||0}/${evt.total||0} · ${stats.join(' · ')}`);
        setProgress(evt.pct,`自动同步: ${evt.pct}%`);
      },
      (evt)=>{
        es.close();
        const result=evt.result||{};
        const parts=Object.entries(result).map(([b,v])=>{
          const total=v.total_records||v.updated||0;
          return `${AS_BIZ_LABELS[b]||b}:${total}条`;
        });
        appendLog(`✅ 自动同步完成 — ${parts.join(' · ')}`,'log-ok');
        setProgress(100,'自动同步完成 ✓');
        $('progFill').className='prog-fill done';
        $('progClose').style.display='inline';
        $('progClose').textContent='✕ 关闭';
        $('progClose').onclick=()=>{$('syncModal').style.display='none';refreshAll()};
        $('asRunBtn').disabled=false;
        $('asRunBtn').textContent='立即执行';
        loadAutoSyncConfig();
        refreshAll();
      },
      (evt)=>{
        es.close();
        appendLog(`❌ 自动同步失败: ${evt.error}`,'log-err');
        $('asRunBtn').disabled=false;
        $('asRunBtn').textContent='立即执行';
      }
    );
  }catch(e){
    $('asRunBtn').disabled=false;
    $('asRunBtn').textContent='立即执行';
    alert('执行失败: '+e.message);
  }
});