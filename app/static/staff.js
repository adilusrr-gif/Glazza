'use strict';

(() => {
  const $ = query => document.querySelector(query);
  const storageKey = 'glazza.restaurant.staff.token';
  const fragment = new URLSearchParams(location.hash.slice(1));
  const suppliedToken = fragment.has('token');
  let token = fragment.get('token') || '';
  if (location.hash) history.replaceState(null,'',location.pathname + location.search);
  if (token && !/^[A-Za-z0-9_-]{20,200}$/.test(token)) token='';
  try {
    if (suppliedToken) { if(token)sessionStorage.setItem(storageKey,token);else sessionStorage.removeItem(storageKey); }
    else token=sessionStorage.getItem(storageKey) || '';
  } catch (_) { /* A private browsing policy may disable storage; keep this tab usable. */ }

  let data=null, requestBusy=false, actionBusy=false, lastSuccess=0, requestSequence=0, stopped=false, refreshAfterPoll=false;
  let soundEnabled=false, audioContext=null, toastTimer=null, historyVisible=false;
  const seenAlerts=new Set();
  function toast(message) { $('#staff-toast').textContent=message;$('#staff-toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#staff-toast').hidden=true,6000); }
  function clearToken() { token='';try{sessionStorage.removeItem(storageKey);}catch(_){} }
  function showAccess(message) {
    $('#staff-person').hidden=true;$('#staff-controls').hidden=true;$('#staff-status').hidden=true;$('#staff-help').hidden=true;$('#staff-offline').hidden=true;
    $('#staff-content').classList.remove('restaurant-stale');
    $('#staff-content').innerHTML=`<section class="staff-access-message"><h1>Экран сотрудника</h1><p>${RestaurantUI.escape(message)}</p></section>`;
  }
  async function request(path='',options={}) {
    const response=await fetch('/api/restaurant/portal'+path,{...options,credentials:'omit',cache:'no-store',headers:{Authorization:'Bearer '+token,...(options.body?{'Content-Type':'application/json'}:{}),...(options.headers || {})}});
    let result;
    try { result=await response.json(); } catch (_) { throw new Error('Сервер временно недоступен. Повторите позже.'); }
    if(response.status===401 || response.status===403) {
      if (!path && response.status===401) { clearToken();stopped=true;data=null;showAccess('Персональная ссылка недействительна или доступ отключён. Попросите администратора выдать новую ссылку.'); }
      throw new Error(typeof result.detail==='string'?result.detail:'Это действие недоступно вашему рабочему экрану.');
    }
    if(!response.ok)throw new Error(Array.isArray(result.detail)?result.detail.map(error=>error.msg).join('. '):result.detail || 'Действие не выполнено.');
    return result;
  }
  function playSignal() {
    if(!soundEnabled || !audioContext)return;
    try {
      if(audioContext.state==='suspended')audioContext.resume().catch(()=>{});
      [0,.22].forEach(delay=>{const oscillator=audioContext.createOscillator(),gain=audioContext.createGain();oscillator.connect(gain);gain.connect(audioContext.destination);oscillator.frequency.value=660;gain.gain.setValueAtTime(.0001,audioContext.currentTime+delay);gain.gain.exponentialRampToValueAtTime(.1,audioContext.currentTime+delay+.02);gain.gain.exponentialRampToValueAtTime(.0001,audioContext.currentTime+delay+.15);oscillator.start(audioContext.currentTime+delay);oscillator.stop(audioContext.currentTime+delay+.17);});
      if(navigator.vibrate)navigator.vibrate([80,60,80]);
    } catch (_) { /* Visual alerts remain available if the browser blocks audio. */ }
  }
  function freshness() {
    if(!data)return;
    const stale=!lastSuccess || Date.now()-lastSuccess>8000;
    $('#staff-offline').hidden=!stale;$('#staff-content').classList.toggle('restaurant-stale',stale);$('#staff-status').classList.toggle('is-offline',stale);
    $('#staff-connection').textContent=stale?'Связь с сервером потеряна':'Рабочий экран подключён';
    $('#staff-updated').textContent=lastSuccess?'Обновлено '+new Date(lastSuccess).toLocaleTimeString('ru-RU'):'';
    document.querySelectorAll('[data-restaurant-action],[data-restaurant-ack]').forEach(button=>{button.disabled=stale || actionBusy;});
    RestaurantUI.timers($('#staff-content'));
  }
  function render() {
    if(!data)return;
    const person=data.current_staff || data.employee || data.staff_member || {};
    const tables=data.tables || [], alerts=data.alerts || [];
    const active=alerts.filter(alert=>!alert.ack_at && !alert.resolved_at);
    const focus=RestaurantUI.rememberFocus($('#staff-content'));
    $('#staff-person').hidden=false;$('#staff-controls').hidden=false;$('#staff-status').hidden=false;$('#staff-help').hidden=false;
    $('#staff-name').textContent=person.name || 'Сотрудник';$('#staff-role').textContent=RestaurantUI.roles[person.role] || 'Ресторан';
    $('#staff-content').innerHTML=`<div class="staff-intro"><h1>${person.role==='manager'?'Зал под контролем':person.role==='cleaner'?'Подготовка столов':'Ваши столы'}</h1><p>${person.role==='manager'?'Проверяйте просроченные вызовы и помогайте смене вовремя обслуживать гостей.':person.role==='cleaner'?'Когда стол подготовлен и камера видит, что он пуст, подтвердите уборку.':'Новые гости появляются в списке автоматически. Отметьте подход после того, как подошли к столу.'}</p></div>`+
      ((data.diagnostics || []).length?`<div class="warning-box">${data.diagnostics.map(item=>RestaurantUI.escape(typeof item==='string'?item:item.message || item.detail || '')).filter(Boolean).join('<br>')}</div>`:'')+
      `<section class="panel restaurant-notifications"><div class="section-heading"><h2>Уведомления <span>${active.length}</span></h2><button class="text-button" id="staff-history" aria-pressed="${historyVisible}">${historyVisible?'Только новые':'История'}</button></div><div class="restaurant-alert-list">${RestaurantUI.alerts(alerts,{history:historyVisible}) || '<p class="muted">Новых уведомлений нет.</p>'}</div></section>`+
      `<section class="restaurant-section"><div class="section-heading"><h2>Столы</h2><span class="small muted">${tables.length} доступно</span></div><div class="restaurant-table-grid">${RestaurantUI.cards(tables)}</div>${tables.length?'':'<section class="panel"><p class="muted">Столы пока не назначены. Попросите менеджера проверить назначения на смену.</p></section>'}</section>`;
    $('#staff-history').onclick=()=>{historyVisible=!historyVisible;render();};
    document.querySelectorAll('[data-restaurant-action]').forEach(button=>{button.onclick=async()=>{
      if(actionBusy)return;
      const table=tables.find(item=>item.id===button.dataset.restaurantTable);if(!table)return;
      await perform('/tables/'+encodeURIComponent(table.id)+'/actions',{action:button.dataset.restaurantAction,visit_id:table.visit_id ?? null,revision:table.revision});
    };});
    document.querySelectorAll('[data-restaurant-ack]').forEach(button=>{button.onclick=()=>perform('/alerts/'+encodeURIComponent(button.dataset.restaurantAck)+'/ack');});
    RestaurantUI.restoreFocus($('#staff-content'),focus);freshness();
  }
  async function poll(force=false) {
    if(!token || stopped || (actionBusy && !force))return;
    if(requestBusy){if(force)refreshAfterPoll=true;return;}
    requestBusy=true;const sequence=++requestSequence;
    try {
      const result=await request();
      if(sequence!==requestSequence || stopped)return;
      data=result;lastSuccess=Date.now();RestaurantUI.setClock(result.server_time);
      const active=(data.alerts || []).filter(alert=>!alert.ack_at && !alert.resolved_at);
      if(active.some(alert=>!seenAlerts.has(alert.id)))playSignal();
      active.forEach(alert=>seenAlerts.add(alert.id));
      if(seenAlerts.size>2000){seenAlerts.clear();active.forEach(alert=>seenAlerts.add(alert.id));}
      render();
    } catch(error) {
      if(!stopped){$('#staff-status').hidden=false;$('#staff-status').classList.add('is-offline');$('#staff-connection').textContent='Не удалось обновить данные';if(!data)showInitialFailure(error.message);}
    } finally {requestBusy=false;freshness();if(refreshAfterPoll){refreshAfterPoll=false;queueMicrotask(()=>poll(true));}}
  }
  function showInitialFailure(message) {
    $('#staff-content').innerHTML=`<section class="staff-access-message"><h1>Не удалось подключиться</h1><p>${RestaurantUI.escape(message)}</p><button class="primary" id="staff-retry">Повторить</button></section>`;
    $('#staff-retry').onclick=()=>poll();
  }
  async function perform(path,body) {
    if(actionBusy || !data || Date.now()-lastSuccess>8000)return;
    actionBusy=true;++requestSequence;freshness();
    try{await request(path,{method:'POST',...(body?{body:JSON.stringify(body)}:{})});toast(path.endsWith('/ack')?'Уведомление принято':'Состояние стола обновлено');}
    catch(error){toast(error.message);}
    finally{actionBusy=false;++requestSequence;await poll(true);freshness();}
  }
  $('#staff-sound').onclick=async()=>{
    if(soundEnabled){soundEnabled=false;$('#staff-sound').textContent='Включить звук';$('#staff-sound').setAttribute('aria-pressed','false');return;}
    try{const Audio=window.AudioContext || window.webkitAudioContext;if(!Audio)throw new Error('Звук недоступен в этом браузере');if(!audioContext)audioContext=new Audio();await audioContext.resume();soundEnabled=true;$('#staff-sound').textContent='Звук включён';$('#staff-sound').setAttribute('aria-pressed','true');playSignal();}catch(error){toast(error.message || 'Не удалось включить звук');}
  };
  $('#staff-refresh').onclick=()=>poll();
  $('#staff-logout').onclick=()=>{stopped=true;++requestSequence;clearToken();data=null;showAccess('Вы вышли из рабочего экрана. Чтобы вернуться, откройте персональную ссылку.');};
  if(token){poll();setInterval(()=>poll(),2000);setInterval(freshness,1000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});}
  else showAccess('Откройте персональную ссылку, которую выдал администратор ресторана.');
})();
