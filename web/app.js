/* GRIDLAB — Adaptive Grid Research Terminal (vanilla SPA по дизайну GRIDLAB).
   Реальные свечи Bybit + бумажный движок. Canvas-графики, перетаскиваемые grid-уровни,
   realtime Live-режим, светлые тултипы. Строго светлая тема. */
(() => {
'use strict';

const S = {
  cfg: null, market: null, result: null,
  selInst: 'BTCUSDT', tab: 'log', tf: '15',
  paper: true, speed: 5, run: 'stopped',
  params: null,
  loading: false, streaming: false, streamMode: null, tradingStarted: false,
  liveCurve: null, liveCandles: null, liveMarkers: null, ws: null,
  live: null,                  // последний realtime-кадр от сервера
  manualGrid: null,            // ручные цены уровней (перетаскивание) или null
  leftCollapsed: false, bottomCollapsed: false, bottomH: 200, paramsOpen: false,
  zoomBars: null, yZoom: 1,     // зум графика: число видимых свечей и масштаб по высоте
  _tx: null, _gridHit: [], _drag: null,
};

const C = {
  accent:'#1F8A5B', green:'#157a4f', red:'#a93529', liq:'#C4453A',
  blue:'#3B82CE', amber:'#C98A1A', mut:'#939a93', mut2:'#5b635e', line:'#e3e6e0',
};

// состояние блока стакана (easing книги + лента) — переживает перерисовки render()
S.book = { asks:new Map(), bids:new Map(), my:{aheadDisp:0}, imbDisp:0.5,
           seen:new Set(), refs:null, _running:false, _t:0, _tapeInit:false, lastUp:true };

// подсказки к параметрам (shadcn-like тултипы)
const TIPS = {
  mode:'Грид (по умолчанию): уровни стоят НЕПОДВИЖНО, купленный на уровне лот продаётся лимиткой на уровень выше — прибыль снимается с колебания цены. Эвристика и Avellaneda-Stoikov — маркет-мейкинг: котировки перецентрируются на текущую цену после каждого исполнения, что в тренде превращается в погоню за ценой.',
  grid_step_mode:'Чем задан шаг лестницы. «В % цены» (по умолчанию) и «в деньгах» НЕ зависят от таймфрейма графика: 1% — это 1% и на минутках, и на часах, поэтому переключение масштаба больше не перестраивает сетку. «От ATR» — прежнее поведение: шаг подстраивается под волатильность выбранного таймфрейма. Шаг в деньгах осмыслен только для одного инструмента: на корзине $100 несопоставимы между BTC и DOGE.',
  grid_step_pct:'Расстояние между уровнями в процентах от цены. Каждый round-trip приносит примерно этот процент минус две maker-комиссии. Слишком мелкий шаг = много сделок с малой прибылью, и доля комиссии растёт.',
  grid_step_abs:'Расстояние между уровнями в деньгах котировки. Осмысленно для работы по одному инструменту.',
  grid_spacing:'Шаг сетки в единицах ATR. Безразмерный (k×ATR), а не в %/$ — чтобы параметр был сравним между инструментами. Больше k = реже ордера, меньше комиссий.',
  levels:'Сколько ордеров с каждой стороны. 2 = по одному (1 buy + 1 sell) — основной режим. Больше = лесенка дальше от цены (ловит только быстрые прострелы за один тик). На скорость набора и риск влияет «Max ордеров» (потолок инвентаря), а на то, как шаг расширяется при наборе позиции — γ (риск-аверсия) в режиме A-S.',
  order_usd:'Размер одного ордера в долларах (нотионал). Количество в монете считается как $ / цена. Например $80 при BTC≈66800 ≈ 0.0012 BTC.',
  tp:'Take-profit в единицах ATR: на сколько ATR выше входа фиксируется прибыль уровня.',
  sl:'Stop-loss всей позиции в единицах ATR: ход против средней цены, после которого позиция закрывается по рынку (taker).',
  ema:'Окно EMA-фильтра тренда. В нисходящем тренде покупки осторожнее, продажи агрессивнее (и наоборот).',
  max_orders:'Потолок одновременно открытых ордеров — ограничение риска инвентаря. При достижении новые ордера на стороне набора не ставятся.',
  expansion:'Как растёт шаг между уровнями вглубь: Geometric (×коэффициент), Arithmetic (1,2,3…), Fibonacci.',
  as_gamma:'γ — риск-аверсия в Avellaneda-Stoikov. Больше γ = сильнее уклон центра против инвентаря и шире спред.',
  as_kappa:'κ — интенсивность потока ордеров в Avellaneda-Stoikov. Влияет на базовую надбавку к спреду.',
  max_drawdown_pct:'Kill-switch: при просадке счёта ≥ этого % движок закрывает позицию по рынку и останавливает котирование (0 = выключен). Аварийный риск-лимит (блок G).',
};

const $ = (id) => document.getElementById(id);
const api = (p, opt) => fetch(p, opt).then(r => r.json());
function toast(t){ const e=$('toast'); e.textContent=t; e.classList.add('on'); clearTimeout(e._t); e._t=setTimeout(()=>e.classList.remove('on'),2400); }
// shadcn-подобный модал подтверждения (светлая тема, без нативного confirm)
function showConfirm(title, msg, onOk, opts){
  opts=opts||{}; const okLabel=opts.okLabel||'Подтвердить', danger=opts.danger;
  const old=$('confirmModal'); if(old) old.remove();
  const ok=danger?'#a93529':'#1F8A5B';
  const wrap=document.createElement('div'); wrap.id='confirmModal';
  wrap.style.cssText='position:fixed;inset:0;z-index:200;display:flex;align-items:center;justify-content:center;background:rgba(20,24,26,.22);backdrop-filter:blur(1px);';
  wrap.innerHTML=`<div role="dialog" style="width:380px;max-width:92vw;background:#fff;border:1px solid #e3e6e0;border-radius:13px;box-shadow:0 18px 50px rgba(20,24,26,.20);padding:18px 18px 14px;font-family:'Space Grotesk',sans-serif;">
    <div style="font-size:15px;font-weight:700;color:#14181a;margin-bottom:6px;">${title}</div>
    <div style="font-size:12.5px;line-height:1.5;color:#5b635e;margin-bottom:16px;white-space:pre-line;">${msg}</div>
    <div style="display:flex;justify-content:flex-end;gap:8px;">
      <button id="cfCancel" class="hov" style="font-size:12.5px;font-weight:500;border:1px solid #e3e6e0;background:#fff;border-radius:8px;padding:7px 14px;cursor:pointer;color:#5b635e;">Отмена</button>
      <button id="cfOk" style="font-size:12.5px;font-weight:600;border:0;background:${ok};color:#fff;border-radius:8px;padding:7px 16px;cursor:pointer;">${okLabel}</button>
    </div></div>`;
  document.body.appendChild(wrap);
  const close=()=>{ wrap.remove(); document.removeEventListener('keydown',onKey); };
  const onKey=(e)=>{ if(e.key==='Escape') close(); else if(e.key==='Enter'){ close(); onOk&&onOk(); } };
  document.addEventListener('keydown',onKey);
  wrap.onmousedown=(e)=>{ if(e.target===wrap) close(); };
  $('cfCancel').onclick=close;
  $('cfOk').onclick=()=>{ close(); onOk&&onOk(); };
  $('cfOk').focus();
}
const fmt = (v,d=2)=> (v>=0?'':'-')+'$'+Math.abs(v).toFixed(d);
function fmtPx(v){ return v>=1000?v.toLocaleString('en-US',{maximumFractionDigits:0}):v>=1?v.toFixed(2):v.toFixed(4); }
function tfLabel(t){ return t==='60'?'1h':t==='240'?'4h':t+'m'; }

// ─────────────────────────── загрузка ───────────────────────────
// ───────── сохранение состояния UI между перезагрузками ─────────
function loadUI(){
  try{ const u=JSON.parse(localStorage.getItem('gridlab_ui')||'{}');
    if(typeof u.leftCollapsed==='boolean') S.leftCollapsed=u.leftCollapsed;
    if(typeof u.bottomCollapsed==='boolean') S.bottomCollapsed=u.bottomCollapsed;
    if(typeof u.bottomH==='number' && u.bottomH>=80 && u.bottomH<=600) S.bottomH=u.bottomH;
    if(typeof u.paramsOpen==='boolean') S.paramsOpen=u.paramsOpen;
    if(typeof u.tab==='string') S.tab=u.tab;
    if(typeof u.tf==='string') S._savedTf=u.tf;
    if(u.params && typeof u.params==='object') S._savedParams=u.params;  // режим/параметры — чтобы не мерцало на F5
  }catch(e){}
}
function saveUI(){
  try{ localStorage.setItem('gridlab_ui', JSON.stringify(
    {leftCollapsed:S.leftCollapsed, bottomCollapsed:S.bottomCollapsed, bottomH:S.bottomH, paramsOpen:S.paramsOpen, tab:S.tab, tf:S.tf, params:S.params})); }catch(e){}
}

async function boot(){
  loadUI();
  setupTooltips();
  S.cfg = await api('/api/config');
  // дефолты + localStorage + ЖИВОЙ режим сервера (высший приоритет) → первый рендер уже верный, без мерцания
  S.params = Object.assign({}, S.cfg.defaults, S._savedParams||{}, S.cfg.live_params||{});
  if(S.cfg.live_params) S.paramsSynced=true;   // сервер уже сообщил режим — ws-кадру не нужно его менять
  S.tf = S._savedTf || S.cfg.interval;   // восстановить выбранный ТФ из localStorage
  S.paper = S.cfg.venue !== 'testnet';
  render();
  await loadInit();
  if(S.market) startStream('live');   // realtime-терминал: торгуем вживую сразу
}

async function loadInit(){
  S.loading = true; render();
  try{
    const m = await api('/api/init?interval='+encodeURIComponent(S.tf));
    if(m.error){ toast('Ошибка: '+m.error); }
    else {
      S.market = m;
      if(!m.instruments.find(i=>i.sym===S.selInst)) S.selInst = m.instruments[0].sym;
      // сброс торгового состояния — чистый график без сделок
      S.tradingStarted=false; S.result=null; S.liveCurve=null; S.liveCandles=null; S.manualGrid=null;
    }
  }catch(e){ toast('Сбой загрузки свечей: '+e.message); }
  S.loading = false; render();
}

async function runBacktest(){
  if(S.streaming) stopAll(true);
  S.loading = true; render();
  try{
    const body = { interval: S.tf, params: S.params, symbols: S.cfg.symbols };
    const r = await api('/api/backtest', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    if(r.error){ toast('Ошибка: '+r.error); S.loading=false; render(); return; }
    S.result = r; S.tradingStarted = true; S.run = 'running'; S.streamMode=null; S.liveCurve=null; S.liveCandles=null;
    if(!r.instruments.find(i=>i.sym===S.selInst)) S.selInst = r.instruments[0].sym;
  }catch(e){ toast('Сбой бэктеста: '+e.message); }
  S.loading = false; render();
}

// ─────────────────────────── доступ к данным ───────────────────────────
function selData(){
  const sym=S.selInst;
  if(S.streamMode==='live' && S.live && S.live.candles && S.live.selected===sym){
    return {candles:S.live.candles, times:S.live.times, markers:S.live.markers||[], last:S.live.price};
  }
  if(S.streamMode==='replay' && S.liveCandles && S.liveCandles.length){
    return {candles:S.liveCandles, times:S.liveTimes, markers:S.liveMarkers||[], last:S.liveCandles[S.liveCandles.length-1][3]};
  }
  if(S.tradingStarted && S.result && S.result.charts && S.result.charts[sym]){
    const ch=S.result.charts[sym]; return {candles:ch.candles, times:ch.times, markers:ch.markers, last:ch.candles[ch.candles.length-1][3]};
  }
  if(S.market){ const m=S.market.instruments.find(i=>i.sym===sym); if(m) return {candles:m.candles, times:m.times, markers:[], last:m.last}; }
  return null;
}
function portfolioList(){
  if(S.streamMode==='live' && S.live) return S.live.instruments;
  if(S.tradingStarted && S.result) return S.result.instruments;
  if(S.market) return S.market.instruments.map(m=>({sym:m.sym,alloc:m.alloc,pnl:0,pnl_pct:0,orders:0,status:'active',spark:m.price_spark}));
  return [];
}
function dash(){
  if(S.streamMode==='live' && S.live) return S.live.dash;
  if(S.tradingStarted && S.result){
    const r=S.result;
    return {balance:r.balance,equity:r.equity,roi:r.roi,drawdown:r.drawdown,realized:r.realized,unrealized:r.unrealized,open_orders:r.open_orders,fees:r.fees,funding:r.funding};
  }
  const cap=S.cfg?S.cfg.start_capital:1000;
  return {balance:cap,equity:cap,roi:0,drawdown:0,realized:0,unrealized:0,open_orders:0,fees:0,funding:0};
}
function logRows(){
  if(S.streamMode==='live' && S.live) return S.live.recent||[];
  if(S.tradingStarted && S.result) return S.result.log||[];
  return [];
}
function eqCurve(){
  if(S.streamMode==='live' && S.live && S.live.equity_curve && S.live.equity_curve.length>1) return S.live.equity_curve;
  if(S.streamMode==='replay' && S.liveCurve && S.liveCurve.length>1) return S.liveCurve;
  if(S.tradingStarted && S.result) return S.result.equity_curve;
  return null;
}
function balCurve(){
  if(S.streamMode==='live' && S.live && S.live.balance_curve && S.live.balance_curve.length>1) return S.live.balance_curve;
  return null;
}
function gridPrices(last){
  // превью до запуска стратегии: ровно одна лимитка снизу (buy) и одна сверху (sell)
  if(S.manualGrid && S.manualGrid.length) return S.manualGrid;
  const p=S.params, sp=last*0.008*p.grid_spacing;
  return [last-sp, last+sp];
}
function resetGrid(){ S.manualGrid=null; }
// стратегия запущена явно (live)?
function stratOn(){ return S.streamMode==='live' && S.live && !!S.live.started; }
// реальные сеточные ордера выбранного инструмента (приходят из live-кадра)
function liveGrid(){ return (S.streamMode==='live' && S.live && S.live.grid) ? S.live.grid : []; }

// ─────────────────────────── helpers рендера ───────────────────────────
function seg(on){
  const b='padding:5px 10px;font-family:\'Space Grotesk\',sans-serif;font-size:11.5px;font-weight:500;border:0;cursor:pointer;border-radius:6px;white-space:nowrap;';
  return on ? b+'background:#1F8A5B;color:#fff;' : b+'background:transparent;color:#5b635e;';
}
function tabStyle(on){
  return on
   ? 'padding:7px 14px;font-family:\'Space Grotesk\';font-size:12px;font-weight:600;border:0;cursor:pointer;border-radius:7px 7px 0 0;background:#fff;color:#14181a;border-bottom:2px solid #1F8A5B;'
   : 'padding:7px 14px;font-family:\'Space Grotesk\';font-size:12px;font-weight:500;border:0;cursor:pointer;border-radius:7px 7px 0 0;background:transparent;color:#939a93;border-bottom:2px solid transparent;';
}
function sparkPath(pts){
  if(!pts||pts.length<2) return '';
  const mn=Math.min(...pts), mx=Math.max(...pts), rng=(mx-mn)||1;
  return pts.map((v,i)=>{const x=(i/(pts.length-1))*56;const y=18-((v-mn)/rng)*16;return (i===0?'M':'L')+x.toFixed(1)+','+y.toFixed(1);}).join(' ');
}
function qq(key){ return `<span class="qq" data-tip="${TIPS[key]||''}">?</span>`; }

// ─────────────────────────── вёрстка ───────────────────────────
function render(){
  const te=$('tipEl'); if(te) te.style.display='none';   // не оставлять залипший тултип после перерисовки
  const root = $('root');
  if(!S.cfg){ root.innerHTML = '<div style="display:flex;height:100vh;align-items:center;justify-content:center;color:#939a93;">Загрузка конфигурации…</div>'; return; }
  root.innerHTML = `
  <div style="height:100vh;display:flex;flex-direction:column;background:#f4f6f3;overflow:hidden;">
    ${topBar()}
    <div style="flex:1;display:flex;min-height:0;overflow:hidden;">
      ${leftPanel()}
      ${centerPanel()}
      ${orderbookPanel()}
      ${tapePanel()}
    </div>
    ${paramsDrawer()}
  </div>`;
  bind();
  requestAnimationFrame(drawAll);
}

function topBar(){
  const runLabel = S.streaming?(S.streamMode==='live'?'● Live':'● Replay'):S.run==='running'?'● Работает':S.run==='paused'?'⏸ Пауза':'○ Остановлен';
  const runColor = (S.streaming||S.run==='running')?C.accent:S.run==='paused'?C.amber:C.mut;
  const paperStyle = S.paper?'background:#e7f3ec;color:#157a4f;border:1px solid #cfe7d9;':'background:#fbf2e0;color:#97681a;border:1px solid #f0deb5;';
  const speeds=[1,2,5,10].map(v=>`<button data-spd="${v}" style="${seg(S.speed===v)}">${v}x</button>`).join('');
  return `
  <div style="flex:0 0 auto;height:46px;display:flex;align-items:center;gap:10px;padding:0 14px;background:#fff;border-bottom:1px solid #e3e6e0;z-index:20;">
    <div style="display:flex;align-items:center;gap:8px;margin-right:4px;">
      <div style="width:22px;height:22px;background:#1F8A5B;border-radius:6px;transform:rotate(45deg);display:flex;align-items:center;justify-content:center;"><div style="width:7px;height:7px;background:#fff;border-radius:1.5px;"></div></div>
      <span style="font-weight:700;font-size:14px;letter-spacing:-.01em;">GRIDLAB</span>
    </div>
    <span style="width:1px;height:22px;background:#e3e6e0;"></span>
    <span class="mono" style="font-size:13px;font-weight:600;">${S.selInst}</span>
    <div style="display:flex;align-items:center;gap:5px;">
      <span style="font-size:10px;letter-spacing:.06em;color:#939a93;font-weight:600;">ТАЙМФРЕЙМ</span>
      <select id="tfSel" class="mono" style="font-size:12px;font-weight:600;color:#14181a;background:#fff;border:1px solid #cdd3cb;border-radius:6px;padding:5px 8px;cursor:pointer;min-width:54px;">
        ${['1','5','15','60','240'].map(t=>`<option value="${t}" ${t===S.tf?'selected':''}>${tfLabel(t)}</option>`).join('')}
      </select>
    </div>
    <button id="paperBtn" class="mono" style="font-size:11px;font-weight:600;letter-spacing:.03em;border-radius:6px;padding:5px 10px;cursor:pointer;${paperStyle}">${S.paper?'Paper':'Testnet'}</button>
    <span style="width:1px;height:22px;background:#e3e6e0;"></span>
    <button id="btnStart" class="hov" data-tip="Прогон по истории: быстрый бэктест на реальных свечах. Маркеры сделок и эквити появятся за весь период." style="font-size:12px;font-weight:500;border:1px solid #e3e6e0;background:#fff;border-radius:6px;padding:5px 10px;cursor:pointer;color:#1F8A5B;">Бэктест</button>
    <button id="btnReplay" class="hov" data-tip="Проигрывание истории по барам с ускорением — эквити растёт реалтайм." style="font-size:12px;font-weight:500;border:1px solid #e3e6e0;background:#fff;border-radius:6px;padding:5px 10px;cursor:pointer;color:#3B82CE;">Replay</button>
    <button id="btnLive" class="hov" data-tip="Реальное время: тянет свежие свечи Bybit, график двигается, бумажный движок торгует вживую." style="font-size:12px;font-weight:500;border:1px solid #e3e6e0;background:#fff;border-radius:6px;padding:5px 10px;cursor:pointer;color:#C98A1A;">● Live</button>
    <button id="btnStop" class="hov" data-tip="Сброс: остановить и очистить сделки, вернуть чистый график." style="font-size:12px;font-weight:500;border:1px solid #e3e6e0;background:#fff;border-radius:6px;padding:5px 10px;cursor:pointer;color:#5b635e;">Reset</button>
    <span style="width:1px;height:22px;background:#e3e6e0;"></span>
    <button id="btnStrat" data-tip="Запустить/остановить grid-стратегию. До запуска автоматических лимиток нет — только ручные. При запуске выставляется многоуровневая адаптивная сетка (число уровней = «Кол-во уровней»/«Max ордеров»); шаг и центр смещаются по инвентарю, сетка перестраивается при срабатываниях." style="font-size:12px;font-weight:600;border-radius:6px;padding:5px 12px;cursor:pointer;${stratOn()?'background:#fbecea;color:#a93529;border:1px solid #f0c8c4;':'background:#e7f3ec;color:#157a4f;border:1px solid #cfe7d9;'}">${stratOn()?'Стоп стратегии':'Старт стратегии'}</button>
    <span style="width:1px;height:22px;background:#e3e6e0;"></span>
    <div style="display:flex;align-items:center;gap:4px;">
      <span style="font-size:10px;letter-spacing:.04em;color:#939a93;font-weight:600;">СЧЁТ $</span>
      <input id="acctCap" class="mono" value="${(S.live&&S.live.capital)||(S.cfg?S.cfg.start_capital:1000)}" style="width:72px;font-size:11px;font-weight:600;text-align:right;border:1px solid #cdd3cb;border-radius:6px;padding:4px 6px;outline:none;">
      <button id="btnNewAcct" class="hov" style="font-size:12px;font-weight:500;border:1px solid #e3e6e0;background:#fff;border-radius:6px;padding:5px 10px;cursor:pointer;color:#1F8A5B;">Новый счёт</button>
    </div>
    <span style="width:1px;height:22px;background:#e3e6e0;"></span>
    <div id="speedBox" style="display:flex;gap:2px;background:#eef0ec;border:1px solid #e3e6e0;border-radius:7px;padding:2px;">${speeds}</div>
    <span style="width:1px;height:22px;background:#e3e6e0;"></span>
    <button id="btnExportCsv" class="hov" data-tip="Live: журнал событий (CSV). Бэктест: сделки прогона (CSV)." style="font-size:12px;font-weight:500;border:1px solid #e3e6e0;background:#fff;border-radius:6px;padding:5px 10px;cursor:pointer;">⤓ CSV</button>
    <button id="btnExportFills" class="hov" data-tip="Live: полный лог исполнений-филлов (CSV)." style="font-size:12px;font-weight:500;border:1px solid #e3e6e0;background:#fff;border-radius:6px;padding:5px 10px;cursor:pointer;">⤓ Филлы</button>
    <button id="btnExportJson" class="hov" data-tip="Live: полный снимок сессии — события, филлы, статистика, кривые (JSON). Бэктест: прогон (JSON)." style="font-size:12px;font-weight:500;border:1px solid #e3e6e0;background:#fff;border-radius:6px;padding:5px 10px;cursor:pointer;">⤓ JSON</button>
    <div style="margin-left:auto;display:flex;align-items:center;gap:6px;">
      <button id="btnParams" class="hov" data-tip="Открыть/скрыть панель параметров стратегии (режим, шаг сетки, размеры, риск-лимиты). Закрыть — Esc или клик по затемнению." style="${S._paramsDirty?'font-size:12px;font-weight:600;border-radius:6px;padding:5px 10px;cursor:pointer;border:1px solid #e6c98a;background:#fdf7ea;color:#97681a;':'font-size:12px;font-weight:600;border-radius:6px;padding:5px 10px;cursor:pointer;border:1px solid #e3e6e0;background:#fff;color:#14181a;'}">Параметры${S._paramsDirty?' ●':''}</button>
      <a href="/replay" style="font-size:12px;font-weight:600;color:#3B82CE;text-decoration:none;border:1px solid #cfe0f2;border-radius:6px;padding:5px 10px;">Replay</a>
      <a href="/lab" style="font-size:12px;font-weight:600;color:#3B82CE;text-decoration:none;border:1px solid #cfe0f2;border-radius:6px;padding:5px 10px;">Лаборатория</a>
      <span class="mono" style="font-size:10px;color:#939a93;">venue: ${S.cfg.venue}</span>
      <span style="width:7px;height:7px;border-radius:50%;background:${runColor};"></span>
      <span class="mono" style="font-size:11px;color:${runColor};">${runLabel}</span>
    </div>
  </div>`;
}

function leftPanel(){
  if(S.leftCollapsed){
    return `
    <div style="flex:0 0 34px;background:#fbfcfa;border-right:1px solid #e3e6e0;display:flex;flex-direction:column;align-items:center;padding-top:10px;gap:10px;">
      <button id="leftExpand" class="hov" style="width:24px;height:24px;border:1px solid #e3e6e0;background:#fff;border-radius:6px;cursor:pointer;font-size:12px;">▸</button>
      <div style="writing-mode:vertical-rl;transform:rotate(180deg);font-size:10px;letter-spacing:.14em;color:#939a93;font-weight:600;">ПОРТФЕЛЬ · ${S.cfg.symbols.length}</div>
    </div>`;
  }
  const list=portfolioList();
  return `
  <div style="flex:0 0 216px;background:#fbfcfa;border-right:1px solid #e3e6e0;display:flex;flex-direction:column;overflow:hidden;">
    <div style="padding:12px 13px 8px;">
      <div style="display:flex;align-items:center;justify-content:space-between;">
        <div style="font-size:11px;letter-spacing:.12em;color:#939a93;font-weight:600;">ПОРТФЕЛЬ · ${list.length||S.cfg.symbols.length} ИНСТР.</div>
        <button id="leftCollapse" class="hov" style="width:22px;height:22px;border:1px solid #e3e6e0;background:#fff;border-radius:6px;cursor:pointer;font-size:12px;line-height:1;color:#5b635e;">◂</button>
      </div>
      <div id="leftHead" class="mono" style="font-size:12px;color:#5b635e;margin-top:6px;">${leftHeadHTML()}</div>
    </div>
    <div id="instList" style="flex:1;overflow-y:auto;">${instRowsHTML()}</div>
  </div>`;
}

function leftHeadHTML(){
  const d=dash(); const roiColor = d.roi>=0?C.green:C.red;
  return `$1 000 → <b style="color:${roiColor};">$${d.equity.toFixed(2)}</b> <span style="color:${roiColor};">${d.roi>=0?'+':''}${d.roi}%</span>`;
}

function instRowsHTML(){
  if(S.loading && !S.market){
    return Array.from({length:10}).map(()=>`<div style="height:54px;border-bottom:1px solid #eef0ec;background:linear-gradient(90deg,#f6f7f4,#eef0ec,#f6f7f4);"></div>`).join('');
  }
  return portfolioList().map(inst=>{
    const pos = inst.pnl>=0; const sc = pos?C.accent:C.red; const active = inst.sym===S.selInst;
    return `
    <div class="hovrow lnk" data-inst="${inst.sym}" style="display:flex;align-items:center;gap:8px;padding:9px 13px;border-bottom:1px solid #eef0ec;${active?'background:#eef2ec;':''}">
      <div style="flex:1;min-width:0;">
        <div style="display:flex;align-items:center;gap:6px;">
          <span class="mono" style="font-size:12px;font-weight:600;">${inst.sym}</span>
          <span style="width:5px;height:5px;border-radius:50%;background:${inst.status==='stop'?C.liq:C.accent};flex:0 0 auto;"></span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;margin-top:4px;">
          <span class="mono" style="font-size:11px;color:${pos?C.green:C.red};font-weight:500;">${pos?'+$':'-$'}${Math.abs(inst.pnl).toFixed(2)}</span>
          <span class="mono" style="font-size:10px;color:#939a93;">${inst.pnl_pct>=0?'+':''}${inst.pnl_pct.toFixed(1)}%</span>
        </div>
        <div class="mono" style="font-size:9.5px;color:#aab0a9;margin-top:3px;">$${inst.alloc.toFixed(0)} · ${inst.orders} орд · ${inst.status==='stop'?'стоп':'активен'}</div>
      </div>
      <svg viewBox="0 0 56 18" style="width:52px;height:17px;flex:0 0 auto;"><path d="${sparkPath(inst.spark)}" fill="none" stroke="${sc}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"></path></svg>
    </div>`;
  }).join('') || '<div style="padding:20px 13px;color:#939a93;font-size:12px;">Загрузка…</div>';
}

function card(label,val,color){
  return `<div style="display:flex;flex-direction:column;padding:6px 10px;border:1px solid #e3e6e0;border-radius:8px;min-width:84px;">
    <span style="font-size:9px;letter-spacing:.06em;color:#939a93;font-weight:600;">${label}</span>
    <span class="mono" style="font-size:13.5px;font-weight:600;margin-top:2px;${color?'color:'+color+';':''}">${val}</span></div>`;
}

function cardsHTML(){
  const d=dash();
  const roiColor = d.roi>=0?C.green:C.red;
  return [
    card('BALANCE','$'+d.balance.toFixed(2)),
    card('EQUITY','$'+d.equity.toFixed(2)),
    card('ROI',(d.roi>=0?'+':'')+d.roi+'%',roiColor),
    card('DRAWDOWN','-'+d.drawdown+'%',C.red),
    card('REALIZED',fmt(d.realized),d.realized>=0?C.green:C.red),
    card('UNREAL.',fmt(d.unrealized),d.unrealized>=0?C.green:C.red),
    card('OPEN ORD',String(d.open_orders)),
    card('FEES',fmt(-d.fees),C.red),
    card('FUNDING',fmt(-d.funding),d.funding>=0?C.red:C.green),
  ].join('');
}

function centerPanel(){
  const cards = cardsHTML();
  const eqc=eqCurve();
  const eqLast = eqc? '$'+eqc[eqc.length-1].toFixed(2) : '$'+ (S.cfg?S.cfg.start_capital.toFixed(2):'1000');
  const dragHint = stratOn()? ' · стратегия запущена: сплошные — активные лимитки, пунктир — превью следующих уровней' : ' · нажми «Старт стратегии», чтобы выставить сетку';
  return `
  <div style="flex:1;display:flex;flex-direction:column;min-width:0;overflow:hidden;">
    <div id="cardsRow" style="display:flex;gap:6px;padding:8px 10px;background:#fff;border-bottom:1px solid #e3e6e0;overflow-x:auto;flex:0 0 auto;">${cards}</div>

    <div id="priceWrap" style="flex:8;min-height:320px;position:relative;background:#fafbf9;border-bottom:1px solid #e3e6e0;">
      <div style="position:absolute;top:8px;left:12px;z-index:2;pointer-events:none;display:flex;align-items:center;gap:8px;">
        <span class="mono" style="font-size:12px;font-weight:600;">${S.selInst}</span>
        <span class="mono" style="font-size:10px;color:#939a93;background:#eef0ec;border-radius:4px;padding:2px 6px;">${tfLabel(S.tf)}</span>
        <span style="font-size:10px;color:#3B82CE;font-weight:500;">Grid · ${S.params.mode==='grid'?'классический грид':S.params.mode==='avellaneda'?'Avellaneda-Stoikov':'эвристика'}${dragHint}</span>
      </div>
      <div id="lwchart" style="position:absolute;inset:0;"></div>
    </div>

    <div id="eqWrap" style="flex:2;min-height:120px;position:relative;background:#fafbf9;border-bottom:1px solid #e3e6e0;">
      <div style="position:absolute;top:6px;left:12px;z-index:2;pointer-events:none;display:flex;align-items:center;gap:10px;">
        <span style="font-size:11px;font-weight:600;">Portfolio Equity ${S.streamMode==='live'?'· live':''}</span>
        <span style="display:flex;align-items:center;gap:4px;"><span style="width:14px;height:2px;background:#1F8A5B;border-radius:2px;"></span><span class="mono" style="font-size:11px;font-weight:600;color:#157a4f;">${eqLast}</span></span>
        ${S.streamMode==='live'&&S.live?`<span data-tip="Balance — только зафиксированная прибыль/убыток (реализованное). Меняется в момент закрытия round-trip, как ты и описал." style="display:flex;align-items:center;gap:4px;pointer-events:auto;"><span style="width:14px;height:0;border-top:2px dashed #5b635e;"></span><span class="mono" id="eqBalVal" style="font-size:11px;font-weight:600;color:#5b635e;">$${S.live.dash.balance.toFixed(2)} bal</span></span>`:''}
      </div>
      <canvas id="eqCv" style="display:block;width:100%;height:100%;"></canvas>
    </div>

    <div id="bottomPanel" style="flex:0 0 ${S.bottomCollapsed?'34px':S.bottomH+'px'};display:flex;flex-direction:column;background:#fff;position:relative;">
      ${S.bottomCollapsed?'':`<div id="bottomResize" title="Потяни, чтобы изменить высоту панели" style="position:absolute;top:-3px;left:0;right:0;height:6px;cursor:row-resize;z-index:5;"></div>`}
      <div style="display:flex;gap:0;align-items:center;border-bottom:1px solid #e3e6e0;padding:0 10px;flex:0 0 auto;">
        <button data-tab="log" style="${tabStyle(S.tab==='log')}">Журнал событий</button>
        <button data-tab="analytics" style="${tabStyle(S.tab==='analytics')}">Аналитика</button>
        <button data-tab="trades" style="${tabStyle(S.tab==='trades')}">Сделки</button>
        <button id="bottomToggle" class="hov" style="margin-left:auto;width:24px;height:24px;border:1px solid #e3e6e0;background:#fff;border-radius:6px;cursor:pointer;font-size:11px;color:#5b635e;">${S.bottomCollapsed?'▴':'▾'}</button>
      </div>
      ${S.bottomCollapsed?'':`<div id="bottomBody" style="flex:1;overflow:auto;">${bottomTab()}</div>`}
    </div>
  </div>`;
}

function bottomTab(){
  const live = S.streamMode==='live' && S.live;
  if(!S.tradingStarted && !live) return '<div style="padding:14px;color:#939a93;font-size:12px;">Терминал запускает realtime-торговлю… события появятся здесь.</div>';
  if(live && !stratOn() && (!S.live.recent || !S.live.recent.length)) return '<div style="padding:14px;color:#939a93;font-size:12px;">Live включён, цены идут реалтайм. Стратегия НЕ запущена — автоматических лимиток нет. Нажми «Старт стратегии» или ставь лимитки вручную мышью на графике.</div>';
  if(S.tab==='analytics'){
    if(!S.result) return '<div style="padding:14px;color:#939a93;font-size:12px;">Аналитика (Sharpe/Sortino/PF…) считается по завершённому прогону — нажми «Бэктест» для сводки за период.</div>';
    const r=S.result; const a=r.analytics;
    const cell=(l,v,c)=>`<div style="border:1px solid #e3e6e0;border-radius:9px;padding:11px 13px;"><div style="font-size:9.5px;letter-spacing:.06em;color:#939a93;font-weight:600;">${l}</div><div class="mono" style="font-size:18px;font-weight:600;margin-top:5px;${c?'color:'+c+';':''}">${v}</div></div>`;
    return `<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:10px;">
      ${cell('SHARPE RATIO',a.sharpe)}${cell('SORTINO RATIO',a.sortino)}${cell('PROFIT FACTOR',a.profit_factor)}
      ${cell('WIN RATE',a.win_rate+'%',a.win_rate>=50?C.green:C.red)}${cell('MAX DRAWDOWN','-'+a.max_dd+'%',C.red)}
      ${cell('AVG TRADE',fmt(a.avg_trade),a.avg_trade>=0?C.green:C.red)}${cell('TOTAL TRADES',a.total_trades)}
      ${cell('ИТОГ $1000→','$'+r.equity.toFixed(2),r.roi>=0?C.green:C.red)}
    </div>`;
  }
  const isTrades = S.tab==='trades';
  const allRows = logRows();
  const rows = isTrades ? allRows.filter(e=>e.action.includes('Grid')||e.action.includes('Ликвид')||e.action.includes('Stop')) : allRows;
  const cols = isTrades ? '62px 100px 78px 96px 64px 64px 1fr' : '62px 104px 74px 92px 58px 52px 60px 1fr';
  const head = isTrades ? ['TIME','SIDE','SYMBOL','PRICE','SIZE','PnL','COMMENT'] : ['TIME','ACTION','SYMBOL','PRICE','SIZE','FEE','PnL','COMMENT'];
  const hrow = `<div style="display:grid;grid-template-columns:${cols};padding:6px 10px;border-bottom:1px solid #eef0ec;background:#fafbf9;position:sticky;top:0;z-index:1;">${head.map(h=>`<span style="font-size:9px;letter-spacing:.08em;color:#939a93;font-weight:600;">${h}</span>`).join('')}</div>`;
  const body = rows.slice(0,120).map(e=>{
    const t = new Date(e.ts).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
    const cells = isTrades
      ? [`<span style="color:#939a93;">${t}</span>`,`<span style="font-weight:500;color:${e.actionColor};">${e.action}</span>`,`<span style="color:#5b635e;">${e.sym}</span>`,`<span>${e.price}</span>`,`<span style="color:#5b635e;">${e.size}</span>`,`<span style="font-weight:600;color:${e.pnlColor};">${e.pnl}</span>`,`<span style="color:#7e857d;font-size:10px;">${e.comment}</span>`]
      : [`<span style="color:#939a93;">${t}</span>`,`<span style="font-weight:500;color:${e.actionColor};">${e.action}</span>`,`<span style="color:#5b635e;">${e.sym}</span>`,`<span>${e.price}</span>`,`<span style="color:#5b635e;">${e.size}</span>`,`<span style="color:#a93529;">${e.fee}</span>`,`<span style="font-weight:600;color:${e.pnlColor};">${e.pnl}</span>`,`<span style="color:#7e857d;font-size:10px;">${e.comment}</span>`];
    return `<div class="mono" style="display:grid;grid-template-columns:${cols};padding:5px 10px;border-bottom:1px solid #f4f5f2;font-size:11px;align-items:center;">${cells.join('')}</div>`;
  }).join('');
  return `<div style="min-width:760px;">${hrow}${body||'<div style="padding:14px;color:#939a93;font-size:12px;">Нет записей.</div>'}</div>`;
}

function paramRow(label,key,step,min,max){
  return `<div style="display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid #eef0ec;">
    <span style="font-size:12px;color:#5b635e;display:flex;align-items:center;">${label}${qq(key)}</span>
    <input type="number" data-param="${key}" value="${S.params[key]}" step="${step}" min="${min}" max="${max}"
      class="mono" style="width:60px;font-size:12px;text-align:right;border:1px solid #e3e6e0;border-radius:6px;padding:5px 6px;background:#fff;outline:none;" /></div>`;
}

// выдвижная панель параметров (off-canvas overlay поверх — без рефлоу графика).
// rightPanel() переносится сюда КАК ЕСТЬ — все id/обработчики внутри неизменны.
function paramsDrawer(){
  const open=S.paramsOpen;
  return `
  <div id="paramsBackdrop" style="position:fixed;top:46px;left:0;right:0;bottom:0;z-index:60;background:rgba(20,24,26,.18);backdrop-filter:blur(1px);transition:opacity .22s ease;${open?'opacity:1;pointer-events:auto;':'opacity:0;pointer-events:none;'}"></div>
  <div id="paramsDrawer" style="position:fixed;top:46px;right:0;bottom:0;width:280px;z-index:61;background:#fbfcfa;border-left:1px solid #e3e6e0;box-shadow:-14px 0 36px rgba(20,24,26,.13);transition:transform .26s cubic-bezier(.4,0,.2,1);transform:translateX(${open?'0':'100%'});display:flex;flex-direction:column;overflow:hidden;">
    ${rightPanel()}
  </div>`;
}

// Пресеты параметров — НАБОРЫ существующих полей (бэкенд не трогаем). Клик только
// предзаполняет форму; на сервер уходит обычной кнопкой «Сохранить параметры».
// ВАЖНО: в режиме avellaneda дистанцию задаёт A-S-спред ≈ γ·σ²·T + (2/γ)·ln(1+γ/κ),
// где член потока ≈ 2/κ доминирует (grid_spacing там НЕ участвует). Значит МЕНЬШЕ κ =
// ШИРЕ спред. Поэтому «Широкий» имеет НИЗКИЙ κ, «Узкий» — высокий (иначе ширина инвертируется).
const PARAM_PRESETS = {
  // A и B — классический грид: широкий шаг = редкие сделки и больше прибыли на сделку,
  // узкий = чаще оборот. Комиссия фиксирована, поэтому слишком узкий шаг съедает эдж.
  A: {mode:'grid', grid_step_mode:'pct', grid_step_pct:1.5, grid_levels:10, grid_notional_mult:1.0, grid_reanchor:0, sl:0, max_drawdown_pct:0},
  B: {mode:'grid', grid_step_mode:'pct', grid_step_pct:0.5, grid_levels:16, grid_notional_mult:1.0, grid_reanchor:0, sl:0, max_drawdown_pct:0},
  // C — маркет-мейкинг для сравнения: та же корзина, но с перецентровкой котировок.
  C: {mode:'avellaneda', grid_spacing:1.8, order_usd:80, as_gamma:0.3, as_kappa:500, as_horizon:200, max_orders:10, sl:0, max_drawdown_pct:0},
};
function presetStyle(name){
  const on = S._activePreset===name;
  return `flex:1;font-size:11px;font-weight:500;border:${on?'1.5px solid #1F8A5B':'1px solid #e3e6e0'};background:${on?'#e7f3ec':'#fff'};border-radius:7px;padding:7px 4px;cursor:pointer;color:#14181a;`;
}
function applyPreset(name){
  const p = PARAM_PRESETS[name]; if(!p) return;
  S.params = Object.assign({}, S.params, p);   // только предзаполнение полей
  S._activePreset = name;                       // подсветить активный пресет
  S._paramsDirty = true;                        // подсветить «Сохранить» (как при ручной правке)
  saveUI(); resetGrid(); render();              // render — чтобы появились поля γ/κ (режим A-S) + подсветка
  setParamsBtnDirty(true);
  toast('Пресет '+name+' загружен — проверь значения и нажми «Сохранить»');
}

function rightPanel(){
  const p=S.params;
  let step='—', range='—', ordersCnt=null;
  const sd=selData();
  const f=v=>v>=1000?v.toFixed(0):v>=1?v.toFixed(2):v.toFixed(4);
  const gi=(S.streamMode==='live'&&S.live&&S.live.grid_info)?S.live.grid_info:null;
  if(gi && gi.count){
    // реальные шаг/диапазон движка (единый Quoter) — приоритетно
    if(gi.step) step=f(gi.step);
    if(gi.low!=null&&gi.high!=null) range=f(gi.low)+'–'+f(gi.high);
    ordersCnt=gi.count;
  } else if(sd){
    const last=sd.last; const prices=gridPrices(last);
    if(prices.length){ const mn=Math.min(...prices), mx=Math.max(...prices);
      const sp=(prices.length>1)?Math.abs(prices[1]-prices[0]):last*0.008*p.grid_spacing;
      step=f(sp); range=f(mn)+'–'+f(mx); }
  }
  return `
  <div style="flex:1;min-height:0;background:#fbfcfa;overflow-y:auto;display:flex;flex-direction:column;">
    <div style="padding:12px 14px 8px;">
      <div style="display:flex;align-items:center;justify-content:space-between;">
        <div style="font-size:11px;letter-spacing:.12em;color:#939a93;font-weight:600;">ПАРАМЕТРЫ СТРАТЕГИИ</div>
        <button id="paramsClose" class="hov" data-tip="Закрыть панель (Esc)" style="width:22px;height:22px;border:1px solid #e3e6e0;background:#fff;border-radius:6px;cursor:pointer;font-size:12px;line-height:1;color:#5b635e;">✕</button>
      </div>
      <div class="mono" style="font-size:11px;color:#5b635e;margin-top:5px;">${S.selInst} · ${tfLabel(S.tf)}</div>
    </div>
    <div style="padding:0 14px 0;">
      <div style="font-size:10px;letter-spacing:.08em;color:#aab0a9;font-weight:600;margin-bottom:6px;">ПРЕСЕТ</div>
      <div style="display:flex;gap:6px;">
        <button data-preset="A" class="hov" data-tip="Грид, широкий шаг (2 ATR): редкие сделки, больше прибыли на round-trip, комиссия почти незаметна. Спокойный режим для длинных прогонов." style="${presetStyle('A')}">A · Широкий</button>
        <button data-preset="B" class="hov" data-tip="Грид, узкий шаг (0.8 ATR) и 16 уровней: частые сделки, быстрый набор статистики. Прибыль на сделку меньше — доля комиссии выше." style="${presetStyle('B')}">B · Узкий</button>
        <button data-preset="C" class="hov" data-tip="Маркет-мейкинг Avellaneda-Stoikov для СРАВНЕНИЯ: котировки перецентрируются после каждого исполнения. На исторических прогонах уступает гриду — режим оставлен как база сравнения." style="${presetStyle('C')}">C · Маркет-мейк</button>
      </div>
      <div style="height:1px;background:#e3e6e0;margin:11px 0 0;"></div>
    </div>
    <div style="padding:10px 14px 10px;">
      <div style="font-size:10px;letter-spacing:.08em;color:#aab0a9;font-weight:600;margin-bottom:6px;display:flex;align-items:center;">РЕЖИМ АДАПТАЦИИ${qq('mode')}</div>
      <div style="display:flex;gap:2px;background:#eef0ec;border:1px solid #e3e6e0;border-radius:8px;padding:2px;">
        <button data-mode="grid" style="${seg(p.mode==='grid')}">Грид</button>
        <button data-mode="heuristic" style="${seg(p.mode==='heuristic')}">Эвристика</button>
        <button data-mode="avellaneda" style="${seg(p.mode==='avellaneda')}">Avellaneda-S.</button>
      </div>
    </div>
    <div style="padding:0 14px;display:flex;flex-direction:column;">
      ${p.mode==='grid'?`
      <div style="display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid #eef0ec;">
        <span style="font-size:12px;color:#5b635e;display:flex;align-items:center;">Шаг задаётся${qq('grid_step_mode')}</span>
        <select id="stepModeSel" class="mono" style="width:104px;font-size:11px;background:#fff;border:1px solid #e3e6e0;border-radius:6px;padding:5px 8px;cursor:pointer;">
          <option value="pct" ${p.grid_step_mode==='pct'?'selected':''}>в % цены</option>
          <option value="abs" ${p.grid_step_mode==='abs'?'selected':''}>в деньгах</option>
          <option value="atr" ${p.grid_step_mode==='atr'?'selected':''}>от ATR</option>
        </select>
      </div>`:''}
      ${p.mode==='grid'
        ? (p.grid_step_mode==='pct' ? paramRow('Шаг, % от цены','grid_step_pct',0.05,0.05,20)
           : p.grid_step_mode==='abs' ? paramRow('Шаг, $','grid_step_abs',1,0,1000000)
           : paramRow('Grid Spacing (ATR ×)','grid_spacing',0.1,0.1,10))
          + paramRow('Уровней лестницы','grid_levels',1,2,50)
          + paramRow('Нотионал (× капитала)','grid_notional_mult',0.1,0.1,10)
          + paramRow('Ре-анкор (0 = выкл)','grid_reanchor',0.25,0,5)
        : paramRow('Grid Spacing (ATR ×)','grid_spacing',0.1,0.1,10)
          + paramRow('Кол-во уровней','levels',1,2,30)
          + paramRow('Размер ордера ($)','order_usd',10,1,100000)
          + paramRow('Max ордеров','max_orders',1,2,50)}
      ${paramRow('EMA-фильтр','ema',1,5,200)}
      ${paramRow('Kill-switch просадка %','max_drawdown_pct',0.5,0,90)}
      ${p.mode==='avellaneda'?paramRow('γ (риск-аверсия)','as_gamma',0.05,0.05,3)+paramRow('κ (поток)','as_kappa',0.1,0.1,10):''}
      <div style="display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid #eef0ec;">
        <span style="font-size:12px;color:#5b635e;display:flex;align-items:center;">Расширение${qq('expansion')}</span>
        <select id="expSel" class="mono" style="width:104px;font-size:11px;background:#fff;border:1px solid #e3e6e0;border-radius:6px;padding:5px 8px;cursor:pointer;">
          ${['geometric','arithmetic','fibonacci'].map(x=>`<option value="${x}" ${p.expansion===x?'selected':''}>${x[0].toUpperCase()+x.slice(1)}</option>`).join('')}
        </select>
      </div>
    </div>
    <div style="padding:10px 14px 4px;">
      <button id="btnApply" data-tip="Сохранить параметры стратегии (режим, число уровней, размеры) в работающую сессию. Это НЕ запуск — запуск отдельной кнопкой «Старт стратегии» вверху." style="width:100%;background:${S._paramsDirty?'#C98A1A':'#1F8A5B'};color:#fff;border:0;border-radius:8px;padding:9px;font-size:12.5px;font-weight:600;cursor:pointer;">${S.streamMode==='live'?(S._paramsDirty?'Сохранить изменения':'Сохранить параметры'):'Пересчитать бэктест'}</button>
      <button id="btnRestoreGrid" class="hov" data-tip="Вернуть лимитные ордера на позиции по текущим параметрам (сбросить ручное перетаскивание на графике)." style="width:100%;margin-top:6px;background:#fff;border:1px solid #e3e6e0;border-radius:8px;padding:8px;font-size:12px;cursor:pointer;color:#5b635e;">Восстановить ордера по параметрам</button>
      <button id="btnSweep" class="hov" data-tip="Прогон по сетке шага k на этом инструменте: показывает лучший k. Сравни между инструментами — сходятся ⇒ параметр универсален." style="width:100%;margin-top:6px;background:#fff;border:1px solid #e3e6e0;border-radius:8px;padding:8px;font-size:12px;cursor:pointer;color:#3B82CE;">k-Sweep: тест универсальности</button>
      <button id="btnResetAcct" class="hov" data-tip="Сброс сессии: обнулить позиции, ордера, метрики и вернуть счёт к стартовому капиталу. Эквити и банк начнутся заново." style="width:100%;margin-top:6px;background:#fff;border:1px solid #f0c8c4;border-radius:8px;padding:8px;font-size:12px;cursor:pointer;color:#a93529;">Сброс счёта</button>
    </div>
    <div id="sweepBox" style="padding:0 14px;"></div>
    <div style="margin-top:auto;padding:10px 14px;">
      <div style="background:#fff;border:1px solid #e3e6e0;border-radius:10px;padding:11px;">
        <div style="font-size:10px;letter-spacing:.08em;color:#939a93;font-weight:600;margin-bottom:6px;">ТЕКУЩАЯ СЕТКА${S.manualGrid?' · ручная':''}</div>
        <div class="mono" style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:4px;"><span style="color:#5b635e;">Шаг</span><span>${step}</span></div>
        <div class="mono" style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:4px;"><span style="color:#5b635e;">Диапазон</span><span>${range}</span></div>
        <div class="mono" style="display:flex;justify-content:space-between;font-size:11px;"><span style="color:#5b635e;">Ордеров</span><span>${ordersCnt!=null?ordersCnt:gridPrices(sd?sd.last:0).length}</span></div>
      </div>
    </div>
  </div>`;
}

// ─────────────────────────── стакан + лента (order flow) ───────────────────────────
// Блок стакана из дизайна (лестница/стены/вспышки, спред, моя заявка+маркер, очередь,
// лента РЕАЛЬНЫХ сделок) на ЖИВЫХ данных кадра. Локальный rAF-easing для плавности
// (кадр сервера ~3/с, отрисовка ~30/с). Симуляция Math.random не используется.
function liveManual(){ return (S.streamMode==='live' && S.live && S.live.manual) ? S.live.manual : []; }

const OB_N = 16;   // слотов лестницы с каждой стороны (лишние клипуются CSS у спреда)

function obSideRow(side){
  const el=document.createElement('div'); el.className='row '+side; el._price=0;
  el.innerHTML='<div class="depthbar"></div><span class="wall-tag"></span><span class="px num"></span><span class="sz num"></span><span class="cum num"></span>';
  return {el, bar:el.querySelector('.depthbar'), px:el.querySelector('.px'), sz:el.querySelector('.sz'), cum:el.querySelector('.cum')};
}

function orderbookPanel(){
  const live = S.streamMode==='live' && S.live;
  return `
  <div id="obFlow" style="flex:0 0 300px;background:#fff;border-left:1px solid #e3e6e0;">
    <div style="padding:9px 12px 8px;border-bottom:1px solid #e3e6e0;display:flex;align-items:center;gap:8px;flex:none;">
      <span style="font-size:11px;letter-spacing:.1em;color:#939a93;font-weight:600;">СТАКАН</span>
      <span class="mono" style="font-size:10px;color:#5b635e;">${S.selInst}</span>
      <span style="margin-left:auto;display:flex;align-items:center;gap:5px;font-size:9px;letter-spacing:.05em;color:#939a93;font-weight:600;">
        <span style="width:7px;height:7px;border-radius:50%;background:${live?'#1F8A5B':'#cdd3cb'};"></span>${live?'LIVE':'—'}</span>
    </div>
    <div class="ob-stats">
      <div class="imb"><span class="lab">Дисбаланс</span>
        <div class="bar"><span class="g" id="obImbG" style="width:50%"></span><span class="r" id="obImbR" style="width:50%"></span></div>
        <span class="pct num" id="obImbPct">50%</span></div>
      <div class="depthrow"><span>Глубина bid <b class="num" id="obDepBid">—</b></span><span><b class="num" id="obDepAsk">—</b> ask</span></div>
    </div>
    <div class="colhead"><div>Цена</div><div class="r">Объём</div><div class="r">Σ</div></div>
    <div class="ladder">
      <div class="side asks" id="obAsks"></div>
      <div class="spread">
        <div class="sp-l"><span class="k">Спред</span><span class="v num" id="obSpVal">—</span></div>
        <div class="mid num" id="obSpMid"><span class="arrow" id="obSpArrow">▲</span><span id="obSpMidVal">—</span></div>
        <div class="sp-r"><span class="k">Последняя</span><span class="v num" id="obSpLast">—</span></div>
      </div>
      <div class="side bids" id="obBids"><div class="my-marker" id="obMyMarker"><div class="edge"></div><div class="badge">МОЯ</div></div></div>
      <div id="obWait" style="position:absolute;inset:0;display:${live?'none':'flex'};align-items:center;justify-content:center;text-align:center;color:#939a93;font-size:11px;padding:0 18px;background:#fff;">Стакан появится в режиме Live</div>
    </div>
    <div class="myorder off" id="obMyCard">
      <div class="mo-head"><span class="dot"></span><span class="ttl">Моя заявка</span>
        <span class="oside buy" id="obMoSide">BUY · LIMIT</span>
        <span class="status"><span class="s-dot"></span><span id="obMoStatus">В очереди</span></span></div>
      <div class="mo-px"><span class="big num" id="obMoPx">—</span><span class="sz">объём <b class="num" id="obMoSz">—</b></span><span class="ticks num" id="obMoTicks"></span></div>
      <div class="queue-labs"><span class="ql">≈ передо мной <span class="num" id="obQAhead">—</span></span><span class="ql me">я <span class="num" id="obQMine">—</span></span><span class="ql">за мной <span class="num" id="obQBehind">—</span></span></div>
      <div class="qbar"><span class="ahead" id="obQbAhead" style="width:33%"></span><span class="me" id="obQbMine" style="width:10%"></span><span class="behind" id="obQbBehind" style="width:57%"></span></div>
      <div class="qprog"><span class="est"><span style="font-family:'JetBrains Mono',monospace;">≈</span> оценка очереди</span><span class="pval">до головы <span id="obQProg">—</span></span></div>
    </div>
  </div>`;
}

// ── лента сделок (T&S) — отдельная колонка справа от стакана ──
// HTML ленты перенесён сюда из #obFlow; refs остаются по тем же id (obTape/obTf*).
function tapePanel(){
  const live = S.streamMode==='live' && S.live;
  return `
  <div id="obTapePanel" style="flex:0 0 210px;background:#fff;border-left:1px solid #e3e6e0;">
    <div style="padding:9px 12px 8px;border-bottom:1px solid #e3e6e0;display:flex;align-items:center;gap:8px;flex:none;">
      <span style="font-size:11px;letter-spacing:.1em;color:#939a93;font-weight:600;">ЛЕНТА</span>
      <span class="mono" style="font-size:10px;color:#5b635e;">T&amp;S · ${S.selInst}</span>
      <span style="margin-left:auto;display:flex;align-items:center;gap:5px;font-size:9px;letter-spacing:.05em;color:#939a93;font-weight:600;">
        <span style="width:7px;height:7px;border-radius:50%;background:${live?'#1F8A5B':'#cdd3cb'};"></span>${live?'LIVE':'—'}</span>
    </div>
    <div class="tapehead"><div>Время</div><div>Цена</div><div class="r">Объём</div></div>
    <div class="tape-list">
      <div id="obTape" class="tape-col"></div>
      <div id="obTapeWait" class="tape-wait" style="display:${live?'none':'flex'};">Лента появится в режиме Live</div>
    </div>
    <div class="tape-foot">
      <div class="tf-row"><span>Поток за 60с</span><span class="v"><span style="color:#1F8A5B;" id="obTfBuy">—</span> / <span style="color:#C4453A;" id="obTfSell">—</span></span></div>
      <div class="tf-bar"><span class="g" id="obTfG" style="width:50%"></span><span class="r" id="obTfR" style="width:50%"></span></div>
    </div>
  </div>`;
}

// ── формат цены/объёма по тику инструмента (не хардкод) ──
function priceDp(sample, tick){
  if(tick && tick>0){ return Math.max(0, Math.min(8, Math.ceil(-Math.log10(tick)-1e-9))); }
  const v=Math.abs(+sample||0); return v>=1000?1:v>=1?2:6;
}
function fmtP(v, dp){ v=+v||0; return v>=1000 ? v.toLocaleString('en-US',{minimumFractionDigits:dp,maximumFractionDigits:dp}) : v.toFixed(dp); }
function fmtQ(v){ v=Math.abs(+v||0); return v>=1000?v.toFixed(0):v>=100?v.toFixed(1):v>=1?v.toFixed(2):v.toFixed(3); }

// ── инициализация после каждого render(): собрать строки, ссылки, запустить цикл ──
function initBook(){
  const host=$('obFlow'); if(!host) return;
  const asksEl=$('obAsks'), bidsEl=$('obBids');
  const askRows=[], bidRows=[];
  for(let i=0;i<OB_N;i++){ const r=obSideRow('ask'); asksEl.appendChild(r.el); askRows.push(r); }
  for(let i=0;i<OB_N;i++){ const r=obSideRow('bid'); bidsEl.appendChild(r.el); bidRows.push(r); }
  S.book.refs={ askRows, bidRows, marker:$('obMyMarker'),
    imbG:$('obImbG'), imbR:$('obImbR'), imbPct:$('obImbPct'), depBid:$('obDepBid'), depAsk:$('obDepAsk'),
    spVal:$('obSpVal'), spMid:$('obSpMid'), spArrow:$('obSpArrow'), spMidVal:$('obSpMidVal'), spLast:$('obSpLast'),
    card:$('obMyCard'), moSide:$('obMoSide'), moStatus:$('obMoStatus'), moPx:$('obMoPx'), moSz:$('obMoSz'), moTicks:$('obMoTicks'),
    qAhead:$('obQAhead'), qMine:$('obQMine'), qBehind:$('obQBehind'),
    qbAhead:$('obQbAhead'), qbMine:$('obQbMine'), qbBehind:$('obQbBehind'), qProg:$('obQProg'),
    tape:$('obTape'), tapeWait:$('obTapeWait'), tfBuy:$('obTfBuy'), tfSell:$('obTfSell'), tfG:$('obTfG'), tfR:$('obTfR'), wait:$('obWait') };
  const onRow=(e)=>{ const row=e.target.closest('.row'); if(row && row._price>0) openTicketAtPrice(row._price); };
  asksEl.onclick=onRow; bidsEl.onclick=onRow;
  S.book.seen=new Set(); S.book._tapeInit=false;   // лента восстановится из текущего кадра
  if(!S.book._running){ S.book._running=true; obFrame(); }
}

function resetBook(){ S.book.asks.clear(); S.book.bids.clear(); S.book.my.aheadDisp=0; S.book.imbDisp=0.5; S.book.seen=new Set(); S.book._tapeInit=false; }

function obFrame(){
  if(!S.book._running) return;
  const now=performance.now();
  if(now-(S.book._t||0) >= 32){ S.book._t=now; try{ paintBook(); }catch(e){} }
  requestAnimationFrame(obFrame);
}

function easeSide(map, levels){
  const seen=new Set();
  for(const lv of levels){ const k=+lv[0]; seen.add(k); let e=map.get(k); if(!e){ e={target:+lv[1], disp:+lv[1]}; map.set(k,e);} else e.target=+lv[1]; }
  for(const [k,e] of map){ if(!seen.has(k)) e.target=0; e.disp += (e.target-e.disp)*0.22; if(!seen.has(k) && e.disp<1e-4) map.delete(k); }
}

function paintBook(){
  const R=S.book.refs; if(!R) return;
  const live = S.streamMode==='live' && S.live;
  const ob = live ? S.live.orderbook : null;
  if(!ob || !ob.a || !ob.b || !ob.a.length || !ob.b.length){
    if(R.wait) R.wait.style.display='flex'; if(R.card) R.card.classList.add('off');
    if(R.marker) R.marker.style.opacity='0';
    return;
  }
  if(R.wait) R.wait.style.display='none';
  const aLv=ob.a.slice(0,OB_N), bLv=ob.b.slice(0,OB_N);
  easeSide(S.book.asks, aLv); easeSide(S.book.bids, bLv);
  const aDisp=aLv.map(lv=>(S.book.asks.get(+lv[0])||{disp:0}).disp);
  const bDisp=bLv.map(lv=>(S.book.bids.get(+lv[0])||{disp:0}).disp);
  let maxSz=1; for(const d of aDisp) if(d>maxSz)maxSz=d; for(const d of bDisp) if(d>maxSz)maxSz=d;
  const aSum=aDisp.reduce((s,d)=>s+d,0), bSum=bDisp.reduce((s,d)=>s+d,0);
  const wallThr=Math.max(maxSz*0.6, (aSum+bSum)/((aLv.length+bLv.length)||1)*3.2);
  const tick=(live && S.live.tick)?+S.live.tick:null;
  const dpP=priceDp(+ob.a[0][0], tick);
  // аски: лучшая (a[0]) внизу у спреда, дальше — вверх
  const N=R.askRows.length, K=aLv.length;
  let cum=0; const aCum=aDisp.map(d=>(cum+=d));
  for(let r=0;r<N;r++){
    const j=N-1-r, row=R.askRows[r];
    if(j<0||j>=K){ row.el.classList.add('empty'); row.el._price=0; continue; }
    row.el.classList.remove('empty');
    obPaintRow(row,+aLv[j][0],aDisp[j],aCum[j],maxSz,wallThr,dpP);
  }
  // биды: лучшая (b[0]) сверху у спреда, дальше — вниз
  let cb=0;
  for(let r=0;r<R.bidRows.length;r++){
    const row=R.bidRows[r];
    if(r>=bLv.length){ row.el.classList.add('empty'); row.el._price=0; continue; }
    row.el.classList.remove('empty'); cb+=bDisp[r];
    obPaintRow(row,+bLv[r][0],bDisp[r],cb,maxSz,wallThr,dpP);
  }
  // спред-зона
  const bestAsk=+ob.a[0][0], bestBid=+ob.b[0][0], mid=(bestAsk+bestBid)/2, spread=bestAsk-bestBid;
  const last=+S.live.price||mid;
  const tp=S.live.tape;
  let up=S.book.lastUp; if(tp && tp.length){ up=String(tp[tp.length-1].side).toLowerCase()==='buy'; } S.book.lastUp=up;
  const bidCol='#1F8A5B', askCol='#C4453A';
  R.spMidVal.textContent=fmtP(mid,dpP);
  R.spVal.textContent=spread.toFixed(dpP)+' · '+(mid?(spread/mid*100).toFixed(3):'0.000')+'%';
  R.spArrow.textContent=up?'▲':'▼'; R.spArrow.style.color=up?bidCol:askCol;
  R.spMid.style.color=up?bidCol:askCol;
  R.spLast.textContent=fmtP(last,dpP); R.spLast.style.color=up?bidCol:askCol;
  // дисбаланс/глубина
  const imbT=bSum/((bSum+aSum)||1);
  S.book.imbDisp += (imbT-S.book.imbDisp)*0.12;
  const g=Math.round(S.book.imbDisp*100);
  R.imbG.style.width=g+'%'; R.imbR.style.width=(100-g)+'%';
  R.imbPct.textContent=(g>=50?g:100-g)+'%'; R.imbPct.style.color=g>=50?bidCol:askCol;
  R.depBid.textContent=fmtQ(bSum); R.depAsk.textContent=fmtQ(aSum);
  // моя заявка + лента
  obPaintMyOrder(R,mid,dpP,tick);
  obPaintTape(R);
}

function obPaintRow(row,price,disp,cum,maxSz,wallThr,dpP){
  row.el._price=price;
  row.px.textContent=fmtP(price,dpP);
  row.sz.textContent=fmtQ(disp);
  row.cum.textContent=fmtQ(cum);
  row.bar.style.width=Math.max(1,Math.min(100,disp/maxSz*100))+'%';
  row.el.classList.toggle('wall', disp>=wallThr);
}

function obClearMine(R){ for(const r of R.askRows) r.el.classList.remove('mine'); for(const r of R.bidRows) r.el.classList.remove('mine'); }

function obPaintMyOrder(R,mid,dpP,tick){
  const mo=S.live && S.live.my_order;
  if(!mo){ R.card.classList.add('off'); R.marker.style.opacity='0'; obClearMine(R); return; }
  R.card.classList.remove('off');
  const buy=mo.side==='buy';
  R.moSide.textContent=(buy?'BUY':'SELL')+' · LIMIT'; R.moSide.className='oside '+(buy?'buy':'sell');
  const stMap={queue:'В очереди',exec:'Исполняется',filled:'Исполнена',moving:'Переставляется',halted:'Стоп (kill-switch)'};
  R.moStatus.textContent=stMap[mo.status]||'В очереди';
  R.moPx.textContent=fmtP(mo.price,dpP); R.moSz.textContent=fmtQ(mo.size);
  if(tick && tick>0){ R.moTicks.textContent=Math.round(Math.abs(mid-mo.price)/tick)+' тиков от рынка'; }
  else { R.moTicks.textContent=fmtP(Math.abs(mid-mo.price),dpP)+' от рынка'; }
  let ahead=(mo.queue_ahead>0?mo.queue_ahead:mo.ahead)||0;
  const me=+mo.size||0, behind=+mo.behind||0;
  S.book.my.aheadDisp += (ahead-S.book.my.aheadDisp)*0.18;
  const ad=Math.max(0,S.book.my.aheadDisp), tot=(ad+me+behind)||1;
  R.qbAhead.style.width=(ad/tot*100)+'%'; R.qbMine.style.width=(me/tot*100)+'%'; R.qbBehind.style.width=(behind/tot*100)+'%';
  R.qAhead.textContent=fmtQ(ad); R.qMine.textContent=fmtQ(me); R.qBehind.textContent=fmtQ(behind);
  R.qProg.textContent=Math.round(Math.max(0,Math.min(100,(1-ad/tot)*100)))+'%';
  // подсветка моей строки в лестнице + скользящий маркер (для buy)
  obClearMine(R);
  const rows=buy?R.bidRows:R.askRows; let best=null,bd=1e18;
  for(const r of rows){ if(r.el.classList.contains('empty')||!r.el._price) continue; const d=Math.abs(r.el._price-mo.price); if(d<bd){bd=d;best=r;} }
  if(best) best.el.classList.add('mine');
  if(buy && best){ R.marker.style.opacity='1'; R.marker.style.top=best.el.offsetTop+'px'; }
  else R.marker.style.opacity='0';
}

function obFlash(R,price,side){
  const rows=side==='buy'?R.bidRows:R.askRows; let best=null,bd=1e18;
  for(const r of rows){ if(!r.el._price) continue; const d=Math.abs(r.el._price-price); if(d<bd){bd=d;best=r;} }
  if(best){ const cls=side==='buy'?'flash-bid':'flash-ask'; best.el.classList.remove('flash-ask','flash-bid'); void best.el.offsetWidth; best.el.classList.add(cls); }
}

function obPaintTape(R){
  const tp=S.live && S.live.tape; if(!tp || !tp.length) return;
  if(R.tapeWait && R.tapeWait.style.display!=='none') R.tapeWait.style.display='none';
  if(!S.book.seen) S.book.seen=new Set();
  const fresh=[];
  for(const t of tp){ const k=t.ts+'|'+t.price+'|'+t.size; if(!S.book.seen.has(k)){ S.book.seen.add(k); fresh.push(t); } }
  if(S.book.seen.size>500) S.book.seen=new Set(Array.from(S.book.seen).slice(-250));
  const sizes=tp.map(t=>+t.size).filter(s=>s>0); const avg=sizes.length?sizes.reduce((a,b)=>a+b,0)/sizes.length:0; const bigThr=avg*4;
  const tick=(S.live.tick)?+S.live.tick:null; const dpP=priceDp(+tp[tp.length-1].price,tick);
  const noAnim=!S.book._tapeInit;   // первый показ после render(): пачкой, без анимации и вспышек
  for(const t of fresh){ obAddTrade(R,t,bigThr,dpP,noAnim); if(!noAnim) obFlash(R,+t.price,String(t.side).toLowerCase()==='buy'?'buy':'sell'); }
  S.book._tapeInit=true;
  while(R.tape.children.length>30) R.tape.lastChild.remove();
  const nowTs=tp[tp.length-1].ts; let buy=0,sell=0;
  for(const t of tp){ if(nowTs-t.ts>60000) continue; if(String(t.side).toLowerCase()==='buy') buy+=+t.size; else sell+=+t.size; }
  const tt=(buy+sell)||1, bp=Math.round(buy/tt*100);
  R.tfG.style.width=bp+'%'; R.tfR.style.width=(100-bp)+'%'; R.tfBuy.textContent=bp+'%'; R.tfSell.textContent=(100-bp)+'%';
}

function obAddTrade(R,t,bigThr,dpP,noAnim){
  const side=String(t.side).toLowerCase()==='buy'?'buy':'sell';
  const big=bigThr>0 && (+t.size)>=bigThr;
  const d=new Date(t.ts), p2=n=>String(n).padStart(2,'0');
  const row=document.createElement('div');
  row.className='trade '+side+(big?' big':'')+(noAnim?'':' enter');
  row.innerHTML='<span class="tt">'+p2(d.getHours())+':'+p2(d.getMinutes())+':'+p2(d.getSeconds())+'</span>'+
    '<span class="pp">'+fmtP(+t.price,dpP)+'</span><span class="ss">'+fmtQ(+t.size)+'</span>';
  R.tape.insertBefore(row,R.tape.firstChild);
  if(!noAnim) setTimeout(()=>row.classList.remove('enter'),500);
}

// ─────────────────────────── обработчики ───────────────────────────
function bind(){
  document.querySelectorAll('[data-inst]').forEach(el=>el.onclick=()=>selectInst(el.dataset.inst));
  document.querySelectorAll('[data-tab]').forEach(el=>el.onclick=()=>{S.tab=el.dataset.tab; saveUI(); render();});
  document.querySelectorAll('[data-spd]').forEach(el=>el.onclick=()=>{S.speed=+el.dataset.spd; render();});
  document.querySelectorAll('[data-mode]').forEach(el=>el.onclick=()=>{S.params.mode=el.dataset.mode; S._activePreset=null; saveUI(); resetGrid(); render();});
  const sm=$('stepModeSel'); if(sm) sm.onchange=()=>{
    S.params.grid_step_mode=sm.value; S._activePreset=null; S._paramsDirty=true;
    saveUI(); render(); setParamsBtnDirty(true);
  };
  document.querySelectorAll('[data-preset]').forEach(el=>el.onclick=()=>applyPreset(el.dataset.preset));
  document.querySelectorAll('[data-param]').forEach(el=>el.onchange=()=>{
    const k=el.dataset.param; const v=parseFloat(el.value);
    if(!isFinite(v)) return;
    S.params[k] = (k==='levels'||k==='ema'||k==='max_orders')?Math.round(v):v;
    S._activePreset=null; document.querySelectorAll('[data-preset]').forEach(b=>b.setAttribute('style',presetStyle(b.dataset.preset)));  // ручная правка → пресет больше не «активен»
    saveUI(); resetGrid();        // без полного render(), иначе пересоздаётся кнопка под кликом
    S._paramsDirty=true; const b=$('btnApply'); if(b){ b.textContent='Сохранить изменения'; b.style.background='#C98A1A'; }
    setParamsBtnDirty(true);      // продублировать индикатор на топбар (drawer может быть закрыт)
  });
  const tf=$('tfSel'); if(tf) tf.onchange=async()=>{
    S.tf=tf.value; S.zoomBars=null; S.yZoom=1; saveUI();
    const wasLive=S.streamMode==='live';
    await loadInit();
    if(wasLive) startStream('live');   // переподключить стрим на новом ТФ
  };
  const exp=$('expSel'); if(exp) exp.onchange=()=>{S.params.expansion=exp.value; saveUI(); resetGrid();};
  const pb=$('paperBtn'); if(pb) pb.onclick=()=>{S.paper=!S.paper; if(!S.paper && !S.cfg.has_testnet_keys) toast('Testnet требует ключи в .env — работаю в Paper'); render();};
  bindBtn('leftCollapse',()=>{S.leftCollapsed=true; saveUI(); render();});
  bindBtn('leftExpand',()=>{S.leftCollapsed=false; saveUI(); render();});
  bindBtn('bottomToggle',()=>{S.bottomCollapsed=!S.bottomCollapsed; saveUI(); render();});
  (()=>{ const h=$('bottomResize'); if(!h) return;       // перетаскивание верхней кромки → высота журнала
    h.onmousedown=(e)=>{ e.preventDefault();
      const startY=e.clientY, startH=S.bottomH, panel=$('bottomPanel');
      const move=(ev)=>{ let nh=startH+(startY-ev.clientY); nh=Math.max(80,Math.min(600,nh));
        S.bottomH=nh; if(panel) panel.style.flexBasis=nh+'px'; drawAll(); };
      const up=()=>{ document.removeEventListener('mousemove',move); document.removeEventListener('mouseup',up); saveUI(); };
      document.addEventListener('mousemove',move); document.addEventListener('mouseup',up); };
  })();
  bindBtn('btnStart',runBacktest);
  bindBtn('btnApply',()=>{
    if(S.streamMode==='live'){
      if(!S.ws || S.ws.readyState!==1){ toast('Нет соединения с Live'); return; }
      S.paramsSynced=true;                                   // наши параметры — приоритет
      S.ws.send(JSON.stringify({action:'apply_params', params:S.params}));
      S._paramsDirty=false; setParamsBtnDirty(false);
      const b=$('btnApply'); if(b){ b.textContent='Сохранено'; b.style.background='#1F8A5B'; setTimeout(()=>{const x=$('btnApply'); if(x){x.textContent='Сохранить параметры';}},1400); }
      toast(`Параметры сохранены: ${S.params.mode==='avellaneda'?'A-S':'эвристика'}, ${S.params.levels} ур.`);
    } else { runBacktest(); }
  });
  bindBtn('btnReplay',()=>startStream('replay'));
  bindBtn('btnLive',()=>startStream('live'));
  bindBtn('btnStop',()=>stopAll());
  bindBtn('btnStrat',()=>{
    if(S.streamMode!=='live' || !S.ws || S.ws.readyState!==1){ toast('Сначала включи Live (● Live)'); return; }
    const on=stratOn();
    S.ws.send(JSON.stringify({action: on?'stop_strategy':'start'}));
    toast(on?'Стратегия остановлена — сеточные ордера сняты':`Стратегия запущена — многоуровневая сетка (${S.params.levels} ур.) выставлена`);
  });
  bindBtn('btnNewAcct',()=>{
    if(S.streamMode!=='live' || !S.ws || S.ws.readyState!==1){ toast('Сначала включи Live (● Live)'); return; }
    const v=parseFloat(String(($('acctCap')||{}).value||'').replace(/[\s,$]/g,''));
    if(!(v>0)){ toast('Укажи сумму счёта'); return; }
    showConfirm('Новый счёт на $'+v.toFixed(2)+'?',
      'Текущие позиции, ордера и история будут сброшены. Капитал распределится по корзине заново.',
      ()=>{ S.ws.send(JSON.stringify({action:'reset_account',capital:v})); toast('Новый счёт: $'+v.toFixed(2)); },
      {okLabel:'Создать счёт', danger:true});
  });
  bindBtn('btnRestoreGrid',()=>{resetGrid(); render(); toast('Ордера возвращены по параметрам');});
  bindBtn('btnSweep',doSweep);
  bindBtn('btnResetAcct',()=>{
    if(S.streamMode!=='live' || !S.ws || S.ws.readyState!==1){ toast('Сначала включи Live (● Live)'); return; }
    const def=S.cfg?S.cfg.start_capital:1000;
    showConfirm('Сбросить счёт к $'+def.toFixed(2)+'?',
      'Позиции, ордера, история и метрики сессии будут обнулены. Эквити и банк начнутся заново от стартового капитала.',
      ()=>{ S.ws.send(JSON.stringify({action:'reset_account',capital:def})); toast('Счёт сброшен к $'+def.toFixed(2)); },
      {okLabel:'Сбросить', danger:true});
  });
  bindBtn('btnExportCsv',()=>{
    if(S.streamMode==='live'){ window.location='/api/live/export?fmt=csv&kind=events'; return; }
    if(!S.tradingStarted){toast('Сначала запусти прогон'); return;}
    window.location='/api/export?fmt=csv&kind=trades';
  });
  bindBtn('btnExportFills',()=>{
    if(S.streamMode==='live'){ window.location='/api/live/export?fmt=csv&kind=fills'; return; }
    toast('Лог филлов доступен в Live-режиме');
  });
  bindBtn('btnExportJson',()=>{
    if(S.streamMode==='live'){ window.location='/api/live/export?fmt=json'; return; }
    if(!S.tradingStarted){toast('Сначала запусти прогон'); return;}
    window.location='/api/export?fmt=json';
  });
  bindBtn('btnParams',toggleParams);
  bindBtn('paramsClose',()=>setParams(false));
  bindBtn('paramsBackdrop',()=>setParams(false));
  if(!S._paramsKeyBound){ S._paramsKeyBound=true;
    document.addEventListener('keydown',(e)=>{ if(e.key==='Escape' && S.paramsOpen && !$('confirmModal')) setParams(false); }); }
  attachChartTools();
  initBook();
}
function bindBtn(id,fn){ const e=$(id); if(e) e.onclick=fn; }

// выдвижная панель параметров: open/close через класс-стиль (CSS-анимация, без render → без рефлоу графика)
function setParams(open){
  S.paramsOpen=!!open; saveUI();
  const d=$('paramsDrawer'), b=$('paramsBackdrop'), btn=$('btnParams');
  if(d) d.style.transform='translateX('+(open?'0':'100%')+')';
  if(b){ b.style.opacity=open?'1':'0'; b.style.pointerEvents=open?'auto':'none'; }
  if(btn) btn.setAttribute('aria-expanded',open?'true':'false');
}
function toggleParams(){ setParams(!S.paramsOpen); }
// индикатор «есть несохранённые параметры» на кнопке топбара (панель может быть скрыта в drawer)
function setParamsBtnDirty(on){ const b=$('btnParams'); if(!b) return;
  b.textContent='Параметры'+(on?' ●':'');
  b.style.border='1px solid '+(on?'#e6c98a':'#e3e6e0');
  b.style.background=on?'#fdf7ea':'#fff'; b.style.color=on?'#97681a':'#14181a';
}

// ─────────────────────────── тултипы (делегирование, один раз) ───────────────────────────
function setupTooltips(){
  const show=(t)=>{ const tip=t.getAttribute('data-tip'); if(!tip) return; const el=$('tipEl'); el.textContent=tip;
    const r=t.getBoundingClientRect(); el.style.display='block'; const w=el.offsetWidth||220;
    let left=r.left-w-10; if(left<8) left=r.right+10; if(left+w>window.innerWidth-8) left=window.innerWidth-w-8;
    let top=r.top - 4; if(top+el.offsetHeight>window.innerHeight-8) top=window.innerHeight-el.offsetHeight-8;
    el.style.left=Math.max(8,left)+'px'; el.style.top=Math.max(8,top)+'px'; };
  document.addEventListener('mouseover',e=>{const t=e.target.closest('[data-tip]'); if(t){ S._tipT=t;
    clearTimeout(S._tipTimer); S._tipTimer=setTimeout(()=>{ if(S._tipT===t) show(t); }, 450); }});
  document.addEventListener('mouseout',e=>{const t=e.target.closest('[data-tip]'); if(t){ S._tipT=null;
    clearTimeout(S._tipTimer); $('tipEl').style.display='none'; }});
}
// спрятать тултип, только если наведённый элемент исчез из DOM (после перерисовки)
function hideTipIfStale(){ const te=$('tipEl'); if(!te) return;
  if(!S._tipT || !document.body.contains(S._tipT)){ te.style.display='none'; S._tipT=null; } }

// ───────── инструменты графика: координаты через Lightweight Charts (никакой ручной математики) ─────────
function yToPrice(y){ if(!S._cs) return null; const p=S._cs.coordinateToPrice(y); return (p==null)?null:p; }
function priceToY(p){ if(!S._cs) return null; const y=S._cs.priceToCoordinate(p); return (y==null)?null:y; }
function chartRect(){ const h=$('lwchart'); return h?h.getBoundingClientRect():null; }

// attachChartTools вызывается после каждого render() — (пере)создаёт график LWC на новом контейнере
function attachChartTools(){ initChart(); }

function lwReady(){ return typeof window!=='undefined' && window.LightweightCharts; }

function chartTime(sd,i){
  if(sd && sd.times && sd.times[i]!=null) return sd.times[i];
  return i;
}
function chartCandleData(sd){
  return sd.candles.map((c,i)=>({time:chartTime(sd,i), open:c[0], high:c[1], low:c[2], close:c[3]}));
}

function initChart(){
  const host=$('lwchart'); if(!host) return;
  if(!lwReady()){ if(!host._warned){ host._warned=1; host.innerHTML='<div style="padding:20px;color:#a93529;font-size:12px;">График не загрузился (нет доступа к CDN lightweight-charts). Проверь интернет/прокси.</div>'; } return; }
  if(S._chart){ try{S._chart.remove();}catch(e){} }
  const LC=window.LightweightCharts;
  const ch=LC.createChart(host,{
    autoSize:true,
    layout:{ background:{type:'solid',color:'#fafbf9'}, textColor:'#5b635e', fontFamily:"'JetBrains Mono', monospace", fontSize:10 },
    grid:{ vertLines:{color:'#eef0ec'}, horzLines:{color:'#eef0ec'} },
    rightPriceScale:{ borderColor:'#e3e6e0' },
    timeScale:{ borderColor:'#e3e6e0', timeVisible:true, secondsVisible:false, rightOffset:4 },
    crosshair:{ mode:LC.CrosshairMode.Normal, vertLine:{labelBackgroundColor:'#C98A1A'}, horzLine:{labelBackgroundColor:'#C98A1A'} },
    handleScroll:true, handleScale:true,
  });
  const cs=ch.addCandlestickSeries({ upColor:'#1F8A5B', downColor:'#C4453A',
    borderUpColor:'#1F8A5B', borderDownColor:'#C4453A', wickUpColor:'#1F8A5B', wickDownColor:'#C4453A',
    priceLineColor:'#C98A1A', priceLineStyle:2, lastValueVisible:true });
  S._chart=ch; S._cs=cs; S._chartReady=false;
  // Постановка лимитки кликом по графику. Раньше её тут не было намеренно: на графике
  // зажатая кнопка мыши — это прокрутка, и обработчик клика конфликтовал с ней. Отличаем
  // одно от другого по факту: КЛИК — это нажатие без сдвига (< 5 px) и не дольше 400 мс.
  // Всё, что длиннее или со сдвигом, остаётся протяжкой графика и до нас не доходит.
  if(!host._placeBound){
    host._placeBound=1;
    let dn=null;
    host.addEventListener('mousedown',(e)=>{ if(e.button!==0) return; dn={x:e.clientX,y:e.clientY,t:Date.now()}; });
    host.addEventListener('mouseup',(e)=>{
      if(!dn) return; const d=dn; dn=null;
      if(Math.abs(e.clientX-d.x)>4 || Math.abs(e.clientY-d.y)>4) return;   // это была протяжка
      if(Date.now()-d.t>400) return;                                        // это было удержание
      const r=chartRect(); if(!r) return;
      const pr=yToPrice(e.clientY-r.top);
      if(pr>0) openDraft(pr);
    });
  }
  ch.timeScale().subscribeVisibleLogicalRangeChange(()=>{
    repositionLines(); repositionDraft();
  });
  ensureChartOverlays($('priceWrap'));
  startRepositionLoop();
  updateChart();
}

// перерисовка свечей/маркеров на каждый кадр; ордер-лайны — DOM-оверлеи
function updateChart(){
  if(!S._cs){ initChart(); if(!S._cs) return; }
  const sd=selData(); if(!sd||!sd.candles||!sd.candles.length) return;
  try{ S._cs.setData(chartCandleData(sd)); }catch(e){ return; }
  // метки сделок — на ТОЧНОЙ цене и времени (DOM-оверлей), не привязаны к бару
  S._marks=(sd.markers||[]).map(t=>({ time:chartTime(sd,t.i), price:t.price, side:t.side }))
    .filter(m=>m.time!=null && m.price>0);
  try{ S._cs.setMarkers([]); }catch(e){}
  if(!S._chartReady){ S._chartReady=true; try{S._chart.timeScale().fitContent();}catch(e){} }
  renderMarks(); repositionMarks();
  renderChartLines(); repositionDraft();
}

// метки сделок (только МЕЙКЕР-филлы сетки/ручных лимиток), закреплённые на (время, цена) филла.
// Тейкерные закрытия (ликвидация/стоп/пауза) меток не получают — они в журнале.
function renderMarks(){
  const c=$('obMarks'); if(!c) return;
  const marks=S._marks||[];
  c.innerHTML=marks.map((m,i)=>{ const buy=m.side==='buy'; const col=buy?'#1F8A5B':'#C4453A';
    const t=new Date(m.time*1000).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
    const tip=(buy?'Покупка (мейкер)':'Продажа (мейкер)')+' @ '+fmtPx(m.price)+' · '+t;
    return `<div data-mi="${i}" data-tip="${tip}" style="position:absolute;transform:translate(-50%,-50%);font-size:13px;line-height:1;color:${col};display:none;pointer-events:auto;cursor:help;text-shadow:0 0 2px #fff,0 0 2px #fff,0 0 3px #fff;">${buy?'▲':'▼'}</div>`; }).join('');
}
function repositionMarks(){
  const c=$('obMarks'); if(!c||!S._chart||!S._cs) return;
  const ts=S._chart.timeScale(); const marks=S._marks||[];
  for(const el of c.children){ const m=marks[+el.dataset.mi]; if(!m){ el.style.display='none'; continue; }
    const x=ts.timeToCoordinate(m.time); const y=priceToY(m.price);
    if(x==null||y==null){ el.style.display='none'; } else { el.style.display='block'; el.style.left=x+'px'; el.style.top=y+'px'; } }
}

// лёгкий rAF-цикл: держит ордер-лайны на своих ценах при зуме/прокрутке/автомасштабе
function startRepositionLoop(){
  if(S._reLoop) return;
  const tick=()=>{ S._reLoop=requestAnimationFrame(tick); if(S._chart){ repositionLines(); repositionDraft(); repositionMarks(); } };
  S._reLoop=requestAnimationFrame(tick);
}
function repositionLines(){
  const c=$('obOrders'); if(!c) return;
  for(const row of c.children){ const pr=parseFloat(row.dataset.price);
    const y=priceToY(pr); if(y==null){ row.style.display='none'; } else { row.style.display='block'; row.style.top=y+'px'; } }
}

function ensureChartOverlays(wrap){
  if(getComputedStyle(wrap).position==='static') wrap.style.position='relative';
  // контейнер для уже стоящих ручных лимиток (DOM-линии с ✕ и перетаскиванием)
  if(!$('obOrders')){ const c=document.createElement('div'); c.id='obOrders';
    c.style.cssText='position:absolute;inset:0;pointer-events:none;z-index:7;overflow:hidden;'; wrap.appendChild(c); }
  // контейнер меток сделок — закреплены на точной цене+времени (не едут со свечой)
  if(!$('obMarks')){ const c=document.createElement('div'); c.id='obMarks';
    c.style.cssText='position:absolute;inset:0;pointer-events:none;z-index:6;overflow:hidden;'; wrap.appendChild(c); }
  // черновик новой лимитки (инлайн-линия как в Bybit)
  if(!$('obDraft')){
    const d=document.createElement('div'); d.id='obDraft';
    d.style.cssText='position:absolute;left:0;right:0;height:0;display:none;z-index:9;border-top:1px dashed #1F8A5B;pointer-events:none;';
    // панель управления справа, у шкалы цены — как в Bybit
    d.innerHTML=`<div style="position:absolute;right:62px;top:-13px;height:26px;display:flex;align-items:center;pointer-events:auto;font-family:'Space Grotesk',sans-serif;">
      <span id="obGrip" title="Тяни по вертикали — задать цену" style="cursor:ns-resize;color:#9aa29a;font-size:14px;padding:0 5px;user-select:none;">⠿</span>
      <input id="obQty" placeholder="Сумма $" title="Сумма ордера в долларах" style="width:84px;height:22px;border:1px solid #cdd3cb;border-radius:5px;font-size:11px;text-align:right;padding:0 7px;outline:none;background:#fff;font-family:'JetBrains Mono',monospace;">
      <span id="obTag" title="Кликни — поставить лимитку" style="cursor:pointer;margin-left:5px;height:22px;display:flex;align-items:center;padding:0 10px;border-radius:5px;color:#fff;font-size:11px;font-weight:600;white-space:nowrap;box-shadow:0 1px 5px rgba(0,0,0,.14);"></span>
      <span id="obX" title="Отменить" style="cursor:pointer;width:22px;height:22px;margin-left:4px;display:flex;align-items:center;justify-content:center;background:#fff;border:1px solid #cdd3cb;border-radius:5px;color:#5b635e;font-size:11px;">✕</span>
    </div>`;
    wrap.appendChild(d);
    $('obX').onclick=(ev)=>{ev.stopPropagation();ev.preventDefault();hideDraft();};
    $('obTag').onclick=(ev)=>{ev.stopPropagation();ev.preventDefault();submitDraft();};
    $('obGrip').onmousedown=(ev)=>startDraftDrag(ev,wrap);
    $('obQty').onmousedown=(ev)=>ev.stopPropagation();
  }
}
function showGuide(){}   // направляющая больше не нужна — лимитка ставится кликом по графику
function hideGuide(){ const g=$('obGuide'); if(g) g.style.display='none'; }

function defaultUsd(){ const v=+S.params.order_usd; return v>0?Math.round(v):80; }   // размер ордера в $ по умолчанию
// ── черновик новой лимитки (инлайн как в Bybit): тянешь по вертикали, кликаешь ярлык — ставишь ──
function openTicketAtPrice(price){ openDraft(price); }
function openDraft(price){
  if(S.streamMode!=='live'){ toast('Ручные ордера — в режиме Live'); return; }
  if(!(price>0)) return;
  ensureChartOverlays($('priceWrap'));
  S._draft={price:price, side:'buy'};
  const q=$('obQty'); if(q) q.value=String(defaultUsd());   // сумма ордера в долларах
  repositionDraft();
}
function repositionDraft(){
  const d=$('obDraft'); if(!d) return;
  if(!S._draft){ d.style.display='none'; return; }
  const y=priceToY(S._draft.price); if(y==null){ d.style.display='none'; return; }
  const last=(S.live&&S.live.price)||S._draft.price;
  const side=S._draft.price<=last?'buy':'sell'; S._draft.side=side;
  const col=side==='buy'?'#1F8A5B':'#C4453A';
  d.style.display='block'; d.style.top=y+'px'; d.style.borderTopColor=col;
  const tag=$('obTag'); if(tag){ tag.style.background=col;
    tag.textContent=(side==='buy'?'Лимитная покупка ':'Лимитная продажа ')+fmtPx(S._draft.price); }
}
function startDraftDrag(e){ e.preventDefault(); e.stopPropagation();
  const move=(ev)=>{ if(!S._draft) return; const r=chartRect(); if(!r) return;
    const pr=yToPrice(ev.clientY-r.top); if(pr>0){ S._draft.price=pr; repositionDraft(); } };
  const up=()=>{ window.removeEventListener('mousemove',move); window.removeEventListener('mouseup',up); };
  window.addEventListener('mousemove',move); window.addEventListener('mouseup',up);
}
function submitDraft(){
  if(!S._draft) return; const side=S._draft.side, price=S._draft.price;
  const usd=parseFloat(String(($('obQty')||{}).value||'').replace(/[\s,$]/g,''));
  if(!(price>0)||!(usd>0)){ toast('Укажи сумму ордера ($)'); return; }
  const qty=usd/price;   // движок работает в монете: qty = сумма / цена
  if(S.ws&&S.ws.readyState===1){ S.ws.send(JSON.stringify({action:'place_order',symbol:S.selInst,side,price,qty}));
    toast(`Лимитка ${side==='buy'?'Buy':'Sell'} $${usd.toFixed(0)} @ ${fmtPx(price)} поставлена`); }
  hideDraft();
}
function hideDraft(){ S._draft=null; const d=$('obDraft'); if(d) d.style.display='none'; }

// ── ордер-лайны как в Bybit: панель управления у правой шкалы цены ──
// rows: превью-сетка (до старта, перетаскиваемая), живая сетка (после старта), ручные лимитки (✕ + drag)
function renderChartLines(){
  const c=$('obOrders'); if(!c) return;
  if(S._ordDrag || S._gridDrag) return;       // не пересобирать во время перетаскивания
  c.innerHTML='';
  const mkRow=(price,color,html)=>{ const row=document.createElement('div'); row.dataset.price=price;
    const y=priceToY(price); row.style.cssText=`position:absolute;left:0;right:0;top:${y==null?-9999:y}px;height:0;border-top:1.5px solid ${color};pointer-events:none;`;
    row.innerHTML=`<div style="position:absolute;right:62px;top:-11px;height:22px;display:flex;align-items:center;pointer-events:auto;font-family:'Space Grotesk',sans-serif;">${html}</div>`;
    c.appendChild(row); return row; };

  const started=stratOn();
  // 1) ручные лимитки — приоритетно (перетаскивание + отмена)
  liveManual().forEach(o=>{ const buy=o.side==='buy'; const col=buy?'#1F8A5B':'#C4453A';
    const usd=Math.round((+o.size)*o.price);
    const compact=`${buy?'Buy':'Sell'} $${usd}`;                         // компактный вид (в покое)
    const full=`${buy?'Лимит Buy':'Лимит Sell'} $${usd} @ ${fmtPx(o.price)}`; // полный (при наведении)
    const row=mkRow(o.price,col,
      `<span class="g" style="cursor:ns-resize;background:${col};color:#fff;height:22px;display:flex;align-items:center;padding:0 8px;border-radius:5px 0 0 5px;font:600 10px 'JetBrains Mono',monospace;white-space:nowrap;">${compact}</span>`+
      `<span class="x" title="Отменить лимитку" style="cursor:pointer;background:#fff;border:1px solid ${col};border-left:0;color:${col};height:22px;width:22px;display:flex;align-items:center;justify-content:center;border-radius:0 5px 5px 0;font-size:11px;">✕</span>`);
    row.querySelector('.x').onclick=(ev)=>{ev.stopPropagation();ev.preventDefault();cancelManual(o.oid);};
    const grip=row.querySelector('.g');
    grip.onmouseenter=()=>{ if(!S._ordDrag) grip.textContent=full; };   // hover-expand
    grip.onmouseleave=()=>{ if(!S._ordDrag) grip.textContent=compact; };
    grip.onmousedown=(ev)=>{ ev.preventDefault();ev.stopPropagation(); S._ordDrag=true; let np=o.price;
      const move=(e2)=>{ const r=chartRect(); if(!r) return; const pr=yToPrice(e2.clientY-r.top);
        if(pr>0){ np=pr; const ny=priceToY(pr); if(ny!=null) row.style.top=ny+'px'; grip.textContent=`${buy?'Лимит Buy':'Лимит Sell'} $${Math.round((+o.size)*pr)} @ ${fmtPx(pr)}`; } };
      const up=()=>{ window.removeEventListener('mousemove',move); window.removeEventListener('mouseup',up); S._ordDrag=false;
        if(Math.abs(np-o.price)>1e-9 && S.ws&&S.ws.readyState===1){
          S.ws.send(JSON.stringify({action:'cancel_order',symbol:S.selInst,oid:o.oid}));
          S.ws.send(JSON.stringify({action:'place_order',symbol:S.selInst,side:o.side,price:np,qty:o.size}));
          toast('Лимитка перемещена @ '+fmtPx(np)); } };
      window.addEventListener('mousemove',move); window.addEventListener('mouseup',up);
    };
  });

  if(started){
    // 2) живые сеточные ордера (сплошные) — текущие активные лимитки сетки
    liveGrid().forEach(o=>{ const buy=o.side==='buy'; const col=buy?'#1F8A5B':'#C4453A';
      mkRow(o.price,col,`<span style="background:${col};color:#fff;height:20px;display:flex;align-items:center;padding:0 8px;border-radius:5px;font:600 9px 'JetBrains Mono',monospace;opacity:.92;white-space:nowrap;">${buy?'GRID BUY':'GRID SELL'} ${fmtPx(o.price)}</span>`); });
    // 3) превью СЛЕДУЮЩИХ уровней (пунктир) — куда сетка переставится при срабатывании; считает бэкенд
    ((S.live&&S.live.preview)||[]).forEach(o=>{ const buy=o.side==='buy'; const col=buy?'#1F8A5B':'#C4453A';
      const row=mkRow(o.price,col,`<span style="border:1px dashed ${col};color:${col};background:#fff;height:20px;display:flex;align-items:center;padding:0 8px;border-radius:5px;font:600 9px 'JetBrains Mono',monospace;opacity:.85;white-space:nowrap;">${buy?'BUY превью':'SELL превью'} ${fmtPx(o.price)}</span>`);
      row.style.borderTopStyle='dashed';
    });
  }
}
function cancelManual(oid){
  if(S.ws && S.ws.readyState===1){ S.ws.send(JSON.stringify({action:'cancel_order',symbol:S.selInst,oid})); toast('Лимитка снята'); }
}

// ─────────────────────────── sweep ───────────────────────────
async function doSweep(){
  const box=$('sweepBox'); if(box) box.innerHTML='<div style="color:#939a93;font-size:11px;padding:6px 0;">Прогон k…</div>';
  try{
    const r=await api('/api/sweep',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:S.selInst,interval:S.tf,base:S.params})});
    if(r.error){ box.innerHTML='<div style="color:#a93529;font-size:11px;">'+r.error+'</div>'; return; }
    const mx=Math.max(...r.points.map(p=>Math.abs(p.roi)))||1;
    const bars=r.points.map(p=>{ const h=Math.max(2,Math.abs(p.roi)/mx*34); const col=p.roi>=0?'#1F8A5B':'#C4453A'; const best=p.k===r.best_k;
      return `<div data-tip="k=${p.k} → ROI ${p.roi}% (${p.trades} сделок)" style="display:flex;flex-direction:column;align-items:center;justify-content:flex-end;flex:1;">
        <div style="width:62%;height:${h}px;background:${col};border-radius:2px;opacity:${best?1:.55};"></div>
        <span class="mono" style="font-size:7.5px;color:${best?'#14181a':'#aab0a9'};margin-top:2px;${best?'font-weight:700;':''}">${p.k}</span></div>`;
    }).join('');
    box.innerHTML=`<div style="background:#fff;border:1px solid #e3e6e0;border-radius:10px;padding:10px;">
      <div style="font-size:10px;color:#939a93;font-weight:600;margin-bottom:6px;">k-SWEEP · ${r.symbol}</div>
      <div style="display:flex;align-items:flex-end;gap:2px;height:44px;">${bars}</div>
      <div class="mono" style="font-size:10.5px;margin-top:8px;color:#5b635e;">Лучший k = <b style="color:#14181a;">${r.best_k}</b> → ROI <b style="color:${r.best_roi>=0?'#157a4f':'#a93529'};">${r.best_roi}%</b></div>
    </div>`;
  }catch(e){ if(box) box.innerHTML='<div style="color:#a93529;font-size:11px;">сбой sweep</div>'; }
}

