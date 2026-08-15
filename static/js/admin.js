// Last modified: 2026-08-13
const NODE_W=160;
let pan={x:0,y:0},zoom=1,dragging=false,dragStart=null;
let allNodes=[],allEdges=[];
let _authed=false;

function _checkAuth(){
  fetch("/api/auth/me",{credentials:"same-origin"}).then(r=>{
    if(r.ok)return r.json();
    throw new Error("not logged in");
  }).then(u=>{
    _authed=true;
    document.getElementById("authMask").classList.add("hidden");
    const info=document.getElementById("userInfo");
    const nm=document.getElementById("userName");
    if(info){info.style.display="inline-flex"}
    if(nm){nm.textContent=u.display_name||u.username||"用户"}
    refresh();setInterval(refresh,30000);
  }).catch(()=>{
    _authed=false;
    document.getElementById("authMask").classList.remove("hidden");
    const err=document.getElementById("authErr");
    if(err)err.textContent="请先登录";
  });
}

function doAuthLogin(){
  const u=document.getElementById("authUser").value.trim();
  const p=document.getElementById("authPwd").value;
  const err=document.getElementById("authErr");
  if(err)err.textContent="";
  if(!u||!p){if(err)err.textContent="用户名和密码必填";return;}
  fetch("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},credentials:"same-origin",body:JSON.stringify({username:u,password:p})})
    .then(r=>r.json().then(j=>({status:r.status,ok:r.ok,data:j})))
    .then(({ok,status,data})=>{
      if(ok){_checkAuth();}
      else{if(err)err.textContent=data.error||"登录失败 ("+status+")";}
    }).catch(e=>{if(err)err.textContent="网络错误";});
}

function doAuthLogout(){
  fetch("/api/auth/logout",{method:"POST",credentials:"same-origin"}).finally(()=>{
    _authed=false;
    document.getElementById("authMask").classList.remove("hidden");
    const info=document.getElementById("userInfo");
    if(info)info.style.display="none";
  });
}

document.addEventListener("keydown",e=>{
  if(e.key==="Enter"&&!document.getElementById("authMask").classList.contains("hidden"))doAuthLogin();
});

function _requireAuth(){
  if(_authed)return true;
  document.getElementById("authMask").classList.remove("hidden");
  const err=document.getElementById("authErr");
  if(err)err.textContent="请先登录";
  return false;
}

_checkAuth();

function h(tag,text,cls){const el=document.createElement(tag);if(cls)el.className=cls;if(text!=null)el.textContent=text;return el;}
function refresh(){Promise.all([fetch("/api/admin/status").then(r=>r.json()),fetch("/api/admin/flow").then(r=>r.json())]).then(([s,f])=>{renderStatus(s);allNodes=f.nodes||[];allEdges=f.edges||[];renderFlow();}).catch(e=>console.error(e));}
function fmtSize(kb){return kb>=1024?(kb/1024).toFixed(1)+" MB":kb.toFixed(1)+" KB";}
function osJoin(root,rel){const r=root.replace(/\\/g,"/").replace(/\/$/,""),n=rel.replace(/\\/g,"/");if(n.startsWith("data/"))return r+"/"+n;return r+"/"+n;}
function clearBrowsingCache(){if(!confirm("确认清空浏览器缓存？"))return;console.clear();if(caches)caches.keys().then(k=>k.forEach(x=>caches.delete(x)));alert("已清空");}
function clearAllCache(){if(!_requireAuth())return;if(!confirm("⚠️ 会删除所有 Parquet + 重建 DuckDB。继续？"))return;fetch("/api/admin/clear-all-cache",{method:"POST"}).then(()=>refresh());}
function shutdown(){if(!_requireAuth())return;if(!confirm("⚠️ 确认退出程序？"))return;fetch("/api/admin/shutdown",{method:"POST"}).then(()=>alert("服务器正在关闭...")).catch(()=>{});}

function renderStatus(s){
  document.getElementById("ver").textContent="v"+s.version;
  const meta=document.getElementById("meta");meta.innerHTML="";
  [["系统版本","v"+s.version],["运行时长",s.uptime],["系统平台",s.platform],["工作目录",s.cwd],["运行模式",s.frozen?"EXE 打包":"开发模式"],["通达信连接",s.tdx_connected?"✅ 已连接":"❌ 断开"],["Endpoint 总数",s.endpoints.length+" 个"]].forEach(([k,v])=>{const it=h("div",null,"meta-item");it.appendChild(h("div",k,"k"));it.appendChild(h("div",v,"v"));meta.appendChild(it);});
  const dd=document.getElementById("deps");dd.innerHTML="";Object.entries(s.deps).forEach(([k,v])=>dd.appendChild(h("span",k+" "+v,"dept-row")));
  const body=document.getElementById("endpointsBody");body.innerHTML="";
  s.endpoints.forEach(ep=>{const tr=h("tr","",ep.enabled?"":"disabled");tr.appendChild(h("td",ep.path,"path"));tr.appendChild(h("td",ep.methods.join(", ")));tr.appendChild(h("td",ep.enabled?"✅ 启用":"⛔ 禁用",ep.enabled?"mtd":"mfa"));
    const tdOp=h("td"),btn=h("button",ep.enabled?"禁用":"启用");btn.onclick=()=>fetch("/api/admin/toggle",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:ep.path,enable:!ep.enabled})}).then(refresh);tdOp.appendChild(btn);tr.appendChild(tdOp);body.appendChild(tr);});
  const c=s.cache||{nodes:[],total_kb:0},cs=document.getElementById("cacheSum");cs.innerHTML="";
  const bigBox=h("div",null,"cache-box");bigBox.innerHTML='<div class="lb">CACHE 总大小</div><div class="val">'+fmtSize(c.total_kb)+'</div><div class="sub">'+c.nodes.reduce((a,n)=>a+n.files,0)+' 个文件</div>';cs.appendChild(bigBox);
  const pq=c.nodes.filter(n=>n.name.includes("kline")).reduce((a,n)=>a+n.rows,0);
  cs.appendChild(Object.assign(h("div",null,"cache-box"),{innerHTML:'<div class="lb">K线记录</div><div class="val">'+pq.toLocaleString()+'</div><div class="sub">Parquet 总行数</div>'}));
  const dr=c.nodes.filter(n=>n.name==="data").reduce((a,n)=>a+n.rows,0);
  cs.appendChild(Object.assign(h("div",null,"cache-box"),{innerHTML:'<div class="lb">DuckDB 表行</div><div class="val">'+dr.toLocaleString()+'</div><div class="sub">基本面+持仓+元表</div>'}));
  const mt=c.nodes.reduce((a,n)=>n.mtime>a?n.mtime:a,"-");
  cs.appendChild(Object.assign(h("div",null,"cache-box"),{innerHTML:'<div class="lb">最后更新</div><div class="val" style="font-size:13px;color:#a78bfa">'+mt+'</div><div class="sub">最新缓存写入</div>'}));
  const cb=document.getElementById("cacheBody");cb.innerHTML="";
  c.nodes.forEach(n=>{const tr=h("tr");tr.appendChild(h("td",n.name,"path"));const fp=h("td",n.full_path||osJoin(s.cwd,n.name),"fullpath");fp.title=fp.textContent;tr.appendChild(fp);tr.appendChild(Object.assign(h("td",null,"num"),{textContent:n.files}));tr.appendChild(Object.assign(h("td",null,"num"),{textContent:fmtSize(n.size_kb)}));tr.appendChild(Object.assign(h("td",null,"num"),{textContent:n.rows?n.rows.toLocaleString():"-"}));const mt2=h("td",n.mtime);mt2.style.color="#94a3b8";tr.appendChild(mt2);cb.appendChild(tr);});
  const sys=document.getElementById("sysInfo");sys.innerHTML="";
  [["当前版本","v"+s.version],["后端技术栈","Flask + ECharts 蜡烛图 + MA/MACD"],["数据源","通达信 TQ-Python · DuckDB"],["存储","Parquet + DuckDB"],["运行端口","127.0.0.1:8765"]].forEach(([k,v])=>{const it=h("div",null,"meta-item");it.appendChild(h("div",k,"k"));it.appendChild(h("div",v,"v"));sys.appendChild(it);});
}

