/* GRIDLAB · Replay-as-Live — проигрывание архива фьючерса MOEX как ЖИВОЙ торговли.
   Цены текут через тот же тиковый конвейер и единый Quoter, что и Live. Регулятор:
   скорость (баров/сек) · пауза · перемотка · рестарт · шаг. Строго светлая тема.
   Исполнение: 1 контракт атомарно, мейкер-комиссия 0, PnL в рублях (point_value). */
(() => {
'use strict';

const _defParams = {mode:'heuristic', grid_spacing:1.8, levels:2, max_orders:6, ema:21,
          expansion:'geometric', as_gamma:0.3, as_kappa:1.5, contract_qty:1, session_pause:0};
function _loadSaved(){ try{ const s=localStorage.getItem('gridlab_replay'); if(s){ const o=JSON.parse(s); return o; } }catch(e){} return {}; }
function _save(){ try{ localStorage.setItem('gridlab_replay', JSON.stringify({code:S.code, tf:S.tf, speed:S.speed, capital:S.capital, params:S.params})); }catch(e){} }
const _saved = _loadSaved();
const S = {
  cfg:null, specs:[], default:'Si', tfs:[],
  code:_saved.code||'Si', tf:_saved.tf||'5m', speed:_saved.speed||1, capital:_saved.capital||100000, bars:1800,
  params:Object.assign({}, _defParams, _saved.params||{}),
  ws:null, frame:null, connected:false, _paramsDirty:false,
  tab:'log', _draft:null,
  histC:[], histT:[], _histLoading:false, _histDone:false,   // ленивая подгрузка истории назад
  futC:[], futT:[], _futLoading:false, _futDone:false,       // предпросмотр будущих свечей (вперёд)
};
const C = {accent:'#1F8A5B', green:'#157a4f', red:'#a93529', blue:'#3B82CE', amber:'#C98A1A',
  mut:'#939a93', mut2:'#5b635e', line:'#e3e6e0'};
const TIPS = {
  mode:'Режим торговли. Ручной: без автоматики, только ваши лимитки. Эвристика: чем больше инвентарь — тем шире шаг. A-S: центр сетки смещается против позиции.',
  grid_spacing:'Шаг сетки в единицах ATR (k×ATR). Безразмерный — сравним между инструментами. Больше k = реже сделки.',
  max_orders:'Потолок открытых ордеров — ограничение инвентаря (в контрактах). При достижении новые на стороне набора не ставятся.',
  ema:'Окно EMA-фильтра тренда. В нисходящем тренде покупки осторожнее, продажи агрессивнее.',
  expansion:'Как растёт шаг между уровнями вглубь: Geometric / Arithmetic / Fibonacci.',
  as_gamma:'γ — риск-аверсия Avellaneda-Stoikov. Больше γ = сильнее уклон центра против инвентаря и шире спред.',
  as_kappa:'κ — интенсивность потока в Avellaneda-Stoikov. Влияет на базовую надбавку к спреду.',
  session_pause:'Минут до конца и после начала торгового дня (MOEX 10:00–23:50 МСК): закрыть позицию по рынку, снять все лимитки, не торговать. 0 = выключено.',
  speed:'Скорость проигрывания: сколько баров архива проигрывается за секунду. Замедли — видишь каждую сделку; ускорь — день за секунды.',
};

const $ = id => document.getElementById(id);
const api = (p,o) => fetch(p,o).then(r=>r.json());
function toast(t){ const e=$('toast'); e.textContent=t; e.classList.add('on'); clearTimeout(e._t); e._t=setTimeout(()=>e.classList.remove('on'),2200); }

// ───────── форматирование ₽ ─────────
function spec(){ return S.specs.find(x=>x.code===S.code) || {label:S.code, point_value:1, price_step:1, approx:false}; }
function fmtPx(v){ const st=spec().price_step; if(!(v>0)) return '—';
  if(st>=1) return Math.round(v).toLocaleString('ru-RU');
  if(st>=0.01) return v.toFixed(2); return v.toFixed(4); }
function rub(v,d=0){ const s=v<0?'−':''; return s+Math.abs(v).toLocaleString('ru-RU',{minimumFractionDigits:d,maximumFractionDigits:d})+' ₽'; }
function tfLabel(t){ return t; }

// ───────── boot ─────────
async function boot(){
  setupTooltips();
  const r = await api('/api/replay/instruments');
  S.specs=r.instruments; S.default=r.default; S.tfs=r.tfs; S.code=r.default;
  render();
  connect();
  window.addEventListener('resize', ()=>requestAnimationFrame(drawAll));
}

function connect(){
  if(S.ws){ try{S.ws.close();}catch(e){} }
  const proto = location.protocol==='https:'?'wss':'ws';
  const ws=new WebSocket(`${proto}://${location.host}/ws/replay`); S.ws=ws;
  ws.onopen=()=>{ S.connected=true; syncConn();
    // стартуем НА ПАУЗЕ — проигрывание/торговля идут только по нажатию ▶ (а не сразу при загрузке)
    ws.send(JSON.stringify({code:S.code, tf:S.tf, speed:S.speed, capital:S.capital,
      bars:S.bars, params:S.params, paused:true})); };
  ws.onmessage=(ev)=>{ const m=JSON.parse(ev.data);
    if(m.type==='error'){ toast('Replay: '+m.msg); return; }
    if(m.type==='history'){ applyHistory(m); return; }
    if(m.type==='future'){ applyFuture(m); return; }
    const needFit = !S.frame || S._needFit;
    S.frame=m; S._needFit=false;
    if(m.type==='done') toast('Проигрывание архива завершено');
    apply();
    if(needFit && S._chart){ try{ S._chart.timeScale().fitContent(); S._chart.priceScale('right').applyOptions({autoScale:true}); }catch(e){} }
  };
  ws.onclose=()=>{ S.connected=false; syncReplayBar(); syncConn();
    // авто-переподключение: если сервер перезапустили/связь оборвалась — поднимаем заново
    if(!S._closing){ clearTimeout(S._recon); S._recon=setTimeout(()=>connect(), 1500); }
  };
}
function syncConn(){
  // обновить индикатор связи в шапке без полного render
  const wrap=document.querySelector('[data-conn]'); if(!wrap) return;
  const f=S.frame; const dot=!S.connected?C.mut:(f&&f.playing?C.accent:C.amber);
  const status=!S.connected?'нет связи':(f&&f.playing?'идёт':'пауза');
  wrap.querySelector('[data-conn-dot]').style.background=dot;
  wrap.querySelector('[data-conn-txt]').textContent=status;
  wrap.querySelector('[data-conn-txt]').style.color=dot;
}
function send(o){ if(S.ws && S.ws.readyState===1) S.ws.send(JSON.stringify(o)); }

// ───────── данные кадра ─────────
function dash(){ return (S.frame&&S.frame.dash) || {balance:S.capital,equity:S.capital,roi:0,drawdown:0,realized:0,unrealized:0,position:0,long_filled:0,short_filled:0,avg_entry:'—',trades:0,open_orders:0,fees:0}; }
function eqCurve(){ return (S.frame&&S.frame.equity_curve&&S.frame.equity_curve.length>1)?S.frame.equity_curve:null; }
function balCurve(){ return (S.frame&&S.frame.balance_curve&&S.frame.balance_curve.length>1)?S.frame.balance_curve:null; }
function selData(){ if(!S.frame||!S.frame.candles) return null;
  let cs=S.frame.candles, ts=S.frame.times;
  // история слева (старше окна)
  if(S.histC.length && S.histT.length && S.histT[S.histT.length-1] < ts[0]){
    cs=S.histC.concat(cs); ts=S.histT.concat(ts);
  }
  // предпросмотр справа (новее окна)
  if(S.futC.length && S.futT.length && S.futT[0] > ts[ts.length-1]){
    cs=cs.concat(S.futC); ts=ts.concat(S.futT);
  }
  return {candles:cs, times:ts, markers:S.frame.markers||[], last:S.frame.price}; }
// сброс подгруженной истории (смена инструмента/ТФ/перемотка — окно меняется)
function resetHist(){ S.histC=[]; S.histT=[]; S._histLoading=false; S._histDone=false; resetFut(); }
function resetFut(){ S.futC=[]; S.futT=[]; S._futLoading=false; S._futDone=false; }
function applyHistory(m){
  S._histLoading=false;
  if(m.exhausted) S._histDone=true;
  if(!m.times || !m.times.length) return;
  // оставить только бары СТАРШЕ текущей самой ранней свечи (без пересечений)
  const earliest = S.histT.length? S.histT[0] : (S.frame&&S.frame.times? S.frame.times[0] : Infinity);
  const cut = m.times.findIndex(t=>t>=earliest);
  const end = cut===-1? m.times.length : cut;
  if(end<=0) return;
  S.histC = m.candles.slice(0,end).concat(S.histC);
  S.histT = m.times.slice(0,end).concat(S.histT);
  if(S.histC.length>4000){ const drop=S.histC.length-4000; S.histC=S.histC.slice(drop); S.histT=S.histT.slice(drop); }
  updateChart();   // данные расширились влево — диапазон сохраняется, появляются старые свечи
}
function maybeLoadHistory(r){
  if(!r || S._histLoading || S._histDone) return;
  if(r.from > 12) return;   // не у левого края — не грузим
  const earliest = S.histT.length? S.histT[0] : (S.frame&&S.frame.times? S.frame.times[0] : 0);
  if(!earliest) return;
  S._histLoading=true;
  send({action:'more_history', before_ts: earliest*1000, count:600});
}
function applyFuture(m){
  S._futLoading=false;
  if(m.exhausted) S._futDone=true;
  if(!m.times || !m.times.length) return;
  // оставить только бары НОВЕЕ текущего последнего показанного
  const latest = S.futT.length? S.futT[S.futT.length-1] : (S.frame&&S.frame.times? S.frame.times[S.frame.times.length-1] : -1);
  const start = m.times.findIndex(t=>t>latest);
  if(start===-1) return;
  S.futC = S.futC.concat(m.candles.slice(start));
  S.futT = S.futT.concat(m.times.slice(start));
  if(S.futC.length>4000){ S.futC=S.futC.slice(-4000); S.futT=S.futT.slice(-4000); }
  updateChart();   // вид сохраняется по времени бара внутри updateChart — без каскада
}
function maybeLoadFuture(r){
  if(!r || S._futLoading || S._futDone) return;
  if(!S.frame || S.frame.playing) return;   // вперёд тянем только на паузе (при игре — следим за краем)
  const dataLen = S.histC.length + (S.frame.candles?S.frame.candles.length:0) + S.futC.length;
  // тянем следующие свечи при ПОДХОДЕ к правому краю (в пределах ~3 баров или дальше),
  // чтобы при прокрутке/зуме вперёд они подгружались заранее
  if(r.to < dataLen - 3) return;
  const latest = S.futT.length? S.futT[S.futT.length-1] : (S.frame.times? S.frame.times[S.frame.times.length-1] : 0);
  if(!latest) return;
  S._futLoading=true;
  send({action:'more_future', after_ts: latest*1000, count:600});
}
function logRows(){ return (S.frame&&S.frame.recent)||[]; }

// ─────────────────────────── вёрстка ───────────────────────────
function render(){
  const root=$('root');
  root.innerHTML = `
  <div style="height:100vh;display:flex;flex-direction:column;background:#f4f6f3;overflow:hidden;">
    ${topBar()}
    ${replayBar()}
    <div style="flex:1;display:flex;min-height:0;overflow:hidden;">
      ${centerPanel()}
      ${rightPanel()}
    </div>
  </div>`;
  bind();
  requestAnimationFrame(()=>{ initChart(); drawAll(); });
}

function topBar(){
  const sp=spec();
  const dot = S.connected?(S.frame&&S.frame.playing?C.accent:C.amber):C.mut;
  const status = !S.connected?'нет связи':(S.frame&&S.frame.playing?'идёт':'пауза');
  return `
  <div style="flex:0 0 auto;height:46px;display:flex;align-items:center;gap:10px;padding:0 14px;background:#fff;border-bottom:1px solid #e3e6e0;z-index:20;">
    <div style="display:flex;align-items:center;gap:8px;margin-right:2px;">
      <div style="width:22px;height:22px;background:#1F8A5B;border-radius:6px;transform:rotate(45deg);display:flex;align-items:center;justify-content:center;"><div style="width:7px;height:7px;background:#fff;border-radius:1.5px;"></div></div>
      <span style="font-weight:700;font-size:14px;letter-spacing:-.01em;">GRIDLAB</span>
      <span style="font-size:10px;font-weight:600;color:#3B82CE;background:#eaf2fb;border:1px solid #cfe0f2;border-radius:5px;padding:2px 7px;">REPLAY</span>
    </div>
    <span style="width:1px;height:22px;background:#e3e6e0;"></span>
    <div style="display:flex;align-items:center;gap:5px;">
      <span style="font-size:10px;letter-spacing:.06em;color:#939a93;font-weight:600;">ИНСТРУМЕНТ</span>
      <select id="instSel" class="mono" style="font-size:12px;font-weight:600;color:#14181a;background:#fff;border:1px solid #cdd3cb;border-radius:6px;padding:5px 8px;cursor:pointer;">
        ${S.specs.map(s=>`<option value="${s.code}" ${s.code===S.code?'selected':''}>${s.label}${s.approx?' ~':''}</option>`).join('')}
      </select>
    </div>
    <div style="display:flex;align-items:center;gap:5px;">
      <span style="font-size:10px;letter-spacing:.06em;color:#939a93;font-weight:600;">ТФ</span>
      <select id="tfSel" class="mono" style="font-size:12px;font-weight:600;color:#14181a;background:#fff;border:1px solid #cdd3cb;border-radius:6px;padding:5px 8px;cursor:pointer;">
        ${S.tfs.map(t=>`<option value="${t}" ${t===S.tf?'selected':''}>${t}</option>`).join('')}
      </select>
    </div>
    <span class="mono" style="font-size:11px;color:#5b635e;">1 пункт = <b>${sp.point_value} ₽</b>${sp.approx?' <span style="color:#C98A1A;" title="'+(sp.note||'')+'">≈прибл.</span>':''} · мейкер 0 · funding нет</span>
    <span style="width:1px;height:22px;background:#e3e6e0;"></span>
    <div style="display:flex;align-items:center;gap:4px;">
      <span style="font-size:10px;letter-spacing:.04em;color:#939a93;font-weight:600;">СЧЁТ ₽</span>
      <input id="capInp" class="mono" value="${S.capital}" style="width:88px;font-size:11px;font-weight:600;text-align:right;border:1px solid #cdd3cb;border-radius:6px;padding:4px 6px;outline:none;">
    </div>
    <div style="margin-left:auto;display:flex;align-items:center;gap:8px;">
      <a href="/" style="font-size:12px;font-weight:600;color:#3B82CE;text-decoration:none;border:1px solid #cfe0f2;border-radius:6px;padding:5px 10px;">← Терминал Live</a>
      <a href="/lab" style="font-size:12px;font-weight:600;color:#3B82CE;text-decoration:none;border:1px solid #cfe0f2;border-radius:6px;padding:5px 10px;">Лаборатория</a>
      <span data-conn style="display:flex;align-items:center;gap:6px;">
        <span data-conn-dot style="width:7px;height:7px;border-radius:50%;background:${dot};"></span>
        <span data-conn-txt class="mono" style="font-size:11px;color:${dot};">${status}</span>
      </span>
    </div>
  </div>`;
}

function replayBar(){
  const f=S.frame, playing=f&&f.playing;
  const idx=f?f.idx:0, n=f?f.n:1; const frac=n? idx/n : 0;
  const presets=[1,4,16,60];
  const dt=f?new Date(f.bar_time*1000):null;
  const when=dt?dt.toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',year:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
  return `
  <div style="flex:0 0 auto;height:44px;display:flex;align-items:center;gap:12px;padding:0 14px;background:#fbfcfa;border-bottom:1px solid #e3e6e0;">
    <button id="btnPlay" style="width:34px;height:28px;border:1px solid ${playing?'#f0c8c4':'#cfe7d9'};background:${playing?'#fbecea':'#e7f3ec'};color:${playing?'#a93529':'#157a4f'};border-radius:7px;cursor:pointer;font-size:13px;font-weight:700;">${playing?'❚❚':'▶'}</button>
    <button id="btnStep" data-tip="Шаг на один бар (на паузе)" class="hov" style="width:30px;height:28px;border:1px solid #e3e6e0;background:#fff;border-radius:7px;cursor:pointer;font-size:12px;">▷|</button>
    <button id="btnRestart" data-tip="Начать проигрывание заново" class="hov" style="width:30px;height:28px;border:1px solid #e3e6e0;background:#fff;border-radius:7px;cursor:pointer;font-size:12px;">↺</button>
    <div style="flex:1;display:flex;align-items:center;gap:8px;min-width:120px;">
      <input id="seek" type="range" min="0" max="1000" value="${Math.round(frac*1000)}" style="flex:1;height:4px;">
      <span class="mono" style="font-size:10.5px;color:#5b635e;white-space:nowrap;">${idx.toLocaleString('ru-RU')} / ${n.toLocaleString('ru-RU')} бар · ${when}</span>
    </div>
    <span style="width:1px;height:22px;background:#e3e6e0;"></span>
    <div style="display:flex;align-items:center;gap:6px;">
      <span style="font-size:10px;letter-spacing:.04em;color:#939a93;font-weight:600;">СКОРОСТЬ${qq('speed')}</span>
      <input id="speedRng" type="range" min="0.5" max="120" step="0.5" value="${S.speed}" style="width:120px;height:4px;">
      <span class="mono" style="font-size:11px;font-weight:600;color:#14181a;min-width:74px;">${S.speed} бар/с</span>
      <div style="display:flex;gap:2px;background:#eef0ec;border:1px solid #e3e6e0;border-radius:7px;padding:2px;">
        ${presets.map(v=>`<button data-spd="${v}" style="${segBtn(S.speed===v)}">${v}</button>`).join('')}
      </div>
    </div>
  </div>`;
}
function segBtn(on){ const b='padding:4px 9px;font-size:11px;font-weight:600;border:0;cursor:pointer;border-radius:5px;';
  return on?b+'background:#1F8A5B;color:#fff;':b+'background:transparent;color:#5b635e;'; }
function qq(k){ return `<span class="qq" data-tip="${TIPS[k]||''}">?</span>`; }

function card(label,val,color){
  return `<div style="display:flex;flex-direction:column;padding:6px 10px;border:1px solid #e3e6e0;border-radius:8px;min-width:96px;">
    <span style="font-size:9px;letter-spacing:.06em;color:#939a93;font-weight:600;">${label}</span>
    <span class="mono" style="font-size:13.5px;font-weight:600;margin-top:2px;${color?'color:'+color+';':''}">${val}</span></div>`;
}
function cardsHTML(){
  const d=dash(); const roiC=d.roi>=0?C.green:C.red;
  const posC = d.position>0?C.green:(d.position<0?C.red:C.mut2);
  return [
    card('БАЛАНС', rub(d.balance)),
    card('ЭКВИТИ', rub(d.equity)),
    card('ROI',(d.roi>=0?'+':'')+d.roi+'%',roiC),
    card('ПРОСАДКА','−'+d.drawdown+'%',C.red),
    card('РЕАЛИЗ.', rub(d.realized), d.realized>=0?C.green:C.red),
    card('НЕРЕАЛ.', rub(d.unrealized), d.unrealized>=0?C.green:C.red),
    card('ПОЗИЦИЯ', (d.position>0?'+':'')+d.position+' к', posC),
    card('ЛОНГ', d.position>0?d.position+' к':'0 к', d.position>0?C.green:C.mut),
    card('ШОРТ', d.position<0?Math.abs(d.position)+' к':'0 к', d.position<0?C.red:C.mut),
    card('СР.ЦЕНА', d.avg_entry),
    card('СДЕЛОК', String(d.trades)),
    card('ОРДЕРОВ', String(d.open_orders)),
    card('КОМИССИЯ', rub(d.fees), C.mut2),
  ].join('');
}

function centerPanel(){
  const eqc=eqCurve(); const d=dash();
  const eqLast = rub(d.equity);
  return `
  <div style="flex:1;display:flex;flex-direction:column;min-width:0;overflow-y:auto;overflow-x:hidden;">
    <div id="cardsRow" style="display:flex;gap:6px;padding:8px 10px;background:#fff;border-bottom:1px solid #e3e6e0;overflow-x:auto;flex:0 0 auto;">${cardsHTML()}</div>
    <div id="priceWrap" style="flex:1 1 auto;min-height:360px;position:relative;background:#fafbf9;border-bottom:1px solid #e3e6e0;">
      <div style="position:absolute;top:8px;left:12px;z-index:2;pointer-events:none;display:flex;align-items:center;gap:8px;">
        <span class="mono" style="font-size:12px;font-weight:600;">${spec().label}</span>
        <span class="mono" style="font-size:10px;color:#939a93;background:#eef0ec;border-radius:4px;padding:2px 6px;">${S.tf}</span>
        <span style="font-size:10px;color:#3B82CE;font-weight:500;">Сетка · ${S.params.mode==='avellaneda'?'Avellaneda-Stoikov':S.params.mode==='manual'?'ручной':'эвристика'} · 1 контракт/ордер</span>
      </div>
      <div id="lwchart" style="position:absolute;inset:0;"></div>
    </div>
    <div id="diffWrap" style="flex:0 0 auto;height:80px;position:relative;background:#fafbf9;border-bottom:1px solid #e3e6e0;">
      <div style="position:absolute;top:4px;left:12px;z-index:2;pointer-events:none;">
        <span style="font-size:10px;font-weight:600;color:#939a93;">ΔClose (кум.)</span>
      </div>
      <div id="diffChart" style="position:absolute;inset:0;"></div>
    </div>
    <div id="eqWrap" style="flex:0 0 auto;height:150px;position:relative;background:#fafbf9;border-bottom:1px solid #e3e6e0;">
      <div style="position:absolute;top:6px;left:12px;z-index:2;pointer-events:none;display:flex;align-items:center;gap:10px;">
        <span style="font-size:11px;font-weight:600;">Эквити (₽) · потиково</span>
        <span style="display:flex;align-items:center;gap:4px;"><span style="width:14px;height:2px;background:#1F8A5B;border-radius:2px;"></span><span class="mono" style="font-size:11px;font-weight:600;color:#157a4f;">${eqLast}</span></span>
        <span style="display:flex;align-items:center;gap:4px;"><span style="width:14px;height:0;border-top:2px dashed #5b635e;"></span><span class="mono" style="font-size:11px;font-weight:600;color:#5b635e;">${rub(d.balance)} реализ.</span></span>
      </div>
      <canvas id="eqCv" style="display:block;width:100%;height:100%;"></canvas>
    </div>
    <div style="flex:0 0 auto;height:240px;display:flex;flex-direction:column;background:#fff;">
      <div style="display:flex;gap:0;align-items:center;border-bottom:1px solid #e3e6e0;padding:0 10px;flex:0 0 auto;">
        <button data-tab="log" style="${tabStyle(S.tab==='log')}">Журнал событий</button>
        <button data-tab="trades" style="${tabStyle(S.tab==='trades')}">Сделки</button>
        <button id="btnCsv" class="hov" style="margin-left:auto;font-size:11px;font-weight:600;border:1px solid #cfe0f2;background:#eaf2fb;color:#3B82CE;border-radius:6px;padding:4px 10px;cursor:pointer;">CSV</button>
      </div>
      <div id="bottomBody" style="flex:1;overflow:auto;">${bottomTab()}</div>
    </div>
  </div>`;
}
function tabStyle(on){ return on
   ? "padding:7px 14px;font-size:12px;font-weight:600;border:0;cursor:pointer;border-radius:7px 7px 0 0;background:#fff;color:#14181a;border-bottom:2px solid #1F8A5B;"
   : "padding:7px 14px;font-size:12px;font-weight:500;border:0;cursor:pointer;border-radius:7px 7px 0 0;background:transparent;color:#939a93;border-bottom:2px solid transparent;"; }

function bottomTab(){
  const all=logRows();
  const rows = S.tab==='trades' ? all.filter(e=>e.action.indexOf('Grid')>=0||e.action.indexOf('Ликвид')>=0||e.action.indexOf('Stop')>=0) : all;
  const cols='66px 96px 96px 56px 92px 1fr';
  const head=['ВРЕМЯ','ДЕЙСТВИЕ','ЦЕНА','КОНТР.','PnL','КОММЕНТАРИЙ'];
  const hrow=`<div style="display:grid;grid-template-columns:${cols};padding:6px 10px;border-bottom:1px solid #eef0ec;background:#fafbf9;position:sticky;top:0;z-index:1;">${head.map(h=>`<span style="font-size:9px;letter-spacing:.07em;color:#939a93;font-weight:600;">${h}</span>`).join('')}</div>`;
  const body=rows.slice(0,120).map(e=>{
    const t=e.ts?new Date(e.ts).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'}):'—';
    return `<div class="mono" style="display:grid;grid-template-columns:${cols};padding:5px 10px;border-bottom:1px solid #f4f5f2;font-size:11px;align-items:center;">
      <span style="color:#939a93;">${t}</span>
      <span style="font-weight:500;color:${e.actionColor};">${e.action}</span>
      <span>${e.price}</span>
      <span style="color:#5b635e;">${e.size}</span>
      <span style="font-weight:600;color:${e.pnlColor};">${e.pnl}</span>
      <span style="color:#7e857d;font-size:10px;">${e.comment}</span></div>`;
  }).join('');
  return `<div style="min-width:560px;">${hrow}${body||'<div style="padding:14px;color:#939a93;font-size:12px;">Событий пока нет — проигрывание идёт, сделки появятся здесь.</div>'}</div>`;
}

function paramRow(label,key,step,min,max){
  return `<div style="display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid #eef0ec;">
    <span style="font-size:12px;color:#5b635e;display:flex;align-items:center;">${label}${qq(key)}</span>
    <input type="number" data-param="${key}" value="${S.params[key]}" step="${step}"
      class="mono" style="width:62px;font-size:12px;text-align:right;border:1px solid #e3e6e0;border-radius:6px;padding:5px 6px;background:#fff;outline:none;" /></div>`;
}
function rightPanel(){
  const p=S.params; const gi=(S.frame&&S.frame.grid_info)||{};
  const sp=spec();
  const f=v=>v==null?'—':(sp.price_step>=1?Math.round(v).toLocaleString('ru-RU'):v.toFixed(4));
  return `
  <div style="flex:0 0 264px;background:#fbfcfa;border-left:1px solid #e3e6e0;overflow-y:auto;display:flex;flex-direction:column;">
    <div style="padding:12px 14px 8px;">
      <div style="font-size:11px;letter-spacing:.12em;color:#939a93;font-weight:600;">ПАРАМЕТРЫ СТРАТЕГИИ</div>
      <div class="mono" style="font-size:11px;color:#5b635e;margin-top:5px;">${S.code} · ${S.tf} · единый Quoter</div>
    </div>
    <div style="padding:0 14px 10px;">
      <div style="font-size:10px;letter-spacing:.08em;color:#aab0a9;font-weight:600;margin-bottom:6px;display:flex;align-items:center;">РЕЖИМ${qq('mode')}</div>
      <div style="display:flex;gap:2px;background:#eef0ec;border:1px solid #e3e6e0;border-radius:8px;padding:2px;">
        <button data-mode="manual" style="${segBtn(p.mode==='manual')}flex:1;">Ручной</button>
        <button data-mode="heuristic" style="${segBtn(p.mode==='heuristic')}flex:1;">Эвристика</button>
        <button data-mode="avellaneda" style="${segBtn(p.mode==='avellaneda')}flex:1;">A-S</button>
      </div>
    </div>
    ${p.mode!=='manual'?`<div style="padding:0 14px;display:flex;flex-direction:column;">
      ${paramRow('Grid Spacing (ATR ×)','grid_spacing',0.1,0.1,10)}
      ${paramRow('Max ордеров (контр.)','max_orders',1,1,50)}
      ${paramRow('EMA-фильтр','ema',1,5,200)}
      ${p.mode==='avellaneda'?paramRow('γ (риск-аверсия)','as_gamma',0.05,0.05,3)+paramRow('κ (поток)','as_kappa',0.1,0.1,10):''}
      <div style="display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid #eef0ec;">
        <span style="font-size:12px;color:#5b635e;display:flex;align-items:center;">Расширение${qq('expansion')}</span>
        <select id="expSel" class="mono" style="width:108px;font-size:11px;background:#fff;border:1px solid #e3e6e0;border-radius:6px;padding:5px 8px;cursor:pointer;">
          ${['geometric','arithmetic','fibonacci'].map(x=>`<option value="${x}" ${p.expansion===x?'selected':''}>${x[0].toUpperCase()+x.slice(1)}</option>`).join('')}
        </select>
      </div>
    </div>`:''}
    <div style="padding:0 14px;display:flex;justify-content:space-between;align-items:center;padding:9px 14px;border-bottom:1px solid #eef0ec;">
      <span style="font-size:12px;color:#5b635e;display:flex;align-items:center;">Пауза сессии (мин)${qq('session_pause')}</span>
      <input type="number" data-param="session_pause" value="${p.session_pause||0}" step="5"
        class="mono" style="width:62px;font-size:12px;text-align:right;border:1px solid #e3e6e0;border-radius:6px;padding:5px 6px;background:#fff;outline:none;" />
    </div>
    <div style="padding:10px 14px 4px;">
      <button id="btnApply" style="width:100%;background:${S._paramsDirty?'#C98A1A':'#1F8A5B'};color:#fff;border:0;border-radius:8px;padding:9px;font-size:12.5px;font-weight:600;cursor:pointer;">${S._paramsDirty?'Применить (перемотать с этого места)':'Параметры применены'}</button>
      <div style="font-size:10px;color:#aab0a9;margin-top:6px;line-height:1.4;">Применение перематывает прогон с текущего бара под новые параметры (тот же Quoter).</div>
      <button id="btnOptimize" style="width:100%;margin-top:8px;background:#3B82CE;color:#fff;border:0;border-radius:8px;padding:9px;font-size:12.5px;font-weight:600;cursor:pointer;">Подбор параметров</button>
    </div>
    <div style="padding:10px 14px 4px;">
      <div style="background:#fff;border:1px solid #e3e6e0;border-radius:10px;padding:11px;">
        <div style="font-size:10px;letter-spacing:.08em;color:#939a93;font-weight:600;margin-bottom:8px;">РУЧНОЙ ОРДЕР</div>
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;">
          <span style="font-size:11px;color:#5b635e;min-width:44px;">Цена</span>
          <input id="manPrice" class="mono" value="${S.frame&&S.frame.price?fmtPx(S.frame.price):''}" style="flex:1;font-size:11px;text-align:right;border:1px solid #e3e6e0;border-radius:6px;padding:5px 6px;outline:none;">
        </div>
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px;">
          <span style="font-size:11px;color:#5b635e;min-width:44px;">Контр.</span>
          <input id="manQty" class="mono" value="1" style="flex:1;font-size:11px;text-align:right;border:1px solid #e3e6e0;border-radius:6px;padding:5px 6px;outline:none;">
        </div>
        <div style="display:flex;gap:4px;">
          <button id="btnManBuy" style="flex:1;background:#1F8A5B;color:#fff;border:0;border-radius:7px;padding:7px;font-size:11px;font-weight:600;cursor:pointer;">Buy</button>
          <button id="btnManSell" style="flex:1;background:#C4453A;color:#fff;border:0;border-radius:7px;padding:7px;font-size:11px;font-weight:600;cursor:pointer;">Sell</button>
        </div>
      </div>
    </div>
    <div style="margin-top:auto;padding:10px 14px;">
      <div style="background:#fff;border:1px solid #e3e6e0;border-radius:10px;padding:11px;">
        <div style="font-size:10px;letter-spacing:.08em;color:#939a93;font-weight:600;margin-bottom:6px;">ТЕКУЩАЯ СЕТКА</div>
        <div class="mono" style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:4px;"><span style="color:#5b635e;">Шаг</span><span id="giStep">${gi.step!=null?f(gi.step):'—'}</span></div>
        <div class="mono" style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:4px;"><span style="color:#5b635e;">Диапазон</span><span id="giRange">${gi.low!=null?f(gi.low)+'–'+f(gi.high):'—'}</span></div>
        <div class="mono" style="display:flex;justify-content:space-between;font-size:11px;"><span style="color:#5b635e;">Ордеров</span><span id="giCount">${gi.count!=null?gi.count:'—'}</span></div>
      </div>
    </div>
  </div>`;
}

// ─────────────────────────── биндинги ───────────────────────────
function bind(){
  const inst=$('instSel'); if(inst) inst.onchange=()=>{ S.code=inst.value; S.frame=null; resetHist(); _save(); send({action:'set_instrument',code:S.code}); render(); };
  const tf=$('tfSel'); if(tf) tf.onchange=()=>{ S.tf=tf.value; S.frame=null; resetHist(); _save(); send({action:'set_tf',tf:S.tf}); render(); };
  const cap=$('capInp'); if(cap) cap.onchange=()=>{ const v=parseFloat(String(cap.value).replace(/[\s ₽]/g,'')); if(v>0){ S.capital=v; _save(); send({action:'set_capital',capital:v}); } };
  bindBtn('btnPlay',()=>{ const playing=S.frame&&S.frame.playing; if(!playing) resetFut(); send({action:playing?'pause':'play'}); });
  bindBtn('btnStep',()=>send({action:'step'}));
  bindBtn('btnRestart',()=>{ resetHist();
    S._chartReady=false; S._lastData=null; S._needFit=true;
    send({action:'restart'}); toast('Проигрывание сначала'); });
  const seek=$('seek'); if(seek) seek.onchange=()=>{ resetHist(); send({action:'seek',frac:(+seek.value)/1000}); };
  const sr=$('speedRng'); if(sr) sr.oninput=()=>{ S.speed=+sr.value; const lab=sr.parentElement.querySelector('.mono'); if(lab) lab.textContent=S.speed+' бар/с'; };
  if(sr) sr.onchange=()=>{ _save(); send({action:'speed',speed:S.speed}); };
  document.querySelectorAll('[data-spd]').forEach(el=>el.onclick=()=>{ S.speed=+el.dataset.spd;
    // НЕ render() — иначе пересоздаётся график и теряется зум/позиция. Обновляем точечно.
    const sr=$('speedRng'); if(sr){ sr.value=S.speed; const lab=sr.parentElement.querySelector('.mono'); if(lab) lab.textContent=S.speed+' бар/с'; }
    document.querySelectorAll('[data-spd]').forEach(b=>b.style.cssText=segBtn(+b.dataset.spd===S.speed));
    _save(); send({action:'speed',speed:S.speed}); });
  document.querySelectorAll('[data-mode]').forEach(el=>el.onclick=()=>{ S.params.mode=el.dataset.mode; S._paramsDirty=true; _save(); render(); });
  document.querySelectorAll('[data-param]').forEach(el=>el.onchange=()=>{ const k=el.dataset.param; const v=parseFloat(el.value); if(!isFinite(v))return;
    S.params[k]=(k==='levels'||k==='ema'||k==='max_orders')?Math.round(v):v; S._paramsDirty=true; _save();
    const b=$('btnApply'); if(b){ b.textContent='Применить (перемотать с этого места)'; b.style.background='#C98A1A'; } });
  const exp=$('expSel'); if(exp) exp.onchange=()=>{ S.params.expansion=exp.value; S._paramsDirty=true; _save(); };
  document.querySelectorAll('[data-tab]').forEach(el=>el.onclick=()=>{ S.tab=el.dataset.tab; const bb=$('bottomBody'); if(bb) bb.innerHTML=bottomTab(); document.querySelectorAll('[data-tab]').forEach(t=>t.style.cssText=tabStyle(t.dataset.tab===S.tab)); });
  const manPlace=(side)=>{ const px=parseFloat(String(($('manPrice')||{}).value||'0').replace(/[\s ]/g,'')); const qty=Math.max(1,Math.round(parseFloat(String(($('manQty')||{}).value||'1'))||1));
    if(!(px>0)){toast('Задай цену');return;} send({action:'place_order',side,price:px,qty}); toast(`Лимит ${side==='buy'?'Buy':'Sell'} ${qty} контр. @ ${fmtPx(px)}`); };
  bindBtn('btnManBuy',()=>manPlace('buy'));
  bindBtn('btnManSell',()=>manPlace('sell'));
  bindBtn('btnCsv',()=>{ const all=logRows(); if(!all.length){ toast('Нет событий'); return; }
    const rows=S.tab==='trades'? all.filter(e=>e.action.indexOf('Grid')>=0||e.action.indexOf('Ликвид')>=0||e.action.indexOf('Stop')>=0) : all;
    const hdr='Время;Действие;Цена;Контракты;PnL;Комментарий';
    const body=rows.map(e=>{ const t=e.ts?new Date(e.ts).toLocaleString('ru-RU'):'';
      return [t,e.action,e.price,e.size,e.pnl.replace(/[  ]/g,''),e.comment].join(';'); }).join('\n');
    const blob=new Blob(['﻿'+hdr+'\n'+body],{type:'text/csv;charset=utf-8'});
    const a=document.createElement('a'); a.href=URL.createObjectURL(blob);
    a.download=`gridlab_${S.code}_${S.tab}_${new Date().toISOString().slice(0,10)}.csv`; a.click();
    URL.revokeObjectURL(a.href); toast('CSV сохранён'); });
  bindBtn('btnApply',()=>{ send({action:'apply_params',params:S.params}); S._paramsDirty=false; _save();
    const b=$('btnApply'); if(b){ b.textContent='Параметры применены'; b.style.background='#1F8A5B'; } toast('Параметры применены — перемотка'); });
  bindBtn('btnOptimize',()=>openOptimizer());
  bindBtn('btnLimit',()=>{ const last=S.frame?S.frame.price:0; if(!(last>0)){ toast('Цена ещё не загрузилась'); return; } openDraft(last); });
}
function bindBtn(id,fn){ const e=$(id); if(e) e.onclick=fn; }

// ─────────────────────────── частичное обновление кадра ───────────────────────────
function apply(){
  const cr=$('cardsRow'); if(cr) cr.innerHTML=cardsHTML();
  const bb=$('bottomBody'); if(bb) bb.innerHTML=bottomTab();
  // replay-бар (прогресс/скорость/play) — обновляем без пересборки всего
  syncReplayBar();
  syncConn();
  syncGridInfo();
  drawAll();
  hideTipIfStale();
}
function syncReplayBar(){
  const f=S.frame; if(!f) return;
  const pb=$('btnPlay'); if(pb){ const playing=f.playing;
    pb.textContent=playing?'❚❚':'▶';
    pb.style.cssText=`width:34px;height:28px;border:1px solid ${playing?'#f0c8c4':'#cfe7d9'};background:${playing?'#fbecea':'#e7f3ec'};color:${playing?'#a93529':'#157a4f'};border-radius:7px;cursor:pointer;font-size:13px;font-weight:700;`; }
  const seek=$('seek'); if(seek && document.activeElement!==seek){ seek.value=Math.round((f.n?f.idx/f.n:0)*1000); }
  const lab=seek?seek.parentElement.querySelector('.mono'):null;
  if(lab){ const dt=new Date(f.bar_time*1000); const when=dt.toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',year:'2-digit',hour:'2-digit',minute:'2-digit'});
    lab.textContent=`${f.idx.toLocaleString('ru-RU')} / ${f.n.toLocaleString('ru-RU')} бар · ${when}`; }
}
function syncGridInfo(){
  const gi=(S.frame&&S.frame.grid_info)||{}; const sp=spec();
  const f=v=>v==null?'—':(sp.price_step>=1?Math.round(v).toLocaleString('ru-RU'):v.toFixed(4));
  const st=$('giStep'); if(st) st.textContent=gi.step!=null?f(gi.step):'—';
  const rg=$('giRange'); if(rg) rg.textContent=gi.low!=null?f(gi.low)+'–'+f(gi.high):'—';
  const ct=$('giCount'); if(ct) ct.textContent=gi.count!=null?gi.count:'—';
}

// ─────────────────────────── график (Lightweight Charts) ───────────────────────────
function lwReady(){ return typeof window!=='undefined' && window.LightweightCharts; }
function chartTime(sd,i){ if(sd&&sd.times&&sd.times[i]!=null) return sd.times[i]; return 1700000000+i*300; }
function initChart(){
  const host=$('lwchart'); if(!host) return;
  if(!lwReady()){ host.innerHTML='<div style="padding:20px;color:#a93529;font-size:12px;">График не загрузился (нет доступа к CDN lightweight-charts).</div>'; return; }
  if(S._chart){ try{S._chart.remove();}catch(e){} }
  const LC=window.LightweightCharts;
  const ch=LC.createChart(host,{ autoSize:true,
    layout:{background:{type:'solid',color:'#fafbf9'},textColor:'#5b635e',fontFamily:"'JetBrains Mono',monospace",fontSize:10},
    grid:{vertLines:{color:'#eef0ec'},horzLines:{color:'#eef0ec'}},
    rightPriceScale:{borderColor:'#e3e6e0'},
    timeScale:{borderColor:'#e3e6e0',timeVisible:true,secondsVisible:false,rightOffset:4},
    crosshair:{mode:LC.CrosshairMode.Normal},
    handleScroll:true, handleScale:true });
  const cs=ch.addCandlestickSeries({upColor:'#1F8A5B',downColor:'#C4453A',borderUpColor:'#1F8A5B',borderDownColor:'#C4453A',wickUpColor:'#1F8A5B',wickDownColor:'#C4453A',priceLineColor:'#C98A1A',priceLineStyle:2});
  S._chart=ch; S._cs=cs; S._chartReady=false;
  ensureOverlays(host.parentElement);
  setupPriceAxisBtn(host);
  startRepositionLoop();
  ch.timeScale().subscribeVisibleLogicalRangeChange((r)=>{
    repositionAll(); maybeLoadHistory(r); maybeLoadFuture(r);
    if(S._diffChart && r && !S._syncingDiff){
      S._syncingMain=true;
      try{ S._diffChart.timeScale().setVisibleLogicalRange(r); }catch(e){}
      S._syncingMain=false;
    }
  });
  updateChart();
  setTimeout(initDiffChart, 50);
}

function initDiffChart(){
  const host=$('diffChart'); if(!host||!lwReady()) return;
  if(S._diffChart){ try{S._diffChart.remove();}catch(e){} }
  const LC=window.LightweightCharts;
  const dc=LC.createChart(host,{
    autoSize:true,
    layout:{background:{type:'solid',color:'#fafbf9'},textColor:'#5b635e',fontFamily:"'JetBrains Mono',monospace",fontSize:9},
    grid:{vertLines:{color:'#eef0ec'},horzLines:{color:'#f3f4f1'}},
    rightPriceScale:{borderColor:'#e3e6e0'},
    timeScale:{borderColor:'#e3e6e0',visible:false},
    crosshair:{mode:LC.CrosshairMode.Normal,vertLine:{labelVisible:false},horzLine:{labelBackgroundColor:'#939a93'}},
    handleScroll:true, handleScale:true,
  });
  S._diffSeries=dc.addHistogramSeries({priceFormat:{type:'price',precision:2,minMove:0.01},lastValueVisible:false,priceLineVisible:false});
  S._diffChart=dc;
  const sd=selData();
  if(sd) updateDiffChart(sd);
  dc.timeScale().subscribeVisibleLogicalRangeChange((r)=>{
    if(S._chart && r && !S._syncingMain){
      S._syncingDiff=true;
      try{ S._chart.timeScale().setVisibleLogicalRange(r); }catch(e){}
      S._syncingDiff=false;
    }
  });
  if(S._chart){
    try{ const r=S._chart.timeScale().getVisibleLogicalRange(); if(r) dc.timeScale().setVisibleLogicalRange(r); }catch(e){}
  }
}

function diffColor(val, maxAbs){
  const norm=Math.min(Math.abs(val)/(maxAbs||1),1);
  const alpha=0.15+norm*0.85;
  return val>=0 ? `rgba(31,138,91,${alpha.toFixed(2)})` : `rgba(196,69,58,${alpha.toFixed(2)})`;
}

function updateDiffChart(sd){
  if(!S._diffSeries||!sd||!sd.candles||sd.candles.length<1) return;
  const pts=[]; let cum=0;
  for(let i=0;i<sd.candles.length;i++){
    const c=sd.candles[i], body=c[3]-c[0];
    cum+=body;
    pts.push({time:chartTime(sd,i), value:cum});
  }
  const maxAbs=Math.max(...pts.map(d=>Math.abs(d.value)),1e-9);
  const colored=pts.map(d=>({...d, color:diffColor(d.value,maxAbs)}));
  try{ S._diffSeries.setData(colored); }catch(e){}
}

function setupPriceAxisBtn(host){
  const wrap=host.parentElement;
  // кнопка ⊕ следит за crosshair (не за DOM-mousemove, т.к. canvas LW перехватывает)
  let btn=$('pxAxisBtn');
  if(!btn){ btn=document.createElement('div'); btn.id='pxAxisBtn';
    btn.style.cssText='position:absolute;right:68px;width:26px;height:26px;z-index:20;border-radius:50%;background:#fff;border:1.5px solid #cdd3cb;align-items:center;justify-content:center;font-size:15px;color:#5b635e;line-height:1;user-select:none;box-shadow:0 1px 4px rgba(0,0,0,.13);display:none;pointer-events:none;';
    btn.innerHTML='⊕'; wrap.appendChild(btn);
  }
  S._chart.subscribeCrosshairMove((param)=>{
    if(!param||!param.point||param.point.y==null){ btn.style.display='none'; return; }
    const price=S._cs.coordinateToPrice(param.point.y);
    if(!(price>0)){ btn.style.display='none'; return; }
    S._crossPrice=price;
    const wrapR=wrap.getBoundingClientRect(); const hostR=host.getBoundingClientRect();
    btn.style.display='flex'; btn.style.top=(hostR.top+param.point.y-wrapR.top-13)+'px';
  });
  host.addEventListener('mouseleave',()=>{ btn.style.display='none'; });
  host.addEventListener('mousedown',(ev)=>{
    const br=btn.getBoundingClientRect();
    if(btn.style.display==='none') return;
    const x=ev.clientX, y=ev.clientY;
    if(x>=br.left&&x<=br.right&&y>=br.top&&y<=br.bottom){
      ev.stopPropagation(); ev.preventDefault();
      const price=S._crossPrice;
      if(!(price>0)) return;
      const last=(S.frame&&S.frame.price)||0;
      const side=price<=last?'buy':'sell';
      const qty=Math.max(1,Math.round(parseFloat(String(($('manQty')||{}).value||'1'))||1));
      send({action:'place_order',side,price,qty});
      toast(`Лимит ${side==='buy'?'Buy':'Sell'} ${qty} к. @ ${fmtPx(price)}`);
      const pi=$('manPrice'); if(pi) pi.value=fmtPx(price);
    }
  },true);
}
function priceToY(p){ if(!S._cs) return null; const y=S._cs.priceToCoordinate(p); return y==null?null:y; }
function updateChart(){
  if(!S._cs){ initChart(); if(!S._cs) return; }
  const sd=selData(); if(!sd||!sd.candles||!sd.candles.length) return;
  // строго возрастающие уникальные времена (требование lightweight-charts)
  const data=[]; let prevT=-Infinity;
  for(let i=0;i<sd.candles.length;i++){ const t=chartTime(sd,i); if(!(t>prevT)) continue;
    const c=sd.candles[i]; if(!c||c.length<4) continue;
    const o=c[0],h=c[1],l=c[2],cl=c[3];
    if(!isFinite(o)||!isFinite(h)||!isFinite(l)||!isFinite(cl)) continue;
    prevT=t; data.push({time:t,open:o,high:h,low:l,close:cl}); }
  if(!data.length) return;
  const ts=S._chart.timeScale();
  // ── Сохраняем ВИД по ВРЕМЕНИ бара (не по индексу): тогда подгрузка истории слева,
  //    выпадение старых баров спереди и догрузка вперёд НЕ двигают картинку. ──
  let restore=null, follow=false;
  if(S._chartReady && S._lastData && S._lastData.length){
    const r=ts.getVisibleLogicalRange();
    if(r){ const lastIdx=S._lastData.length-1; const width=r.to-r.from;
      // следуем за live ТОЛЬКО во время проигрывания и если стоим у правого края
      follow = !!(S.frame && S.frame.playing) && r.to >= lastIdx-1.5;
      if(!follow){ const fl=Math.floor(r.from); const frac=r.from-fl;
        const a=Math.max(0,Math.min(lastIdx,fl));
        restore={anchor:S._lastData[a].time, frac, width}; }
      else { restore={follow:true, width}; }
    }
  }
  try{ S._cs.setData(data); }catch(e){
    console.warn('setData error, recreating series:', e);
    try{
      S._chart.removeSeries(S._cs);
      const LC=window.LightweightCharts;
      S._cs=S._chart.addCandlestickSeries({upColor:'#1F8A5B',downColor:'#C4453A',borderUpColor:'#1F8A5B',borderDownColor:'#C4453A',wickUpColor:'#1F8A5B',wickDownColor:'#C4453A',priceLineColor:'#C98A1A',priceLineStyle:2});
      S._cs.setData(data);
    }catch(e2){ console.error('series rebuild also failed:', e2); return; }
  }
  S._lastData=data;
  S._marks=(sd.markers||[]).map(t=>({time:t.time,price:t.price,side:t.side})).filter(m=>m.time!=null&&m.price>0);
  try{ S._cs.setMarkers([]); }catch(e){}
  if(!S._chartReady){ S._chartReady=true; try{ts.fitContent(); S._chart.priceScale('right').applyOptions({autoScale:true});}catch(e){} }
  else if(restore){
    try{
      if(restore.follow){ const li=data.length-1; ts.setVisibleLogicalRange({from:li-restore.width, to:li}); }
      else { // двоичный поиск нового индекса бара-якоря по времени
        let lo=0,hi=data.length-1,idx=data.length-1;
        while(lo<=hi){ const m=(lo+hi)>>1; if(data[m].time<restore.anchor){lo=m+1;} else {idx=m;hi=m-1;} }
        const nf=idx+restore.frac; ts.setVisibleLogicalRange({from:nf, to:nf+restore.width});
      }
    }catch(e){}
  }
  renderMarks(); renderLines(); repositionAll();
  updateDiffChart(sd);
}
function ensureOverlays(wrap){
  if(getComputedStyle(wrap).position==='static') wrap.style.position='relative';
  if(!$('obOrders')){ const c=document.createElement('div'); c.id='obOrders'; c.style.cssText='position:absolute;inset:0;pointer-events:none;z-index:7;overflow:hidden;'; wrap.appendChild(c); }
  if(!$('obMarks')){ const c=document.createElement('div'); c.id='obMarks'; c.style.cssText='position:absolute;inset:0;pointer-events:none;z-index:6;overflow:hidden;'; wrap.appendChild(c); }
  if(!$('obDraft')){
    const d=document.createElement('div'); d.id='obDraft';
    d.style.cssText='position:absolute;left:0;right:0;height:0;display:none;z-index:9;border-top:1px dashed #1F8A5B;pointer-events:none;';
    d.innerHTML=`<div style="position:absolute;right:62px;top:-13px;height:26px;display:flex;align-items:center;pointer-events:auto;font-family:'Space Grotesk',sans-serif;">
      <span id="obGrip" title="Тяни по вертикали — задать цену" style="cursor:ns-resize;color:#9aa29a;font-size:14px;padding:0 5px;user-select:none;">⠿</span>
      <input id="obQty" title="Сколько контрактов" value="1" style="width:52px;height:22px;border:1px solid #cdd3cb;border-radius:5px;font-size:11px;text-align:right;padding:0 7px;outline:none;background:#fff;font-family:'JetBrains Mono',monospace;">
      <span style="font-size:10px;color:#5b635e;margin:0 5px 0 3px;">контр.</span>
      <span id="obTag" title="Поставить лимитку" style="cursor:pointer;height:22px;display:flex;align-items:center;padding:0 10px;border-radius:5px;color:#fff;font-size:11px;font-weight:600;white-space:nowrap;box-shadow:0 1px 5px rgba(0,0,0,.14);"></span>
      <span id="obX" title="Отменить" style="cursor:pointer;width:22px;height:22px;margin-left:4px;display:flex;align-items:center;justify-content:center;background:#fff;border:1px solid #cdd3cb;border-radius:5px;color:#5b635e;font-size:11px;">✕</span>
    </div>`;
    wrap.appendChild(d);
    $('obX').onclick=(ev)=>{ev.stopPropagation();ev.preventDefault();hideDraft();};
    $('obTag').onclick=(ev)=>{ev.stopPropagation();ev.preventDefault();submitDraft();};
    $('obGrip').onmousedown=(ev)=>startDraftDrag(ev);
    $('obQty').onmousedown=(ev)=>ev.stopPropagation();
  }
}
// ── черновик ручной лимитки: тянешь линию по вертикали, задаёшь контракты, ставишь ──
function chartRect(){ const h=$('lwchart'); return h?h.getBoundingClientRect():null; }
function yToPrice(y){ if(!S._cs) return null; const p=S._cs.coordinateToPrice(y); return (p==null)?null:p; }
function openDraft(price){ if(!(price>0)) return; ensureOverlays($('priceWrap')); S._draft={price:price}; const q=$('obQty'); if(q) q.value='1'; repositionDraft(); }
function repositionDraft(){
  const d=$('obDraft'); if(!d) return;
  if(!S._draft){ d.style.display='none'; return; }
  const y=priceToY(S._draft.price); if(y==null){ d.style.display='none'; return; }
  const last=(S.frame&&S.frame.price)||S._draft.price;
  const side=S._draft.price<=last?'buy':'sell'; S._draft.side=side;
  const col=side==='buy'?'#1F8A5B':'#C4453A';
  d.style.display='block'; d.style.top=y+'px'; d.style.borderTopColor=col;
  const tag=$('obTag'); if(tag){ tag.style.background=col; tag.textContent=(side==='buy'?'Лимит Buy ':'Лимит Sell ')+fmtPx(S._draft.price); }
}
function startDraftDrag(e){ e.preventDefault(); e.stopPropagation();
  const move=(ev)=>{ if(!S._draft) return; const r=chartRect(); if(!r) return; const pr=yToPrice(ev.clientY-r.top); if(pr>0){ S._draft.price=pr; repositionDraft(); } };
  const up=()=>{ window.removeEventListener('mousemove',move); window.removeEventListener('mouseup',up); };
  window.addEventListener('mousemove',move); window.addEventListener('mouseup',up);
}
function submitDraft(){ if(!S._draft) return; const side=S._draft.side, price=S._draft.price;
  const qty=Math.max(1, Math.round(parseFloat(String(($('obQty')||{}).value||'1'))||1));
  if(!(price>0)){ toast('Задай цену'); return; }
  send({action:'place_order', side, price, qty});
  toast(`Лимит ${side==='buy'?'Buy':'Sell'} ${qty} контр. @ ${fmtPx(price)} поставлен`);
  hideDraft();
}
function hideDraft(){ S._draft=null; const d=$('obDraft'); if(d) d.style.display='none'; }
function cancelManual(oid){ send({action:'cancel_order', oid}); toast('Лимитка снята'); }
function renderMarks(){ const c=$('obMarks'); if(!c) return; const marks=S._marks||[];
  c.innerHTML=marks.map((m,i)=>{ const buy=m.side==='buy'; const col=buy?'#1F8A5B':'#C4453A';
    return `<div data-mi="${i}" style="position:absolute;transform:translate(-50%,-50%);font-size:10px;line-height:1;color:${col};display:none;">${buy?'▲':'▼'}</div>`; }).join(''); }
function renderLines(){ const c=$('obOrders'); if(!c) return;
  if(S._ordDrag) return;
  // не перестраивать DOM если набор ордеров не изменился
  const manual=S.frame&&S.frame.manual||[], grid=S.frame&&S.frame.grid||[], preview=S.frame&&S.frame.preview||[];
  const key=manual.map(o=>o.oid+':'+o.price).join(',')+';'+grid.map(o=>o.side+o.price).join(',')+';'+preview.map(o=>o.side+o.price).join(',');
  if(key===S._linesKey) return;
  S._linesKey=key;
  c.innerHTML='';
  const mk=(price,col,html,dash)=>{ const row=document.createElement('div'); row.dataset.price=price;
    const y=priceToY(price); row.style.cssText=`position:absolute;left:0;right:0;top:${y==null?-9999:y}px;height:0;border-top:1.5px ${dash?'dashed':'solid'} ${col};pointer-events:none;`;
    const inner=document.createElement('div'); inner.style.cssText='position:absolute;right:62px;top:-11px;height:20px;display:flex;align-items:center;pointer-events:auto;'; inner.innerHTML=html;
    row.appendChild(inner); c.appendChild(row); return row; };
  // ручные лимитки — перетаскиваемые, с отменой ✕ (как в Bybit)
  (S.frame&&S.frame.manual||[]).forEach(o=>{ const buy=o.side==='buy'; const col=buy?'#1F8A5B':'#C4453A';
    const row=mk(o.price,col,
      `<span class="g" style="cursor:ns-resize;background:${col};color:#fff;height:22px;display:flex;align-items:center;padding:0 8px;border-radius:5px 0 0 5px;font:600 10px 'JetBrains Mono',monospace;white-space:nowrap;">${buy?'Buy':'Sell'} ${o.size} @ ${fmtPx(o.price)}</span>`+
      `<span class="x" title="Снять лимитку" style="cursor:pointer;background:#fff;border:1px solid ${col};border-left:0;color:${col};height:22px;width:22px;display:flex;align-items:center;justify-content:center;border-radius:0 5px 5px 0;font-size:11px;">✕</span>`, false);
    row.style.borderTopWidth='1.5px';
    row.querySelector('.x').onclick=(ev)=>{ev.stopPropagation();ev.preventDefault();cancelManual(o.oid);};
    const grip=row.querySelector('.g');
    grip.onmousedown=(ev)=>{ ev.preventDefault();ev.stopPropagation(); S._ordDrag=true; let np=o.price;
      const move=(e2)=>{ const r=chartRect(); if(!r) return; const pr=yToPrice(e2.clientY-r.top); if(pr>0){ np=pr; const ny=priceToY(pr); if(ny!=null) row.style.top=ny+'px'; grip.textContent=`${buy?'Buy':'Sell'} ${o.size} @ ${fmtPx(pr)}`; } };
      const up=()=>{ window.removeEventListener('mousemove',move); window.removeEventListener('mouseup',up); S._ordDrag=false;
        if(Math.abs(np-o.price)>1e-9){ send({action:'cancel_order',oid:o.oid}); send({action:'place_order',side:o.side,price:np,qty:o.size}); toast('Лимитка перемещена @ '+fmtPx(np)); } };
      window.addEventListener('mousemove',move); window.addEventListener('mouseup',up);
    };
  });
  (S.frame&&S.frame.grid||[]).forEach(o=>{ const buy=o.side==='buy'; const col=buy?'#1F8A5B':'#C4453A';
    mk(o.price,col,`<span style="background:${col};color:#fff;height:20px;display:flex;align-items:center;padding:0 8px;border-radius:5px;font:600 9px 'JetBrains Mono',monospace;opacity:.92;white-space:nowrap;">${buy?'BUY':'SELL'} ${fmtPx(o.price)}</span>`,false); });
  (S.frame&&S.frame.preview||[]).forEach(o=>{ const buy=o.side==='buy'; const col=buy?'#1F8A5B':'#C4453A';
    mk(o.price,col,`<span style="border:1px dashed ${col};color:${col};background:#fff;height:20px;display:flex;align-items:center;padding:0 8px;border-radius:5px;font:600 9px 'JetBrains Mono',monospace;opacity:.85;white-space:nowrap;">${buy?'превью':'превью'} ${fmtPx(o.price)}</span>`,true); });
}
function repositionAll(){
  const o=$('obOrders'); if(o) for(const row of o.children){ const y=priceToY(parseFloat(row.dataset.price)); if(y==null) row.style.display='none'; else { row.style.display='block'; row.style.top=y+'px'; } }
  const m=$('obMarks'); if(m&&S._chart){ const ts=S._chart.timeScale(); const marks=S._marks||[];
    for(const el of m.children){ const mk=marks[+el.dataset.mi]; if(!mk){ el.style.display='none'; continue; }
      const x=ts.timeToCoordinate(mk.time); const y=priceToY(mk.price);
      if(x==null||y==null) el.style.display='none'; else { el.style.display='block'; el.style.left=x+'px'; el.style.top=y+'px'; } } }
  repositionDraft();
}
function startRepositionLoop(){ if(S._reLoop) return; const tick=()=>{ S._reLoop=requestAnimationFrame(tick); if(S._chart) repositionAll(); }; S._reLoop=requestAnimationFrame(tick); }

// ─────────────────────────── эквити-канвас (₽) ───────────────────────────
function drawAll(){ updateChart(); drawEq(); }
function drawEq(){
  const cv=$('eqCv'), wrap=$('eqWrap'); if(!cv||!wrap) return;
  const dpr=window.devicePixelRatio||1; const W=wrap.clientWidth,H=wrap.clientHeight; if(!W||!H) return;
  cv.width=W*dpr; cv.height=H*dpr; const ctx=cv.getContext('2d'); ctx.scale(dpr,dpr);
  ctx.fillStyle='#fafbf9'; ctx.fillRect(0,0,W,H);
  const cap=S.capital; let eq=eqCurve();
  const ml=6,mr=72,mt=24,mb=4; const cw=W-ml-mr;
  if(!eq||eq.length<2){ const y=mt+(H-mt-mb)*0.4;
    ctx.strokeStyle='#cdd6cf'; ctx.setLineDash([4,3]); ctx.lineWidth=1.5; ctx.beginPath(); ctx.moveTo(ml,y); ctx.lineTo(W-mr,y); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle='#b3b8af'; ctx.font='10px "JetBrains Mono",monospace'; ctx.textAlign='right'; ctx.fillText(rub(cap),W-4,y+3);
    ctx.fillStyle='#aab0a9'; ctx.font='10px "Space Grotesk",sans-serif'; ctx.textAlign='left'; ctx.fillText('Запусти проигрывание — эквити пойдёт отсюда',ml+6,y-7); return; }
  const bal=balCurve();
  const allv=bal&&bal.length>1?eq.concat(bal):eq;
  const eMin=Math.min(...allv), eMax=Math.max(...allv), eR=(eMax-eMin)||1;
  const toY=v=>mt+(H-mt-mb)-((v-eMin)/eR)*(H-mt-mb); const toX=i=>ml+(i/(eq.length-1))*cw;
  ctx.strokeStyle='#eaece7'; ctx.lineWidth=0.7; ctx.font='9px "JetBrains Mono",monospace'; ctx.fillStyle='#b3b8af'; ctx.textAlign='right';
  for(let i=0;i<=3;i++){ const v=eMin+i*(eR/3); const y=toY(v); ctx.beginPath(); ctx.moveTo(ml,y); ctx.lineTo(W-mr+4,y); ctx.stroke(); ctx.fillText(Math.round(v).toLocaleString('ru-RU'),W-4,y+3); }
  const grad=ctx.createLinearGradient(0,mt,0,H-mb); grad.addColorStop(0,'rgba(31,138,91,0.22)'); grad.addColorStop(1,'rgba(31,138,91,0.02)');
  ctx.beginPath(); ctx.moveTo(toX(0),toY(eq[0])); eq.forEach((v,i)=>ctx.lineTo(toX(i),toY(v))); ctx.lineTo(toX(eq.length-1),H-mb); ctx.lineTo(toX(0),H-mb); ctx.closePath(); ctx.fillStyle=grad; ctx.fill();
  ctx.beginPath(); ctx.moveTo(toX(0),toY(eq[0])); eq.forEach((v,i)=>ctx.lineTo(toX(i),toY(v))); ctx.strokeStyle='#1F8A5B'; ctx.lineWidth=2; ctx.stroke();
  // baseline капитала
  if(cap>=eMin&&cap<=eMax){ const y=toY(cap); ctx.save(); ctx.setLineDash([2,3]); ctx.strokeStyle='#b9c0b6'; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(ml,y); ctx.lineTo(W-mr,y); ctx.stroke(); ctx.restore(); }
  if(bal&&bal.length>1){ const bx=i=>ml+(i/(bal.length-1))*cw; ctx.save(); ctx.setLineDash([4,3]); ctx.strokeStyle='#7e857d'; ctx.lineWidth=1.3; ctx.beginPath(); bal.forEach((v,i)=>{ const x=bx(i),y=toY(v); if(i===0)ctx.moveTo(x,y); else ctx.lineTo(x,y); }); ctx.stroke(); ctx.restore(); }
  const last=eq[eq.length-1]; ctx.fillStyle='#1F8A5B'; ctx.beginPath(); ctx.arc(toX(eq.length-1),toY(last),3.5,0,Math.PI*2); ctx.fill();
}

// ─────────────────────────── тултипы ───────────────────────────
function setupTooltips(){
  const show=(t)=>{ const tip=t.getAttribute('data-tip'); if(!tip)return; const el=$('tipEl'); el.textContent=tip;
    const r=t.getBoundingClientRect(); el.style.display='block'; const w=el.offsetWidth||220;
    let left=r.left-w-10; if(left<8) left=r.right+10; if(left+w>window.innerWidth-8) left=window.innerWidth-w-8;
    let top=r.top-4; if(top+el.offsetHeight>window.innerHeight-8) top=window.innerHeight-el.offsetHeight-8;
    el.style.left=Math.max(8,left)+'px'; el.style.top=Math.max(8,top)+'px'; };
  document.addEventListener('mouseover',e=>{const t=e.target.closest('[data-tip]'); if(t){ S._tipT=t; clearTimeout(S._tipTimer); S._tipTimer=setTimeout(()=>{ if(S._tipT===t) show(t); },400); }});
  document.addEventListener('mouseout',e=>{const t=e.target.closest('[data-tip]'); if(t){ S._tipT=null; clearTimeout(S._tipTimer); $('tipEl').style.display='none'; }});
}
function hideTipIfStale(){ const te=$('tipEl'); if(!te) return; if(!S._tipT||!document.body.contains(S._tipT)){ te.style.display='none'; S._tipT=null; } }

// ═════════════════════════ ОПТИМИЗАТОР ═════════════════════════
const OPT_TIPS={
  grid_spacing:'Шаг сетки в единицах ATR. Больше = шире расстояние между ордерами, меньше сделок но безопаснее. Меньше = плотнее сетка, больше сделок но выше риск.',
  max_orders:'Потолок позиции: сколько ордеров может исполниться в одну сторону. Набрано 6 buy → новые buy-лимитки не ставятся. Защита от чрезмерного накопления позиции.',
  sl:'Stop-Loss в единицах ATR от средней входа. 0 = выключен. Защита от сильного движения против позиции.',
  ema:'Период EMA-фильтра тренда. В нисходящем тренде покупки осторожнее, продажи агрессивнее. Короткий = чувствительнее к тренду.',
  as_gamma:'Риск-аверсия Avellaneda-Stoikov. Больше = сильнее смещение центра сетки против инвентаря, осторожнее набор позиции.',
  as_kappa:'Интенсивность потока ордеров. Влияет на базовый спред через ln(1+gamma/kappa). Меньше = шире спред.',
  expansion:'Как увеличивается шаг от уровня к уровню. Geometric = экспоненциально (1.6x), Arithmetic = линейно.',
  mode:'Полный перебор: все комбинации. Вокруг текущих: узкий диапазон рядом с текущими параметрами. Случайный: N случайных комбинаций из пространства.',
  preset:'Robust = ROI - 0.5*DD + Sharpe*10 (баланс прибыли/риска). Max profit = только ROI. Min DD = только просадка. Profit/DD = отношение.',
  heatmap:'Тепловая карта: цвет ячейки показывает score для каждой пары значений двух параметров. Яркий = хорошо.',
  random_n:'Сколько случайных комбинаций проверить (только в режиме «Случайный»). Чем больше — тем дольше, но выше шанс найти хорошую точку.',
  bars:'Сколько последних свечей из архива взять для бэктеста. 4000 свечей × 5m ≈ 14 торговых дней. Больше = надёжнее, но медленнее.',
  heatmap_axis:'Выберите два параметра для тепловой карты. Карта покажет score для каждой пары значений этих параметров.',
};
function optTip(key){
  const tip=OPT_TIPS[key]; if(!tip) return '';
  return ' <span class="opt-tip" style="display:inline-block;width:16px;height:16px;line-height:16px;text-align:center;border-radius:50%;background:#eef0ec;color:#7e857d;font-size:10px;font-weight:700;cursor:help;vertical-align:middle;position:relative;" data-otip="'+tip.replace(/"/g,'&quot;')+'">?</span>';
}

function openOptimizer(){
  try{
  let overlay=document.getElementById('optOverlay');
  if(overlay){ overlay.style.display='flex'; return; }
  overlay=document.createElement('div'); overlay.id='optOverlay';
  overlay.style.cssText='position:fixed;inset:0;z-index:9000;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;';
  const hdrS='font-size:13px;font-weight:600;color:#5b635e;margin-bottom:4px;';
  const lblS='font-size:13px;color:#5b635e;display:block;margin-bottom:3px;';
  const inpS='font-size:13px;border:1px solid #d4d8d1;border-radius:7px;padding:7px 9px;background:#fff;outline:none;';
  const selS='font-size:13px;border:1px solid #d4d8d1;border-radius:7px;padding:7px 9px;background:#fff;width:100%;';
  const secS='background:#fafbf9;border:1px solid #e3e6e0;border-radius:12px;padding:16px 18px;margin-bottom:14px;';

  overlay.innerHTML='<div id="optModal" style="background:#fff;border-radius:16px;width:980px;max-width:96vw;max-height:94vh;overflow-y:auto;box-shadow:0 16px 48px rgba(0,0,0,0.3);font-family:Space Grotesk,sans-serif;">'+
    '<div style="display:flex;justify-content:space-between;align-items:center;padding:18px 24px;border-bottom:1px solid #e3e6e0;">'+
      '<div><span style="font-weight:700;font-size:20px;color:#14181a;">Подбор параметров</span>'+
      '<div style="font-size:12px;color:#939a93;margin-top:2px;">Систематический перебор комбинаций на архиве котировок</div></div>'+
      '<button id="optClose" style="background:none;border:0;font-size:26px;cursor:pointer;color:#939a93;padding:0 4px;">&times;</button>'+
    '</div>'+
    '<div style="padding:20px 24px;">'+
      '<div style="'+secS+'">'+
        '<div style="font-size:11px;letter-spacing:.1em;color:#939a93;font-weight:700;margin-bottom:12px;">ДИАПАЗОНЫ ПАРАМЕТРОВ</div>'+
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px 20px;">'+
          optRange('Grid Spacing (ATR ×)','o_gs',1,10,0.5,'grid_spacing',lblS,inpS)+
          optRange('Max ордеров','o_max',2,10,2,'max_orders',lblS,inpS)+
          optRange('SL (ATR×)','o_sl',2,6,1,'sl',lblS,inpS)+
          optList('EMA','o_ema','10,21,50,100,200','ema',lblS,inpS)+
          optList('γ Gamma','o_gamma','0.3,1,2,3,5,10','as_gamma',lblS,inpS)+
          optList('κ Kappa','o_kappa','1,2,3,5,10,20','as_kappa',lblS,inpS)+
          optList('Expansion','o_exp','geometric,arithmetic','expansion',lblS,inpS)+
        '</div>'+
      '</div>'+
      '<div style="'+secS+'">'+
        '<div style="font-size:11px;letter-spacing:.1em;color:#939a93;font-weight:700;margin-bottom:12px;">НАСТРОЙКИ ОПТИМИЗАЦИИ</div>'+
        '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;">'+
          '<div><label style="'+lblS+'">Режим поиска'+optTip('mode')+'</label><select id="o_mode" style="'+selS+'">'+
            '<option value="full">Полный перебор</option><option value="around">Вокруг текущих</option><option value="random">Случайный</option>'+
          '</select></div>'+
          '<div><label style="'+lblS+'">Цель'+optTip('preset')+'</label><select id="o_preset" style="'+selS+'">'+
            '<option value="robust">Robust (ROI−DD+Sharpe)</option><option value="max_profit">Макс. прибыль</option>'+
            '<option value="min_dd">Мин. просадка</option><option value="profit_dd">Profit/DD</option><option value="max_sharpe">Макс. Sharpe</option>'+
          '</select></div>'+
          '<div><label style="'+lblS+'">Random N'+optTip('random_n')+'</label><input id="o_nrand" type="number" value="500" style="'+inpS+'width:100%;"/></div>'+
        '</div>'+
        '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-top:12px;">'+
          '<div><label style="'+lblS+'">Heatmap X'+optTip('heatmap')+'</label><select id="o_hx" style="'+selS+'">'+
            ['grid_spacing','sl','max_orders','ema','as_gamma','as_kappa'].map(function(k){return '<option'+(k==='grid_spacing'?' selected':'')+'>'+k+'</option>';}).join('')+
          '</select></div>'+
          '<div><label style="'+lblS+'">Heatmap Y'+optTip('heatmap_axis')+'</label><select id="o_hy" style="'+selS+'">'+
            ['grid_spacing','sl','max_orders','ema','as_gamma','as_kappa'].map(function(k){return '<option'+(k==='as_gamma'?' selected':'')+'>'+k+'</option>';}).join('')+
          '</select></div>'+
          '<div><label style="'+lblS+'">Свечей'+optTip('bars')+'</label><input id="o_bars" type="number" value="4000" style="'+inpS+'width:100%;"/></div>'+
        '</div>'+
      '</div>'+
      '<div style="display:flex;gap:12px;align-items:center;">'+
        '<button id="o_run" style="background:#3B82CE;color:#fff;border:0;border-radius:10px;padding:12px 32px;font-size:15px;font-weight:700;cursor:pointer;letter-spacing:.02em;">Запустить оптимизацию</button>'+
        '<span id="o_combo_info" style="font-size:13px;color:#939a93;"></span>'+
      '</div>'+
    '</div>'+
    '<div id="o_progress" style="display:none;padding:0 24px 12px;">'+
      '<div style="background:#e3e6e0;border-radius:8px;height:24px;overflow:hidden;">'+
        '<div id="o_bar" style="height:100%;background:linear-gradient(90deg,#3B82CE,#1F8A5B);width:0%;transition:width 0.2s;border-radius:8px;"></div>'+
      '</div>'+
      '<div id="o_status" style="font-family:Space Mono,monospace;font-size:13px;color:#5b635e;margin-top:6px;">0 / 0</div>'+
    '</div>'+
    '<div id="o_results" style="padding:0 24px 24px;"></div>'+
  '</div>';

  document.body.appendChild(overlay);

  // тултипы для [?]
  overlay.addEventListener('mouseover',function(e){
    var t=e.target.closest('[data-otip]'); if(!t) return;
    var existing=document.getElementById('optTipBox'); if(existing) existing.remove();
    var box=document.createElement('div'); box.id='optTipBox';
    box.style.cssText='position:fixed;z-index:9999;background:#f9faf8;color:#3a3f3a;border:1px solid #d4d8d1;border-radius:10px;padding:12px 14px;font-size:13px;line-height:1.5;max-width:340px;box-shadow:0 4px 16px rgba(0,0,0,0.12);pointer-events:none;';
    box.textContent=t.getAttribute('data-otip');
    document.body.appendChild(box);
    var r=t.getBoundingClientRect();
    box.style.left=Math.min(r.left,window.innerWidth-360)+'px';
    box.style.top=(r.bottom+6)+'px';
  });
  overlay.addEventListener('mouseout',function(e){
    var t=e.target.closest('[data-otip]'); if(!t) return;
    var box=document.getElementById('optTipBox'); if(box) box.remove();
  });

  document.getElementById('optClose').onclick=function(){ overlay.style.display='none'; };
  overlay.addEventListener('mousedown',function(e){ if(e.target===overlay) overlay.style.display='none'; });
  updateOptComboCount();
  document.querySelectorAll('#optModal input, #optModal select').forEach(function(el){el.addEventListener('change',updateOptComboCount);});
  document.getElementById('o_run').onclick=function(){runOpt();};
  }catch(err){ alert('openOptimizer error: '+err.message); console.error(err); }
}

function optRange(label,id,from,to,step,tipKey,lblS,inpS){
  return '<div><label style="'+lblS+'">'+label+optTip(tipKey)+'</label>'+
    '<div style="display:flex;gap:5px;">'+
    '<input id="'+id+'_f" type="number" value="'+from+'" style="'+inpS+'width:60px;" placeholder="от"/>'+
    '<input id="'+id+'_t" type="number" value="'+to+'" style="'+inpS+'width:60px;" placeholder="до"/>'+
    '<input id="'+id+'_s" type="number" value="'+step+'" style="'+inpS+'width:55px;" placeholder="шаг"/>'+
    '</div></div>';
}
function optList(label,id,def,tipKey,lblS,inpS){
  return '<div><label style="'+lblS+'">'+label+optTip(tipKey)+' <span style="color:#b0b6ae;font-size:11px;">(через запятую)</span></label>'+
    '<input id="'+id+'" value="'+def+'" style="'+inpS+'width:100%;"/></div>';
}
function readOptRange(id){
  const f=document.getElementById(id+'_f'), t=document.getElementById(id+'_t'), s=document.getElementById(id+'_s');
  if(!f) return [];
  const from=parseFloat(f.value), to=parseFloat(t.value), step=parseFloat(s.value);
  if(!isFinite(from)||!isFinite(to)||!isFinite(step)||step<=0) return [from].filter(isFinite);
  const arr=[]; for(let v=from;v<=to+1e-9;v+=step) arr.push(Math.round(v*1e6)/1e6);
  return arr;
}
function readOptList(id){
  const el=document.getElementById(id); if(!el) return [];
  return el.value.split(',').map(x=>x.trim()).filter(Boolean).map(x=>{const n=parseFloat(x); return isFinite(n)?n:x;});
}
function buildOptRanges(){
  const r={};
  const gs=readOptRange('o_gs'); if(gs.length) r.grid_spacing=gs;
  const mx=readOptRange('o_max'); if(mx.length) r.max_orders=mx;
  const sl=readOptRange('o_sl'); if(sl.length) r.sl=sl;
  const ema=readOptList('o_ema'); if(ema.length) r.ema=ema;
  const gam=readOptList('o_gamma'); if(gam.length) r.as_gamma=gam;
  const kap=readOptList('o_kappa'); if(kap.length) r.as_kappa=kap;
  const exp=readOptList('o_exp'); if(exp.length) r.expansion=exp;
  return r;
}
function updateOptComboCount(){
  const ranges=buildOptRanges(); let n=1;
  Object.values(ranges).forEach(v=>{n*=v.length;});
  const mode=(document.getElementById('o_mode')||{}).value||'full';
  if(mode==='random') n=Math.min(n,+(document.getElementById('o_nrand')||{}).value||500);
  const el=document.getElementById('o_combo_info');
  if(el) el.textContent=`≈ ${n.toLocaleString('ru-RU')} комбинаций`;
}

let _optWs=null, _optLastTop=null;
function _optBtnReset(){
  const b=document.getElementById('o_run');
  if(b){ b.textContent='Запустить оптимизацию'; b.style.background='#3B82CE'; }
  _optWs=null;
}
function runOpt(){
  if(_optWs){
    const ws=_optWs;
    _optWs=null;
    ws.close();
    document.getElementById('o_status').textContent='Остановлено';
    _optBtnReset();
    if(_optLastTop && _optLastTop.length){
      renderOptResults({top:_optLastTop, heatmap:null, total:_optLastTop.length, preset:'robust'});
    }
    return;
  }
  _optLastTop=null;
  const ranges=buildOptRanges();
  const mode=document.getElementById('o_mode').value;
  const preset=document.getElementById('o_preset').value;
  const hx=document.getElementById('o_hx').value;
  const hy=document.getElementById('o_hy').value;
  const bars=+(document.getElementById('o_bars').value)||4000;
  const nrand=+(document.getElementById('o_nrand').value)||500;

  document.getElementById('o_progress').style.display='';
  document.getElementById('o_bar').style.width='0%';
  document.getElementById('o_status').textContent='Подключение…';
  document.getElementById('o_results').innerHTML='';
  const runBtn=document.getElementById('o_run');
  runBtn.textContent='Остановить';
  runBtn.style.background='#C4453A';

  const proto=location.protocol==='https:'?'wss:':'ws:';
  const ws=new WebSocket(`${proto}//${location.host}/ws/optimizer`);
  _optWs=ws;

  ws.onopen=()=>{
    ws.send(JSON.stringify({
      code:S.code, tf:S.tf||'5m', bars, capital:S.capital||100000,
      base:S.params, ranges, search_mode:mode, preset, n_random:nrand,
      current:mode==='around'?S.params:null,
      heatmap_x:hx, heatmap_y:hy,
    }));
  };

  let total=0, received=0;
  ws.onmessage=(ev)=>{
    const msg=JSON.parse(ev.data);
    if(msg.type==='start'){
      total=msg.total;
      document.getElementById('o_status').textContent=`0 / ${total} · ${msg.workers} ядер`;
    } else if(msg.type==='result'){
      received=msg.i;
      const pct=Math.round(received/total*100);
      document.getElementById('o_bar').style.width=pct+'%';
      document.getElementById('o_status').textContent=`${received} / ${total} (${pct}%)`;
      if(msg.top) _optLastTop=msg.top;
    } else if(msg.type==='done'){
      document.getElementById('o_bar').style.width='100%';
      document.getElementById('o_status').textContent=`Готово · ${msg.total} комбинаций`;
      _optBtnReset();
      renderOptResults(msg);
    } else if(msg.type==='error'){
      document.getElementById('o_status').textContent=`Ошибка: ${msg.msg}`;
      _optBtnReset();
    }
  };
  ws.onerror=()=>{ document.getElementById('o_status').textContent='Ошибка WebSocket'; _optBtnReset(); };
  ws.onclose=()=>{ _optBtnReset(); };
}

function renderOptResults(msg){
  const top=msg.top||[];
  const hm=msg.heatmap;
  const f2=v=>(v==null?'—':(+v).toFixed(2));
  const f3=v=>(v==null?'—':(+v).toFixed(3));
  const cls=v=>v>0?'color:#1F8A5B':(v<0?'color:#a93529':'');

  let html=`<div style="font-size:10px;letter-spacing:.08em;color:#939a93;font-weight:600;margin-bottom:8px;">ТОП-20 ПО ${(msg.preset||'robust').toUpperCase()}</div>
  <div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:11px;">
  <thead><tr style="border-bottom:2px solid #e3e6e0;">
    <th style="text-align:left;padding:5px 6px;font-size:10px;color:#939a93;">#</th>
    <th style="text-align:right;padding:5px 6px;font-size:10px;color:#939a93;">Score</th>
    <th style="text-align:right;padding:5px 6px;font-size:10px;color:#939a93;">ROI%</th>
    <th style="text-align:right;padding:5px 6px;font-size:10px;color:#939a93;">DD%</th>
    <th style="text-align:right;padding:5px 6px;font-size:10px;color:#939a93;">Sharpe</th>
    <th style="text-align:right;padding:5px 6px;font-size:10px;color:#939a93;">Sortino</th>
    <th style="text-align:right;padding:5px 6px;font-size:10px;color:#939a93;">PF</th>
    <th style="text-align:right;padding:5px 6px;font-size:10px;color:#939a93;">Trades</th>
    <th style="text-align:right;padding:5px 6px;font-size:10px;color:#939a93;">Avg$</th>
    <th style="text-align:right;padding:5px 6px;font-size:10px;color:#939a93;">Robust</th>
    <th style="text-align:left;padding:5px 6px;font-size:10px;color:#939a93;">Параметры</th>
    <th></th>
  </tr></thead><tbody>`;
  top.forEach((r,i)=>{
    const m=r.metrics||{}, rb=r.robustness||{};
    html+=`<tr style="border-bottom:1px solid #f1f2ef;">
      <td style="padding:4px 6px;">${i+1}</td>
      <td style="text-align:right;padding:4px 6px;font-weight:600;">${f2(r.score)}</td>
      <td style="text-align:right;padding:4px 6px;${cls(m.roi)}">${f3(m.roi)}</td>
      <td style="text-align:right;padding:4px 6px;">${f3(m.max_dd)}</td>
      <td style="text-align:right;padding:4px 6px;">${f3(m.sharpe)}</td>
      <td style="text-align:right;padding:4px 6px;">${f3(m.sortino)}</td>
      <td style="text-align:right;padding:4px 6px;">${f3(m.profit_factor)}</td>
      <td style="text-align:right;padding:4px 6px;">${m.trades}</td>
      <td style="text-align:right;padding:4px 6px;">${f2(m.avg_trade)}</td>
      <td style="text-align:right;padding:4px 6px;${(rb.neighbor_share||0)>0.6?'color:#1F8A5B':'color:#a93529'}">${rb.neighbor_profitable||0}/${rb.neighbor_total||0} (${((rb.neighbor_share||0)*100).toFixed(0)}%)</td>
      <td style="padding:4px 6px;font-family:'Space Mono',monospace;font-size:10px;">${Object.entries(r.combo||{}).map(([k,v])=>k+'='+v).join(' ')}</td>
      <td style="padding:4px 6px;"><button onclick="optApply(${i})" style="background:#1F8A5B;color:#fff;border:0;border-radius:5px;padding:4px 10px;font-size:10px;font-weight:600;cursor:pointer;">Применить</button></td>
    </tr>`;
  });
  html+=`</tbody></table></div>`;

  // Heatmap
  if(hm && hm.grid && hm.grid.length){
    const flat=[]; hm.grid.forEach(row=>row.forEach(v=>{if(v>-900)flat.push(v);}));
    const mn=Math.min(...flat), mx=Math.max(...flat);
    const color=v=>{ if(v<=-900) return '#f5f5f5'; if(mx===mn) return '#eef0ec';
      const t=(v-mn)/(mx-mn);
      const r=t<0.5?210:Math.round(210-(t-0.5)*2*180);
      const g=t<0.5?Math.round(60+t*2*150):200;
      return `rgb(${r},${g},70)`; };
    let best={i:0,j:0,val:-1e18};
    hm.grid.forEach((row,j)=>row.forEach((v,i)=>{if(v>best.val) best={i,j,val:v};}));

    html+=`<div style="margin-top:14px;font-size:10px;letter-spacing:.08em;color:#939a93;font-weight:600;margin-bottom:8px;">HEATMAP · ${hm.xkey} × ${hm.ykey}</div>`;
    html+=`<div style="overflow-x:auto;"><table style="border-collapse:separate;border-spacing:2px;font-size:11px;">
      <thead><tr><th></th>${hm.xvals.map(x=>`<th style="text-align:center;font-size:10px;color:#939a93;">${x}</th>`).join('')}</tr></thead><tbody>`;
    hm.grid.forEach((row,j)=>{
      html+=`<tr><th style="font-size:10px;color:#939a93;text-align:right;padding-right:6px;">${hm.yvals[j]}</th>`;
      row.forEach((v,i)=>{
        const isBest=(best.i===i&&best.j===j);
        const roi=hm.roi?hm.roi[j][i]:null;
        html+=`<td style="text-align:center;color:#14181a;background:${color(v)};border-radius:4px;padding:6px 5px;font-size:10px;${isBest?'outline:2px solid #14181a;font-weight:700;':''}" title="${hm.xkey}=${hm.xvals[i]} ${hm.ykey}=${hm.yvals[j]} score=${f2(v)}${roi!=null?' roi='+f3(roi)+'%':''}">${v>-900?f2(v):'—'}</td>`;
      });
      html+=`</tr>`;
    });
    html+=`</tbody></table></div>`;
  }

  // сохраним для применения
  S._optTop=top;
  document.getElementById('o_results').innerHTML=html;
}

// глобальная функция для onclick из таблицы
window.optApply=function(idx){
  const r=S._optTop&&S._optTop[idx]; if(!r) return;
  Object.entries(r.combo).forEach(([k,v])=>{ S.params[k]=v; });
  S._paramsDirty=true; _save(); render();
  const overlay=document.getElementById('optOverlay');
  if(overlay) overlay.style.display='none';
  toast('Параметры из оптимизатора применены — нажмите "Применить" для перемотки');
};

boot();
})();
