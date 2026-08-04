// GRIDLAB Optimization Lab — single / sweep / heatmap / walk-forward на едином Quoter.
'use strict';
const $ = id => document.getElementById(id);
const S = { tab:'single', source:'local', tf:'15m', bars:4000, capital:1000,
  base:{ mode:'grid', grid_spacing:1.5, levels:2, max_orders:2, tp:1.5, sl:4, ema:21,
         expansion:'geometric', as_gamma:0.3, as_kappa:1.5 },
  weights:{ w_dd:0.5, w_fees:0.2 },
  last:{} };

const f2=v=>(v==null?'—':(+v).toFixed(2));
const f3=v=>(v==null?'—':(+v).toFixed(3));
const cls=v=>v>0?'pos':(v<0?'neg':'');
const dt=ms=>{ if(!ms) return '—'; const d=new Date(ms); return d.toISOString().slice(0,10); };

async function api(url, body){
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  return r.json();
}
function req(extra){ return Object.assign({ source:S.source, symbol:'ETHBTC', tf:S.tf, bars:+S.bars,
  capital:+S.capital, base:S.base, weights:S.weights }, extra||{}); }

// ─────────────────────────── боковая панель ───────────────────────────
function renderSide(){
  const b=S.base;
  $('side').innerHTML = `
  <div class="card">
    <div class="h">Данные</div>
    <label>Источник</label>
    <select id="src">
      <option value="local" ${S.source==='local'?'selected':''}>ETHBTC.txt (спот, локально)</option>
      <option value="bybit" ${S.source==='bybit'?'selected':''}>Bybit (перп, API)</option>
    </select>
    <label>Таймфрейм</label>
    <select id="tf">${['5m','15m','30m','1h','4h'].map(t=>`<option ${S.tf===t?'selected':''}>${t}</option>`).join('')}</select>
    <label>Свечей (с конца)</label>
    <input id="bars" type="number" value="${S.bars}" step="500" min="200" />
    <label>Капитал $</label>
    <input id="cap" type="number" value="${S.capital}" step="100" min="100" />
  </div>
  <div class="card">
    <div class="h">База стратегии</div>
    <label>Режим</label>
    <select id="mode">
      <option value="grid" ${b.mode==='grid'?'selected':''}>Классический грид</option>
      <option value="avellaneda" ${b.mode==='avellaneda'?'selected':''}>Avellaneda-Stoikov</option>
      <option value="heuristic" ${b.mode==='heuristic'?'selected':''}>Эвристика</option>
    </select>
    <div class="grid2">
      <div><label>Levels</label><input id="levels" type="number" value="${b.levels}" min="2"/></div>
      <div><label>Max ордеров</label><input id="max_orders" type="number" value="${b.max_orders}" min="2"/></div>
      <div><label>Размер $</label><input id="order_usd" type="number" value="${b.order_usd||80}" min="1"/></div>
      <div><label>EMA</label><input id="ema" type="number" value="${b.ema}" min="5"/></div>
    </div>
  </div>
  <div class="card">
    <div class="h">Робастная метрика</div>
    <div class="note">score = ROI − w_dd·DD − w_fees·fees%</div>
    <div class="grid2">
      <div><label>w_dd</label><input id="w_dd" type="number" value="${S.weights.w_dd}" step="0.1"/></div>
      <div><label>w_fees</label><input id="w_fees" type="number" value="${S.weights.w_fees}" step="0.1"/></div>
    </div>
  </div>`;
  $('src').onchange=e=>{S.source=e.target.value;};
  $('tf').onchange=e=>{S.tf=e.target.value;};
  $('bars').onchange=e=>{S.bars=+e.target.value;};
  $('cap').onchange=e=>{S.capital=+e.target.value;};
  $('mode').onchange=e=>{S.base.mode=e.target.value;};
  ['levels','max_orders','order_usd','ema'].forEach(k=>{const el=$(k); if(el) el.onchange=e=>S.base[k]=+e.target.value;});
  $('w_dd').onchange=e=>S.weights.w_dd=+e.target.value;
  $('w_fees').onchange=e=>S.weights.w_fees=+e.target.value;
}