/* ====================== FLOW ====================== */
const DOMAIN_COLOR = {
  trade: '#556070', screener: '#f97316', fundamental: '#a855f7',
  admin: '#6b7280', external: '#9ca3af', default: '#94a3b8'
};
const DOMAIN_NAME = {
  trade: '🔵 行情交易', screener: '🟠 选股引擎',
  fundamental: '🟣 基本面', admin: '⚪ 系统管理', external: '外部源'
};

function renderFlow(){
  const svg=document.getElementById("flowSvg");while(svg.firstChild)svg.removeChild(svg.firstChild);
  const NS="http://www.w3.org/2000/svg";
  const defs=document.createElementNS(NS,"defs");
  defs.innerHTML=`
    <marker id="arrReal" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#556070"/></marker>
    <marker id="arrQuery" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#3b82f6"/></marker>
    <marker id="arrScreener" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#f97316"/></marker>
    <marker id="arrFund" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#a855f7"/></marker>
    <marker id="arrAdmin" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#6b7280"/></marker>
    <marker id="arrExt" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#9ca3af"/></marker>
  `;
  svg.appendChild(defs);

  const LAYER_X={external:30,tq:180,storage:330,engine:470,api:610,fe:790};
  const layers={};allNodes.forEach(n=>{(layers[n.type]=layers[n.type]||[]).push(n)});
  const pos={}, ORDER=["external","tq","storage","engine","api","fe"];
  const AVAIL_H=750, START_Y=20, GAP=8;
  ORDER.forEach(t=>{const list=layers[t]||[];if(!list.length)return;
    const spacing=(AVAIL_H-(list.length-1)*GAP)/Math.max(list.length,1);
    list.forEach((n,i)=>{pos[n.id]={x:LAYER_X[t],y:START_Y+i*(spacing+GAP)};});});
  const tOrder={external:0,tq:1,storage:2,engine:3,api:4,fe:5};

  const gRoot=document.createElementNS(NS,"g");gRoot.setAttribute("id","flowRoot");
  const edgeElems=[];

  allEdges.forEach((edge,i)=>{
    const aid=edge[0],bid=edge[1],edomain=edge[2]||"default",ekind=edge[3]||"query";
    const a=allNodes.find(n=>n.id===aid),b=allNodes.find(n=>n.id===bid);
    const pa=pos[aid],pb=pos[bid];if(!pa||!pb)return;
    const color=DOMAIN_COLOR[edomain]||DOMAIN_COLOR.default;
    const markerMap={trade:"arrReal",screener:"arrScreener",fundamental:"arrFund",admin:"arrAdmin",external:"arrExt",default:"arrQuery"};
    const markerId=markerMap[edomain]||markerMap.default;
    const NODE_H_A = a.sig?56:44;
    const NODE_H_B = b.sig?56:44;
    const y1 = pa.y + NODE_H_A/2;
    const y2 = pb.y + NODE_H_B/2;
    const x1 = pa.x + NODE_W;
    const x2 = pb.x;
    const dx = Math.abs(x2 - x1);
    const cx1 = x1 + dx * 0.4;
    const cx2 = x2 - dx * 0.4;
    const e = document.createElementNS(NS,"path");
    e.setAttribute("d",`M${x1},${y1} C${cx1},${y1} ${cx2},${y2} ${x2},${y2}`);
    e.setAttribute("class","flow-edge domain-"+edomain+" kind-"+ekind);
    e.setAttribute("stroke", color);
    if(ekind==="query"){e.setAttribute("stroke-dasharray","5,4");e.setAttribute("stroke-width","1.3");}
    else{e.setAttribute("stroke-width","1.8");}
    e.setAttribute("marker-end","url(#"+markerId+")");
    e.dataset.from=aid;e.dataset.to=bid;e.dataset.domain=edomain;
    edgeElems.push(e);
  });
  edgeElems.sort((a,b)=>{const ka=a.dataset.domain,kb=b.dataset.domain;return ka.localeCompare(kb);});
  edgeElems.forEach(e=>gRoot.appendChild(e));

  const nodeElems={};
  allNodes.forEach(n=>{const p=pos[n.id];if(!p)return;
    const nh = n.sig?56:44;
    const g=document.createElementNS(NS,"g");g.setAttribute("class","flow-node "+n.type);g.dataset.id=n.id;
    if(n.domain){g.dataset.domain=n.domain;}
    const rect=document.createElementNS(NS,"rect");
    rect.setAttribute("x",p.x);rect.setAttribute("y",p.y);
    rect.setAttribute("width",NODE_W);rect.setAttribute("height",nh);
    g.appendChild(rect);
    const txt=document.createElementNS(NS,"text");
    txt.setAttribute("class","t-main");
    txt.setAttribute("x",p.x+NODE_W/2);txt.setAttribute("y",p.y+nh/2+(n.sig?-4:4));
    txt.setAttribute("text-anchor","middle");txt.textContent=n.name;
    g.appendChild(txt);
    if(n.sig){const s2=document.createElementNS(NS,"text");
      s2.setAttribute("class","t-sub");
      s2.setAttribute("x",p.x+NODE_W/2);s2.setAttribute("y",p.y+nh-6);
      s2.setAttribute("text-anchor","middle");s2.textContent=n.sig;g.appendChild(s2);}
    g.style.cursor="pointer";
    g.addEventListener("click",ev=>{ev.stopPropagation();openNodeModal(n);});
    g.addEventListener("mouseenter",()=>{
      const conn=new Set([n.id]);
      edgeElems.forEach(e=>{const f=e.dataset.from,t=e.dataset.to;
        if(f===n.id||t===n.id){e.classList.add("highlight");conn.add(f);conn.add(t);}
        else e.classList.add("dimmed");});
      Object.entries(nodeElems).forEach(([id,el])=>{if(conn.has(id))el.classList.add("highlight");else el.classList.add("dimmed");});});
    g.addEventListener("mouseleave",()=>{edgeElems.forEach(e=>{e.classList.remove("highlight");e.classList.remove("dimmed");});Object.values(nodeElems).forEach(el=>{el.classList.remove("highlight");el.classList.remove("dimmed");});});
    nodeElems[n.id]=g;gRoot.appendChild(g);});
  svg.appendChild(gRoot);applyTransform();attachPanZoom();fitAll();

  const existing=document.getElementById("flowLegend");
  if(existing)existing.remove();
  const leg=document.createElement("div");leg.id="flowLegend";leg.style.cssText="position:absolute;top:8px;right:8px;background:rgba(15,23,42,.85);border:1px solid #334155;border-radius:8px;padding:8px 12px;font-size:11px;display:flex;gap:10px;flex-wrap:wrap;z-index:5;";
  Object.entries(DOMAIN_NAME).filter(([k])=>k!=="default").forEach(([k,v])=>{
    const row=document.createElement("div");row.style.cssText="display:flex;align-items:center;gap:4px;";
    const dot=document.createElement("span");dot.style.cssText="display:inline-block;width:10px;height:10px;border-radius:50%;background:"+(DOMAIN_COLOR[k]||DOMAIN_COLOR.default)+";";
    row.appendChild(dot);row.appendChild(document.createTextNode(v));leg.appendChild(row);
  });
  const wrap=document.getElementById("flowWrap");
  wrap.style.position="relative";wrap.appendChild(leg);
}

