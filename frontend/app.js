(() => {
  const tg = window.Telegram?.WebApp;
  const API_BASE = (window.SOLWATCHER_API_BASE || '').replace(/\/$/, '');
  const symbols = ['BTC','ETH','SOL','BNB','XRP','ADA','DOGE','AVAX','LINK','DOT'];
  const names = {BTC:'Bitcoin',ETH:'Ethereum',SOL:'Solana',BNB:'BNB',XRP:'XRP',ADA:'Cardano',DOGE:'Dogecoin',AVAX:'Avalanche',LINK:'Chainlink',DOT:'Polkadot'};
  const colors = {BTC:'#f2f2f2',ETH:'#f2f2f2',SOL:'#f2f2f2',BNB:'#f2f2f2',XRP:'#f2f2f2',ADA:'#f2f2f2',DOGE:'#f2f2f2',AVAX:'#f2f2f2',LINK:'#f2f2f2',DOT:'#f2f2f2'};
  const state = {prices:{},alerts:[],history:[],busy:false};
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = n => { const v=Number(n); if(!Number.isFinite(v))return '—'; return '$'+v.toLocaleString('en-US',{minimumFractionDigits:v<1?4:2,maximumFractionDigits:v<1?6:2}); };
  const headers = () => ({'Content-Type':'application/json','X-Telegram-Init-Data':tg?.initData || ''});
  async function api(path, options={}) {
    const res=await fetch(API_BASE+path,{...options,headers:{...headers(),...(options.headers||{})}});
    if(!res.ok){let message='Request failed ('+res.status+')';try{const body=await res.json();message=body.detail||message}catch{}throw new Error(message)}
    if(res.status===204)return null; return res.json();
  }
  function setConnection(online) { const el=$('#connection'); el.classList.toggle('offline',!online);el.innerHTML='<i></i> '+(online?'CONNECTED':'OFFLINE'); }
  function toast(message){const el=$('#toast');el.textContent=message;el.classList.add('show');clearTimeout(toast.timer);toast.timer=setTimeout(()=>el.classList.remove('show'),2600)}
  function priceCards(){
    const grid=$('#market-grid');
    const entries=symbols.filter(s=>state.prices[s]);
    if(!entries.length){grid.innerHTML='<div class="empty-state market-empty"><strong>Market data unavailable</strong><span>Pull to refresh or try again shortly.</span></div>';return}
    grid.innerHTML=entries.map(s=>{const p=state.prices[s],change=Number(p.change24h||0),up=change>=0;return '<article class="market-card"><div class="coin-line"><span class="coin-icon" style="color:'+esc(colors[s])+'">'+esc(s.slice(0,1))+'</span>'+esc(names[s])+' <span class="coin-symbol">'+esc(s)+'</span></div><div class="price">'+money(p.usd)+'</div><div class="change '+(up?'positive':'negative')+'">'+(up?'↗':'↘')+' '+(up?'+':'')+change.toFixed(2)+'% <span style="color:#66706b">24H</span></div></article>'}).join('');
  }
  function alertRule(a){
    if(a.alert_type==='price')return (a.direction==='below'?'Below ':'Above ')+'<strong>'+money(a.target)+'</strong>';
    if(a.alert_type==='change')return '± <strong>'+esc(a.threshold)+'%</strong> in '+esc(a.window_minutes)+'m';
    return '<strong>'+esc(a.threshold)+'×</strong> 1m volume';
  }
  function badge(a){const kind=a.alert_type||'price';return '<span class="alert-badge"><i class="badge-dot '+esc(kind)+'"></i>'+({price:'Price target',change:'Movement',volume:'Volume spike'}[kind]||'Alert')+'</span>'}
  function renderAlerts(){
    $('#alert-count').textContent=state.alerts.length;
    const list=$('#alert-list');
    if(!state.alerts.length){list.innerHTML='<div class="empty-state"><div class="empty-icon">⌁</div><strong>No active alerts</strong><span>Create an alert and we’ll notify you in Telegram.</span></div>';return}
    list.innerHTML=state.alerts.map(a=>'<article class="alert-row"><div class="asset-cell"><span class="coin-icon" style="color:'+esc(colors[a.coin]||'#b3ff5c')+'">'+esc(String(a.coin).slice(0,1))+'</span><div><div class="asset-name">'+esc(names[a.coin]||a.coin)+' <span class="coin-symbol">'+esc(a.coin)+'</span></div><div class="asset-sub">ALERT #'+esc(a.id)+'</div></div></div><div class="rule">'+alertRule(a)+'</div>'+badge(a)+'<div class="row-actions"><button class="toggle '+(a.enabled?'enabled':'')+'" data-toggle="'+esc(a.id)+'" data-enabled="'+(a.enabled?'1':'0')+'" aria-label="'+(a.enabled?'Disable':'Enable')+' alert" title="'+(a.enabled?'Disable':'Enable')+'"></button><button class="delete-button" data-delete="'+esc(a.id)+'" aria-label="Delete alert" title="Delete">×</button></div></article>').join('');
  }
  function renderHistory(){
    const list=$('#history-list');
    if(!state.history.length){list.innerHTML='<div class="empty-state"><div class="empty-icon">◷</div><strong>No triggered alerts yet</strong><span>When an alert fires, it will appear here.</span></div>';return}
    list.innerHTML=state.history.map(h=>{const date=new Date(h.triggered_at);return '<article class="alert-row history-row"><div class="asset-cell"><span class="coin-icon" style="color:'+esc(colors[h.coin]||'#b3ff5c')+'">'+esc(String(h.coin).slice(0,1))+'</span><div><div class="asset-name">'+esc(names[h.coin]||h.coin)+' <span class="coin-symbol">'+esc(h.coin)+'</span></div></div></div><div class="history-message">'+esc(h.message)+'</div>'+badge(h)+'<div class="history-time">'+(Number.isNaN(date.getTime())?'':date.toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}))+'</div></article>'}).join('');
  }
  async function loadPrices(refresh=false){try{const d=await api('/api/prices'+(refresh?'?refresh=true':''));state.prices=d.prices||{};priceCards();$('#updated').textContent='UPDATED '+new Date(d.updated_at||Date.now()).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});setConnection(true)}catch(e){setConnection(false);priceCards();if(!state.pricesLoaded)toast(e.message)}finally{state.pricesLoaded=true}}
  async function loadAlerts(){try{state.alerts=await api('/api/alerts');renderAlerts()}catch(e){$('#alert-list').innerHTML='<div class="empty-state"><strong>Could not load alerts</strong><span>'+esc(e.message)+'</span></div>';setConnection(false)}}
  async function loadHistory(){try{state.history=await api('/api/history?limit=50');renderHistory()}catch(e){$('#history-list').innerHTML='<div class="empty-state"><strong>Could not load history</strong><span>'+esc(e.message)+'</span></div>'}}
  async function refreshAll(force=false){if(state.busy)return;state.busy=true;$('#refresh').classList.add('spinning');await Promise.allSettled([loadPrices(force),loadAlerts(),loadHistory()]);state.busy=false;$('#refresh').classList.remove('spinning')}
  function showTypeFields(){const type=$('#alert-type').value;$('#price-fields').classList.toggle('hidden',type!=='price');$('#change-fields').classList.toggle('hidden',type!=='change');$('#volume-fields').classList.toggle('hidden',type!=='volume');$('#target').required=type==='price';$('#change-threshold').required=type==='change';$('#volume-threshold').required=type==='volume'}
  function openModal(){ $('#form-error').classList.add('hidden');$('#modal').classList.remove('hidden');document.body.style.overflow='hidden';showTypeFields();setTimeout(()=>$('#coin').focus(),50)}
  function closeModal(){ $('#modal').classList.add('hidden');document.body.style.overflow=''; }
  async function createAlert(event){
    event.preventDefault();const form=$('#alert-form'),type=$('#alert-type').value;const body={coin:$('#coin').value,alert_type:type};
    if(type==='price'){body.direction=$('#direction').value;body.target=Number($('#target').value)}
    if(type==='change'){body.threshold=Number($('#change-threshold').value);body.window_minutes=Number($('#window').value)}
    if(type==='volume')body.threshold=Number($('#volume-threshold').value);
    const button=$('#submit-alert');button.disabled=true;button.textContent='Creating…';
    try{await api('/api/alerts',{method:'POST',body:JSON.stringify(body)});form.reset();$('#coin').value='SOL';showTypeFields();closeModal();toast('Alert created');await Promise.all([loadAlerts(),loadHistory()])}
    catch(e){$('#form-error').textContent=e.message;$('#form-error').classList.remove('hidden')}
    finally{button.disabled=false;button.innerHTML='Create alert <span>→</span>'}
  }
  document.addEventListener('click',async e=>{
    if(e.target.closest('[data-close]'))closeModal();
    const toggle=e.target.closest('[data-toggle]');if(toggle){const id=toggle.dataset.toggle,enabled=toggle.dataset.enabled!=='1';toggle.disabled=true;try{await api('/api/alerts/'+encodeURIComponent(id)+'?enabled='+enabled,{method:'PATCH'});await loadAlerts()}catch(err){toast(err.message);toggle.disabled=false}}
    const del=e.target.closest('[data-delete]');if(del){del.disabled=true;try{await api('/api/alerts/'+encodeURIComponent(del.dataset.delete),{method:'DELETE'});toast('Alert deleted');await loadAlerts()}catch(err){toast(err.message);del.disabled=false}}
    const tab=e.target.closest('[data-view]');if(tab){document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===tab));const history=tab.dataset.view==='history';$('#alert-list').classList.toggle('hidden',history);$('#history-list').classList.toggle('hidden',!history)}
  });
  $('#new-alert').addEventListener('click',openModal);$('#alert-type').addEventListener('change',showTypeFields);$('#alert-form').addEventListener('submit',createAlert);$('#refresh').addEventListener('click',()=>loadPrices(true));document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal()});
  if(tg){tg.ready();tg.expand();try{tg.setHeaderColor('#080a0b');tg.setBackgroundColor('#080a0b');tg.enableClosingConfirmation(false)}catch{}}
  const user=tg?.initDataUnsafe?.user; if(user){const name=user.first_name||'trader';$('#greeting-name').textContent=', '+name;$('#avatar').textContent=(name[0]||'T').toUpperCase()}
  $('#footer-year').textContent=new Date().getFullYear();
  refreshAll();setInterval(()=>loadPrices(false),30000);
})();