// ─────────────────────────── основной экран ───────────────────────────
function renderMain(){
  const tabs=[['single','Single'],['sweep','Sweep'],['heatmap','Heatmap'],['walk','Walk-Forward'],['optimizer','Оптимизатор']];
  $('main').innerHTML = `
    <div class="tabs">${tabs.map(([k,l])=>`<div class="tab ${S.tab===k?'on':''}" data-tab="${k}">${l}</div>`).join('')}</div>
    <div id="body"></div>`;
  document.querySelectorAll('[data-tab]').forEach(el=>el.onclick=()=>{S.tab=el.dataset.tab; renderMain();});
  ({single:bodySingle, sweep:bodySweep, heatmap:bodyHeatmap, walk:bodyWalk, optimizer:bodyOptimizer}[S.tab])();
}

function busy(msg){ $('body').innerHTML=`<div class="card muted">${msg||'Считаю…'}</div>`; }

// ── Single ──
function bodySingle(){
  $('body').innerHTML=`<div class="card"><div class="row"><button class="btn" style="width:auto;padding:9px 18px;" id="run">Запустить бэктест</button>
    <span class="note">Один прогон на выбранном периоде единым Quoter (как в Live).</span></div></div>
    <div id="out"></div>`;
  $('run').onclick=async()=>{ $('out').innerHTML='<div class="card muted">Считаю…</div>';
    const r=await api('/api/lab/single', req()); S.last.single=r;
    if(r.error){ $('out').innerHTML=`<div class="card neg">Ошибка: ${r.error}</div>`; return; }
    const m=r.metrics;
    $('out').innerHTML=`
    <div class="card"><div class="h">Результат · ${r.bars} свечей · ${dt(r.from)} → ${dt(r.to)} ${r.spot?'· спот (funding off)':''}</div>
      <div class="grid2">
        <div><div class="kv"><span>ROI</span><b class="${cls(m.roi)}">${f3(m.roi)}%</b></div>
          <div class="kv"><span>Макс. просадка</span><b>${f3(m.max_dd)}%</b></div>
          <div class="kv"><span>Сделок</span><b>${m.trades}</b></div>
          <div class="kv"><span>Profit factor</span><b>${f3(m.profit_factor)}</b></div></div>
        <div><div class="kv"><span>Комиссии</span><b>$${f2(m.fees)} (${f3(m.fees_pct)}%)</b></div>
          <div class="kv"><span>Funding</span><b>$${f2(m.funding)}</b></div>
          <div class="kv"><span>Sharpe (на бар)</span><b>${f3(m.sharpe)}</b></div>
          <div class="kv"><span>Ликвидация</span><b class="${m.liquidated?'neg':''}">${m.liquidated?'ДА':'нет'}</b></div></div>
      </div>
      ${equitySVG(m.equity_curve, +S.capital)}
      <div class="row" style="margin-top:10px;"><button class="btn alt" style="width:auto;padding:7px 14px;" onclick='dl("single")'>Экспорт JSON</button></div>
    </div>`;
  };
}

