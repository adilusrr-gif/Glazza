'use strict';

// Shared presentation helpers for the administrator and scoped staff screen.
const RestaurantUI = (() => {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const states = {ready:'Готов к посадке', waiting:'Ожидают официанта', served:'Обслуживаются', needs_cleaning:'Требуется уборка'};
  const roles = {waiter:'Официант', manager:'Менеджер', cleaner:'Уборка столов'};
  const actions = {greeted:'Подошёл', cleaned:'Убрано', hold:'Гости отошли', resume:'Вернулись'};
  const colors = ['green','red','yellow','gray'];
  const alertNames = {arrival:'Новые гости', escalation:'Ожидание просрочено', cleaning:'Нужна уборка', assignment:'Нужен ответственный'};
  let offset = 0;
  function setClock(at) { if (Number.isFinite(at)) offset = at * 1000 - Date.now(); }
  function now() { return (Date.now() + offset) / 1000; }
  function duration(seconds) {
    const n = Math.max(0, Math.floor(Number(seconds) || 0));
    return n >= 3600 ? `${Math.floor(n / 3600)}:${String(Math.floor(n / 60) % 60).padStart(2,'0')}:${String(n % 60).padStart(2,'0')}` : `${String(Math.floor(n / 60)).padStart(2,'0')}:${String(n % 60).padStart(2,'0')}`;
  }
  function color(table) { return colors.includes(table.color) ? table.color : 'gray'; }
  function allowed(table) {
    if (Array.isArray(table.allowed_actions)) return table.allowed_actions.filter(action => Object.hasOwn(actions, action));
    if (['waiting','served'].includes(table.state)) return [...(table.state === 'waiting' ? ['greeted'] : []), table.hold ? 'resume' : 'hold'];
    return table.state === 'needs_cleaning' && table.occupancy === 'empty' ? ['cleaned'] : [];
  }
  function cards(tables, {admin=false} = {}) {
    return tables.map(table => {
      const unknown = table.occupancy === 'unknown' || color(table) === 'gray';
      const label = unknown ? 'Нет актуальных данных' : states[table.state] || 'Проверка состояния';
      const timer = table.arrived_at && ['waiting','served'].includes(table.state) ? `<div class="restaurant-timer"><span>${table.state === 'waiting' ? 'Ждут первого подхода' : 'Время визита'}</span><strong data-restaurant-since="${Number(table.arrived_at)}">${duration(now() - table.arrived_at)}</strong></div>` : '';
      return `<article class="restaurant-table restaurant-${color(table)}${table.overdue ? ' restaurant-overdue' : ''}" data-table-card="${escape(table.id)}" tabindex="-1">
        <div class="restaurant-table-heading"><h3>${escape(table.name)}</h3>${admin ? `<button class="icon-button" data-restaurant-edit="${escape(table.id)}" aria-label="Настроить ${escape(table.name)}" title="Настроить стол">⚙</button>` : ''}</div>
        <p class="restaurant-state"><span class="restaurant-dot" aria-hidden="true"></span>${escape(label)}</p>
        ${unknown ? `<p class="restaurant-last-state">Последнее состояние: ${escape(states[table.state] || 'ещё не определено')}</p>` : ''}
        ${timer}
        <div class="restaurant-tags">${table.overdue ? '<span class="restaurant-tag restaurant-tag-alert">Ожидание просрочено</span>' : ''}${table.hold ? '<span class="restaurant-tag">Место сохранено · гости отошли</span>' : ''}</div>
        <dl class="restaurant-meta"><div><dt>Официант</dt><dd>${escape(table.waiter_name || 'Не назначен')}</dd></div>${table.cleaner_name ? `<div><dt>Уборка</dt><dd>${escape(table.cleaner_name)}</dd></div>` : ''}<div><dt>Камера</dt><dd>${escape(table.camera_name || table.camera)}</dd></div>${!unknown ? `<div><dt>Людей в зоне</dt><dd>${Number.isFinite(table.people) ? table.people : '—'}</dd></div>` : ''}</dl>
        <div class="restaurant-table-actions">${allowed(table).map(action => `<button class="${action === 'greeted' || action === 'cleaned' ? 'primary' : 'secondary'}" data-restaurant-action="${action}" data-restaurant-table="${escape(table.id)}">${actions[action]}</button>`).join('')}</div>
      </article>`;
    }).join('');
  }
  function alerts(items, {history=false} = {}) {
    const shown = history ? items : items.filter(item => !item.ack_at && !item.resolved_at);
    return shown.map(alert => `<article class="restaurant-alert${alert.kind === 'escalation' ? ' restaurant-alert-urgent' : ''}" data-alert-card="${escape(alert.id)}">
      <div><strong>${escape(alert.table_name || 'Стол')} · ${escape(alertNames[alert.kind] || 'Уведомление')}</strong><p>${escape(alert.message || '')}</p><div class="restaurant-alert-meta"><span>${escape(alert.recipient_name || 'Менеджеру / администратору')}</span><time>${Number.isFinite(alert.at) ? new Date(alert.at * 1000).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'}) : ''}</time>${alert.delivery_state === 'failed' || alert.delivery_error ? '<span class="error">Внешняя доставка не подтверждена</span>' : ''}${alert.resolved_at ? '<span>Задача закрыта</span>' : alert.ack_at ? '<span>Уведомление принято</span>' : ''}</div></div>
      ${!alert.ack_at && !alert.resolved_at ? `<button class="secondary" data-restaurant-ack="${escape(alert.id)}">Принято</button>` : ''}
    </article>`).join('');
  }
  function timers(root=document) {
    root.querySelectorAll('[data-restaurant-since]').forEach(node => { node.textContent = duration(now() - Number(node.dataset.restaurantSince)); });
  }
  function rememberFocus(root) {
    const active = document.activeElement;
    if (!root?.contains(active)) return null;
    if (active.id) return {id:active.id};
    for (const attribute of ['data-restaurant-action','data-restaurant-edit','data-restaurant-ack','data-nav','data-action']) {
      if (active.hasAttribute(attribute)) return {attribute, value:active.getAttribute(attribute), table:active.dataset.restaurantTable};
    }
    return null;
  }
  function restoreFocus(root, saved) {
    if (!saved) return;
    const target = saved.id ? document.getElementById(saved.id) : [...root.querySelectorAll(`[${saved.attribute}]`)].find(node => node.getAttribute(saved.attribute) === saved.value && (!saved.table || node.dataset.restaurantTable === saved.table));
    if (target && root.contains(target)) target.focus({preventScroll:true});
    else {
      const table=saved.table ? [...root.querySelectorAll('[data-table-card]')].find(node=>node.dataset.tableCard===saved.table) : null;
      (table || root).focus({preventScroll:true});
    }
  }
  return {escape, states, roles, actions, cards, alerts, color, allowed, duration, setClock, now, timers, rememberFocus, restoreFocus};
})();