function applyTransform(){const r=document.getElementById("flowRoot");if(r)r.setAttribute("transform",`translate(${pan.x},${pan.y}) scale(${zoom})`);document.getElementById("zoomInfo").textContent=Math.round(zoom*100)+"%";}
function zoomBy(f){zoom=Math.max(0.3,Math.min(3,zoom*f));applyTransform();}
function zoomReset(){zoom=1;pan={x:0,y:0};applyTransform();}
function fitAll(){if(!allNodes.length)return;const wrap=document.getElementById("flowWrap");const W0=wrap.clientWidth,H0=wrap.clientHeight;zoom=Math.min(W0/1000,H0/820)*0.92;pan={x:(W0-1000*zoom)/2,y:(H0-820*zoom)/2};applyTransform();}
function attachPanZoom(){
  const wrap=document.getElementById("flowWrap");
  wrap.addEventListener("wheel",e=>{e.preventDefault();const old=zoom;const rect=wrap.getBoundingClientRect();const cx=e.clientX-rect.left,cy=e.clientY-rect.top;const wx=(cx-pan.x)/old,wy=(cy-pan.y)/old;zoom=Math.max(0.3,Math.min(3,old*(e.deltaY<0?1.15:0.88)));pan={x:cx-wx*zoom,y:cy-wy*zoom};applyTransform();},{passive:false});
  wrap.addEventListener("mousedown",e=>{if(e.button!==0)return;dragging=true;dragStart={x:e.clientX-pan.x,y:e.clientY-pan.y};});
  window.addEventListener("mousemove",e=>{if(!dragging)return;pan={x:e.clientX-dragStart.x,y:e.clientY-dragStart.y};applyTransform();});
  window.addEventListener("mouseup",()=>{dragging=false;});
}