// ── Sweep ──
function gridSpecUI(){
  return `<div class="card"><div class="h">Сетка перебора (через запятую)</div>
    <div class="grid2">
      <div><label>Grid ATR (k)</label><input id="g_grid" value="1.0,1.25,1.5,1.75,2.0,2.5,3.0"/></div>
      <div><label>SL ATR</label><input id="g_sl" value="2,3,4,5,6"/></div>
      <div><label>Max ордеров</label><input id="g_max" value="2"/></div>
      <div><label>EMA</label><input id="g_ema" value="21"/></div>
    </div>
    <div class="note" style="margin-top:6px;">Примечание: TP в текущей Quoter-модели не влияет (round-trip через перекотировку, без отдельного TP-ордера).</div>
  </div>`;
}
function readGrid(){
  const p=s=>s.split(',').map(x=>parseFloat(x.trim())).filter(x=>isFinite(x));
  const g={};
  const gs=p($('g_grid').value); if(gs.length) g.grid_spacing=gs;
  const sl=p($('g_sl').value); if(sl.length) g.sl=sl;
  const mx=p($('g_max').value); if(mx.length) g.max_orders=mx;
  const em=p($('g_ema').value); if(em.length) g.ema=em;
  return g;
}
function bodySweep(){
  $('body').innerHTML=gridSpecUI()+
    `<div class="card"><div class="row"><button class="btn" style="width:auto;padding:9px 18px;" id="run">Найти робастные параметры</button>
      <span class="note">Полный перебор. Ранжирование по robust score, не по ROI.</span></div></div><div id="out"></div>`;
  $('run').onclick=async()=>{ $('out').innerHTML='<div class="card muted">Перебираю комбинации…</div>';
    const r=await api('/api/lab/sweep', req({grid:readGrid()})); S.last.sweep=r;
    if(r.error){ $('out').innerHTML=`<div class="card neg">Ошибка: ${r.error}</div>`; return; }
    const z=r.zone; const top=r.results.slice(0,15);
    $('out').innerHTML=`
    <div class="card"><div class="h">Ширина прибыльной зоны</div>
      <div class="kv"><span>Комбинаций</span><b>${z.total}${r.truncated?' (обрезано до '+r.max_combos+')':''}</b></div>
      <div class="kv"><span>Прибыльных</span><b class="${z.profitable_share>0.5?'pos':''}">${z.profitable} (${(z.profitable_share*100).toFixed(0)}%)</b></div>
      <div class="note">Широкая сплошная прибыльная зона = робастно. Одна точка среди убытков = оверфит.</div>
    </div>
    <div class="card"><div class="h">Топ по robust score</div>
      <table><thead><tr><th>Параметры</th><th>Score</th><th>ROI%</th><th>DD%</th><th>Сделок</th><th>PF</th><th>Fees$</th></tr></thead>
      <tbody>${top.map(x=>`<tr><td class="mono">${Object.entries(x.params).map(([k,v])=>k+'='+v).join(' ')}</td>
        <td><b>${f3(x.score)}</b></td><td class="${cls(x.metrics.roi)}">${f3(x.metrics.roi)}</td>
        <td>${f3(x.metrics.max_dd)}</td><td>${x.metrics.trades}</td><td>${f3(x.metrics.profit_factor)}</td>
        <td>${f2(x.metrics.fees)}</td></tr>`).join('')}</tbody></table>
      <div class="row" style="margin-top:10px;"><button class="btn alt" style="width:auto;padding:7px 14px;" onclick='dl("sweep")'>Экспорт JSON</button>
      <button class="btn alt" style="width:auto;padding:7px 14px;" onclick='dlCsv()'>Экспорт CSV</button></div>
    </div>`;
  };
}

// ── Heatmap ──
function bodyHeatmap(){
  $('body').innerHTML=`<div class="card"><div class="h">Оси карты</div>
    <div class="grid2">
      <div><label>X-параметр</label><select id="xk">${optKeys('grid_spacing')}</select>
        <input id="xv" value="1.0,1.25,1.5,1.75,2.0,2.5,3.0" style="margin-top:4px;"/></div>
      <div><label>Y-параметр</label><select id="yk">${optKeys('sl')}</select>
        <input id="yv" value="2,3,4,5,6" style="margin-top:4px;"/></div>
    </div>
    <label>Метрика (цвет)</label><select id="mt">${['score','roi','max_dd','trades'].map(m=>`<option ${m==='score'?'selected':''}>${m}</option>`).join('')}</select>
    <button class="btn" id="run">Построить heatmap</button></div><div id="out"></div>`;
  $('run').onclick=async()=>{ $('out').innerHTML='<div class="card muted">Считаю карту…</div>';
    const p=s=>s.split(',').map(x=>parseFloat(x.trim())).filter(x=>isFinite(x));
    const r=await api('/api/lab/heatmap', req({xkey:$('xk').value, ykey:$('yk').value,
      xvals:p($('xv').value), yvals:p($('yv').value), metric:$('mt').value})); S.last.heatmap=r;
    if(r.error){ $('out').innerHTML=`<div class="card neg">Ошибка: ${r.error}</div>`; return; }
    $('out').innerHTML=`<div class="card"><div class="h">Heatmap · ${r.metric} (${r.bars} свечей)</div>
      ${heatmapHTML(r)}
      <div class="kv" style="margin-top:10px;"><span>Лучшая точка</span><b class="mono">${r.xkey}=${r.best.x} ${r.ykey}=${r.best.y} · ${r.metric}=${f3(r.best.val)}</b></div>
      <div class="kv"><span>Прибыльных соседей вокруг оптимума</span><b class="${r.neighbor_share>0.6?'pos':'neg'}">${r.neighbor_profitable}/${r.neighbor_total} (${(r.neighbor_share*100).toFixed(0)}%)</b></div>
      <div class="note">Высокая доля прибыльных соседей = робастная зона. Низкая = изолированный пик (оверфит).</div>
      <div class="row" style="margin-top:10px;"><button class="btn alt" style="width:auto;padding:7px 14px;" onclick='dl("heatmap")'>Экспорт JSON</button></div>
    </div>`;
  };
}
function optKeys(sel){ return ['grid_spacing','sl','max_orders','ema','as_gamma','as_kappa']
  .map(k=>`<option ${k===sel?'selected':''}>${k}</option>`).join(''); }