let restaurantPending = null;
let restaurantRequestSequence = 0;
let restaurantReceivedAt = 0;
let restaurantLastError = '';
let restaurantMutationBusy = false;
let restaurantFloorCamera = '';
let restaurantHistory = false;
const restaurantFrames = new Map();

function resetRestaurantState() {
  ++restaurantRequestSequence;restaurantPending=null;restaurantReceivedAt=0;restaurantLastError='';restaurantFloorCamera='';restaurantFrames.clear();
  state.restaurant=null;
}

function invalidateRestaurantRequest() { ++restaurantRequestSequence;restaurantPending=null; }

async function restaurantWrite(path,options) {
  invalidateRestaurantRequest();
  try { return await api(path,options); }
  finally { invalidateRestaurantRequest(); }
}

async function loadRestaurantData() {
  const scope = selectedSite;
  if (restaurantPending?.scope === scope) return restaurantPending.promise;
  const sequence = ++restaurantRequestSequence;
  const promise = api(scopeUrl('/restaurant')).then(data => {
    if (sequence === restaurantRequestSequence && scope === selectedSite && state.session) {
      state.restaurant = data;
      RestaurantUI.setClock(data.server_time);
      restaurantReceivedAt = Date.now();
      restaurantLastError = '';
    }
    return data;
  }).catch(error => {
    if (sequence === restaurantRequestSequence && scope === selectedSite) restaurantLastError = error.message;
    throw error;
  }).finally(() => { if (restaurantPending?.sequence === sequence) restaurantPending = null; });
  restaurantPending = {scope, sequence, promise};
  return promise;
}

