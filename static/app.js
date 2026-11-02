/* 拾音流状态页逻辑（独立文件，便于 node --check 语法校验） */
const $ = id => document.getElementById(id);
const fmt = t => t ? new Date(t*1000).toLocaleString("zh-CN",{hour12:false}) : "-";
const esc = s => (s||"").replace(/"/g,"&quot;");
const badge = s => {
  const cls = s==="uploaded"?"b-uploaded":s==="failed"?"b-failed":
    (["discovered","downloaded","converted"].includes(s)?"b-pending":"b-run");
  return `<span class="badge ${cls}">${s}</span>`;
};
function render(data){
  const st = data.stats||{};
  const pend = (st.discovered||0)+(st.downloaded||0)+(st.converted||0);
  const total = Object.values(st).reduce((a,b)=>a+b,0);
  $("stats").innerHTML = [["全部",total],["已上传",st.uploaded||0],["待处理",pend],["失败",st.failed||0]]
    .map(([n,v])=>`<div class="stat"><b>${v}</b><span>${n}</span></div>`).join("");
  const rows = (data.records||[]).map(r=>`<tr>
    <td>${badge(r.status)}</td>
    <td class="title" title="${esc(r.title)}">${r.title||r.aweme_id}</td>
    <td>${r.account||""}</td><td>${r.attempts||0}</td>
    <td>${r.error?`<span class="err" title="${esc(r.error)}">${r.error}</span>`:""}</td>
    <td>${fmt(r.updated_at)}</td></tr>`).join("");
  $("rows").innerHTML = rows || `<tr><td colspan="6" style="color:var(--dim)">暂无记录，等待首次轮询…</td></tr>`;
}
async function load(){ try{ render(await (await fetch("/api/records")).json()); }catch(e){ $("msg").textContent=""+e; } }

/* ---- 通用扫码登录轮询（喜马拉雅 xm / 抖音 dy） ---- */
const LOGIN_APIS = {
  xm: {status:"/api/login/status", start:"/api/login/start", qr:"/login_qr.png"},
  dy: {status:"/api/douyin/login/status", start:"/api/douyin/login/start",
       qr:"/douyin_qr.png", verify:"/api/douyin/login/verify"},
};
async function pollLogin(kind){
  const api = LOGIN_APIS[kind];
  try{
    const s = await (await fetch(api.status)).json();
    const el = $(kind+"-state"), qr = $(kind+"-qr");
    const vf = $(kind+"-verify");
    if (s.logged_in){
      const who = s.account ? `（${esc(s.account)}）` : "";
      const av = s.avatar ? `<img src="${esc(s.avatar)}" style="width:18px;height:18px;border-radius:50%;vertical-align:-4px;margin-right:4px" onerror="this.remove()">` : "";
      el.innerHTML = `${av}已登录 ✓${who}`;
      el.className = "login-state ok";
      qr.style.display = "none";
      if (vf) vf.style.display = "none";
    } else if (s.pending_verify){
      el.textContent = s.verify_hint || "需要二次验证";
      el.className = "login-state";
      if (vf) vf.style.display = "flex";
    } else if (s.running){
      el.textContent = s.message || "等待扫码…";
      el.className = "login-state";
      if (vf) vf.style.display = "none";
    } else {
      el.textContent = "未登录";
      el.className = "login-state";
      if (vf) vf.style.display = "none";
    }
    if (s.running && s.qr){ qr.src = api.qr+"?t="+Date.now(); qr.style.display = "block"; }
    else if (!s.logged_in) { qr.style.display = "none"; }
  }catch(e){}
}
function bindLogin(kind){
  $(kind+"-login").onclick = async () => {
    $(kind+"-login").disabled = true;
    try {
      await fetch(LOGIN_APIS[kind].start, {method:"POST"});
      $("msg").textContent = "登录任务已发起，二维码出现后用对应 App 扫码（约 5 分钟内有效）";
      pollLogin(kind);
    } catch(e){ $("msg").textContent = "发起失败："+e; }
    finally { $(kind+"-login").disabled = false; }
  };
  const vbtn = $(kind+"-code-submit");
  if (vbtn) {
    vbtn.onclick = async () => {
      const v = $(kind+"-code").value.trim();
      if (!v) return;
      vbtn.disabled = true;
      try {
        const r = await fetch(LOGIN_APIS[kind].verify, {
          method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({value: v})});
        const j = await r.json().catch(()=>({detail:"未知错误"}));
        $("msg").textContent = r.ok ? "验证已提交，等待抖音确认…" : ("提交失败："+JSON.stringify(j.detail||j));
        pollLogin(kind);
      } catch(e){ $("msg").textContent = "提交失败："+e; }
      finally { vbtn.disabled = false; }
    };
  }
}

/* ---- 监控账号管理 ---- */
async function loadAccounts(){
  try{
    const r = await (await fetch("/api/accounts")).json();
    const rows = (r.accounts||[]).map(a=>`<div class="acct-row">
      <span class="nm" title="${esc(a.sec_uid)}">${esc(a.name)}</span>
      <span class="badge b-src">${a.source==="config"?"配置":"搜索"}</span>
      <span class="badge ${a.fetched_all?"b-uploaded":"b-pending"}">${a.fetched_all?"已全量":"待全量"}</span>
      ${a.source==="search"?`<button class="mini danger" onclick="removeAcct('${a.sec_uid}')">移除</button>`:""}
    </div>`).join("");
    $("acct-list").innerHTML = rows || `<div class="hint">还没有监控博主：先扫码登录抖音，然后搜索添加。</div>`;
  }catch(e){}
}
window.removeAcct = async sec => {
  if (!confirm("确定移除该博主？（已处理记录保留）")) return;
  const r = await fetch("/api/accounts/"+encodeURIComponent(sec), {method:"DELETE"});
  $("msg").textContent = r.ok ? "已移除" : ("移除失败："+(await r.json()).detail);
  loadAccounts();
};
$("search").onclick = async () => {
  const kw = $("kw").value.trim();
  if (!kw) return;
  $("search").disabled = true; $("msg").textContent = "搜索中…（需已完成抖音登录）";
  try{
    const r = await fetch("/api/accounts/search?q="+encodeURIComponent(kw));
    const j = await r.json().catch(()=>({detail:"未知错误"}));
    if (!r.ok){ $("msg").textContent = "搜索失败："+JSON.stringify(j.detail||j); return; }
    const list = j.results||[];
    $("results").style.display = list.length ? "block" : "none";
    $("results").innerHTML = list.map(c=>`<div class="acct-row">
      <span class="nm">${esc(c.nickname)}</span>
      <button class="mini" onclick='addAcct(${JSON.stringify(c.sec_uid)}, ${JSON.stringify(c.nickname)})'>添加监控</button>
    </div>`).join("");
    $("msg").textContent = list.length ? `找到 ${list.length} 个候选` : "没有找到候选，换个关键词试试";
  } catch(e){ $("msg").textContent = "搜索失败："+e; }
  finally { $("search").disabled = false; }
};
window.addAcct = async (sec, name) => {
  const r = await fetch("/api/accounts", {method:"POST",headers:{"Content-Type":"application/json"},
    body: JSON.stringify({sec_uid: sec, name})});
  const j = await r.json().catch(()=>({detail:"未知错误"}));
  $("msg").textContent = r.ok ? `已添加「${name}」，即将开始全量拉取全部作品…` : ("添加失败："+JSON.stringify(j.detail||j));
  if (r.ok){ loadAccounts(); $("results").style.display="none"; }
};

$("submit").onclick = async () => {
  const url = $("url").value.trim();
  if (!url) return;
  $("submit").disabled = true; $("msg").textContent = "已提交，处理中…";
  try {
    const r = await fetch("/api/submit",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url})});
    const j = await r.json().catch(()=>({detail:"未知错误"}));
    $("msg").textContent = r.ok ? ("完成：" + JSON.stringify(j)) : ("失败：" + JSON.stringify(j.detail||j));
    load();
  } catch(e){ $("msg").textContent="失败："+e; }
  finally { $("submit").disabled = false; }
};
$("poll").onclick = async () => {
  $("poll").disabled = true; $("msg").textContent = "轮询中，可能需要几分钟…";
  try { const r = await (await fetch("/api/poll",{method:"POST"})).json(); $("msg").textContent = "完成：" + JSON.stringify(r); load(); loadAccounts(); }
  catch(e){ $("msg").textContent = "失败：" + e; }
  finally { $("poll").disabled = false; }
};
$("refresh").onclick = () => { load(); loadAccounts(); };
bindLogin("xm"); bindLogin("dy");
load(); loadAccounts();
pollLogin("xm"); pollLogin("dy");
setInterval(load, 15000);
setInterval(()=>{ pollLogin("xm"); pollLogin("dy"); }, 8000);