// ─────────────────────────── стрим (replay/live) ───────────────────────────
function startStream(mode, apply){
  if(!S.market){ toast('Свечи ещё грузятся'); return; }
  stopAll(true); resetBook();
  S.streaming=true; S.streamMode=mode; S.tradingStarted=true; S.run='running'; S.liveCurve=[]; S.liveCandles=null; S.liveMarkers=null;
  if(apply) S.paramsSynced=true;        // явное «Применить» — наши params важнее серверных
  else S.paramsSynced=false;            // обычный коннект/F5 — принять серверный режим один раз
  render();
  const proto = location.protocol==='https:'?'wss':'ws';
  const ws=new WebSocket(`${proto}://${location.host}/ws/stream`); S.ws=ws;
  ws.onopen=()=>ws.send(JSON.stringify({mode,speed:S.speed,symbol:S.selInst,interval:S.tf,params:S.params,apply:!!apply}));
  ws.onmessage=(ev)=>{
    const m=JSON.parse(ev.data);
    if(m.type==='frame'){
      if(mode==='live'){ S.live=m; liveApply(); return; }
      // replay
      if(m.candles){ S.liveCandles=m.candles; S.liveMarkers=m.markers||[]; }
      if(m.curve) S.liveCurve=m.curve;
      else if(typeof m.equity==='number'){ S.liveCurve.push(m.equity); if(S.liveCurve.length>400)S.liveCurve.shift(); }
      drawAll();
      const badge=document.querySelector('#eqWrap .mono'); if(badge && m.equity!=null) badge.textContent='$'+m.equity.toFixed(2);
    } else if(m.type==='done'){ S.streaming=false; S.run='paused'; toast('Replay завершён'); render(); }
    else if(m.type==='error'){ toast('Стрим: '+m.msg); S.streaming=false; render(); }
  };
  ws.onclose=()=>{ if(S.streaming){ S.streaming=false; render(); } };
}