function restaurantSites() { return (state.platform?.sites || []).filter(site => site.modules.includes('restaurant')); }
function restaurantSiteOptions(value) {
  return restaurantSites().map(site => `<option value="${escapeHTML(site.id)}" ${site.id === value ? 'selected' : ''}>${escapeHTML(site.name)}</option>`).join('');
}
function restaurantDefaultSite() { return restaurantSites().find(site => site.id === selectedSite)?.id || restaurantSites()[0]?.id || ''; }
function restaurantHealthText(data) {
  const health = data?.health || {};
  if (health.state === 'online') return 'Канал занятости подключён';
  const states={waiting:'Ожидание актуальных данных камер',offline:'Нет связи с каналом занятости',not_configured:'Канал занятости ещё не настроен',disabled:'Ресторанный мониторинг выключен',connecting:'Подключаем канал занятости'};
  return health.message || health.detail || states[health.state] || 'Ожидание актуальных данных камер';
}

function renderRestaurant() {
  const data = state.restaurant || {tables:[],staff:[],alerts:[],health:{}};
  const sites = restaurantSites();
  const enabled = selectedSite ? sites.some(site => site.id === selectedSite) : sites.length > 0;
  const tables = data.tables || [], staff = data.staff || [], alerts = data.alerts || [];
  const focused = RestaurantUI.rememberFocus($('#content'));
  const activeAlerts = alerts.filter(alert => !alert.ack_at && !alert.resolved_at);
  const canConfigure = enabled && !state.session.demo;
  $('#content').innerHTML = head('Ресторан', 'Занятость определяется по зоне посадочных мест. Подход официанта и готовность после уборки подтверждает сотрудник.', `<button class="secondary" id="restaurant-staff" ${canConfigure ? '' : 'disabled'}>Сотрудники</button><button class="secondary" data-action="apply" ${canConfigure ? '' : 'disabled'}>Применить настройки</button><button class="primary" id="restaurant-add-table" ${canConfigure ? '' : 'disabled'}>+ Стол</button>`) +
    (!enabled ? `<section class="panel restaurant-setup"><h2>Подготовьте ресторан</h2><p>${selectedSite ? 'Включите модуль «Ресторан» в настройках выбранного объекта.' : 'Создайте объект с профилем «Ресторан» или включите ресторанный модуль у существующего объекта.'}</p><button class="primary" data-nav="platform">Открыть объекты</button></section>` : '') +
    `<div id="restaurant-connection" class="restaurant-connection" role="status"><span class="restaurant-dot" aria-hidden="true"></span><span>${escapeHTML(restaurantHealthText(data))}</span><small id="restaurant-updated"></small></div>` +
    `<div class="restaurant-summary" aria-label="Состояния столов">${[['green','Готовы'],['red','Заняты'],['yellow','Нужна уборка'],['gray','Нет данных']].map(([color,label]) => `<div class="restaurant-summary-item restaurant-${color}"><span class="restaurant-dot" aria-hidden="true"></span><span>${label}</span><strong>${tables.filter(table => RestaurantUI.color(table) === color).length}</strong></div>`).join('')}</div>` +
    `<p id="restaurant-stale" class="warning-box" role="status" hidden>Нет свежего ответа сервера. Показаны последние известные состояния.</p>` +
    ((data.diagnostics || []).length ? `<div class="warning-box">${data.diagnostics.map(item => escapeHTML(typeof item === 'string' ? item : item.message || item.detail || '')).filter(Boolean).join('<br>')}</div>` : '') +
    (tables.length ? restaurantFloorMarkup(tables) : '') +
    `<section class="restaurant-section"><div class="section-heading"><h2>Столы</h2><span class="small muted">${tables.length} всего</span></div><div class="restaurant-table-grid">${RestaurantUI.cards(tables,{admin:true})}</div>${!tables.length ? empty('Столы ещё не настроены', 'Добавьте камеру, сотрудников и разметьте посадочные места каждого стола. Затем примените настройки камер.') : ''}</section>` +
    `<section class="panel restaurant-notifications"><div class="section-heading"><h2>Уведомления <span>${activeAlerts.length}</span></h2><button class="text-button" id="restaurant-history" aria-pressed="${restaurantHistory}">${restaurantHistory ? 'Только новые' : 'Показать историю'}</button></div><p class="small">«Принято» отмечает получение сигнала. Подход и уборка подтверждаются на карточке стола.</p><div class="restaurant-alert-list">${RestaurantUI.alerts(alerts,{history:restaurantHistory}) || '<p class="muted">Новых уведомлений нет.</p>'}</div></section>` +
    (enabled ? `<section class="panel restaurant-guide"><h2>Настройка смены</h2><ol><li>Добавьте официанта и менеджера. Откройте каждому персональную ссылку на телефоне.</li><li>У стола выберите ответственного и обведите места гостей на кадре камеры.</li><li>Примените настройки. Когда камера подтвердит пустой стол, нажмите «Убрано».</li></ol><p class="small">Если гости временно отошли, сохраните им место кнопкой на столе. Уведомление менеджеру создаётся через установленное время, пока подход не подтверждён.</p><p class="small">${data.telegram_configured ? 'Telegram подключён на сервере. Для доставки укажите chat ID сотрудника.' : 'Для уведомлений в Telegram администратор сервера может подключить бота. Персональные экраны уже показывают адресные сигналы.'}</p></section>` : '');
  $('#restaurant-add-table').onclick = () => restaurantTableForm();
  $('#restaurant-staff').onclick = () => restaurantStaffList();
  $('#restaurant-history').onclick = () => { restaurantHistory = !restaurantHistory; renderRestaurant(); bindActions(); };
  $$('[data-restaurant-edit]').forEach(button => { button.onclick = () => restaurantTableForm(tables.find(table => table.id === button.dataset.restaurantEdit)); });
  bindRestaurantActions($('#content'));
  const cameraSelect = $('#restaurant-camera');
  if (cameraSelect) { cameraSelect.onchange = event => { restaurantFloorCamera = event.target.value; drawRestaurantFloor(); }; drawRestaurantFloor(); }
  RestaurantUI.restoreFocus($('#content'), focused);
  updateRestaurantFreshness();
}