/* ====================== MODAL ====================== */
function openNodeModal(n){
  const info=n.info||{};
  document.getElementById("modalTag").textContent=n.type.toUpperCase();
  document.getElementById("modalTag").className="type-tag "+n.type;
  let title=n.name;if(info.method&&info.url)title=info.method+" "+info.url;
  if(n.sig&&!info.method)title+=" · "+n.sig;
  document.getElementById("modalTitle").textContent=title;
  const body=document.getElementById("modalBody");body.innerHTML="";
  body.appendChild(h("p",info.desc||"-","desc"));
  if(info.params&&info.params.length){
    body.appendChild(h("h4","参数说明"));
    const tbl=h("table","","params-table");const thead=document.createElement("thead"),trh=document.createElement("tr");
    ["参数","类型/方法","说明"].forEach(t=>trh.appendChild(h("th",t)));thead.appendChild(trh);tbl.appendChild(thead);
    const tbody=document.createElement("tbody");info.params.forEach(row=>{const tr=document.createElement("tr");row.forEach(cell=>tr.appendChild(h("td",cell)));tbody.appendChild(tr);});
    tbl.appendChild(tbody);body.appendChild(tbl);}
  if(info.example){body.appendChild(h("h4","示例 · 点击复制"));const ex=h("div",info.example,"code-block");ex.onclick=()=>{navigator.clipboard?.writeText(info.example);ex.style.background="#1e3a8a";setTimeout(()=>ex.style.background="",600);};body.appendChild(ex);}

  // TQ test section (TQ nodes with tq_func)
  if(n.type==="tq" && info.testable && info.tq_func){
    body.appendChild(h("h4","🔬 TQ-Python 接口测试"));
    const sec=h("div","","test-section");
    sec.appendChild(h("h4","_tq."+info.tq_func+"("+ (info.tq_params||[]).join(", ") +")"));
    const inputs=h("div","","test-inputs");
    (info.tq_params||[]).forEach(pname=>{
      const lb=h("label");
      lb.appendChild(h("span",pname));
      const inp=document.createElement("input");inp.name=pname;
      const def={code:"000001.SZ",period:"1d",keyword:"茅台",sector:"白酒",limit:"3"}[pname]||"";
      inp.value=def;
      lb.appendChild(inp);inputs.appendChild(lb);});
    sec.appendChild(inputs);
    const row=h("div","","test-row");
    const btn=h("button","▶️ 调用 TQ-Python","test-btn tq-test");
    btn.onclick=()=>runTqTest(info,sec);row.appendChild(btn);
    const st=h("span","就绪","test-status");row.appendChild(st);sec.appendChild(row);
    body.appendChild(sec);
  }

  // API test section (api nodes with testable)
  if(n.type==="api" && info.testable && info.url){
    body.appendChild(h("h4","🔧 HTTP 接口测试"));
    const sec=h("div","","test-section");
    sec.appendChild(h("h4",(info.dangerous?"⚠️ 模拟交易接口 ":"")+info.method+" "+info.url));
    const inputs=h("div","","test-inputs");
    if(info.method==="GET"){const lb=h("label","","");lb.appendChild(h("span","code (股票代码)"));const inp=document.createElement("input");inp.name="code";inp.placeholder="如 000001.SZ";lb.appendChild(inp);inputs.appendChild(lb);}
    if(info.method==="POST"){["code","price","qty"].forEach(nm=>{const lb=h("label","");lb.appendChild(h("span",nm));const inp=document.createElement("input");inp.name=nm;if(nm==="price"){inp.type="number";inp.placeholder="0=市价";}lb.appendChild(inp);inputs.appendChild(lb);});}
    sec.appendChild(inputs);
    const row=h("div","","test-row");
    const btn=h("button",info.dangerous?"⚠️ 执行交易":"▶️ 发送请求","test-btn"+(info.dangerous?" danger-test":""));
    btn.onclick=()=>runApiTest(info,sec);row.appendChild(btn);
    const st=h("span","就绪","test-status");row.appendChild(st);sec.appendChild(row);
    body.appendChild(sec);
  }

  document.getElementById("modal").classList.add("open");
}