function heatmapHTML(r){
  const flat=[]; r.grid.forEach(row=>row.forEach(v=>flat.push(v)));
  const mn=Math.min(...flat), mx=Math.max(...flat);
  const color=v=>{ if(mx===mn) return '#eef0ec';
    const t=(v-mn)/(mx-mn);
    // красный(0) → жёлтый(.5) → зелёный(1)
    const r=t<0.5?210:Math.round(210-(t-0.5)*2*180);
    const g=t<0.5?Math.round(60+t*2*150):200;
    return `rgb(${r},${g},70)`; };
  let h='<table style="border-collapse:separate;border-spacing:2px;"><thead><tr><th></th>'+
    r.xvals.map(x=>`<th style="text-align:center;">${x}</th>`).join('')+'</tr></thead><tbody>';
  r.grid.forEach((row,j)=>{ h+=`<tr><th>${r.yvals[j]}</th>`+
    row.map((v,i)=>{ const isBest=(r.best.i===i&&r.best.j===j);
      return `<td style="text-align:center;color:#14181a;background:${color(v)};border-radius:4px;padding:7px 6px;${isBest?'outline:2px solid #14181a;':''}" title="${r.xkey}=${r.xvals[i]} ${r.ykey}=${r.yvals[j]} roi=${f3(r.roi[j][i])}%">${f2(v)}</td>`; }).join('')+'</tr>'; });
  h+='</tbody></table>'; return `<div style="overflow-x:auto;">${h}</div>`;
}