function restaurantFloorMarkup(tables) {
  const cameras = [...new Set(tables.map(table => table.camera))];
  if (!cameras.includes(restaurantFloorCamera)) restaurantFloorCamera = cameras[0] || '';
  return `<section class="panel restaurant-floor-panel"><div class="section-heading"><h2>Столы на камере</h2><select id="restaurant-camera" aria-label="Камера ресторана">${cameras.map(camera => `<option value="${escapeHTML(camera)}" ${restaurantFloorCamera === camera ? 'selected' : ''}>${escapeHTML(tables.find(table => table.camera === camera)?.camera_name || camera)}</option>`).join('')}</select></div><canvas id="restaurant-floor" width="960" height="540" role="img" aria-label="Кадр камеры с цветными зонами столов"></canvas><p class="small muted" id="restaurant-frame-status">Загружаем кадр камеры…</p><p class="small muted">Цветные области показывают зоны посадочных мест. Подробности и действия доступны в карточках ниже.</p></section>`;
}

function drawRestaurantPolygon(context, points, width, height, color, label='') {
  if (!points?.length) return;
  const palette = {green:['#129260','#0a6d45'], red:['#df4555','#b01f33'], yellow:['#e3ad2c','#946300'], gray:['#8a99ac','#4e6178']};
  const colors = palette[color] || palette.gray;
  context.beginPath();
  points.forEach(([x,y],index) => index ? context.lineTo(x*width,y*height) : context.moveTo(x*width,y*height));
  context.closePath(); context.fillStyle = colors[0]+'40'; context.fill(); context.strokeStyle=colors[0]; context.lineWidth=4; context.stroke();
  points.forEach(([x,y]) => { context.beginPath();context.arc(x*width,y*height,4,0,Math.PI*2);context.fillStyle=colors[0];context.fill(); });
  if (label) {
    const x = points.reduce((sum,point) => sum+point[0],0)/points.length*width;
    const y = points.reduce((sum,point) => sum+point[1],0)/points.length*height;
    context.font='600 20px system-ui'; const text=label.slice(0,36), textWidth=Math.min(width-20,context.measureText(text).width+22);
    const left=Math.max(5,Math.min(width-textWidth-5,x-textWidth/2)), top=Math.max(5,Math.min(height-38,y-16));
    context.fillStyle='#fff';context.fillRect(left,top,textWidth,32);context.fillStyle=colors[1];context.textAlign='center';context.fillText(text,left+textWidth/2,top+23,textWidth-12);context.textAlign='start';
  }
}

