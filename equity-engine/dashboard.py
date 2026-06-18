"""
Self-contained equity dashboard — a single HTML page served by equity-engine itself
(same-origin, so no api-service proxy or CORS needed). Two sections: POTENTIAL stocks
(momentum candidates + LLM entry advice) and MY HOLDINGS (ACTUAL + PAPER, P&L + LLM
exit advice). Mode toggle (simulation/live) and buy/sell with a live-order confirm.

Deliberately vanilla JS — zero build step. Can be reimplemented in the React app later.
"""

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Equity Advisor</title>
<style>
  :root{--bg:#0f1419;--card:#1a2029;--line:#2a3340;--fg:#e6edf3;--mut:#8b97a7;--grn:#3fb950;--red:#f85149;--acc:#388bfd}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
  header{display:flex;align-items:center;gap:14px;padding:14px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg)}
  h1{font-size:17px;margin:0;font-weight:600} .sp{flex:1}
  .badge{padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600}
  .sim{background:#1f3a24;color:var(--grn)} .live{background:#3a1f1f;color:var(--red)}
  .tag{padding:1px 7px;border-radius:4px;font-size:11px;font-weight:600}
  .tactual{background:#13315c;color:#79c0ff} .tpaper{background:#3d3522;color:#e3b341}
  button{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:6px 12px;font:inherit;cursor:pointer}
  button.ghost{background:transparent;border:1px solid var(--line);color:var(--fg)}
  button.sell{background:var(--red)} button.buy{background:var(--grn)} button:disabled{opacity:.5;cursor:wait}
  .wrap{padding:20px;display:grid;grid-template-columns:1fr 1fr;gap:20px} @media(max-width:1000px){.wrap{grid-template-columns:1fr}}
  .col h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);margin:0 0 10px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px}
  .row{display:flex;align-items:center;gap:8px} .nm{font-weight:600;font-size:15px} .mut{color:var(--mut)}
  .pos{color:var(--grn)} .neg{color:var(--red)}
  .lv{font-size:12px;color:var(--mut);margin:6px 0}
  .rec{margin-top:8px;padding:8px 10px;background:#0d1117;border-radius:6px;font-size:13px}
  .act{font-weight:700} .reason{color:var(--mut);margin-top:3px}
  .spin{color:var(--mut);font-size:13px}
</style></head>
<body>
<header>
  <h1>Equity Advisor</h1>
  <span id="mode" class="badge sim">…</span>
  <button class="ghost" onclick="toggleMode()">switch mode</button>
  <span class="sp"></span>
  <button id="runbtn" onclick="runAnalysis()">Run AI analysis</button>
  <button class="ghost" onclick="loadAll()">refresh</button>
</header>
<div class="wrap">
  <div class="col"><h2>Potential stocks</h2><div id="cands"><div class="spin">Click "Run AI analysis" to rank candidates and get entry advice.</div></div></div>
  <div class="col"><h2>My holdings</h2><div id="holds"><div class="spin">loading…</div></div></div>
</div>
<script>
let MODE="simulation", ADVICE={};
const fmt=n=>n==null?"–":Number(n).toLocaleString('en-IN',{maximumFractionDigits:2});
const cls=v=>v>0?'pos':v<0?'neg':'';
async function api(p,o){const r=await fetch(p,o||{});return r.json();}

async function loadMode(){const d=await api('/mode');MODE=d.mode;const e=document.getElementById('mode');e.textContent=MODE.toUpperCase();e.className='badge '+(MODE==='live'?'live':'sim');}
async function toggleMode(){const next=MODE==='live'?'simulation':'live';if(next==='live'&&!confirm('Switch to LIVE? Buy/Sell will place REAL orders.'))return;await api('/mode?mode='+next,{method:'POST'});loadMode();}

async function loadPositions(){const d=await api('/positions');renderHolds(d.positions||[]);}
function renderHolds(rows){
  const el=document.getElementById('holds');
  if(!rows.length){el.innerHTML='<div class="spin">No holdings.</div>';return;}
  el.innerHTML=rows.map(r=>{
    const a=ADVICE[r.symbol];
    return `<div class="card"><div class="row">
      <span class="nm">${r.name}</span>
      <span class="tag ${r.source==='ACTUAL'?'tactual':'tpaper'}">${r.source==='ACTUAL'?'actual':'paper'}</span>
      <span class="sp"></span>
      <button class="sell" onclick="trade('${r.symbol}','SELL')">Sell</button></div>
      <div class="lv">${r.qty} @ ₹${fmt(r.avg_price)} · LTP ₹${fmt(r.ltp)} ·
        <span class="${cls(r.pnl)}">P&L ₹${fmt(r.pnl)} (${fmt(r.pnl_pct)}%)</span></div>
      ${a?`<div class="rec"><span class="act ${a.action==='EXIT'?'neg':a.action==='HOLD'||a.action==='ADD'?'pos':''}">${a.action||'—'}</span>
        <span class="mut">(${a.conviction||'?'})</span> · key resist ₹${fmt(a.key_resistance)} · stop ₹${fmt(a.stop)}
        <div class="reason">${a.reasons||''}</div></div>`:''}
    </div>`;}).join('');
}

async function runAnalysis(){
  const btn=document.getElementById('runbtn');btn.disabled=true;btn.textContent='Analysing… (minutes)';
  document.getElementById('cands').innerHTML='<div class="spin">Running momentum screen + LLM analysis…</div>';
  try{
    const d=await api('/analysis/run?candidates=6&clean=true',{method:'POST'});
    ADVICE={};(d.holdings||[]).forEach(c=>ADVICE[c.symbol]=c.recommendation||{});
    renderCands(d.candidates||[]); await loadPositions();
  }catch(e){document.getElementById('cands').innerHTML='<div class="spin">Analysis failed: '+e+'</div>';}
  btn.disabled=false;btn.textContent='Run AI analysis';
}
function renderCands(rows){
  const el=document.getElementById('cands');
  if(!rows.length){el.innerHTML='<div class="spin">No candidates.</div>';return;}
  el.innerHTML=rows.map(r=>{const a=r.recommendation||{};
    return `<div class="card"><div class="row">
      <span class="nm">${r.name}</span><span class="mut">₹${fmt(r.ltp)} · ${r.regime} · mom ${fmt(r.momentum_12m_pct)}%</span>
      <span class="sp"></span><button class="buy" onclick="trade('${r.symbol}','BUY')">Buy</button></div>
      <div class="lv">resistance ${JSON.stringify(r.resistances)} · support ${JSON.stringify(r.supports)}</div>
      <div class="rec"><span class="act ${a.action==='ENTER'?'pos':a.action==='AVOID'?'neg':''}">${a.action||'—'}</span>
        <span class="mut">(${a.conviction||'?'})</span> · stop ₹${fmt(a.stop)} · target ₹${fmt(a.target)}
        <div class="reason">${a.reasons||''}</div></div></div>`;}).join('');
}

async function trade(symbol,side){
  const qty=parseInt(prompt(`${side} how many shares of ${symbol.split(':')[1]}?`,'1'));
  if(!qty||qty<=0)return;
  let d=await api(`/trade?symbol=${encodeURIComponent(symbol)}&side=${side}&qty=${qty}`,{method:'POST'});
  if(d.status==='confirm_required'){if(!confirm(d.message))return;
    d=await api(`/trade?symbol=${encodeURIComponent(symbol)}&side=${side}&qty=${qty}&confirm=true`,{method:'POST'});}
  alert((d.action||d.status)+' '+(d.mode||'')+' '+symbol+(d.fill?(' @ ₹'+d.fill):'')+(d.realized_pnl!=null?(' · P&L ₹'+d.realized_pnl):''));
  loadPositions();
}
function loadAll(){loadMode();loadPositions();}
loadAll();
</script></body></html>"""