function runTqTest(info,sec){
  if(!_requireAuth())return;
  const inputs=sec.querySelectorAll(".test-inputs input");const args={};
  inputs.forEach(inp=>{let v=inp.value.trim();if(v!==""){if(!isNaN(Number(v))&&v!=="")v=Number(v);args[inp.name]=v;}});
  const st=sec.querySelector(".test-status");st.textContent="调用中...";
  fetch("/api/admin/tq-test",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({func:info.tq_func,args})}).then(async r=>{
    const j=await r.json();
    const res=document.createElement("pre");res.className="test-result "+(j.ok?"ok":"err");
    let txt=(j.elapsed?"⏱ "+j.elapsed+"s\n\n":"")+(j.ok?JSON.stringify(j.result,null,2):(j.error||"Error"));
    if(j.trace)txt+="\n\n"+j.trace;
    res.textContent=txt;
    sec.querySelectorAll(".test-result").forEach(x=>x.remove());sec.appendChild(res);
    st.textContent=(j.ok?"成功":"失败")+" · HTTP "+r.status;
  }).catch(err=>{const res=document.createElement("pre");res.className="test-result err";res.textContent="Network Error: "+err.message;sec.querySelectorAll(".test-result").forEach(x=>x.remove());sec.appendChild(res);st.textContent="失败";});
}