function drawRestaurantFloor() {
  const canvas = $('#restaurant-floor'), camera = restaurantFloorCamera;
  if (!canvas || !camera) return;
  const draw = image => {
    if ($('#restaurant-floor') !== canvas || restaurantFloorCamera !== camera) return;
    const context=canvas.getContext('2d');context.fillStyle='#e9eef5';context.fillRect(0,0,canvas.width,canvas.height);
    if (image) context.drawImage(image,0,0,canvas.width,canvas.height);
    else {context.fillStyle='#4d6482';context.font='20px system-ui';context.fillText('Кадр камеры недоступен',30,42);}
    (state.restaurant?.tables || []).filter(table => table.camera === camera).forEach(table => drawRestaurantPolygon(context,table.points,canvas.width,canvas.height,RestaurantUI.color(table),table.name));
  };
  draw(restaurantFrames.get(camera));
  if (state.session.demo) return;
  const image=new Image();image.onload=()=>{
    restaurantFrames.set(camera,image);if(restaurantFrames.size>12)restaurantFrames.delete(restaurantFrames.keys().next().value);draw(image);
    if($('#restaurant-floor')===canvas && restaurantFloorCamera===camera)$('#restaurant-frame-status').textContent='Кадр получен '+new Date().toLocaleTimeString('ru-RU');
  };image.onerror=()=>{
    draw(restaurantFrames.get(camera));
    if($('#restaurant-floor')===canvas && restaurantFloorCamera===camera)$('#restaurant-frame-status').textContent=restaurantFrames.has(camera)?'Последний доступный кадр · обновление изображения недоступно':'Кадр камеры недоступен';
  };image.src='/api/cameras/'+encodeURIComponent(camera)+'/snapshot?t='+Date.now();
}

function updateRestaurantFreshness() {
  if (typeof state === 'undefined' || state.view !== 'restaurant' || !state.session) return;
  const stale = !restaurantReceivedAt || Date.now()-restaurantReceivedAt > 10000 || Boolean(restaurantLastError);
  const banner=$('#restaurant-stale');if(banner)banner.hidden=!stale;
  const content=$('#content');if(content)content.classList.toggle('restaurant-stale',stale);
  const updated=$('#restaurant-updated');if(updated)updated.textContent=restaurantReceivedAt ? 'Обновлено '+new Date(restaurantReceivedAt).toLocaleTimeString('ru-RU') : '';
  RestaurantUI.timers(content || document);
}

async function refreshRestaurantView() {
  await loadRestaurantData();
  if (state.view === 'restaurant' && !$('#modal').open && !helpAnchor && !['INPUT','SELECT','TEXTAREA'].includes(document.activeElement?.tagName)) { renderRestaurant(); bindActions(); }
}

function bindRestaurantActions(root) {
  $$('[data-restaurant-action]',root).forEach(button => { button.onclick = async () => {
    if (restaurantMutationBusy) return;
    const table=(state.restaurant?.tables || []).find(item => item.id === button.dataset.restaurantTable);if(!table)return;
    restaurantMutationBusy=true;button.disabled=true;
    try { await restaurantWrite('/restaurant/tables/'+encodeURIComponent(table.id)+'/actions',{method:'POST',body:JSON.stringify({action:button.dataset.restaurantAction,visit_id:table.visit_id ?? null,revision:table.revision})});toast('Состояние стола обновлено');await refreshRestaurantView(); }
    catch(error){toast(error.message);}finally{restaurantMutationBusy=false;button.disabled=false;}
  }; });
  $$('[data-restaurant-ack]',root).forEach(button => { button.onclick = async () => {
    if(restaurantMutationBusy)return;restaurantMutationBusy=true;button.disabled=true;
    try{await restaurantWrite('/restaurant/alerts/'+encodeURIComponent(button.dataset.restaurantAck)+'/ack',{method:'POST'});await refreshRestaurantView();}catch(error){toast(error.message);}finally{restaurantMutationBusy=false;button.disabled=false;}
  }; });
}

