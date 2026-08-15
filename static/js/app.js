// Last modified: 2026-08-11 20:35:24
const KC=echarts.init(document.getElementById('kline'));
const VC=echarts.init(document.getElementById('vol'));
const MC=echarts.init(document.getElementById('macd'));
const JC=echarts.init(document.getElementById('kdj'));
KC.group=VC.group=MC.group=JC.group='klineGroup';
echarts.connect('klineGroup');
let cc=null, cp='1d';
let _rangeKey='1y';

const q=document.getElementById('q'), sug=document.getElementById('sug');
let _sugItems=[], _sugIdx=-1;
const _TYPE_LABEL={stock:'A股',index:'指数',sector:'板块',etf:'ETF',bond:'债券',fund:'基金'};
function _hl(t,kw){if(!kw)return t;const p=t.toLowerCase().indexOf(kw.toLowerCase());if(p<0)return t;return t.slice(0,p)+'<b>'+t.slice(p,p+kw.length)+'</b>'+t.slice(p+kw.length)}
function _renderSug(items,kw){
  const grouped={};
  const order=['stock','index','sector','etf','bond','fund'];
  items.forEach(x=>{(grouped[x.type]=grouped[x.type]||[]).push(x)});
  let html='';
  order.forEach(t=>{
    if(grouped[t]&&grouped[t].length){
      html+='<div class="grp">'+_TYPE_LABEL[t]+' · '+grouped[t].length+'</div>';
      grouped[t].forEach(x=>{
        html+=`<div data-code="${x.code}" data-name="${x.name}" data-type="${x.type}">
          <div class="nm"><b>${_hl(x.name,kw)}</b> <span class="cd">${_hl(x.code,kw)}</span></div>
          <span class="tag ${x.type}">${_TYPE_LABEL[x.type]||x.type}</span></div>`;
      });
    }
  });
  return html;
}
q.addEventListener('input', debounce(()=>{
  const kw=q.value.trim();
  if(kw.length<1){sug.style.display='none';return}
  fetch('/api/search?q='+encodeURIComponent(kw)).then(r=>r.json()).then(l=>{
    _sugItems=l; _sugIdx=-1;
    if(!l.length){sug.style.display='none';return}
    sug.innerHTML=_renderSug(l,kw);
    sug.style.display='block';
  }).catch(()=>{sug.style.display='none'});
},220));
q.addEventListener('keydown',e=>{
  const rows=sug.querySelectorAll('[data-code]');
  if(!rows.length)return;
  if(e.key==='ArrowDown'){
    e.preventDefault(); _sugIdx=Math.min(_sugIdx+1,rows.length-1);
    rows.forEach((r,i)=>r.classList.toggle('active',i===_sugIdx));
    rows[_sugIdx].scrollIntoView({block:'nearest'});
  } else if(e.key==='ArrowUp'){
    e.preventDefault(); _sugIdx=Math.max(_sugIdx-1,-1);
    rows.forEach((r,i)=>r.classList.toggle('active',i===_sugIdx));
    if(_sugIdx>=0)rows[_sugIdx].scrollIntoView({block:'nearest'});
  } else if(e.key==='Enter'&&_sugIdx>=0){
    e.preventDefault(); rows[_sugIdx].click();
  } else if(e.key==='Escape'){
    sug.style.display='none';
  }
});
sug.addEventListener('click',e=>{
  const d=e.target.closest('[data-code]');if(!d)return;
  sug.style.display='none';q.value=d.dataset.name;loadStock(d.dataset.code,d.dataset.name);
});
document.addEventListener('click',e=>{if(!e.target.closest('.sb'))sug.style.display='none'});
function debounce(fn,t=300){let h;return(...a)=>{clearTimeout(h);h=setTimeout(()=>fn(...a),t)}}

function sp(b,p){document.querySelectorAll('.ct button').forEach(x=>x.classList.remove('on'));b.classList.add('on');cp=p;if(cc)loadStock(cc)}
function rk(){if(cc)loadStock(cc,null,true)}

function setRange(k){
  _rangeKey=k;
  document.querySelectorAll('.ct .range-btn').forEach(x=>x.classList.remove('on'));
  if(k==='custom'){
    document.getElementById('dateStart').style.borderColor='#3b82f6';
    document.getElementById('dateEnd').style.borderColor='#3b82f6';
  } else {
    const el=document.getElementById('range_'+k);
    if(el)el.classList.add('on');
    document.getElementById('dateStart').style.borderColor='#cbd5e1';
    document.getElementById('dateEnd').style.borderColor='#cbd5e1';
    document.getElementById('dateStart').value='';
    document.getElementById('dateEnd').value='';
  }
  if(cc)loadStock(cc);
}

function getRangeParams(){
  const end=new Date();
  let start=null;
  if(_rangeKey==='custom'){
    const ds=document.getElementById('dateStart').value;
    const de=document.getElementById('dateEnd').value;
    if(ds||de){
      const params={n:1000};
      if(ds)params.start=ds;
      if(de)params.end=de;
      return params;
    }
  } else if(_rangeKey==='all'){
    return {n:1000};
  } else {
    const months={'3m':3,'6m':6,'1y':12,'3y':36}[_rangeKey]||12;
    start=new Date();start.setMonth(start.getMonth()-months);
    const fmt=d=>d.toISOString().slice(0,10);
    return {start:fmt(start),end:fmt(end),n:1000};
  }
  return {n:180};
}