// ── Walk-Forward ──
function bodyWalk(){
  $('body').innerHTML=gridSpecUI()+
    `<div class="card"><div class="grid2">
      <div><label>Train, дней</label><input id="trd" type="number" value="30"/></div>
      <div><label>Test, дней</label><input id="ted" type="number" value="7"/></div>
    </div><button class="btn" id="run">Запустить walk-forward</button>
    <div class="note">Оптимизация на train → проверка на следующем test (out-of-sample). Анти-оверфит.</div></div><div id="out"></div>`;
  $('run').onclick=async()=>{ $('out').innerHTML='<div class="card muted">Скольжу по окнам…</div>';
    const r=await api('/api/lab/walkforward', req({grid:readGrid(), train_days:+$('trd').value, test_days:+$('ted').value, max_windows:12})); S.last.walk=r;
    if(r.error){ $('out').innerHTML=`<div class="card neg">Ошибка: ${r.error}</div>`; return; }
    const s=r.summary;
    $('out').innerHTML=`
    <div class="card"><div class="h">Итог out-of-sample</div>
      <div class="kv"><span>Окон</span><b>${s.windows}</b></div>
      <div class="kv"><span>Средний OOS ROI</span><b class="${cls(s.oos_avg_roi)}">${f3(s.oos_avg_roi)}%</b></div>
      <div class="kv"><span>Прибыльных OOS-окон</span><b class="${s.oos_profitable_share>0.5?'pos':'neg'}">${s.oos_profitable}/${s.windows} (${(s.oos_profitable_share*100).toFixed(0)}%)</b></div>
      <div class="note">Если OOS≈0 и лучшие параметры скачут от окна к окну — устойчивого edge нет (переобучение).</div>
    </div>
    <div class="card"><div class="h">In-sample → Out-of-sample по окнам</div>
      <table><thead><tr><th>Train</th><th>Test</th><th>Параметры (на train)</th><th>IS ROI%</th><th>OOS ROI%</th></tr></thead>
      <tbody>${r.windows.map(w=>`<tr><td>${dt(w.train_from)}–${dt(w.train_to)}</td><td>${dt(w.test_from)}–${dt(w.test_to)}</td>
        <td class="mono">${Object.entries(w.best_params).map(([k,v])=>k+'='+v).join(' ')}</td>
        <td class="${cls(w.in_sample.roi)}">${f3(w.in_sample.roi)}</td>
        <td class="${cls(w.out_sample.roi)}">${f3(w.out_sample.roi)}</td></tr>`).join('')}</tbody></table>
      <div class="row" style="margin-top:10px;"><button class="btn alt" style="width:auto;padding:7px 14px;" onclick='dl("walk")'>Экспорт JSON</button></div>
    </div>`;
  };
}

// ── Optimizer ──
function rangeRow(id, label, def_from, def_to, def_step, type='num'){
  if(type==='list') return `<div><label>${label}</label><input id="${id}" value="${def_from}" style="font-size:11px;"/><div class="note" style="font-size:10px;">через запятую</div></div>`;
  return `<div><label>${label}</label><div class="row" style="gap:4px;">
    <input id="${id}_from" type="number" value="${def_from}" style="width:60px;" placeholder="от"/>
    <input id="${id}_to" type="number" value="${def_to}" style="width:60px;" placeholder="до"/>
    <input id="${id}_step" type="number" value="${def_step}" style="width:60px;" placeholder="шаг"/>
  </div></div>`;
}
function readRange(id, type){
  if(type==='list'){
    return $(id).value.split(',').map(x=>parseFloat(x.trim())).filter(x=>isFinite(x));
  }
  const from=parseFloat($(`${id}_from`).value), to=parseFloat($(`${id}_to`).value), step=parseFloat($(`${id}_step`).value);
  if(!isFinite(from)||!isFinite(to)||!isFinite(step)||step<=0) return [from];
  const arr=[];
  for(let v=from;v<=to+1e-9;v+=step) arr.push(Math.round(v*1000)/1000);
  return arr;
}
function bodyOptimizer(){
  $('body').innerHTML=`
  <div class="card"><div class="h">Диапазоны параметров</div>
    <div class="grid2">
      ${rangeRow('o_gs','Grid Spacing (ATR×)',1,10,0.5)}
      ${rangeRow('o_lev','Уровней',2,10,2)}
      ${rangeRow('o_max','Макс ордеров',2,10,2)}
      ${rangeRow('o_ema','EMA','10,21,50,100,200','','','list')}
      ${rangeRow('o_gamma','γ (риск-аверсия)','0.3,1,2,3,5,10','','','list')}
      ${rangeRow('o_kappa','κ (поток)','1,2,3,5,10,20','','','list')}
      ${rangeRow('o_exp','Expansion','geometric,arithmetic','','','list')}
      ${rangeRow('o_sl','SL (ATR×)',2,6,1)}
    </div>
  </div>
  <div class="card"><div class="h">Настройки оптимизации</div>
    <div class="grid2">
      <div><label>Режим поиска</label><select id="o_mode">
        <option value="full">Полный перебор</option>
        <option value="around">Вокруг текущих</option>
        <option value="random">Случайный поиск</option>
      </select></div>
      <div><label>Цель (скоринг)</label><select id="o_preset">
        <option value="robust">Robust (ROI−DD+Sharpe)</option>
        <option value="max_profit">Максимальная прибыль</option>
        <option value="min_dd">Минимальная просадка</option>
        <option value="profit_dd">Profit / Drawdown</option>
        <option value="max_sharpe">Максимальный Sharpe</option>
      </select></div>
      <div><label>Heatmap X</label><select id="o_hx">${optKeysOpt('grid_spacing')}</select></div>
      <div><label>Heatmap Y</label><select id="o_hy">${optKeysOpt('as_gamma')}</select></div>
      <div><label>Random N (если случайный)</label><input id="o_nrand" type="number" value="500"/></div>
    </div>
    <div class="row" style="margin-top:10px;gap:8px;">
      <button class="btn" style="width:auto;padding:9px 18px;" id="o_run">Запустить оптимизацию</button>
      <span id="o_combo_count" class="note"></span>
    </div>
  </div>
  <div id="o_progress" style="display:none;" class="card">
    <div class="h">Прогресс</div>
    <div style="background:#e3e6e0;border-radius:6px;height:22px;overflow:hidden;margin-bottom:6px;">
      <div id="o_bar" style="height:100%;background:var(--green);width:0%;transition:width 0.15s;border-radius:6px;"></div>
    </div>
    <span id="o_status" class="mono muted" style="font-size:11px;">0 / 0</span>
  </div>
  <div id="o_out"></div>`;

  // подсчёт комбинаций при изменении
  function updateCount(){
    const ranges=buildRanges(); let n=1;
    Object.values(ranges).forEach(v=>{n*=v.length;});
    const mode=$('o_mode').value;
    if(mode==='random') n=Math.min(n, +$('o_nrand').value||500);
    if(mode==='around') n=Math.min(n, 500);
    $('o_combo_count').textContent=`≈ ${n.toLocaleString()} комбинаций`;
  }
  document.querySelectorAll('#o_gs_from,#o_gs_to,#o_gs_step,#o_lev_from,#o_lev_to,#o_lev_step,#o_max_from,#o_max_to,#o_max_step,#o_sl_from,#o_sl_to,#o_sl_step,#o_ema,#o_gamma,#o_kappa,#o_exp,#o_mode,#o_nrand').forEach(el=>{if(el) el.addEventListener('change',updateCount);});
  updateCount();

  $('o_run').onclick=()=>runOptimizer();
}