function restaurantStaffList() {
  const staff=state.restaurant?.staff || [];
  modal('Сотрудники ресторана',`<p>Персональная ссылка открывает сотруднику только его рабочий экран. Для смены нужен хотя бы один менеджер.</p><div class="restaurant-staff-list">${staff.map(person=>`<div class="restaurant-staff-row"><div><strong>${escapeHTML(person.name)}</strong><p>${escapeHTML(RestaurantUI.roles[person.role] || person.role)}${person.enabled === false ? ' · отключён' : ''}</p></div><div class="restaurant-staff-actions"><button class="secondary" data-restaurant-staff-edit="${escapeHTML(person.id)}">Изменить</button><button class="secondary" data-restaurant-staff-link="${escapeHTML(person.id)}">Новая ссылка</button></div></div>`).join('') || '<p class="muted">Сотрудников пока нет.</p>'}</div><div class="form-actions"><button class="primary" id="restaurant-add-staff">+ Сотрудник</button></div>`);
  $('#restaurant-add-staff').onclick=()=>restaurantStaffForm();
  $$('[data-restaurant-staff-edit]').forEach(button=>button.onclick=()=>restaurantStaffForm(staff.find(person=>person.id===button.dataset.restaurantStaffEdit)));
  $$('[data-restaurant-staff-link]').forEach(button=>button.onclick=()=>{
    const person=staff.find(item=>item.id===button.dataset.restaurantStaffLink);
    modal('Обновить ссылку сотрудника?',`<p>Для ${escapeHTML(person.name)} будет создана новая персональная ссылка. Предыдущая перестанет работать.</p><p class="error" id="restaurant-link-error" role="alert"></p><div class="form-actions"><button class="primary" id="restaurant-rotate-token">Создать новую ссылку</button></div>`);
    $('#restaurant-rotate-token').onclick=async event=>{event.target.disabled=true;try{const result=await restaurantWrite('/restaurant/staff/'+encodeURIComponent(person.id)+'/token',{method:'POST'});restaurantStaffLink(result,person.name);}catch(error){$('#restaurant-link-error').textContent=error.message;event.target.disabled=false;}};
  });
}

function restaurantStaffForm(person) {
  const item=person || {site_id:restaurantDefaultSite(),name:'',role:'waiter',telegram_chat_id:'',enabled:true};
  modal(person?'Изменить сотрудника':'Добавить сотрудника',`<form id="restaurant-staff-form"><label>Объект<select name="site_id" ${person?'disabled':''}>${restaurantSiteOptions(item.site_id)}</select></label><label>Имя сотрудника<input name="name" required maxlength="100" value="${escapeHTML(item.name)}" autocomplete="off"></label><label>Роль<select name="role">${Object.entries(RestaurantUI.roles).map(([role,label])=>`<option value="${role}" ${item.role===role?'selected':''}>${label}</option>`).join('')}</select></label><label>Telegram chat ID · необязательно<input name="telegram_chat_id" inputmode="numeric" pattern="-?[0-9]{1,20}" maxlength="21" placeholder="Например, 123456789" value="${escapeHTML(item.telegram_chat_id || '')}"></label><p class="small">${state.restaurant?.telegram_configured ? 'Бот подключён. Сотрудник должен предварительно открыть чат с ботом и нажать «Запустить».' : 'Для доставки в Telegram сначала подключите бота в настройках сервера. Без Telegram работает персональный экран.'}</p>${person?`<label class="restaurant-check"><input type="checkbox" name="enabled" ${item.enabled?'checked':''}>Сотрудник активен</label>`:''}<p class="error" id="restaurant-staff-error" role="alert"></p><div class="form-actions"><button class="primary" type="submit">Сохранить сотрудника</button></div></form>`);
  $('#restaurant-staff-form').onsubmit=async event=>{
    event.preventDefault();const button=$('button[type=submit]',event.target);button.disabled=true;const form=new FormData(event.target);
    const body={site_id:person?item.site_id:form.get('site_id'),name:form.get('name'),role:form.get('role'),telegram_chat_id:form.get('telegram_chat_id') || '',enabled:person?form.get('enabled')==='on':true};
    try{const result=await restaurantWrite('/restaurant/staff'+(person?'/'+encodeURIComponent(person.id):''),{method:person?'PUT':'POST',body:JSON.stringify(body)});await loadRestaurantData();if(result.portal_url || result.token)restaurantStaffLink(result,body.name);else{$('#modal').close();await refreshRestaurantView();toast('Сотрудник сохранён');}}
    catch(error){$('#restaurant-staff-error').textContent=error.message;}finally{button.disabled=false;}
  };
}