// точечное обновление realtime-кадра — без полного render (не теряем фокус инпутов)
function liveApply(){
  // принять серверный режим/параметры один раз (после F5/реконнекта сервер — источник истины)
  if(!S.paramsSynced && S.live && S.live.params){
    S.params = Object.assign({}, S.params, S.live.params);
    S.paramsSynced = true;
    render();   // одноразовая полная перерисовка — панель параметров с верным режимом
    return;
  }
  // если фокус на input параметра — не трогаем правую панель (её и не обновляем)
  const cr=$('cardsRow'); if(cr) cr.innerHTML=cardsHTML();
  const il=$('instList'); if(il) il.innerHTML=instRowsHTML();
  const lh=$('leftHead'); if(lh) lh.innerHTML=leftHeadHTML();
  // журнал перестраиваем ТОЛЬКО при изменении (новое событие/смена вкладки/старт-стоп) —
  // иначе 600 строк innerHTML каждые 0.33с тормозили бы прокрутку
  const bb=$('bottomBody');
  if(bb){
    const rec=(S.live&&S.live.recent)||[];
    const sig=S.tab+'|'+(stratOn()?1:0)+'|'+rec.length+'|'+(rec[0]?rec[0].ts:0)+'|'+(rec.length?rec[rec.length-1].ts:0);
    if(sig!==S._logSig){ bb.innerHTML=bottomTab(); S._logSig=sig; }
  }
  // стакан/лента обновляются собственным rAF-циклом (paintBook), читая S.live напрямую
  // перевесить клики по инструментам (innerHTML заменил узлы)
  if(il) il.querySelectorAll('[data-inst]').forEach(el=>el.onclick=()=>selectInst(el.dataset.inst));
  const bs=$('btnStrat'); if(bs){ const on=stratOn();
    bs.textContent=on?'Стоп стратегии':'Старт стратегии';
    bs.style.cssText='font-size:12px;font-weight:600;border-radius:6px;padding:5px 12px;cursor:pointer;'+
      (on?'background:#fbecea;color:#a93529;border:1px solid #f0c8c4;':'background:#e7f3ec;color:#157a4f;border:1px solid #cfe7d9;');
  }
  drawAll();
  hideTipIfStale();   // тултип гасим только если наведённый элемент исчез — иначе он мерцал каждый кадр
  const badge=document.querySelector('#eqWrap .mono'); if(badge && S.live) badge.textContent='$'+S.live.dash.equity.toFixed(2);
  const eb=$('eqBalVal'); if(eb && S.live) eb.textContent='$'+S.live.dash.balance.toFixed(2)+' bal';   // серая подпись «bal» — обновлять каждый кадр (не только на render)
}