function optKeysOpt(sel){ return ['grid_spacing','sl','max_orders','ema','as_gamma','as_kappa','levels']
  .map(k=>`<option ${k===sel?'selected':''}>${k}</option>`).join(''); }

function buildRanges(){
  const r={};
  const gs=readRange('o_gs'); if(gs.length) r.grid_spacing=gs;
  const lev=readRange('o_lev'); if(lev.length) r.levels=lev;
  const mx=readRange('o_max'); if(mx.length) r.max_orders=mx;
  const sl=readRange('o_sl'); if(sl.length) r.sl=sl;
  const ema=readRange('o_ema','list'); if(ema.length) r.ema=ema;
  const gam=readRange('o_gamma','list'); if(gam.length) r.as_gamma=gam;
  const kap=readRange('o_kappa','list'); if(kap.length) r.as_kappa=kap;
  const exp=readRange('o_exp','list');
  if(exp.length){
    const mapped=exp.map(v=>{
      if(typeof v==='string') return v;
      return v;
    });
    // expansion это строка — нужно прочитать сырые значения
  }
  return r;
}

function runOptimizer(){
  const ranges=buildRanges();
  // expansion — строковый параметр, читаем отдельно
  const expRaw=$('o_exp').value.split(',').map(s=>s.trim()).filter(Boolean);
  if(expRaw.length) ranges.expansion=expRaw;

  const mode=$('o_mode').value;
  const preset=$('o_preset').value;
  const hx=$('o_hx').value, hy=$('o_hy').value;

  $('o_progress').style.display='';
  $('o_bar').style.width='0%';
  $('o_status').textContent='Подключение…';
  $('o_out').innerHTML='';
  $('o_run').disabled=true;

  const proto=location.protocol==='https:'?'wss:':'ws:';
  const ws=new WebSocket(`${proto}//${location.host}/ws/optimizer`);
  let received=0, total=0;
  const allResults=[];

  ws.onopen=()=>{
    ws.send(JSON.stringify({
      source:S.source, tf:S.tf, bars:+S.bars, capital:+S.capital,
      base:S.base, ranges, search_mode:mode, preset,
      n_random:+($('o_nrand').value||500),
      current: mode==='around'? S.base : null,
      heatmap_x:hx, heatmap_y:hy,
    }));
  };

  ws.onmessage=(ev)=>{
    const msg=JSON.parse(ev.data);
    if(msg.type==='start'){
      total=msg.total;
      $('o_status').textContent=`0 / ${total} · ${msg.workers} ядер`;
    } else if(msg.type==='result'){
      received++;
      allResults.push(msg);
      const pct=Math.round(received/total*100);
      $('o_bar').style.width=pct+'%';
      $('o_status').textContent=`${received} / ${total} (${pct}%)`;
    } else if(msg.type==='done'){
      $('o_bar').style.width='100%';
      $('o_status').textContent=`Готово · ${msg.total} комбинаций`;
      $('o_run').disabled=false;
      renderOptimizerResults(msg);
    } else if(msg.type==='error'){
      $('o_status').textContent=`Ошибка: ${msg.msg}`;
      $('o_run').disabled=false;
    }
  };
  ws.onerror=()=>{ $('o_status').textContent='Ошибка WebSocket'; $('o_run').disabled=false; };
  ws.onclose=()=>{ $('o_run').disabled=false; };
}