function restaurantStaffLink(result,name) {
  const path=result.portal_url || '/staff#token='+encodeURIComponent(result.token);
  const link=new URL(path,location.origin);if(link.origin!==location.origin || link.pathname!=='/staff')throw new Error('Некорректная ссылка сотрудника');
  modal('Персональная ссылка',`<p>Рабочий экран для <strong>${escapeHTML(name)}</strong>. Сохраните эту ссылку: повторно она не показывается.</p><label>Ссылка сотрудника<input id="restaurant-staff-url" type="text" readonly autocomplete="off"></label><p class="small">Передайте её только этому сотруднику. Администратор может отключить доступ или выпустить новую ссылку.</p><p id="restaurant-copy-status" role="status"></p><div class="form-actions"><button class="secondary" id="restaurant-copy-link">Копировать</button><a class="primary" id="restaurant-open-link" target="_blank" rel="noopener">Открыть экран</a></div>`);
  $('#restaurant-staff-url').value=link.href;$('#restaurant-open-link').href=link.href;
  $('#restaurant-copy-link').onclick=async()=>{try{await navigator.clipboard.writeText(link.href);$('#restaurant-copy-status').textContent='Ссылка скопирована';}catch(_){$('#restaurant-staff-url').select();$('#restaurant-copy-status').textContent='Скопируйте выделенную ссылку';}};
}

