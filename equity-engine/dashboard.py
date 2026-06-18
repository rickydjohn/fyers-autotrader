"""
Self-contained equity dashboard served by equity-engine at GET / (same-origin, no
React/proxy). Loads the CACHED LLM report instantly (recommendations + reasons), with a
background Refresh. Two sections: POTENTIAL stocks (entry advice) and MY HOLDINGS
(ACTUAL + PAPER, P&L + exit advice). Mode toggle + buy/sell with live confirm.
Vanilla JS, zero build step.
"""

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Equity Advisor</title>
<style>
  :root{--bg:#0d1117;--card:#161b22;--card2:#0d1117;--line:#21262d;--fg:#e6edf3;--mut:#8b949e;
        --grn:#3fb950;--red:#f85149;--amb:#d29922;--acc:#388bfd}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif}
  header{display:flex;align-items:center;gap:12px;padding:14px 22px;border-bottom:1px solid var(--line);
    position:sticky;top:0;background:rgba(13,17,23,.95);backdrop-filter:blur(6px);z-index:5}
  h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.01em} .sp{flex:1}
  .upd{font-size:12px;color:var(--mut)}
  .badge{padding:3px 11px;border-radius:20px;font-size:11px;font-weight:700;letter-spacing:.03em}
  .sim{background:#102a17;color:var(--grn);border:1px solid #1f5132} .live{background:#2d1418;color:var(--red);border:1px solid #6e2731}
  button{background:var(--acc);color:#fff;border:0;border-radius:7px;padding:7px 14px;font:inherit;font-weight:600;cursor:pointer}
  button:hover{filter:brightness(1.08)} button.ghost{background:#1c2230;border:1px solid var(--line);color:var(--fg);font-weight:500}
  button.buy{background:var(--grn)} button.sell{background:var(--red)} button:disabled{opacity:.55;cursor:default}
  .btn-sm{padding:4px 11px;font-size:12px;border-radius:6px}
  .wrap{padding:20px 22px;display:grid;grid-template-columns:1fr 1fr;gap:22px;max-width:1500px;margin:0 auto}
  @media(max-width:980px){.wrap{grid-template-columns:1fr}}
  h2{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--mut);margin:0 0 12px;font-weight:700}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:12px}
  .top{display:flex;align-items:center;gap:9px} .nm{font-weight:700;font-size:16px}
  .px{color:var(--mut);font-size:13px} .sp2{flex:1}
  .tag{padding:2px 8px;border-radius:5px;font-size:10px;font-weight:700;letter-spacing:.04em;text-transform:uppercase}
  .tactual{background:#0d2847;color:#79c0ff} .tpaper{background:#3a3115;color:#e3b341}
  .pill{padding:3px 10px;border-radius:6px;font-size:12px;font-weight:700;letter-spacing:.02em}
  .p-enter,.p-hold,.p-add{background:#102a17;color:var(--grn)} .p-exit,.p-avoid{background:#2d1418;color:var(--red)}
  .p-watch,.p-trim{background:#2e2710;color:var(--amb)} .p-na{background:#21262d;color:var(--mut)}
  .meta{margin:9px 0 0;font-size:12px;color:var(--mut)} .meta b{color:var(--fg);font-weight:600}
  .pos{color:var(--grn)} .neg{color:var(--red)}
  .reason{margin-top:9px;padding:10px 12px;background:var(--card2);border:1px solid var(--line);border-radius:8px;
    font-size:13px;color:#c9d1d9} .reason:before{content:"WHY ";color:var(--mut);font-size:10px;font-weight:700;letter-spacing:.08em}
  .empty{color:var(--mut);padding:24px;text-align:center;border:1px dashed var(--line);border-radius:12px;font-size:13px}
  .conv{font-size:11px;color:var(--mut);margin-left:4px}
  .spinner{display:inline-block;width:13px;height:13px;border:2px solid var(--mut);border-top-color:transparent;
    border-radius:50%;animation:spin .8s linear infinite;vertical-align:-2px;margin-right:6px}
  @keyframes spin{to{transform:rotate(360deg)}}
</style></head>
<body>
<header>
  <h1>📈 Equity Advisor</h1>
  <span id="mode" class="badge sim">…</span>
  <button class="ghost btn-sm" onclick="toggleMode()">switch</button>
  <span class="sp"></span>
  <span id="upd" class="upd"></span>
  <button id="refbtn" onclick="refresh()">↻ Refresh recommendations</button>
</header>
<div class="wrap">
  <div><h2>Potential stocks — entry advice</h2><div id="cands"></div></div>
  <div><h2>My holdings — exit advice</h2><div id="holds"></div></div>
</div>
<script>
let MODE="simulation", ADVICE={}, lastGen=null, pollTimer=null;
const fmt=n=>(n==null||n==="")?"–":Number(n).toLocaleString('en-IN',{maximumFractionDigits:2});
const cls=v=>v>0?'pos':v<0?'neg':'';
const pill=a=>'p-'+String(a||'na').toLowerCase();
async function api(p,o){const r=await fetch(p,o||{});return r.json();}
function setUpd(t){document.getElementById('upd').textContent=t?('updated '+new Date(t).toLocaleString('en-IN')):'no analysis yet';}

async function loadMode(){const d=await api('/mode');MODE=d.mode;const e=document.getElementById('mode');
  e.textContent=MODE.toUpperCase();e.className='badge '+(MODE==='live'?'live':'sim');}
async function toggleMode(){const next=MODE==='live'?'simulation':'live';
  if(next==='live'&&!confirm('Switch to LIVE? Buy/Sell will place REAL Fyers orders.'))return;
  await api('/mode?mode='+next,{method:'POST'});loadMode();}

async function loadCached(){const d=await api('/analysis/cached');const r=d.report;
  ADVICE={};
  if(r){lastGen=r.generated_at;setUpd(r.generated_at);
    (r.holdings||[]).forEach(c=>ADVICE[c.symbol]=c.recommendation||{});
    renderCands(r.candidates||[]);}
  else{setUpd(null);document.getElementById('cands').innerHTML=
    '<div class="empty">No recommendations yet.<br>Click <b>↻ Refresh recommendations</b> (≈1–2 min) to rank candidates and analyse your holdings.</div>';}
}
async function loadPositions(){const d=await api('/positions');renderHolds(d.positions||[]);}

function renderCands(rows){const el=document.getElementById('cands');
  if(!rows.length){el.innerHTML='<div class="empty">No candidates passed the screen.</div>';return;}
  el.innerHTML=rows.map(r=>{const a=r.recommendation||{};return `<div class="card">
    <div class="top"><span class="nm">${r.name}</span><span class="px">₹${fmt(r.ltp)} · ${r.regime} · mom ${fmt(r.momentum_12m_pct)}%</span>
      <span class="sp2"></span><span class="pill ${pill(a.action)}">${a.action||'—'}</span><span class="conv">${a.conviction||''}</span></div>
    <div class="meta">entry <b>₹${fmt((a.entry_zone&&a.entry_zone[0])||r.ltp)}</b> · stop <b>₹${fmt(a.stop)}</b> · target <b>₹${fmt(a.target)}</b>
      &nbsp;|&nbsp; resistance ${JSON.stringify(r.resistances)} · support ${JSON.stringify(r.supports)}</div>
    <div class="reason">${a.reasons||'(no reason returned)'}</div>
    <div class="meta"><button class="buy btn-sm" onclick="trade('${r.symbol}','BUY')">Buy</button></div>
  </div>`;}).join('');}

function renderHolds(rows){const el=document.getElementById('holds');
  if(!rows.length){el.innerHTML='<div class="empty">No holdings.</div>';return;}
  el.innerHTML=rows.map(r=>{const a=ADVICE[r.symbol]||{};return `<div class="card">
    <div class="top"><span class="nm">${r.name}</span>
      <span class="tag ${r.source==='ACTUAL'?'tactual':'tpaper'}">${r.source}</span>
      <span class="sp2"></span>${a.action?`<span class="pill ${pill(a.action)}">${a.action}</span><span class="conv">${a.conviction||''}</span>`:''}</div>
    <div class="meta">${r.qty} @ ₹${fmt(r.avg_price)} · LTP ₹${fmt(r.ltp)} ·
      <span class="${cls(r.pnl)}"><b>P&L ₹${fmt(r.pnl)} (${fmt(r.pnl_pct)}%)</b></span>
      ${a.key_resistance?` &nbsp;|&nbsp; resist <b>₹${fmt(a.key_resistance)}</b> · stop <b>₹${fmt(a.stop)}</b>`:''}</div>
    ${a.reasons?`<div class="reason">${a.reasons}</div>`:''}
    <div class="meta"><button class="sell btn-sm" onclick="trade('${r.symbol}','SELL')">Sell</button></div>
  </div>`;}).join('');}

async function refresh(){const b=document.getElementById('refbtn');
  const d=await api('/analysis/refresh?candidates=8',{method:'POST'});
  b.disabled=true;b.innerHTML='<span class="spinner"></span>Analysing…';
  if(pollTimer)clearInterval(pollTimer);let waited=0;
  pollTimer=setInterval(async()=>{waited+=8;const r=(await api('/analysis/cached')).report;
    if(r&&r.generated_at!==lastGen){clearInterval(pollTimer);await loadCached();await loadPositions();
      b.disabled=false;b.innerHTML='↻ Refresh recommendations';}
    else if(waited>360){clearInterval(pollTimer);b.disabled=false;b.innerHTML='↻ Refresh recommendations';}
  },8000);}

async function trade(symbol,side){const n=symbol.split(':')[1].replace('-EQ','');
  const qty=parseInt(prompt(`${side} how many shares of ${n}?`,'1'));if(!qty||qty<=0)return;
  let d=await api(`/trade?symbol=${encodeURIComponent(symbol)}&side=${side}&qty=${qty}`,{method:'POST'});
  if(d.status==='confirm_required'){if(!confirm(d.message))return;
    d=await api(`/trade?symbol=${encodeURIComponent(symbol)}&side=${side}&qty=${qty}&confirm=true`,{method:'POST'});}
  alert(`${d.action||d.status} ${d.mode||''} ${n}`+(d.fill?` @ ₹${d.fill}`:'')+(d.realized_pnl!=null?` · P&L ₹${d.realized_pnl}`:'')+(d.detail?` — ${d.detail}`:''));
  loadPositions();}

(async()=>{await loadMode();await loadCached();await loadPositions();})();
</script></body></html>"""