function loadStock(code,name,refresh){
  cc=code;
  const rp=getRangeParams();
  const qs=Object.entries({code,period:cp,...rp,...(refresh?{refresh:1}:{})}).map(([k,v])=>`${k}=${v}`).join('&');
  fetch(`/api/kline?${qs}`).then(r=>r.json()).then(d=>{
    renderKpi(d);renderKC(d);renderInd(d.more);loadPredict(code);
    fetch('/api/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({code:code,name:d.more?.name||name||code})}).then(r=>r.json()).finally(renderWL);
  });
}

function renderKpi(d){
  const l=d.latest||{}, s=d.snap||{}, nm=d.more?.name||d.code;
  const pc=s.preclose||l.close, nv=s.now||l.close;
  const ch=pc?(nv-pc):0, cp_=pc?(ch/pc*100):0;
  const cls=ch>=0?'up':'down', sym=ch>=0?'+':'';
  document.getElementById('kpi').style.display='flex';
  document.getElementById('kpi').innerHTML=`
    <div class="k"><div class="l">名称</div><div class="v">${nm}</div></div>
    <div class="k"><div class="l">现价</div><div class="v ${cls}">${nv}</div></div>
    <div class="k"><div class="l">涨跌幅</div><div class="v ${cls}">${sym}${ch.toFixed(2)} (${sym}${cp_.toFixed(2)}%)</div></div>
    <div class="k"><div class="l">今开</div><div class="v">${s.open||l.open}</div></div>
    <div class="k"><div class="l">最高/最低</div><div class="v">${s.high||l.high} / ${s.low||l.low}</div></div>
    <div class="k"><div class="l">成交额(万)</div><div class="v">${(s.amount||l.amount||0).toLocaleString()}</div></div>`;
}
function renderKC(d){
  if(!d||!Array.isArray(d.data)||!d.data.length){const empty=[{value:0}];KC.setOption({xAxis:{data:[]},series:[{type:'candlestick',data:empty}]});return;}
  const dt=d.data, dates=dt.map(x=>x.date);
  const ohlc=dt.map(x=>[x.open,x.close,x.low,x.high]);
  const volData=dt.map((x,i)=>({value:x.volume/100,itemStyle:{color:(dt[i].close-(i>0?dt[i-1].close:dt[i].close))>=0?'#dc2626':'#16a34a'}}));
  const mk=(k)=>dt.map(x=>[x.date,x[k]]);
  KC.setOption({
    backgroundColor:'transparent',animation:false,
    tooltip:{trigger:'axis',axisPointer:{type:'cross',link:[{xAxisIndex:'all'}]}},
    legend:{data:['K线','MA5','MA10','MA20','MA60'],top:6,textStyle:{fontSize:11,color:'#94a3b8'}},
    grid:{left:60,right:60,top:40,bottom:42},
    xAxis:{type:'category',data:dates,axisLabel:{show:false},splitLine:{show:false},axisTick:{show:false}},
    yAxis:{type:'value',scale:true,axisLabel:{fontSize:10,color:'#94a3b8'},splitLine:{lineStyle:{color:'#f1f5f9'}}},
    dataZoom:[
      {type:'inside',xAxisIndex:0},
      {type:'slider',xAxisIndex:0,height:16,bottom:6,borderColor:'#e2e8f0',fillerColor:'rgba(99,102,241,.25)',handleStyle:{color:'#6366f1'},textStyle:{color:'#64748b',fontSize:10},showDetail:true}
    ],
    series:[
      {name:'K线',type:'candlestick',data:ohlc,itemStyle:{color:'#dc2626',color0:'#16a34a',borderColor:'#dc2626',borderColor0:'#16a34a'}},
      {name:'MA5',type:'line',data:mk('ma5'),smooth:true,symbol:'none',lineStyle:{width:1,color:'#f59e0b'}},
      {name:'MA10',type:'line',data:mk('ma10'),smooth:true,symbol:'none',lineStyle:{width:1,color:'#3b82f6'}},
      {name:'MA20',type:'line',data:mk('ma20'),smooth:true,symbol:'none',lineStyle:{width:1,color:'#8b5cf6'}},
      {name:'MA60',type:'line',data:mk('ma60'),smooth:true,symbol:'none',lineStyle:{width:1,color:'#ec4899'}}
    ]
  },true);
  VC.setOption({
    backgroundColor:'transparent',animation:false,
    tooltip:{trigger:'axis',axisPointer:{type:'cross',link:[{xAxisIndex:'all'}]}},
    legend:{data:['成交量'],top:2,textStyle:{fontSize:11,color:'#94a3b8'}},
    grid:{left:60,right:60,top:24,bottom:10},
    xAxis:{type:'category',data:dates,axisLabel:{show:false},splitLine:{show:false},axisTick:{show:false}},
    yAxis:{type:'value',scale:true,axisLabel:{fontSize:10,color:'#94a3b8'},splitLine:{lineStyle:{color:'#f1f5f9'}}},
    dataZoom:[{type:'inside',xAxisIndex:0}],
    series:[{name:'成交量',type:'bar',data:volData}]
  },true);
  MC.setOption({
    backgroundColor:'transparent',animation:false,tooltip:{trigger:'axis',axisPointer:{type:'cross',link:[{xAxisIndex:'all'}]}},
    legend:{data:['DIF','DEA','MACD'],top:2,textStyle:{fontSize:11,color:'#94a3b8'}},
    grid:{left:60,right:60,top:24,bottom:10},
    xAxis:{type:'category',data:dates,axisLabel:{show:false},splitLine:{show:false}},
    yAxis:{type:'value',scale:true,axisLabel:{fontSize:10,color:'#94a3b8'},splitLine:{lineStyle:{color:'#f1f5f9'}}},
    dataZoom:[{type:'inside',xAxisIndex:0}],
    series:[
      {name:'DIF',type:'line',data:mk('dif'),smooth:true,symbol:'none',lineStyle:{width:1,color:'#3b82f6'}},
      {name:'DEA',type:'line',data:mk('dea'),smooth:true,symbol:'none',lineStyle:{width:1,color:'#f59e0b'}},
      {name:'MACD',type:'bar',data:dt.map(x=>({value:x.macd,itemStyle:{color:x.macd>=0?'#dc2626':'#16a34a'}}))}
    ]
  },true);
  JC.setOption({
    backgroundColor:'transparent',animation:false,tooltip:{trigger:'axis',axisPointer:{type:'cross',link:[{xAxisIndex:'all'}]}},
    legend:{data:['K','D','J'],top:2,textStyle:{fontSize:11,color:'#94a3b8'}},
    grid:{left:60,right:40,top:22,bottom:20},
    xAxis:{type:'category',data:dates,axisLabel:{fontSize:10,color:'#94a3b8'},splitLine:{show:false}},
    yAxis:{type:'value',scale:true,axisLabel:{fontSize:10,color:'#94a3b8'},splitLine:{lineStyle:{color:'#f1f5f9'}}},
    dataZoom:[
      {type:'inside',xAxisIndex:0},
      {type:'inside',yAxisIndex:0},
      {type:'slider',yAxisIndex:0,width:14,right:4,borderColor:'#e2e8f0',fillerColor:'rgba(139,92,246,.3)',handleStyle:{color:'#8b5cf6'},textStyle:{color:'#64748b',fontSize:10,align:'right'},showDetail:true}
    ],
    series:[
      {name:'K',type:'line',data:mk('k'),smooth:true,symbol:'none',lineStyle:{width:1,color:'#f59e0b'}},
      {name:'D',type:'line',data:mk('d'),smooth:true,symbol:'none',lineStyle:{width:1,color:'#3b82f6'}},
      {name:'J',type:'line',data:mk('j'),smooth:true,symbol:'none',lineStyle:{width:1,color:'#8b5cf6'}}
    ]
  },true);
}

const _chartRefs={kline:KC,vol:VC,macd:MC,kdj:JC};
function openChartModal(id,title){
  const src=_chartRefs[id];if(!src)return;
  document.getElementById('cmTitle').textContent=title+' — 全屏视图';
  const body=document.getElementById('cmBody');body.innerHTML='';
  const host=document.createElement('div');host.style.cssText='width:100%;height:100%';body.appendChild(host);
  document.getElementById('chartModal').classList.add('on');
  setTimeout(()=>{
    const m=echarts.init(host);
    const opt=src.getOption();
    opt.grid=(opt.grid||[]).map(g=>({...g,left:60,right:30,top:(g.top||24),bottom:(g.bottom||40)}));
    opt.xAxis=(opt.xAxis||[]).map(a=>({...a,axisLabel:{...(a.axisLabel||{}),show:true,fontSize:11}}));
    opt.yAxis=(opt.yAxis||[]).map(a=>({...a,axisLabel:{...(a.axisLabel||{}),fontSize:11}}));
    opt.legend=(opt.legend||[]).map(l=>({...l,textStyle:{...(l.textStyle||{}),fontSize:12}}));
    m.setOption(opt);
    m.resize();
    src._modalRef=m;
    window._modalChart=m;
    window.addEventListener('resize',()=>m.resize());
  },50);
}
function closeChartModal(){
  document.getElementById('chartModal').classList.remove('on');
  if(window._modalChart){try{window._modalChart.dispose()}catch(e){};window._modalChart=null}
}

function renderInd(m){
  if(!m){document.getElementById('inds').style.display='none';return}
  document.getElementById('inds').style.display='block';

  const IND = [
    {key:'pe',    label:'市盈率 PE',  tip:'PE=股价/每股收益，衡量估值水平。<b>PE 低</b> → 低估/价值股；<b>PE 高</b> → 高估/成长股'},
    {key:'pb',    label:'市净率 PB',  tip:'PB=股价/每股净资产，<b>PB<1</b> 称破净，PB<b>2~5</b> 为优质区间'},
    {key:'zsz',   label:'总市值',     tip:'公司全部股份×股价。<b>大盘>500亿</b> 稳；<b>小盘<50亿</b> 波动大'},
    {key:'hsl',   label:'换手率',     tip:'当日成交股/流通股本，<b>>5%</b> 亢奋风险；<b><1%</b> 流动性差'},
    {key:'liab',  label:'量比',       tip:'当日成交量/5日均量。<b>>2</b> 放量；<b><0.5</b> 缩量。衡量关注度变化'},
    {key:'beta',  label:'Beta 系数',  tip:'相对大盘波动。<b>Beta>1</b> 波动更大；<b><1</b> 防守型；<b>负数</b> 反向走势'},
    {key:'dy_ratio',label:'股息率%',   tip:'年分红/股价×100。<b>>4%</b> 高息诱人；<b><2%</b> 分红偏弱'},
    {key:'mid_high_pct',label:'52周位置',tip:'当前价在52周高低区间的百分比位置。<b><30%</b> 接近底部区域；<b>>80%</b> 接近高位'}
  ];

  const fmt = v => v===null||v===undefined||v==='-' ? '-' : v;
  const fmtNum = (v,dec=2) => {
    if(v===null||v===undefined||v==='-') return '-';
    const n = Number(v);
    if(isNaN(n)) return v;
    return Number.isInteger(n) ? n : n.toFixed(dec);
  };

  let summary = '';
  const s1 = m.pe_label||'-', s2 = m.pb_label||'-', s3 = m.beta_label||'-', s4 = m.dy_label||'-', s5 = m.hsl_label||'-';
  const peHL = ['低估','合理','低PB','高息','热门','活跃'].filter(x=>[s1,s2,s4,s5].includes(x)).length;
  const peDL = ['高估','破净','亢奋','低迷','偏高'].filter(x=>[s1,s2,s4,s5].includes(x)).length;
  let verdict = '';
  if(peHL>=3) verdict = '<b class="hl">综合评分：偏多</b> — 多项指标共振向好，可关注左侧布局或回调买入机会';
  else if(peDL>=2) verdict = '<b class="hl">综合评分：偏空</b> — 估值偏高或流动性堪忧，建议观望或逢高减仓';
  else verdict = '<b class="hl">综合评分：中性</b> — 指标分化，等待方向选择';
  if(m.mid_high_pct!==undefined && m.mid_high_pct!==null){
    const pos = Number(m.mid_high_pct);
    if(pos<30) verdict += ' 当前价处于<b class="hl">52周低位区间</b>，具备安全边际';
    else if(pos>75) verdict += ' 当前价接近<b class="hl">52周高位</b>，追高需谨慎';
  }
  summary = `<div class="ind-summary">
    <div class="title">🧠 智能解读 · ${m.name||''} · ${m.industry||'-'}</div>
    <div class="verdict">${verdict}</div>
  </div>`;

  let cards = IND.map(it=>{
    const raw = m[it.key];
    let val, unit='', badge='', badgeColor='#94a3b8';
    let bar = '';

    switch(it.key){
      case 'pe': val = fmtNum(m.pe); badge = m.pe_label||'-'; badgeColor = m.pe_color||'#94a3b8'; break;
      case 'pb': val = fmtNum(m.pb); badge = m.pb_label||'-'; badgeColor = m.pb_color||'#94a3b8'; break;
      case 'zsz': val = m.zsz?fmtNum(m.zsz,2):'-'; unit='亿'; break;
      case 'hsl': val = fmtNum(m.hsl,2); unit='%'; badge = m.hsl_label||'-'; badgeColor = m.hsl_color||'#94a3b8'; break;
      case 'liab': val = fmtNum(m.liab,2); break;
      case 'beta': val = fmtNum(m.beta,2); badge = m.beta_label||'-'; badgeColor = m.beta_color||'#94a3b8'; break;
      case 'dy_ratio': val = fmtNum(m.dy_ratio,2); unit='%'; badge = m.dy_label||'-'; badgeColor = m.dy_color||'#94a3b8'; break;
      case 'mid_high_pct':
        if(m.mid_high_pct!==undefined && m.mid_high_pct!==null){
          val = m.mid_high_pct+'%';
          const pct = Number(m.mid_high_pct);
          let bc='#2563eb';
          if(pct<30) bc='#16a34a'; else if(pct>70) bc='#dc2626';
          bar = `<div class="ic-bar"><div class="ic-bar-fill" style="width:${pct}%;background:${bc}"></div></div>`;
        } else val='-';
        break;
    }

    const badgeHtml = badge ? `<span class="ic-badge" style="background:${badgeColor}15;color:${badgeColor}">${badge}</span>` : '';
    return `<div class="ind-card" onmouseenter="showTip(event,'${it.label}','${it.tip}')" onmousemove="moveTip(event)" onmouseleave="hideTip()">
      <span class="tip-hint">?</span>
      <div class="ic-l">${it.label}</div>
      <div class="ic-v">${val!==undefined?val:'-'}${unit?`<span class="unit">${unit}</span>`:''}${badgeHtml}</div>
      ${bar}
    </div>`;
  }).join('');

  document.getElementById('inds').innerHTML = `
    <div class="ind-wrap">
      ${summary}
      <div class="ind-head">
        <div class="t">📊 专业指标</div>
        <div class="ind-name">💡 悬停卡片查看解释</div>
      </div>
      <div class="ind-grid">${cards}</div>
    </div>`;
}
function showTip(e,title,content){
  const p = document.getElementById('tip-panel');
  p.innerHTML = `<b>${title}</b><br><div style="margin-top:6px;color:#cbd5e1;line-height:1.6">${content}</div>`;
  p.style.opacity = '1';
  moveTip(e);
}
function moveTip(e){
  const p = document.getElementById('tip-panel');
  let x = e.clientX + 18, y = e.clientY + 18;
  if(x + 300 > window.innerWidth) x = e.clientX - 300;
  if(y + 200 > window.innerHeight) y = e.clientY - 200;
  p.style.left = x + 'px';
  p.style.top = y + 'px';
}
function hideTip(){ document.getElementById('tip-panel').style.opacity='0'; }

function loadPredict(code){
  fetch(`/api/predict?code=${code}`).then(r=>r.json()).then(p=>{
    const cls=p.score>=2?'pos':p.score<=-2?'neg':'neu';
    const box=document.getElementById('pb');
    fetch('/api/portfolio').then(r=>r.json()).then(d=>{
      const myPos=(d.positions||[]).find(x=>x.code===code);
      const hasPos = !!myPos;
      const currentName = myPos?.name || p.name || code;
      let html=`<div class="predict">
        <div style="font-size:11px;color:#94a3b8;margin-bottom:4px">🎯 选择要预测的股票</div>
        <div style="position:relative;margin-bottom:8px">
          <input id="predict_q" placeholder="搜索代码 / 名称 / 拼音 (如 300750 / 宁德时代 / ND)"
                 oninput="predictSearchKw()" autocomplete="off"
                 value="${code}" style="width:100%;padding:6px 8px;border:1px solid #e2e8f0;border-radius:6px;font-size:13px">
          <div id="predict_sug" style="display:none;position:absolute;top:100%;left:0;right:0;background:#fff;border:1px solid #e2e8f0;border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,0.1);z-index:10;max-height:260px;overflow-y:auto"></div>
        </div>
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <span style="font-size:11px;color:#64748b">当前: <b style="color:#1e293b">${currentName}</b> <span style="color:#94a3b8">${code}</span></span>
          <button onclick="loadStock('${code}')" style="font-size:11px;padding:2px 8px;background:#eef2ff;color:#4f46e5;border:none;border-radius:4px;cursor:pointer">📊 加载到K线图</button>
        </div>
        <div style="font-size:11px;color:#94a3b8;margin-bottom:6px">📐 交易预测功能</div>
        <div class="hdr-seg">
          <button class="on" onclick="switchPredictMode('noadd',this)">不加仓预测</button>
          <button onclick="switchPredictMode('add',this)">加仓预测</button>
        </div>
        <div id="predict_formula" class="formula">B = A × (1+C)</div>
        <div id="predict_cond" class="cond">${hasPos?`
          <div>已知条件:</div>
          <div>1. 持仓数量 <b>N₁=${myPos.qty}</b>、成本价 <b>A=${myPos.cost}</b></div>
          <div>2. 预计收益率 <b>C</b> (待输入)</div>
          <div>3. 且不进行加仓</div>
          <div style="margin-top:6px;color:#1e293b">→ 计算股票的目标售价 B</div>
        `:`
          <div style="color:#dc2626;margin-bottom:6px">⚠️ 此股票无持仓 · 请手动输入虚拟预测参数</div>
          <div>已知条件:</div>
          <div>1. 手动输入持仓数量 <b>N₁</b> 和成本价 <b>A</b></div>
          <div>2. 预计收益率 <b>C</b> (待输入)</div>
        `}</div>
        ${hasPos?'':`
        <div class="row2" style="margin-top:8px">
          <div><label>手动输入 · 持仓数量 N₁ (股)</label><input id="p_n1" type="number" step="100" value="1000" oninput="runPredict()"></div>
          <div><label>手动输入 · 成本价 A (元)</label><input id="p_a" type="number" step="0.01" value="${p.last_price||10}" oninput="runPredict()"></div>
        </div>`}
        <label>预计收益率 C (%)</label>
        <input id="p_r" type="number" step="1" value="15" oninput="runPredict()">
        <div id="add_block" style="display:none">
          <div class="row2">
            <div><label>加仓数量 N₂ (股)</label><input id="p_n2" type="number" step="100" value="1000" oninput="runPredict()"></div>
            <div><label>加仓价格 (默认=现价 B₁)</label><input id="p_ap" type="number" step="0.01" placeholder="留空用现价" oninput="runPredict()"></div>
          </div>
        </div>
        <button class="btn-calc" onclick="runPredict()">计算目标售价</button>
        <div id="p_out" style="margin-top:6px"></div>
        <div class="tech">
          <div class="title">📊 技术面分析</div>
          <div style="margin-bottom:6px">综合评分 <span class="tag-score ${cls}">${p.score>0?'+':''}${p.score}</span> <span style="color:#64748b;font-size:11px">${p.conclusion}</span></div>
          <div class="item"><span>MA5 / MA20</span><b>${p.ma5} / ${p.ma20}</b></div>
          <div class="item"><span>MA形态</span><b>${p.ma_trend}</b></div>
          <div class="item"><span>DIF / DEA</span><b>${p.dif} / ${p.dea}</b></div>
          <div class="item"><span>MACD柱</span><b>${p.macd_bar}</b></div>
          ${p.recent_crosses&&p.recent_crosses.length?`<div style="margin-top:8px;color:#64748b;font-size:11px">近期交叉: ${p.recent_crosses.reverse().map(x=>`<span style="margin-right:8px">${x.date} <b style="color:${x.signal==='MA金叉'?'#dc2626':'#16a34a'}">${x.signal}</b></span>`).join('')}</div>`:''}
        </div>
      </div>`;
      box.innerHTML=html;
      window._predictMode='noadd';
      window._predictCode=code;
      window._hasPortfolio=hasPos;
      setTimeout(runPredict,50);
    });
  });
}

let _predictSearchTimer=null;
function predictSearchKw(){
  const kw=document.getElementById('predict_q').value.trim();
  const sug=document.getElementById('predict_sug');
  if(!kw){sug.style.display='none';return}
  clearTimeout(_predictSearchTimer);
  _predictSearchTimer=setTimeout(()=>{
    fetch('/api/search?q='+encodeURIComponent(kw)).then(r=>r.json()).then(list=>{
      if(!list||!list.length){sug.innerHTML='<div style="padding:8px;color:#94a3b8;font-size:12px">没找到</div>';sug.style.display='block';return}
      sug.innerHTML=list.slice(0,12).map(x=>{
        const hl=k=>{const ik=k.toLowerCase(),iw=kw.toLowerCase(),p=ik.indexOf(iw);if(p<0)return k;return k.slice(0,p)+'<b>'+k.slice(p,p+kw.length)+'</b>'+k.slice(p+kw.length)};
        return `<div onclick="pickPredictStock('${x.code}','${x.name}')" style="padding:6px 8px;cursor:pointer;border-bottom:1px solid #f1f5f9;font-size:12px;display:flex;justify-content:space-between" onmouseover="this.style.background='#f1f5f9'" onmouseout="this.style.background=''">
          <span><b>${hl(x.name)}</b> <span style="color:#94a3b8">${x.code}</span></span>
        </div>`;
      }).join('');
      sug.style.display='block';
    });
  },200);
}
function pickPredictStock(code,name){
  document.getElementById('predict_sug').style.display='none';
  loadPredict(code);
}
document.addEventListener('click',e=>{
  if(!e.target.closest('#pb #predict_q') && !e.target.closest('#predict_sug')){
    const s=document.getElementById('predict_sug');if(s)s.style.display='none';
  }
});

function switchPredictMode(mode,btn){
  window._predictMode=mode;
  document.querySelectorAll('.predict .hdr-seg button').forEach(x=>x.classList.remove('on'));
  btn.classList.add('on');
  const formula=document.getElementById('predict_formula');
  const addBlock=document.getElementById('add_block');
  if(mode==='add'){
    formula.textContent='B₂ = (N₁·A + N₂·B₁) × (1+C) / (N₁+N₂)';
    addBlock.style.display='block';
  } else {
    formula.textContent='B = A × (1+C)';
    addBlock.style.display='none';
  }
  runPredict();
}

function runPredict(){
  const code=window._predictCode;
  if(!code) return;
  const r=document.getElementById('p_r').value||0;
  let url=`/api/predict-sell?code=${code}&target_r=${r}`;
  // 如果没有持仓, 带手动输入的 n1 和 a
  if(!window._hasPortfolio){
    const n1=document.getElementById('p_n1');
    const a=document.getElementById('p_a');
    if(n1 && a){
      url += `&n1=${n1.value}&a=${a.value}`;
    }
  }
  if(window._predictMode==='add'){
    const n2=document.getElementById('p_n2').value||0;
    const ap=document.getElementById('p_ap').value;
    url += `&add_qty=${n2}`;
    if(ap) url += `&add_price=${ap}`;
  }
  fetch(url).then(r=>r.json()).then(d=>{
    const out=document.getElementById('p_out');
    if(d.error){out.innerHTML='<div style="color:#dc2626;font-size:12px;padding:8px;background:#fee2e2;border-radius:6px;margin-top:10px">'+d.error+'</div>';return}
    const srcTag = d.from_portfolio?'<span style="color:#16a34a">已匹配持仓</span>':'<span style="color:#f59e0b">手动输入</span>';
    if(d.mode==='add'){
      const cls=d.rise_from_now_pct>=0?'up':'';
      out.innerHTML=`<div class="result">
        <div style="text-align:center;font-size:11px;color:#64748b">🎯 加仓预测 · 目标售价 B₂ · ${srcTag}</div>
        <div class="target-price ${cls}">¥ ${d.b2}</div>
        <div class="info-grid">
          <span>公式</span><b>${d.formula}</b>
          <span>持仓 N₁</span><b>${d.n1} 股 @ ¥${d.a}</b>
          <span>加仓 N₂</span><b>${d.n2} 股 @ ¥${d.add_price}</b>
          <span>加仓金额</span><b>¥ ${d.add_amount.toLocaleString()}</b>
          <span>加仓后总数</span><b>${d.total_shares} 股</b>
          <span>新加权成本</span><b>¥ ${d.new_avg_cost}</b>
          <span>目标收益率</span><b>${d.target_r_pct}%</b>
          <span>从现价需涨</span><b style="color:${d.rise_from_now_pct>=0?'#dc2626':'#16a34a'}">${d.rise_from_now_pct>=0?'+':''}${d.rise_from_now_pct}%</b>
          <span>从新成本需涨</span><b style="color:${d.rise_from_new_avg_pct>=0?'#dc2626':'#16a34a'}">${d.rise_from_new_avg_pct>=0?'+':''}${d.rise_from_new_avg_pct}%</b>
        </div>
      </div>`;
    } else {
      const cls=d.rise_from_now_pct>=0?'up':'';
      out.innerHTML=`<div class="result">
        <div style="text-align:center;font-size:11px;color:#64748b">🎯 不加仓预测 · 目标售价 B · ${srcTag}</div>
        <div class="target-price ${cls}">¥ ${d.b}</div>
        <div class="info-grid">
          <span>公式</span><b>${d.formula}</b>
          <span>持仓 N₁</span><b>${d.n1} 股 @ ¥${d.a}</b>
          <span>现价 B₁</span><b>¥ ${d.b1}</b>
          <span>目标收益率</span><b>${d.target_r_pct}%</b>
          <span>从现价需涨</span><b style="color:${d.rise_from_now_pct>=0?'#dc2626':'#16a34a'}">${d.rise_from_now_pct>=0?'+':''}${d.rise_from_now_pct}%</b>
        </div>
      </div>`;
    }
  });
}

function renderWL(){
  const box=document.getElementById('wl');
  fetch('/api/watchlist').then(r=>r.json()).then(items=>{
    if(!items||!items.length){box.innerHTML='<div class="emp">搜索股票开始</div>';return}
    box.innerHTML=items.map(x=>{
      const pct = x.pct || 0;
      const cls = pct>=0?'up':'down';
      const close = x.now>0?x.now:'?';
      return `<div class="wi ${cc===x.code?'sel':''}" onclick="loadStock('${x.code}')">
        <div><div class="nm">${x.name}</div><div class="cd">${x.code}</div></div>
        <div style="text-align:right">
          <div class="pnl ${cls}">${close}</div>
          <div class="pnl ${cls}" style="font-size:12px">${pct>=0?'+':''}${pct.toFixed(2)}%</div>
        </div>
        <span onclick="event.stopPropagation();removeWL('${x.code}')" style="margin-left:6px;color:#94a3b8;cursor:pointer;font-size:14px;opacity:0.4" title="从自选移除">✕</span>
      </div>`;
    }).join('');
  });
}
function removeWL(code){
  fetch('/api/watchlist?code='+encodeURIComponent(code),{method:'DELETE'}).finally(renderWL);
}

window.addEventListener('resize',()=>{KC.resize();VC.resize();MC.resize();JC.resize()});
renderWL();
loadPortfolio();

function switchTab(b,i){
  if(!b) return;
  document.querySelectorAll('.tabs .tab').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');
  [0,1,2,3,4].forEach(j=>document.getElementById('tab'+j).style.display=i===j?'block':'none');
  if(i===0) loadPortfolio();
  if(i===1){ if(cc) loadPredict(cc); else loadPredictEmpty(); }
  if(i===2) loadTrade();
  if(i===3) loadSector();
  if(i===4) loadTrades();
}

function loadPredictEmpty(){
  const box=document.getElementById('pb');
  let html=`<div class="predict">
    <div style="font-size:11px;color:#94a3b8;margin-bottom:4px">🎯 选择要预测的股票</div>
    <div style="position:relative;margin-bottom:8px">
      <input id="predict_q" placeholder="搜索代码 / 名称 / 拼音 (如 300750 / 宁德时代 / ND)"
             oninput="predictSearchKw()" autocomplete="off"
             style="width:100%;padding:6px 8px;border:1px solid #e2e8f0;border-radius:6px;font-size:13px">
      <div id="predict_sug" style="display:none;position:absolute;top:100%;left:0;right:0;background:#fff;border:1px solid #e2e8f0;border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,0.1);z-index:10;max-height:260px;overflow-y:auto"></div>
    </div>
    <div class="emp" style="padding:20px;font-size:12px;color:#94a3b8">👆 在上方输入股票代码后点击联想项即可开始预测</div>
  </div>`;
  box.innerHTML=html;
}

function renderAsideAssets(d){
  const cls=v=>v>=0?'up':'down',sym=v=>v>=0?'+':'',fmt=n=>n==null||n==undefined?'—':(+n).toLocaleString();
  const el=document.getElementById('asideAssets');
  const pctCls=d.total_pnl_pct>0?'up':d.total_pnl_pct<0?'down':'neu';
  el.innerHTML=`
    <div class="a-hd"><span class="t">💼 账户概览</span><button onclick="toggleCashAdj()">⚙️ 调整现金</button></div>
    <div class="a-big ${cls(d.total_pnl)}">${fmt(d.total_asset)}</div>
    <div class="a-line">
      <span>市值 <b>${fmt(d.total_mkt)}</b></span>
      <span>现金 <b>${fmt(d.cash)}</b></span>
    </div>
    <div class="a-line">
      <span>盈亏 <b class="${cls(d.total_pnl)}">${sym(d.total_pnl)}${fmt(d.total_pnl)}</b></span>
      <span>收益率 <b class="${cls(d.total_pnl_pct)}">${sym(d.total_pnl_pct)}${d.total_pnl_pct}%</b></span>
    </div>
    <div class="a-stats">
      <div class="ai"><div class="l">持仓数</div><div class="v">${d.positions?.length||0}</div></div>
      <div class="ai"><div class="l">现金占比</div><div class="v">${d.total_asset?((d.cash/d.total_asset)*100).toFixed(0)+'%':'—'}</div></div>
      <div class="ai"><div class="l">收益率</div><div class="v ${cls(d.total_pnl_pct)}">${sym(d.total_pnl_pct)}${d.total_pnl_pct}%</div></div>
    </div>`;
}

function loadPortfolio(){
  fetch('/api/portfolio').then(r=>r.json()).then(d=>{
    const box=document.getElementById('pb_portfolio');
    const cls=v=>v>=0?'up':'down',sym=v=>v>=0?'+':'',fmt=n=>n==null||n==undefined?'—':(+n).toLocaleString();
    renderAsideAssets(d);
    let html='';
    if(d.positions&&d.positions.length){
      html += `<div id="pfPie" class="pf-pie"></div>`;
    }
    html += `<div class="cashadj" id="cashadj" style="display:none">
      <div class="mode">
        <button class="on" onclick="setCashMode('set',this)">设置为</button>
        <button onclick="setCashMode('delta',this)">增减</button>
      </div>
      <div class="row">
        <div><label id="cashLbl">新现金余额 (元)</label><input id="cashVal" type="number" step="1" placeholder="如 1000000" value="${d.cash}"></div>
        <div><label>备注 (选填)</label><input id="cashReason" placeholder="如 银行转存" value=""></div>
      </div>
      <div class="quick">
        <span style="font-size:11px;color:#94a3b8;align-self:center">快捷:</span>
        <button onclick="setCashMode('delta',document.querySelector('#cashadj .mode button:last-child'));document.getElementById('cashVal').value=+10000">+1万</button>
        <button onclick="setCashMode('delta',document.querySelector('#cashadj .mode button:last-child'));document.getElementById('cashVal').value=-5000">-5千</button>
        <button onclick="setCashMode('delta',document.querySelector('#cashadj .mode button:last-child'));document.getElementById('cashVal').value=+100000">+10万</button>
        <button onclick="setCashMode('set',document.querySelector('#cashadj .mode button'));document.getElementById('cashVal').value=1000000">重置 100万</button>
      </div>
      <button class="okbtn" onclick="doAdjustCash()">确认调整</button>
      <div id="cashMsg"></div>
    </div>`;

    if(!d.positions||!d.positions.length){
      html += '<div class="emp" style="padding:40px 20px">当前空仓 — 到"🛒 交易"Tab 买入第一只股票吧</div>';
      box.innerHTML=html;return;
    }

    const COLORS=['#3b82f6','#dc2626','#16a34a','#f59e0b','#8b5cf6','#ec4899','#14b8a6','#f97316','#6366f1','#ef4444'];

    const pieData = [{name:'💵 现金',value:d.cash,itemStyle:{color:'#94a3b8'}}];
    d.positions.forEach(p=>{ pieData.push({name:p.name,value:p.mkt,itemStyle:{color:COLORS[pieData.length%COLORS.length]}}); });

    d.positions.forEach((p,i)=>{
      const pnlCls=cls(p.pnl),sym2=sym(p.pnl);
      const pctOfAsset = d.total_mkt?(p.mkt/d.total_mkt*100):0;
      html += `
        <div class="pf" style="border-bottom:1px solid #f1f5f9;padding:12px 16px">
          <div class="pf-head">
            <div><div class="nm" style="font-size:14px;font-weight:600">${p.name}</div>
              <div style="font-size:11px;color:#94a3b8;margin-top:1px">${p.code}</div></div>
            <div class="pn ${pnlCls}">${sym2}${p.pnl_pct}%<div class="per">持仓占比 ${pctOfAsset.toFixed(1)}%</div></div>
          </div>
          <div class="pf-row" style="margin-top:8px">
            <span>数量</span><b>${p.qty}</b>
            <span>成本</span><b>${p.cost}</b>
            <span>现价</span><b>${p.now}</b>
            <span>市值</span><b>${fmt(p.mkt)}</b>
            <span>盈亏</span><b class="${pnlCls}">${sym2}${fmt(p.pnl)}</b>
            <span>盈亏比</span><b class="${pnlCls}">${sym2}${p.pnl_pct}%</b>
          </div>
          <div class="pf-actions">
            <button class="buy" onclick="preloadTrade('${p.code}','${p.name}',${p.now},100);switchTab(document.querySelectorAll('.tabs .tab')[2],2)">📈 买入</button>
            <button class="sell" onclick="preloadTrade('${p.code}','${p.name}',${p.now},${p.qty});switchTab(document.querySelectorAll('.tabs .tab')[2],2)">📉 卖出</button>
            <button class="pred" onclick="loadStock('${p.code}');switchTab(document.querySelectorAll('.tabs .tab')[1],1)">🎯 预测</button>
          </div>
        </div>`;
    });
    box.innerHTML=html;
    if(d.positions.length) window._lastPosCode=d.positions[0].code;

    setTimeout(()=>{
      const pieEl = document.getElementById('pfPie');
      if(!pieEl) return;
      const pe = echarts.init(pieEl);
      pe.setOption({
        tooltip:{trigger:'item',formatter:'{b}: ¥{c} ({d}%)',confine:true,extraCssText:'white-space:nowrap;z-index:999'},
        legend:{orient:'vertical',right:0,top:'center',textStyle:{fontSize:10,color:'#64748b'}},
        series:[{
          type:'pie',radius:['42%','68%'],center:['35%','50%'],
          avoidLabelOverlap:true,
          itemStyle:{borderRadius:6,borderColor:'#fff',borderWidth:2},
          label:{show:true,fontSize:9,formatter:'{b}\n{d}%',color:'#64748b'},
          data:pieData
        }]
      });
      window.addEventListener('resize',()=>{pe.resize()});
      requestAnimationFrame(()=>pe.resize());
      setTimeout(()=>pe.resize(),200);
    },120);
  });
}

let _cashMode='set';
function toggleCashAdj(){
  const box=document.getElementById('cashadj');
  box.style.display = box.style.display==='none'?'block':'none';
  if(box.style.display==='block'){
    // 刷新当前现金
    fetch('/api/portfolio').then(r=>r.json()).then(d=>{
      document.getElementById('cashVal').value=d.cash;
    }).catch(()=>{});
  }
}
function setCashMode(mode,btn){
  _cashMode=mode;
  document.querySelectorAll('#cashadj .mode button').forEach(x=>x.classList.remove('on'));
  btn.classList.add('on');
  document.getElementById('cashLbl').textContent = mode==='set'?'新现金余额 (元)':'增减额 (正=增加, 负=提取)';
}
function doAdjustCash(){
  const msg=document.getElementById('cashMsg');
  msg.innerHTML='';
  const val=+document.getElementById('cashVal').value;
  const reason=document.getElementById('cashReason').value.trim()||'手动调整';
  if(isNaN(val)||val===0){msg.innerHTML='<div class="msg err">请输入有效金额</div>';return}
  const body={reason};
  if(_cashMode==='set'){body.cash=val}else{body.delta=val}
  fetch('/api/portfolio/cash',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(r=>r.json().then(d=>({ok:r.ok,data:d})))
    .then(({ok,data})=>{
      if(!ok){msg.innerHTML='<div class="msg err">'+(data.error||'调整失败')+'</div>';return}
      msg.innerHTML='<div class="msg ok">✅ 成功: '+data.message+' · 当前现金 '+data.after.toLocaleString()+'</div>';
      loadPortfolio();
    })
    .catch(e=>msg.innerHTML='<div class="msg err">网络错误: '+e.message+'</div>');
}

let _tradeSide='buy', _tradePreload=null;
function setTradeSide(side){
  _tradeSide=side;
  const s=document.querySelectorAll('#pb_trade .seg button');
  s.forEach(x=>x.classList.remove('on'));
  s[side==='buy'?0:1].classList.add('on');
  const btn=document.querySelector('#pb_trade .submit');
  btn.className='submit '+(side==='buy'?'buy':'sell');
  btn.textContent=side==='buy'?'📈 确认买入':'📉 确认卖出';
  updateTradeSummary();
}
function preloadTrade(code,name,defaultPrice,defaultQty){
  _tradePreload={code:code,name:name,price:defaultPrice,qty:defaultQty};
  switchTab(document.querySelectorAll('.tabs .tab')[2],2);
}
function loadTrade(){
  const box=document.getElementById('pb_trade');
  const pre=_tradePreload||{};
  _tradePreload=null;
  box.innerHTML=`
    <div class="trade">
      <div class="seg">
        <button class="on buy" onclick="setTradeSide('buy')">📈 买入</button>
        <button class="sell" onclick="setTradeSide('sell')">📉 卖出</button>
      </div>
      <label>股票代码 / 名称 (输入任一项即可, 自动联想)</label>
      <div class="wrap">
        <input id="t_code" placeholder="代码: 002415.SZ  或  名称: 海康" value="${pre.code||''}"
               oninput="onTradeInput('code',this.value)" onfocus="onTradeInput('code',this.value)"
               onchange="onTradeCodeChange()" autocomplete="off">
        <div class="sug" id="sug_code" style="display:none"></div>
      </div>
      <label style="margin-top:12px">已识别的股票名称 (自动填充, 可手动改)</label>
      <input id="t_name" placeholder="选中联想项后自动填入" value="${pre.name||''}"
             readonly style="background:#f8fafc"
             onclick="this.select()">
      <div class="row2">
        <div><label>价格 (元)</label><input id="t_price" type="number" step="0.01" placeholder="0=用现价" value="${pre.price||''}" oninput="updateTradeSummary()"></div>
        <div><label>数量 (股, 100整数倍)</label><input id="t_qty" type="number" step="100" value="${pre.qty||100}" oninput="updateTradeSummary()"></div>
      </div>
      <div class="qck" id="t_qck"></div>
      <div id="t_sum"></div>
      <div style="margin-top:10px;font-size:11px;color:#94a3b8">快捷: <a href="#" onclick="fetchCurrentPrice();return false">用现价</a></div>
      <button class="submit buy" onclick="doTrade()">📈 确认买入</button>
      <div id="t_msg"></div>
    </div>`;
  setTradeSide(_tradeSide);
  updateTradeSummary();
  buildQuickBtns();
  if(pre.code) onTradeCodeChange();
}
function buildQuickBtns(){
  fetch('/api/portfolio').then(r=>r.json()).then(d=>{
    const host=document.getElementById('t_qck');
    if(!host) return;
    let html='';
    d.positions.forEach(p=>{
      html += `<button onclick="quickPick('${p.code}','${p.name}',${p.now},${p.qty})">${p.name} 卖全部 ${p.qty}</button>`;
    });
    if(html) host.innerHTML=html; else host.innerHTML='<span style="font-size:11px;color:#94a3b8">暂无持仓快捷项</span>';
  });
}
function quickPick(code,name,now,qty){
  document.getElementById('t_code').value=code;
  document.getElementById('t_name').value=name;
  document.getElementById('t_price').value=now;
  document.getElementById('t_qty').value=qty;
  setTradeSide('sell');
  updateTradeSummary();
}
let _searchTimer=null;
function onTradeInput(field,val){
  const kw=(val||'').trim();
  const sug=document.getElementById('sug_code');
  if(!kw){sug.style.display='none';sug.innerHTML='';return;}
  clearTimeout(_searchTimer);
  _searchTimer=setTimeout(()=>{
    fetch('/api/search?q='+encodeURIComponent(kw)).then(r=>r.json()).then(list=>{
      if(!list||!list.length){
        sug.innerHTML='<div class="hd">没有匹配的股票</div>';sug.style.display='block';return;
      }
      // 如果精确匹配 code, 直接自动填 (联想也展示出来让用户确认)
      const codeBox=document.getElementById('t_code');
      const nameBox=document.getElementById('t_name');
      const exact = list.find(x=>x.code.toUpperCase()===kw.toUpperCase() || x.name===kw);
      if(exact && !nameBox.value){
        codeBox.value=exact.code;
        nameBox.value=exact.name;
        autoFillPrice();
        if(typeof loadStock==='function') loadStock(exact.code,exact.name);
      }
      sug.innerHTML='<div class="hd">'+list.length+' 条候选 · 点击选中</div>'+
        list.slice(0,12).map(x=>{
          // 高亮匹配片段
          const hl=k=>{
            const ik = k.toLowerCase(), iw = kw.toLowerCase(), p = ik.indexOf(iw);
            if(p < 0) return k;
            return k.slice(0,p) + '<b>' + k.slice(p,p+kw.length) + '</b>' + k.slice(p+kw.length);
          };
          const isHK = x.code.endsWith('.HK')||x.code.endsWith('.OF')||x.code.endsWith('.NQ')||x.code.endsWith('.OT');
          const tag = x.code.includes('.SH')||x.code.includes('.SZ')?'A股':(isHK?'非A股'+' ':'');
          return `<div class="item" onclick="pickStock('${x.code}','${x.name}')">
            <span><span class="n">${hl(x.name)}</span><span class="m">${tag}</span></span>
            <span class="c">${hl(x.code)}</span>
          </div>`;
        }).join('');
      sug.style.display='block';
    }).catch(()=>{sug.style.display='none';});
  }, 220);
}
function pickStock(code,name){
  document.getElementById('t_code').value=code;
  document.getElementById('t_name').value=name;
  document.getElementById('sug_code').style.display='none';
  document.getElementById('sug_code').innerHTML='';
  autoFillPrice();
  updateTradeSummary();
  if(typeof loadStock==='function') loadStock(code,name||'');
}
function autoFillPrice(){
  const code=document.getElementById('t_code').value.trim();
  if(!code) return;
  fetch('/api/quote?code='+encodeURIComponent(code)).then(r=>r.json()).then(q=>{
    if(q && q.price>0){
      const pbox=document.getElementById('t_price');
      pbox.value = q.price;
      updateTradeSummary();
    }
  }).catch(()=>{});
}
// 点页面其他地方时关闭联想
document.addEventListener('click', e=>{
  if(!e.target.closest('.wrap')){
    document.querySelectorAll('.trade .sug').forEach(s=>s.style.display='none');
  }
});
function onTradeCodeChange(){
  // 兜底: 失焦时如果 code 有效但 name 空, 再补一次
  const code=document.getElementById('t_code').value.trim();
  if(!code) return;
  const nameBox=document.getElementById('t_name');
  if(!nameBox.value){
    fetch('/api/quote?code='+encodeURIComponent(code)).then(r=>r.json()).then(q=>{
      if(q && q.name) nameBox.value=q.name;
    }).catch(()=>{});
  }
  autoFillPrice();
  updateTradeSummary();
}
function updateTradeSummary(){
  const price=+document.getElementById('t_price').value||0;
  const qty=+document.getElementById('t_qty').value||0;
  const amt=price*qty;
  const box=document.getElementById('t_sum');
  if(!box) return;
  if(!price||!qty){box.innerHTML='';return;}
  box.innerHTML=`<div class="summary">
    <div style="text-align:center;color:#64748b">${_tradeSide==='buy'?'买入金额':'卖出预计'}</div>
    <div class="big">¥ ${amt.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}</div>
  </div>`;
}
function fetchCurrentPrice(){
  const code=document.getElementById('t_code').value.trim();
  if(!code){alert('请先输入股票代码');return;}
  fetch('/api/quote?code='+encodeURIComponent(code)).then(r=>r.json()).then(d=>{
    if(d.error){alert(d.error);return;}
    document.getElementById('t_price').value=d.price;
    if(d.name && d.name!==code) document.getElementById('t_name').value=d.name;
    updateTradeSummary();
  }).catch(e=>alert('获取现价失败: '+e.message));
}
function doTrade(){
  const code=document.getElementById('t_code').value.trim();
  const name=document.getElementById('t_name').value.trim();
  const price=+document.getElementById('t_price').value;
  let qty=+document.getElementById('t_qty').value;
  const msg=document.getElementById('t_msg');
  msg.innerHTML='';
  if(!code){msg.innerHTML='<div class="err">请输入股票代码</div>';return;}
  if(!qty||qty<=0){msg.innerHTML='<div class="err">数量必须大于 0</div>';return;}
  qty = Math.floor(qty/100)*100;
  if(qty<=0){msg.innerHTML='<div class="err">数量必须为 100 的整数倍</div>';return;}
  let body={code:code,name:name,quantity:qty};
  if(price>0) body.price=price;
  const url=_tradeSide==='buy'?'/api/portfolio/buy':'/api/portfolio/sell';
  fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(r=>r.json().then(d=>({ok:r.ok,data:d})))
    .then(({ok,data})=>{
      if(!ok){msg.innerHTML=`<div class="err">${data.error||'交易失败'}</div>`;return;}
      const actName=name||code;
      const txt=_tradeSide==='buy'?'买入':'卖出';
      msg.innerHTML=`<div class="ok">✅ ${txt}成功：${actName} ${data.quantity} 股 × ¥${data.price} = ¥${(data.price*data.quantity).toFixed(2)}</div>`;
      setTimeout(()=>{loadPortfolio();},300);
      buildQuickBtns();
    })
    .catch(e=>{msg.innerHTML=`<div class="err">网络错误: ${e.message}</div>`;});
}
let _sectorType='industry', _sectorPeriod='1d';
function loadSector(){
  const box=document.getElementById('pb_sector');
  box.innerHTML=`<div class="emp">加载中...</div>`;
  fetch(`/api/sector?type=${_sectorType}&period=${_sectorPeriod}`).then(r=>r.json()).then(d=>{
    box.innerHTML=renderSector(d);
  }).catch(e=>{box.innerHTML=`<div class="err">加载失败: ${e.message}</div>`;});
}
function renderSector(d){
  const list=d.list||[];
  const periodLabel={'1d':'今日','5d':'5日','10d':'10日','20d':'20日','60d':'60日','ytd':'年初至今'}[_sectorPeriod]||_sectorPeriod;
  const typeLabel={industry:'行业',concept:'概念',regional:'地区',all:'全部'}[_sectorType]||_sectorType;
  let upCount=0,downCount=0,totalAmt=0;
  list.forEach(r=>{if(r.zangsu>0)upCount++;else if(r.zangsu<0)downCount++;totalAmt+=r.amount;});
  let html=`
  <div style="padding:10px 12px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;border-bottom:1px solid #e2e8f0">
    <span style="font-size:13px;font-weight:600;color:#1e293b">📊 ${typeLabel}板块 · ${periodLabel}涨幅榜</span>
    <span style="font-size:11px;color:#64748b">共 ${d.count||list.length} 个板块</span>
    <span style="font-size:11px;color:#16a34a">▲ ${upCount}</span>
    <span style="font-size:11px;color:#dc2626">▼ ${downCount}</span>
    <span style="font-size:11px;color:#64748b">总额 ${(totalAmt/10000).toFixed(0)}亿</span>
    <div style="flex:1"></div>
    <div class="seg" style="display:inline-flex;border:1px solid #e2e8f0;border-radius:6px;overflow:hidden">
      <button id="st_industry" class="${_sectorType==='industry'?'on':''}" onclick="setSectorType('industry')">行业</button>
      <button id="st_concept" class="${_sectorType==='concept'?'on':''}" onclick="setSectorType('concept')">概念</button>
      <button id="st_regional" class="${_sectorType==='regional'?'on':''}" onclick="setSectorType('regional')">地区</button>
    </div>
    <div class="seg" style="display:inline-flex;border:1px solid #e2e8f0;border-radius:6px;overflow:hidden">
      ${[['1d','今日'],['5d','5日'],['10d','10日'],['20d','20日'],['60d','60日'],['ytd','年初']].map(([k,l])=>
        `<button id="sp_${k}" class="${_sectorPeriod===k?'on':''}" onclick="setSectorPeriod('${k}')">${l}</button>`).join('')}
    </div>
  </div>
  <div style="overflow:auto;max-height:calc(100vh - 300px)">
  <table style="width:100%;border-collapse:collapse;font-size:12px">
    <thead style="position:sticky;top:0;background:#f8fafc;z-index:1">
      <tr style="border-bottom:2px solid #e2e8f0;text-align:left;color:#64748b">
        <th style="padding:8px 6px">#</th>
        <th style="padding:8px 6px">板块名称</th>
        <th style="padding:8px 6px;text-align:right">现价</th>
        <th style="padding:8px 6px;text-align:right">${periodLabel}涨幅</th>
        <th style="padding:8px 6px;text-align:center">上涨/下跌</th>
        <th style="padding:8px 6px;text-align:right">成交额(万)</th>
        <th style="padding:8px 6px;text-align:right">成份</th>
      </tr>
    </thead>
    <tbody>`;
  list.forEach((r,i)=>{
    const ret=r.ret_period!==undefined?r.ret_period:r.zangsu;
    const cls=ret>=0?'up':'down';
    const sign=ret>=0?'+':'';
    const barColor=ret>=0?'#dc2626':'#16a34a';
    const maxAbs=Math.max(...list.map(x=>Math.abs(x.ret_period!==undefined?x.ret_period:x.zangsu)),0.01);
    const pct=Math.round(Math.abs(ret)/maxAbs*100);
    html+=`<tr style="border-bottom:1px solid #f1f5f9;cursor:pointer" onclick="loadStock('${r.code}');switchTab(document.querySelectorAll('.tabs .tab')[0],0)">
      <td style="padding:6px;color:#94a3b8">${i+1}</td>
      <td style="padding:6px;font-weight:500;color:#1e293b">${r.name}</td>
      <td style="padding:6px;text-align:right;color:#475569">${r.now.toFixed(2)}</td>
      <td style="padding:6px;text-align:right;position:relative">
        <div style="position:absolute;right:0;top:0;bottom:0;width:${pct}%;background:${barColor}15"></div>
        <b style="color:${barColor};position:relative">${sign}${ret.toFixed(2)}%</b>
      </td>
      <td style="padding:6px;text-align:center;color:#64748b"><span style="color:#dc2626">${r.up}</span>/<span style="color:#16a34a">${r.down}</span></td>
      <td style="padding:6px;text-align:right;color:#475569">${(r.amount/10000).toFixed(0)}</td>
      <td style="padding:6px;text-align:right;color:#94a3b8">${r.items}</td>
    </tr>`;
  });
  html+=`</tbody></table>
  </div>`;
  return html;
}
function setSectorType(t){ _sectorType=t; loadSector(); }
function setSectorPeriod(p){ _sectorPeriod=p; loadSector(); }

// ---- 用户认证 ----
let _authMode='login';
function showAuth(mode){
  _authMode=mode||'login';
  document.getElementById('authTitle').textContent=_authMode==='login'?'登录':'注册新账号';
  document.getElementById('authTabLogin').classList.toggle('on',_authMode==='login');
  document.getElementById('authTabRegister').classList.toggle('on',_authMode==='register');
  document.getElementById('authExtra').style.display=_authMode==='register'?'block':'none';
  document.getElementById('authSubmit').textContent=_authMode==='login'?'登录':'注册';
  document.getElementById('authMsg').textContent='';
  document.getElementById('authModal').style.display='flex';
  setTimeout(()=>document.getElementById('authUsername').focus(),100);
}
function switchAuth(mode){ showAuth(mode); }
function closeAuth(){ document.getElementById('authModal').style.display='none'; }

async function doAuth(){
  const username=document.getElementById('authUsername').value.trim();
  const password=document.getElementById('authPassword').value;
  const display_name=document.getElementById('authDisplay').value.trim();
  const msg=document.getElementById('authMsg');
  const btn=document.getElementById('authSubmit');
  msg.style.color='#dc2626';
  btn.disabled=true; btn.textContent='处理中...';
  try{
    const url=_authMode==='login'?'/api/auth/login':'/api/auth/register';
    const body={username,password};
    if(_authMode==='register' && display_name) body.display_name=display_name;
    const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(d.error){ msg.textContent=d.error; btn.disabled=false; btn.textContent=_authMode==='login'?'登录':'注册'; return; }
    // 成功
    msg.style.color='#16a34a'; msg.textContent='✓ 成功';
    btn.textContent='✓';
    setTimeout(()=>{closeAuth(); refreshUserUI(); loadPortfolio(); loadTrades();},400);
  }catch(e){ msg.textContent='网络错误: '+e.message; btn.disabled=false; btn.textContent=_authMode==='login'?'登录':'注册'; }
}

async function logout(){
  if(!confirm('确定要退出登录吗?')) return;
  try{
    const r=await fetch('/api/auth/logout',{method:'POST'});
    if(!r.ok){ alert('退出失败: '+r.statusText); return; }
  }catch(e){ alert('退出网络错误: '+e.message); return; }
  refreshUserUI();
  document.getElementById('pb_portfolio').innerHTML='<div class="emp">请先登录</div>';
  document.getElementById('pb_trades').innerHTML='<div class="emp">请先登录</div>';
  document.getElementById('pb_trade').innerHTML='<div class="emp">请先登录后进行交易</div>';
  document.getElementById('wl').innerHTML='<div class="emp">请先登录</div>';
}

async function refreshUserUI(){
  try{
    const r=await fetch('/api/auth/me');
    const d=await r.json();
    const area=document.getElementById('userArea');
    if(d.logged_in){
      const name=d.user.display_name||d.user.username;
      const initial=name.charAt(0).toUpperCase();
      area.innerHTML=`<div class="user-info"><div class="avatar">${initial}</div><span class="name">${name}</span><button class="btn-logout" onclick="logout()">退出</button></div>`;
    }else{
      area.innerHTML=`<button class="btn-login" onclick="showAuth('login')">登录</button>`;
    }
  }catch(e){}
}

// ---- 交易记录 ----
let _tradesPage=0,_tradesLimit=30,_tradesTotal=0;
async function loadTrades(page){
  if(page!==undefined) _tradesPage=page;
  const box=document.getElementById('pb_trades');
  box.innerHTML='<div class="emp">加载中...</div>';
  try{
    const r=await fetch(`/api/portfolio/trades?limit=${_tradesLimit}&offset=${_tradesPage*_tradesLimit}`);
    if(r.status===401){ box.innerHTML='<div class="emp">请先登录</div>'; return; }
    const d=await r.json();
    const trades=d.trades||[];
    _tradesTotal=d.total||trades.length;
    if(trades.length===0){
      box.innerHTML='<table class="trades-table"><tr><td class="empty">暂无交易记录</td></tr></table>';
      return;
    }
    const rows=trades.map(t=>{
      const side=t.side==='BUY'?'<span class="side buy">买入</span>':'<span class="side sell">卖出</span>';
      const amt=t.side==='BUY'?`-¥${t.amount.toFixed(2)}`:`+¥${t.amount.toFixed(2)}`;
      const amtCls=t.side==='BUY'?'amount buy':'amount sell';
      return `<tr>
        <td>${side}</td>
        <td>${t.code}</td>
        <td>${t.name||''}</td>
        <td>${t.price.toFixed(2)}</td>
        <td>${t.quantity}</td>
        <td class="${amtCls}">${amt}</td>
        <td>${(t.reason||'').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'})[c])}</td>
        <td style="white-space:nowrap;font-size:11px;color:#94a3b8">${(t.created_at||'').replace('T',' ').slice(0,19)}</td>
      </tr>`;
    }).join('');
    const pager=`<div class="trades-pager">
      <div>共 ${_tradesTotal} 条记录，每页 ${_tradesLimit}</div>
      <div>
        <button onclick="loadTrades(${_tradesPage-1})" ${_tradesPage===0?'disabled':''}>上一页</button>
        <span style="margin:0 8px">第 ${_tradesPage+1} 页</span>
        <button onclick="loadTrades(${_tradesPage+1})" ${_tradesPage*_tradesLimit+trades.length>=_tradesTotal?'disabled':''}>下一页</button>
      </div>
    </div>`;
    box.innerHTML=`<table class="trades-table"><thead><tr>
      <th>方向</th><th>代码</th><th>名称</th><th>价格</th><th>数量</th><th>金额</th><th>备注</th><th>时间</th>
    </tr></thead><tbody>${rows}</tbody></table>${pager}`;
  }catch(e){ box.innerHTML='<div class="emp">加载失败: '+e.message+'</div>'; }
}

// 页面加载完成后检查登录状态
refreshUserUI();