function renderOptimizerResults(msg){
  const top=msg.top||[];
  const hm=msg.heatmap;
  let html='';

  // Таблица результатов
  html+=`<div class="card"><div class="h">Топ-20 по ${msg.preset} · робастность соседей</div>
    <table><thead><tr><th>#</th><th>Score</th><th>ROI%</th><th>DD%</th><th>Sharpe</th><th>Sortino</th><th>PF</th><th>Trades</th><th>Avg$</th><th>Fees$</th><th>Robust</th><th>Параметры</th><th></th></tr></thead>
    <tbody>${top.map((r,i)=>{
      const m=r.metrics, rb=r.robustness||{};
      return `<tr>
        <td>${i+1}</td>
        <td><b>${(+r.score).toFixed(2)}</b></td>
        <td class="${cls(m.roi)}">${f3(m.roi)}</td>
        <td>${f3(m.max_dd)}</td>
        <td>${f3(m.sharpe)}</td>
        <td>${f3(m.sortino)}</td>
        <td>${f3(m.profit_factor)}</td>
        <td>${m.trades}</td>
        <td>${f2(m.avg_trade)}</td>
        <td>${f2(m.fees)}</td>
        <td class="${(rb.neighbor_share||0)>0.6?'pos':'neg'}">${rb.neighbor_profitable||0}/${rb.neighbor_total||0} (${((rb.neighbor_share||0)*100).toFixed(0)}%)</td>
        <td class="mono" style="font-size:10px;">${Object.entries(r.combo).map(([k,v])=>k+'='+v).join(' ')}</td>
        <td><button class="btn alt" style="width:auto;padding:4px 10px;font-size:10px;margin:0;" onclick='applyParams(${JSON.stringify(r.combo)})'>Применить</button></td>
      </tr>`;
    }).join('')}</tbody></table>
    <div class="row" style="margin-top:10px;gap:8px;">
      <button class="btn alt" style="width:auto;padding:7px 14px;" onclick="dlOpt()">Экспорт JSON</button>
      <button class="btn alt" style="width:auto;padding:7px 14px;" onclick="dlOptCsv()">Экспорт CSV</button>
    </div>
  </div>`;

  // Heatmap
  if(hm && hm.grid){
    html+=`<div class="card"><div class="h">Heatmap · ${hm.xkey} × ${hm.ykey} · score</div>
      ${heatmapHTML({...hm, metric:'score', best:findBest(hm)})}
    </div>`;
  }

  S.last.optimizer={top, heatmap:hm, preset:msg.preset};
  $('o_out').innerHTML=html;
}