function selectInst(sym){
  S.selInst=sym; resetGrid(); resetBook();
  if(S.streamMode==='live' && S.ws && S.ws.readyState===1){ S.ws.send(JSON.stringify({action:'select',symbol:sym})); }
  render();
}
function stopAll(silent){
  if(S.ws){ try{S.ws.close();}catch(e){} S.ws=null; }
  S.streaming=false; S.streamMode=null; S.liveCurve=null; S.liveCandles=null; S.liveMarkers=null;
  S.run='stopped'; S.tradingStarted=false;
  if(!silent) render();
}

// ─────────────────────────── отрисовка ───────────────────────────
function drawAll(){ updateChart(); drawEq(); }


function drawEq(){
  const cv=$('eqCv'), wrap=$('eqWrap'); if(!cv||!wrap) return;
  const dpr=window.devicePixelRatio||1;
  const W=wrap.clientWidth,H=wrap.clientHeight; if(!W||!H) return;
  cv.width=W*dpr; cv.height=H*dpr; const ctx=cv.getContext('2d'); ctx.scale(dpr,dpr);
  ctx.fillStyle='#fafbf9'; ctx.fillRect(0,0,W,H);
  const cap=S.cfg?S.cfg.start_capital:1000;
  let eq=eqCurve();
  const ml=6,mr=60,mt=26,mb=4; const cw=W-ml-mr;
  if(!eq||eq.length<2){
    // плоская линия на старте (торговля не запущена) — строго светлый фон
    const y=mt+(H-mt-mb)*0.4;
    ctx.strokeStyle='#cdd6cf'; ctx.setLineDash([4,3]); ctx.lineWidth=1.5; ctx.beginPath(); ctx.moveTo(ml,y); ctx.lineTo(W-mr,y); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle='#b3b8af'; ctx.font='10px "JetBrains Mono",monospace'; ctx.textAlign='right'; ctx.fillText('$'+cap.toFixed(0),W-4,y+3);
    ctx.fillStyle='#aab0a9'; ctx.font='10px "Space Grotesk",sans-serif'; ctx.textAlign='left'; ctx.fillText('Запусти прогон — эквити пойдёт отсюда',ml+6,y-7);
    return;
  }
  const bal=balCurve();
  const eqH=(H-mt-mb)*0.72; const ddTop=mt+eqH; const ddH=(H-mt-mb)*0.28;
  const allv = bal&&bal.length>1 ? eq.concat(bal) : eq;
  const rawMin=Math.min(...allv), rawMax=Math.max(...allv);
  const pad=Math.max(0.5,(rawMax-rawMin)*0.08);   // адаптивный отступ: видно движения в $1–2, флэт не «взрывается» шумом
  const eMin=rawMin-pad, eMax=rawMax+pad, eR=(eMax-eMin)||1;
  const toY=v=>mt+eqH-((v-eMin)/eR)*eqH; const toX=i=>ml+(i/(eq.length-1))*cw;
  ctx.strokeStyle='#eaece7'; ctx.lineWidth=0.7; ctx.font='10px "JetBrains Mono",monospace'; ctx.fillStyle='#b3b8af'; ctx.textAlign='right';
  const es=eR/4; for(let i=0;i<=4;i++){ const v=eMin+i*es; const y=toY(v); ctx.beginPath(); ctx.moveTo(ml,y); ctx.lineTo(W-mr+4,y); ctx.stroke(); ctx.fillText('$'+v.toFixed(0),W-4,y+3); }
  const grad=ctx.createLinearGradient(0,mt,0,mt+eqH); grad.addColorStop(0,'rgba(31,138,91,0.25)'); grad.addColorStop(1,'rgba(31,138,91,0.02)');
  ctx.beginPath(); ctx.moveTo(toX(0),toY(eq[0])); eq.forEach((v,i)=>ctx.lineTo(toX(i),toY(v))); ctx.lineTo(toX(eq.length-1),mt+eqH); ctx.lineTo(toX(0),mt+eqH); ctx.closePath(); ctx.fillStyle=grad; ctx.fill();
  ctx.beginPath(); ctx.moveTo(toX(0),toY(eq[0])); eq.forEach((v,i)=>ctx.lineTo(toX(i),toY(v))); ctx.strokeStyle='#1F8A5B'; ctx.lineWidth=2; ctx.stroke();
  let hwm=eq[0]; ctx.save(); ctx.setLineDash([3,2]); ctx.strokeStyle='rgba(31,138,91,0.2)'; ctx.lineWidth=1; ctx.beginPath(); eq.forEach((v,i)=>{if(v>hwm)hwm=v;ctx[i===0?'moveTo':'lineTo'](toX(i),toY(hwm));}); ctx.stroke(); ctx.restore();
  hwm=eq[0]; const dds=eq.map(v=>{if(v>hwm)hwm=v;return (hwm-v)/hwm;}); const maxDD=Math.max(...dds);
  if(maxDD>0){
    ctx.fillStyle='rgba(196,69,58,0.12)'; ctx.beginPath(); ctx.moveTo(toX(0),ddTop); dds.forEach((dd,i)=>ctx.lineTo(toX(i),ddTop+(dd/(maxDD||0.001))*ddH)); ctx.lineTo(toX(dds.length-1),ddTop); ctx.closePath(); ctx.fill();
    ctx.beginPath(); ctx.moveTo(toX(0),ddTop); dds.forEach((dd,i)=>ctx.lineTo(toX(i),ddTop+(dd/(maxDD||0.001))*ddH)); ctx.strokeStyle='rgba(196,69,58,0.45)'; ctx.lineWidth=1; ctx.stroke();
    ctx.fillStyle='#C4453A'; ctx.font='9px "JetBrains Mono",monospace'; ctx.textAlign='right'; ctx.fillText('DD -'+(maxDD*100).toFixed(1)+'%',W-4,ddTop+ddH-4);
  }
  ctx.strokeStyle='#dfe2dc'; ctx.lineWidth=0.5; ctx.beginPath(); ctx.moveTo(ml,ddTop); ctx.lineTo(W-mr,ddTop); ctx.stroke();
  // линия Balance (только реализованное) — ступеньками, как фиксируется PnL
  if(bal && bal.length>1){
    const bx=i=>ml+(i/(bal.length-1))*cw;
    ctx.save(); ctx.setLineDash([4,3]); ctx.strokeStyle='#7e857d'; ctx.lineWidth=1.4; ctx.beginPath();
    bal.forEach((v,i)=>{ const x=bx(i),y=toY(v); if(i===0)ctx.moveTo(x,y); else { ctx.lineTo(bx(i),toY(bal[i-1])); ctx.lineTo(x,y); } });
    ctx.stroke(); ctx.restore();
  }
  const last=eq[eq.length-1], ly=toY(last); ctx.fillStyle='#1F8A5B'; ctx.beginPath(); ctx.arc(toX(eq.length-1),ly,3.5,0,Math.PI*2); ctx.fill();
}

window.addEventListener('resize', ()=>requestAnimationFrame(drawAll));
boot();
})();