function runApiTest(info,sec){
  if(!_requireAuth())return;
  const inputs=sec.querySelectorAll(".test-inputs input");const params={};
  inputs.forEach(inp=>{if(inp.value!=="")params[inp.name]=inp.value;});
  let url=info.url,method=info.method;const opts={method,headers:{"Content-Type":"application/json"}};
  if(method==="GET"){const q=new URLSearchParams(params).toString();if(q)url=info.url+"?"+q;}
  else{opts.body=JSON.stringify(params);}
  const st=sec.querySelector(".test-status");st.textContent="请求中...";
  fetch(url,opts).then(async r=>{const txt=await r.text();let d=txt;try{d=JSON.stringify(JSON.parse(txt),null,2);}catch(e){}
    const res=document.createElement("pre");res.className="test-result "+(r.ok?"ok":"err");res.textContent="HTTP "+r.status+"\n\n"+(d||"(空)");
    sec.querySelectorAll(".test-result").forEach(x=>x.remove());sec.appendChild(res);st.textContent="HTTP "+r.status;})
  .catch(err=>{const res=document.createElement("pre");res.className="test-result err";res.textContent="Network Error: "+err.message;sec.querySelectorAll(".test-result").forEach(x=>x.remove());sec.appendChild(res);st.textContent="失败";});
}

function closeModal(){document.getElementById("modal").classList.remove("open");}
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeModal();});