function findBest(hm){
  let best={i:0,j:0,val:-1e18,x:hm.xvals[0],y:hm.yvals[0]};
  hm.grid.forEach((row,j)=>row.forEach((v,i)=>{
    if(v>best.val) best={i,j,val:v,x:hm.xvals[i],y:hm.yvals[j]};
  }));
  return best;
}

function applyParams(combo){
  Object.entries(combo).forEach(([k,v])=>{ S.base[k]=v; });
  renderSide();
  alert('Параметры применены в боковую панель');
}

function dlOpt(){
  const d=S.last.optimizer; if(!d) return;
  const blob=new Blob([JSON.stringify(d,null,2)],{type:'application/json'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='gridlab_optimizer.json'; a.click();
}
function dlOptCsv(){
  const d=S.last.optimizer; if(!d||!d.top) return;
  const rows=[['rank','score','roi','max_dd','sharpe','sortino','profit_factor','trades','avg_trade','fees','robust_share','params']];
  d.top.forEach((r,i)=>{
    const m=r.metrics, rb=r.robustness||{};
    rows.push([i+1,r.score,m.roi,m.max_dd,m.sharpe,m.sortino,m.profit_factor,m.trades,m.avg_trade,m.fees,
      rb.neighbor_share||0, Object.entries(r.combo).map(([k,v])=>k+'='+v).join(' ')]);
  });
  const csv=rows.map(r=>r.map(c=>`"${c}"`).join(',')).join('\n');
  const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'})); a.download='gridlab_optimizer.csv'; a.click();
}

// ── equity SVG ──
function equitySVG(curve, cap){
  if(!curve||curve.length<2) return '';
  const W=720,H=140,pad=4; const mn=Math.min(...curve),mx=Math.max(...curve);
  const sx=i=>pad+i/(curve.length-1)*(W-2*pad);
  const sy=v=>{ if(mx===mn) return H/2; return H-pad-(v-mn)/(mx-mn)*(H-2*pad); };
  const d=curve.map((v,i)=>`${i?'L':'M'}${sx(i).toFixed(1)},${sy(v).toFixed(1)}`).join(' ');
  const last=curve[curve.length-1]; const col=last>=cap?'#1F8A5B':'#a93529';
  const y0=sy(cap);
  return `<div class="h" style="margin-top:12px;">Equity curve</div>
  <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:140px;background:#fbfcfa;border:1px solid var(--line);border-radius:8px;">
    <line x1="0" y1="${y0.toFixed(1)}" x2="${W}" y2="${y0.toFixed(1)}" stroke="#cbd2cb" stroke-dasharray="3,3"/>
    <path d="${d}" fill="none" stroke="${col}" stroke-width="1.5"/>
  </svg>
  <div class="note">старт $${cap.toFixed(0)} · финал $${last.toFixed(2)}</div>`;
}

// ── экспорт ──
function dl(kind){ const data=S.last[kind]; if(!data) return;
  const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=`gridlab_lab_${kind}.json`; a.click(); }
function dlCsv(){ const r=S.last.sweep; if(!r) return;
  const rows=[['params','score','roi','max_dd','trades','profit_factor','fees']];
  r.results.forEach(x=>rows.push([Object.entries(x.params).map(([k,v])=>k+'='+v).join(' '),
    x.score,x.metrics.roi,x.metrics.max_dd,x.metrics.trades,x.metrics.profit_factor,x.metrics.fees]));
  const csv=rows.map(r=>r.map(c=>`"${c}"`).join(',')).join('\n');
  const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'})); a.download='gridlab_sweep.csv'; a.click(); }

// ── boot ──
async function boot(){
  renderSide(); renderMain();
  const info=await (await fetch('/api/lab/data_info')).json();
  if(info.exists) $('dataInfo').textContent=`${info.symbol} · ${info.base_tf} · ${info.from}→${info.to} · ${info.bars_1m.toLocaleString()} баров · спот (funding off)`;
  else $('dataInfo').textContent='ETHBTC.txt не найден';
}
boot();
