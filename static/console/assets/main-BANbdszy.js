import{W as G,S as _,P as q,G as O,T as j,C as F,a as W,B as z,b as U,c as K,A as Z,d as X,l as Y,e as J}from"./three.module-Bcl9ZI_9.js";function a(t){return t.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}function Q(t){if(!t)return"—";const e=t.match(/T(\d{2}:\d{2}:\d{2})/);return e?`${e[1]}Z`:t}function V(t){if(!t)return null;try{const e=new URL(t),n=e.pathname.replace(/\/+$/,"").split("/").filter(Boolean),s=n[1]||n[0]||e.hostname,i=n[n.length-1];return i&&/^\d+$/.test(i)?{href:t,label:`${s}#${i}`}:{href:t,label:s}}catch{return{href:t,label:t}}}function w(t){const e=V(t);return e?`<a href="${a(e.href)}" target="_blank" rel="noreferrer">${a(e.label)}</a>`:"—"}function C(t,e){if(!t)return"—";const n=t.split("/")[1]||t;return e?`${n}#${e}`:n}function N(t){return t==="Merged"||t==="MERGED"?"ok":t==="Closed, not merged"||t==="CLOSED"?"hot":""}function tt(t){const e=t.archive||[];return`
    <section class="hero compact">
      <p class="kicker">Archive  ·  parked before 24 Aug</p>
      <h1 class="page-title">Older work, kept off the sprint tape.</h1>
      <p class="lede">These ${e.length} rows opened before this run. They are here so judges can see the full ledger without inflating the 24–28 Aug count.</p>
    </section>
    <section class="card table-card">
      <table class="grid-table wide">
        <thead><tr><th>PR</th><th>Title</th><th>State</th><th>Opened</th></tr></thead>
        <tbody>
          ${e.map(n=>`<tr>
                <td>${n.url?w(n.url):a(C(n.repo,n.number))}</td>
                <td>${a(n.title||"—")}</td>
                <td><span class="pill ${N(n.state)}">${a(n.state||"—")}</span></td>
                <td class="mono">${a(n.opened||"—")}</td>
              </tr>`).join("")}
        </tbody>
      </table>
    </section>`}function et(t){var n;const e=t.claims||[];return`
    <section class="hero compact">
      <p class="kicker">Claims  ·  ${e.length} issues</p>
      <h1 class="page-title">Pipeline, not receivables.</h1>
      <p class="lede">${a(((n=t.meta)==null?void 0:n.money)||"Payouts are unknown.")}</p>
    </section>
    <section class="card table-card">
      <table class="grid-table wide">
        <thead><tr><th>Issue</th><th>Title</th><th>Status</th><th>Payout</th></tr></thead>
        <tbody>
          ${e.map(s=>`<tr>
                <td>${s.url?w(s.url):a(C(s.repo,s.number))}</td>
                <td>${a(s.title||"—")}</td>
                <td>${a(s.status||"—")}</td>
                <td class="mono">unknown</td>
              </tr>`).join("")}
        </tbody>
      </table>
    </section>`}function P(t,e="all",n=""){var c;const s=((c=t.sprint)==null?void 0:c.prs)||[],i=n.trim().toLowerCase(),o=s.filter(r=>e==="waiting"&&r.outcome!=="Waiting on human"||e==="merged"&&r.outcome!=="Merged"||e==="closed"&&r.outcome!=="Closed, not merged"?!1:i?[r.repo,r.title,r.number,r.outcome].some(l=>String(l||"").toLowerCase().includes(i)):!0);return`
    <section class="hero compact">
      <p class="kicker">History  ·  ${s.length} sprint PRs</p>
      <h1 class="page-title">Everything the fleet opened this run.</h1>
      <p class="lede">24–28 Aug 2026. Filter to waiting, merged, or closed. The agent already did its part on the waiting rows.</p>
    </section>
    <div class="toolbar">
      ${b("all","All",e)}
      ${b("waiting","Waiting",e)}
      ${b("merged","Merged",e)}
      ${b("closed","Closed",e)}
      <input id="history-q" class="search" type="search" placeholder="Filter repo or title" value="${a(n)}" />
    </div>
    <section class="card table-card">
      <table class="grid-table wide">
        <thead><tr><th>PR</th><th>Title</th><th>Outcome</th><th>Opened</th></tr></thead>
        <tbody>${o.map(nt).join("")||'<tr><td colspan="4" class="empty">No rows match.</td></tr>'}</tbody>
      </table>
    </section>`}function b(t,e,n){return`<button class="chip ${t===n?"active":""}" data-filter="${t}" type="button">${e}</button>`}function nt(t){return`<tr>
    <td>${t.url?w(t.url):a(C(t.repo,t.number))}</td>
    <td>${a(t.title||"—")}</td>
    <td><span class="pill ${N(t.outcome)}">${a(t.outcome||"—")}</span></td>
    <td class="mono">${a(t.opened||"—")}</td>
  </tr>`}function st(t,e){const n=()=>{const s=t.querySelector(".chip.active"),i=s instanceof HTMLElement&&s.dataset.filter||"all",o=t.querySelector("#history-q"),c=o instanceof HTMLInputElement?o.value:"",r=t.querySelector("tbody");if(!r)return;const l=P(e,i,c),m=document.createElement("div");m.innerHTML=l;const p=m.querySelector("tbody");p&&r.replaceWith(p)};t.addEventListener("click",s=>{const i=s.target.closest("[data-filter]");!(i instanceof HTMLElement)||!i.dataset.filter||(t.querySelectorAll(".chip").forEach(o=>o.classList.toggle("active",o===i)),n())}),t.addEventListener("input",s=>{!(s.target instanceof HTMLInputElement)||s.target.id!=="history-q"||n()})}function it(){return`
    <section class="hero">
      <p class="kicker">Fortified Enterprise Fleet</p>
      <h1 id="banner" class="status PENDING">PENDING</h1>
      <p id="banner-sub" class="lede">Waiting for fleet state.</p>
    </section>
    <main class="cards">
      <section class="card">
        <h2>Bounty</h2>
        <div id="bounty" class="empty">No live bounty yet.</div>
      </section>
      <section class="card">
        <h2>Trace</h2>
        <div id="timeline" class="empty">Intake, Executor, and Auditor write here.</div>
      </section>
      <section class="card">
        <h2>Registry</h2>
        <div id="registry"></div>
      </section>
    </main>`}function ot(t){var o,c;const e=t.sprint||{},n=e.days||[],s=Math.max(1,...n.map(r=>r.opened)),i=(e.repos||[]).slice(0,12);return`
    <section class="hero compact">
      <p class="kicker">Ops  ·  ${a(((o=t.meta)==null?void 0:o.window)||"this sprint")}</p>
      <h1 class="page-title">Most of the pile is waiting on humans.</h1>
      <p class="lede">${a(((c=t.meta)==null?void 0:c.rule)||"")}</p>
    </section>
    <div class="stats">
      ${y("Opened",e.opened??0)}
      ${y("Waiting",e.waiting??0,"pending")}
      ${y("Merged",e.merged??0,"ok")}
      ${y("Closed",e.closed??0,"hot")}
    </div>
    <div class="split">
      <section class="card">
        <h2>Opened by day</h2>
        <div class="bars">
          ${n.map(r=>{const l=Math.round(r.opened/s*100);return`<div class="bar-row"><span>${a(r.day.slice(5))}</span><i style="width:${l}%"></i><b>${r.opened}</b></div>`}).join("")}
        </div>
      </section>
      <section class="card">
        <h2>By repo</h2>
        <table class="grid-table">
          <thead><tr><th>Repo</th><th>Open</th><th>Wait</th><th>Merged</th></tr></thead>
          <tbody>
            ${i.map(r=>`<tr>
                  <td>${a(r.repo.split("/")[1]||r.repo)}</td>
                  <td>${r.opened}</td>
                  <td>${r.waiting}</td>
                  <td>${r.merged}</td>
                </tr>`).join("")}
          </tbody>
        </table>
      </section>
    </div>`}function y(t,e,n=""){return`<div class="stat ${n}"><b>${e}</b><span>${t}</span></div>`}const d=t=>document.getElementById(t);function at(t){if(!t)return{title:"PENDING",cls:"PENDING",sub:"Waiting for fleet state."};const e=t.audit_status||"PENDING";return e==="FAIL"?{title:"BLOCKED",cls:"BLOCKED",sub:`Merge denied${t.cheat_detected?` · ${t.cheat_detected}`:""}. Auditor holds the gate until the cheat is gone.`}:e==="PASS"&&t.merge_allowed?{title:"CLEARED",cls:"CLEARED",sub:"Auditor approved. Merge is allowed."}:{title:"PENDING",cls:"PENDING",sub:"Fleet in flight. Waiting for the next GitHub state change."}}function rt(t){const e=d("bounty");if(!e)return;if(!t){e.className="empty",e.textContent="No bounty in Memory Bank.";return}const n=t.escrow||{},s=t.merge_allowed?"ok":"hot";e.className="",e.innerHTML=`
    <p class="bounty-title">${a(t.title||t.bounty_id||"Untitled bounty")}</p>
    <div class="kv">
      <b>issue</b><div>${w(t.issue_url)}</div>
      <b>draft pr</b><div>${w(t.pr_url)}</div>
      <b>escrow</b><div>${n.verified?`verified $${a(String(n.amount_usd??0))} ${n.source?`<span class="pill">${a(n.source)}</span>`:""}`:"not verified"}</div>
    </div>
    <div class="pills">
      ${t.cheat_detected?`<span class="pill hot">${a(t.cheat_detected)}</span>`:'<span class="pill ok">cheat none</span>'}
      <span class="pill ${s}">${t.merge_allowed?"merge allowed":"merge blocked"}</span>
    </div>`}function ct(t){const e=d("timeline");if(!e)return;if(!t||!t.length){e.className="empty",e.textContent="No events yet.";return}const n=t.slice(-6);e.className="",e.innerHTML=n.map(s=>{const i=s.type||"event";return`<div class="event ${a(i)}">
        <div class="t">${a(Q(s.t))} · ${a(i)}</div>
        <div>${a(s.detail||"")}</div>
      </div>`}).join("")}function lt(t,e){const n=d("registry");if(!n)return;const s=(e==null?void 0:e.agents)||{};n.innerHTML=t.map(i=>{const o=s[i.id]||i.status||"idle",c=o==="active"||o==="pass"?"ok":o==="fail"?"hot":"";return`<div class="agent">
        <div class="agent-head">
          <strong>${a(i.name)}</strong>
          <span class="pill ${c}">${a(o)}</span>
        </div>
        <div class="scope">${a((i.tool_scope||[]).join(" · "))}</div>
      </div>`}).join("")}function dt(t){const e=d("gcp");if(!e)return;const n=(t==null?void 0:t.gcp)||{};e.textContent=[n.project,n.region,n.firestore_doc].filter(Boolean).join(" · ")}function ut(){const t=d("clock");t&&(t.textContent=new Date().toISOString().slice(11,19)+"Z")}function S(t,e){const n=d("health-dot"),s=d("health-label");n&&(n.className=t?"dot ok":"dot bad"),s&&(s.textContent=e)}function ht(t){const e=d("banner"),n=d("banner-sub"),s=at(t);e&&(e.className=`status ${s.cls}`,e.textContent=s.title),n&&(n.textContent=s.sub)}const I=["live","ops","history","claims","archive"];function pt(){const e=(window.location.pathname.replace(/\/+$/,"")||"/console").replace(/^\/console\/?/,"");return I.includes(e)?e:"live"}function ft(t){return t==="live"?"/console":`/console/${t}`}function mt(t){const e=ft(t);window.location.pathname.replace(/\/+$/,"")!==e&&(window.history.pushState({route:t},"",e),window.dispatchEvent(new Event("routechange")))}function gt(t){window.addEventListener("popstate",t),window.addEventListener("routechange",t),document.addEventListener("click",e=>{const n=e.target;if(!(n instanceof Element))return;const s=n.closest("a[data-route]");if(!(s instanceof HTMLAnchorElement))return;const i=s.dataset.route;!i||!I.includes(i)||(e.preventDefault(),mt(i))})}function vt(t){document.querySelectorAll("a[data-route]").forEach(e=>{e.classList.toggle("active",e.getAttribute("data-route")===t)})}function wt(){const e=document.createElement("canvas");e.width=64,e.height=64;const n=e.getContext("2d");if(!n)return new j;const s=n.createRadialGradient(32,32,0,32,32,32);s.addColorStop(0,"rgba(255,255,255,1)"),s.addColorStop(.08,"rgba(255,255,255,0.95)"),s.addColorStop(.22,"rgba(255,255,255,0.28)"),s.addColorStop(.55,"rgba(255,255,255,0.04)"),s.addColorStop(1,"rgba(255,255,255,0)"),n.fillStyle=s,n.fillRect(0,0,64,64);const i=new F(e);return i.colorSpace=W,i}function E(t,e){const n=new Float32Array(t*3);for(let s=0;s<t;s+=1){const i=e*(.25+Math.random()*.75),o=Math.random()*Math.PI*2,c=Math.acos(2*Math.random()-1);n[s*3]=i*Math.sin(c)*Math.cos(o),n[s*3+1]=i*Math.sin(c)*Math.sin(o),n[s*3+2]=i*Math.cos(c)}return n}function T(t,e,n){const s=new Float32Array(t*3);for(let i=0;i<t;i+=1){const o=e*Math.sqrt(Math.random()),c=Math.random()*Math.PI*2;s[i*3]=o*Math.cos(c),s[i*3+1]=(Math.random()-.5)*n,s[i*3+2]=o*Math.sin(c)}return s}function v(t,e,n,s){const i=new z;i.setAttribute("position",new U(t,3));const o=new K({size:e,map:s,color:n,transparent:!0,depthWrite:!1,blending:Z,sizeAttenuation:!0,opacity:.95});return new X(i,o)}function $t(t){const e=window.matchMedia("(prefers-reduced-motion: reduce)").matches,n=new G({canvas:t,antialias:!0,alpha:!1});n.setPixelRatio(Math.min(window.devicePixelRatio,2)),n.setClearColor(328707,1);const s=new _,i=new q(55,1,.1,400);i.position.set(0,0,8);const o=wt(),c=v(E(12e3,170),.38,16448249,o),r=v(E(3800,110),.62,15197668,o),l=v(T(7e3,130,18),.5,14078929,o),m=v(E(420,52),1.05,14090138,o),p=v(T(80,100,12),2.4,2696484,o);l.rotation.x=.55,l.rotation.z=.22,p.rotation.x=.55,p.rotation.z=.22;const g=new O;g.add(c,r,l,m,p),s.add(g);const $={x:0,y:0},B=u=>{$.x=u.clientX/window.innerWidth*2-1,$.y=u.clientY/window.innerHeight*2-1};window.addEventListener("pointermove",B,{passive:!0});const L=()=>{const u=window.innerWidth,f=window.innerHeight;n.setSize(u,f,!1),i.aspect=u/f,i.updateProjectionMatrix()};window.addEventListener("resize",L),L();const x=u=>{requestAnimationFrame(x);const f=u*2e-4;g.rotation.y=f,g.rotation.x=Math.sin(f*.45)*.12,g.rotation.z=Math.cos(f*.2)*.03;const D=m.material;D.opacity=.72+Math.sin(u*.0016)*.22,i.position.x+=($.x*1.4-i.position.x)*.02,i.position.y+=(-$.y*.9-i.position.y)*.02,i.lookAt(0,0,0),n.render(s,i)};n.render(s,i),e||requestAnimationFrame(x)}const A=document.getElementById("universe");A instanceof HTMLCanvasElement&&$t(A);const h=document.getElementById("app");let k=null,M=null;async function bt(){return k||(k=await J()),k}async function H(){ut();try{const[t,e,n]=await Y();S(!0,"live");const s=n.bounty||null;ht(s),rt(s),ct(s==null?void 0:s.events),lt(e.agents||[],s),dt(s)}catch{S(!1,"offline")}}function yt(){M!==null&&(window.clearInterval(M),M=null)}async function R(){var s,i,o;if(!h)return;const t=pt();if(vt(t),yt(),t==="live"){h.innerHTML=it(),await H(),M=window.setInterval(H,2e3);return}const e=await bt();t==="ops"&&(h.innerHTML=ot(e)),t==="history"&&(h.innerHTML=P(e),st(h,e)),t==="claims"&&(h.innerHTML=et(e)),t==="archive"&&(h.innerHTML=tt(e));const n=document.getElementById("gcp");n&&(n.textContent=[(s=e.meta)==null?void 0:s.source,(i=e.meta)==null?void 0:i.snapshot,(o=e.meta)==null?void 0:o.window].filter(Boolean).join(" · "))}gt(()=>{R()});R();