function restaurantTableForm(table) {
  const item=table || {site_id:restaurantDefaultSite(),name:'',camera:'',points:[],waiter_id:null,cleaner_id:null,wait_seconds:120,arrival_seconds:5,vacancy_seconds:60};
  const allCameras=state.allCameras || state.cameras;
  let points=(item.points || []).map(point=>[...point]), background=null, imageRequest=0;
  modal(table?'Настроить стол':'Добавить стол',`<form id="restaurant-table-form"><div class="form-row"><label>Объект<select name="site_id" ${table?'disabled':''}>${restaurantSiteOptions(item.site_id)}</select></label><label>Название<input name="name" placeholder="Стол 7" value="${escapeHTML(item.name)}" maxlength="80" required></label></div><label>Камера<select name="camera" required></select></label><div class="restaurant-zone-editor"><canvas id="restaurant-zone" width="960" height="540" aria-label="Нарисуйте зону посадочных мест стола"></canvas><div class="restaurant-zone-tools"><span id="restaurant-point-count">0 точек</span><button type="button" class="secondary" id="restaurant-zone-undo">Отменить точку</button><button type="button" class="secondary" id="restaurant-zone-clear">Очистить</button></div></div><p class="small">Нажмите на кадр, чтобы обвести места гостей: от 3 до 20 точек. Не включайте проход официанта. Камера должна видеть людей на посадочных местах.</p><div class="form-row"><label>Официант<select name="waiter_id"></select></label><label>Уборка<select name="cleaner_id"></select></label></div><div class="restaurant-timing-fields"><label>До вызова менеджера, сек<input name="wait_seconds" type="number" min="30" max="1800" step="1" required value="${Number(item.wait_seconds)}"></label><label>Подтверждение посадки, сек<input name="arrival_seconds" type="number" min="1" max="60" step="1" required value="${Number(item.arrival_seconds)}"></label><label>Пустой стол до уборки, сек<input name="vacancy_seconds" type="number" min="10" max="600" step="1" required value="${Number(item.vacancy_seconds)}"></label></div><p class="small">Подход и уборку сотрудник подтверждает кнопкой. Сохраните место, если гости временно отошли. После изменения разметки примените настройки камер.</p><p class="error" id="restaurant-table-error" role="alert"></p><div class="form-actions">${table?'<button type="button" class="danger" id="restaurant-delete-table">Удалить стол</button>':''}<button class="primary" type="submit">Сохранить стол</button></div></form>`);
  const form=$('#restaurant-table-form'), canvas=$('#restaurant-zone'), context=canvas.getContext('2d');
  const siteSelect=$('select[name=site_id]',form),cameraSelect=$('select[name=camera]',form);
  function currentSite(){return table?item.site_id:siteSelect.value;}
  function draw(){
    context.fillStyle='#e9eef5';context.fillRect(0,0,canvas.width,canvas.height);
    if(background)context.drawImage(background,0,0,canvas.width,canvas.height);else{context.fillStyle='#4d6482';context.font='22px system-ui';context.fillText('Нет кадра — проверьте подключение камеры',24,45);}
    (state.restaurant?.tables || []).filter(other=>other.id!==item.id && other.camera===cameraSelect.value).forEach(other=>drawRestaurantPolygon(context,other.points,canvas.width,canvas.height,'gray',other.name));
    drawRestaurantPolygon(context,points,canvas.width,canvas.height,'green',String($('input[name=name]',form).value || 'Новый стол'));
    $('#restaurant-point-count').textContent=points.length+' / 20 точек';
  }
  function loadImage(){
    const request=++imageRequest;background=null;draw();if(!cameraSelect.value)return;
    const image=new Image();image.onload=()=>{if(request!==imageRequest || $('#restaurant-zone')!==canvas)return;background=image;draw();};image.onerror=()=>{if(request===imageRequest && $('#restaurant-zone')===canvas)draw();};image.src='/api/cameras/'+encodeURIComponent(cameraSelect.value)+'/snapshot?t='+Date.now();
  }
  function updateOptions(preserve){
    const cameras=allCameras.filter(camera=>camera.site_id===currentSite());
    cameraSelect.innerHTML=cameras.map(camera=>`<option value="${escapeHTML(camera.id)}">${escapeHTML(camera.name)}</option>`).join('') || '<option value="">Сначала добавьте камеру</option>';
    if(preserve && cameras.some(camera=>camera.id===item.camera))cameraSelect.value=item.camera;
    const staff=(state.restaurant?.staff || []).filter(person=>person.site_id===currentSite() && person.enabled!==false);
    for(const [name,role,id] of [['waiter_id','waiter',item.waiter_id],['cleaner_id','cleaner',item.cleaner_id]]){
      const select=$(`select[name=${name}]`,form);select.innerHTML=`<option value="">${name==='waiter_id'?'Не назначен · сигнал менеджеру':'Ответственный официант'}</option>`+staff.filter(person=>person.role===role).map(person=>`<option value="${escapeHTML(person.id)}">${escapeHTML(person.name)}</option>`).join('');
      if(preserve && staff.some(person=>person.id===id && person.role===role))select.value=id;
    }
    loadImage();
  }
  updateOptions(true);
  siteSelect.onchange=()=>{points=[];updateOptions(false);};cameraSelect.onchange=()=>{points=[];loadImage();};
  $('input[name=name]',form).oninput=draw;
  canvas.onpointerdown=event=>{if(!cameraSelect.value || points.length>=20)return;const rect=canvas.getBoundingClientRect();points.push([Math.min(1,Math.max(0,(event.clientX-rect.left)/rect.width)),Math.min(1,Math.max(0,(event.clientY-rect.top)/rect.height))]);draw();};
  $('#restaurant-zone-undo').onclick=()=>{points.pop();draw();};$('#restaurant-zone-clear').onclick=()=>{points=[];draw();};
  form.onsubmit=async event=>{
    event.preventDefault();const button=$('button[type=submit]',form);button.disabled=true;
    try{if(points.length<3)throw new Error('Нарисуйте зону минимум из трёх точек.');if(!cameraSelect.value)throw new Error('Выберите подключённую камеру.');const fields=new FormData(form),body={site_id:currentSite(),camera:cameraSelect.value,name:String(fields.get('name')).trim(),points,waiter_id:fields.get('waiter_id') || null,cleaner_id:fields.get('cleaner_id') || null,wait_seconds:Number(fields.get('wait_seconds')),arrival_seconds:Number(fields.get('arrival_seconds')),vacancy_seconds:Number(fields.get('vacancy_seconds'))};await restaurantWrite('/restaurant/tables'+(table?'/'+encodeURIComponent(table.id):''),{method:table?'PUT':'POST',body:JSON.stringify(body)});$('#modal').close();toast('Стол сохранён. Примените настройки камер.');await refreshRestaurantView();}
    catch(error){$('#restaurant-table-error').textContent=error.message;}finally{button.disabled=false;}
  };
  if(table)$('#restaurant-delete-table').onclick=()=>{
    modal('Удалить стол?',`<p>Зона «${escapeHTML(table.name)}» будет удалена из ресторанного модуля. Для удаления зоны из камеры затем примените настройки.</p><p class="error" id="restaurant-delete-error" role="alert"></p><div class="form-actions"><button class="danger" id="restaurant-confirm-delete">Удалить стол</button></div>`);
    $('#restaurant-confirm-delete').onclick=async event=>{event.target.disabled=true;try{await restaurantWrite('/restaurant/tables/'+encodeURIComponent(table.id),{method:'DELETE'});$('#modal').close();toast('Стол удалён. Примените настройки камер.');await refreshRestaurantView();}catch(error){$('#restaurant-delete-error').textContent=error.message;event.target.disabled=false;}};
  };
}

if (document.getElementById('workspace')) {
  setInterval(() => {
    if (typeof state === 'undefined' || !state.session || state.view !== 'restaurant' || document.hidden || refreshing || restaurantMutationBusy) return;
    refreshRestaurantView().catch(() => updateRestaurantFreshness());
  }, 2000);
  setInterval(updateRestaurantFreshness,1000);
}
