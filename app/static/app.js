'use strict';
const $ = (q, root = document) => root.querySelector(q);
const $$ = (q, root = document) => [...root.querySelectorAll(q)];
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const titles = {platform:'Организации и объекты',admission:'Допуск и посетители',overview:'Обзор', cameras:'Камеры', map:'Карта Казахстана', events:'События', analysis:'Анализ видео', system:'Система'};
const kinds = {intrusion:'Вход в запретную зону', fight:'Возможная драка', theft:'Возможная кража', person:'Человек в кадре'};
const symbols = {intrusion:'◇', fight:'ϟ', theft:'◈', person:'♙'};
const statuses = {new:'Не проверено', confirmed:'Подтверждено', dismissed:'Отклонено'};
const jobStatuses = {queued:'В очереди', running:'Анализируем', failed:'Ошибка', succeeded:'Готово'};
let state = {view:'overview', session:null, overview:null, cameras:[], cameraStatus:{}, events:[], jobs:[]};
let refreshing = false, uploadBusy = false, timer, imageTimer, toastTimer;

async function api(url, options = {}) {
  const headers = {...(options.headers || {})};
  if (state.session) headers['X-CSRF-Token'] = state.session.csrf;
  if (options.body && !(options.body instanceof Blob)) headers['Content-Type'] = 'application/json';
  const response = await fetch('/api' + url, {...options, headers});
  if (response.status === 401 && url !== '/login') { showLogin(); throw new Error('Сессия закончилась. Войдите снова.'); }
  const data = await response.json();
  if (!response.ok) throw new Error(Array.isArray(data.detail) ? data.detail.map(e => e.msg).join('. ') : data.detail || 'Не удалось выполнить запрос');
  return data;
}
function toast(message) { const node = $('#toast'); node.textContent = message; node.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => node.hidden = true, 5000); }
function timeLabel(at) { return new Date(at * 1000).toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'}); }
function dateLabel(at) { return new Date(at * 1000).toLocaleString('ru-RU', {day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit'}); }
function cameraName(id) { return id === 'webcam' ? 'Веб-камера' : id === 'upload' ? 'Загруженное видео' : state.cameras.find(c => c.id === id)?.name || id; }
function head(title, description, actions = '') { return `<div class="page-head"><div class="heading-with-help"><h1>${title}</h1>${description ? help(description, "О разделе «" + title + "»") : ""}</div><div class="head-actions">${actions}</div></div>`; }
function empty(title, description = '') { return `<div class="empty"><span>◎</span><div class="heading-with-help">${escapeHTML(title)}${description ? help(description) : ""}</div></div>`; }
function pill(label, type = '') { return `<span class="pill ${type}">${escapeHTML(label)}</span>`; }
function eventStatus(event) { return pill(statuses[event.status], event.status === 'new' ? 'warning' : event.status === 'dismissed' ? 'neutral' : ''); }
function showLogin() { hideHelp(); stopWebcam(); disposeCameraMap(); state.session = null; $('#login').hidden = false; $('#workspace').hidden = true; clearInterval(timer); clearInterval(imageTimer); }
function showWorkspace() {
  $('#login').hidden = true; $('#workspace').hidden = false; $('#demo-banner').hidden = !state.session.demo;
  $('#user-name').textContent = state.session.username;
  clearInterval(timer); timer = setInterval(refresh, 10000);
  clearInterval(imageTimer); imageTimer = setInterval(refreshImages, 2000);
  refresh();
}
async function refresh() {
  if (!state.session || refreshing) return;
  refreshing = true;
  try {
    const [overview, cameras, events, jobs, cameraStatus, admission, platform, allCameras] = await Promise.all([api(scopeUrl('/overview')), api(scopeUrl('/cameras')), api(scopeUrl('/events?limit=200' + (state.view === 'events' ? '&' + new URLSearchParams(Object.entries(filters).filter(([,v])=>v)) : ''))), api(scopeUrl('/jobs')), api('/cameras/status'), api(scopeUrl('/admission')), api('/platform'), api('/cameras')]);
    Object.assign(state, {overview, cameras, events, jobs, cameraStatus, admission, platform, allCameras});
    updateSiteSelector();
    $('#nav-count').textContent = overview.pending;
    $('#sync-status').textContent = overview.health.frigate === 'online' ? 'NVR подключён' : 'Нет связи с NVR';
    setHelp('#sync-status', overview.health.last_sync ? 'Последняя синхронизация: ' + timeLabel(overview.health.last_sync) : 'Frigate ещё не подключён');
    // Preserve filters and interaction focus; refresh tables only when idle outside forms.
    if (state.view === 'map' && !$('#modal').open) render();
    else if (!helpAnchor && !uploadBusy && !$('#video-file')?.files.length && !$('#modal').open && !['SELECT','INPUT','TEXTAREA'].includes(document.activeElement?.tagName)) render();
  } catch (error) { if (state.session) toast(error.message); }
  finally { refreshing = false; }
}
function navigate(view) { hideHelp(); state.view = view; render(); refresh(); }
function render() {
  if (!state.overview) return;
  $$('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.view === state.view));
  $('#breadcrumb').textContent = titles[state.view];
  if (state.view !== 'map') disposeCameraMap();
  const methods = {platform:renderPlatform,admission:renderAdmission,map:renderCameraMap, overview:renderOverview, cameras:renderCameras, events:renderEvents, analysis:renderAnalysis, system:renderSystem};
  $('#content').classList.toggle('platform-view',state.view==='platform');
  methods[state.view]();
  if (state.admission?.pending && state.view !== 'admission') {
    $('#content').insertAdjacentHTML('afterbegin', `<div class="warning-box" role="alert">Допуск не подтверждён: ${state.admission.pending} <button class="secondary" id="admission-alert">Проверить входящих</button></div>`);
    $('#admission-alert').onclick=()=>navigate('admission');
  }
  $$('.fight-alert', $('#content')).forEach(node => node.remove());
  if (state.overview.pending_fights > 0) {
    $('#content').insertAdjacentHTML('afterbegin', `<div class="fight-alert" role="alert"><div><strong>ϟ Возможная драка · ${state.overview.pending_fights}</strong>${help("Откройте запись и проверьте происходящее.", "Действия при сигнале драки")}</div><button class="secondary" id="open-fights">Проверить сигналы</button></div>`);
    $('#open-fights').onclick = () => {filters.kind='fight';filters.status='new';filters.camera='';navigate('events');};
  }
  bindActions(); refreshImages();
}
function cameraCard(camera) {
  return `<article class="camera-card"><div class="camera-picture"><div class="camera-offline ${state.session.demo ? 'demo' : ''}"><span>▣</span>${state.session.demo ? 'Пример камеры · без трансляции' : 'Ожидание сигнала камеры'}</div>${state.session.demo ? '' : `<img data-camera-img="${camera.id}" alt="Камера ${escapeHTML(camera.name)}" hidden>`}<span class="camera-label" data-camera-label="${camera.id}">${state.session.demo ? 'ДЕМОНСТРАЦИЯ' : 'ОЖИДАНИЕ СИГНАЛА'}</span></div><div class="camera-meta"><div><div class="heading-with-help"><h3>${escapeHTML(camera.name)}</h3>${help((camera.zones.length ? camera.zones.length + " зон наблюдения" : "Зоны не настроены") + " · " + (camera.analyze ? "анализ событий" : "контроль объектов"), "О камере " + camera.name)}</div></div><button class="icon-button" data-camera="${camera.id}" aria-label="Настройки камеры ${escapeHTML(camera.name)}">↗</button></div></article>`;
}
function addTile() { return `<button class="add-tile" data-action="add-camera" data-help="RTSP, HTTP(S), RTMP · любая марка"><span>+</span>Подключить камеру</button>`; }
function renderOverview() {
  const o = state.overview, count = Object.values(o.counts).reduce((a,b) => a + b, 0);
  const stats = [[o.cameras,'Камеры на объекте','▣','Настроенные подключения'],[count,'События за 24 часа','◎','Люди и сигналы моделей'],[o.pending,'Требуют внимания','◇','Ожидают проверки оператором'],[o.counts.fight || 0,'Сигналы возможной драки','ϟ','За последние 24 часа']];
  $('#content').innerHTML = head('Всё под наблюдением.', 'Важные события вашего объекта — в одном месте.', `<button class="secondary" data-action="refresh">↻ Обновить</button><button class="primary" data-action="add-camera">+ Подключить камеру</button>`) +
    `<div class="stats">${stats.map(([n,label,sym,note]) => `<div class="stat"><div class="stat-top">${label}${help(note, label)}</div><div class="stat-value">${n.toString().padStart(2,'0')}</div></div>`).join('')}</div><div class="grid-main"><section><div class="section-heading"><div class="heading-with-help"><h2>Камеры</h2>${help(state.session.demo ? "Примеры подключения" : "Кадр обновляется каждые 2 секунды", "О просмотре камер")}</div><button class="text-button" data-nav="cameras">Все камеры ↗</button></div><div class="camera-grid">${state.cameras.slice(0,3).map(cameraCard).join('')}${addTile()}</div></section><aside class="feed"><div class="feed-head"><h3>Последние события</h3>${pill('24 часа')}</div><div class="feed-items">${state.events.slice(0,5).map(e => `<button class="feed-item" data-event="${e.id}"><span class="event-icon ${e.kind}">${symbols[e.kind] || '◎'}</span><div><strong>${kinds[e.kind] || escapeHTML(e.kind)}</strong><p>${escapeHTML(cameraName(e.camera))}</p><p>${statuses[e.status]}</p></div><span class="feed-time">${timeLabel(e.started)}</span></button>`).join('') || empty('Пока спокойно','События появятся после подключения камеры.')}</div><div class="feed-footer"><button class="text-button" data-nav="events">Открыть журнал событий →</button></div></aside></div>`;
}
function renderCameras() {
  $('#content').innerHTML = head('Камеры и зоны', 'Подключите поток, отметьте запретную зону и примените настройки.', `<button class="secondary" data-nav="map">⌖ На карте</button><button class="secondary" data-action="apply">Применить настройки</button><button class="primary" data-action="add-camera">+ Подключить камеру</button>`) +
    `<div class="camera-grid">${state.cameras.map(cameraCard).join('')}${addTile()}</div>`;
}
let filters = {kind:'', status:'', camera:''};
function renderEvents() {
  const rows = state.events.filter(e => (!filters.kind || filters.kind === e.kind) && (!filters.status || filters.status === e.status) && (!filters.camera || filters.camera === e.camera));
  const options = (items, selected) => items.map(([v,label]) => `<option value="${escapeHTML(v)}" ${selected === v ? 'selected' : ''}>${escapeHTML(label)}</option>`).join('');
  $('#content').innerHTML = head('Журнал событий', 'Проверьте контекст и сохраните решение. Показаны последние 200 событий.', '<button class="secondary" data-action="export">↓ Экспорт JSON</button>') +
    `<div class="filters"><select aria-label="Тип события" data-filter="kind">${options([['','Все события'],...Object.entries(kinds)],filters.kind)}</select><select aria-label="Статус" data-filter="status">${options([['','Все статусы'],...Object.entries(statuses)],filters.status)}</select><select aria-label="Камера" data-filter="camera">${options([['','Все камеры'],...state.cameras.map(c => [c.id,c.name]),['upload','Загруженное видео'],['webcam','Веб-камера']],filters.camera)}</select></div>` +
    (rows.length ? `<div class="table-wrap"><table><thead><tr><th>Событие</th><th>Камера</th><th>Время</th><th>Оценка модели</th><th>Решение</th><th></th></tr></thead><tbody>${rows.map(e => `<tr data-event="${e.id}" tabindex="0" role="button" aria-label="Открыть ${kinds[e.kind] || e.kind}"><td><span class="table-title"><span class="event-icon ${e.kind}">${symbols[e.kind] || '◎'}</span>${kinds[e.kind] || escapeHTML(e.kind)}</span></td><td>${escapeHTML(cameraName(e.camera))}</td><td>${dateLabel(e.started)}</td><td>${e.score == null ? '—' : Math.round(e.score * 100) + '%'}</td><td>${eventStatus(e)}</td><td>↗</td></tr>`).join('')}</tbody></table></div>` : empty('Событий по этим фильтрам нет','Измените фильтры или подключите камеры.'));
  $$('[data-filter]').forEach(node => node.onchange = () => { filters[node.dataset.filter] = node.value; node.blur(); render(); refresh(); });
}
function modelCard(name, description, symbol, status) {
  const labels = {ready:'Модель загружена', missing:'Нужны файлы модели', error:'Ошибка загрузки', unavailable:'Сервис не запущен', demo:'Не выполняется в демо'};
  return `<article class="panel model-card"><div class="model-heading"><span class="model-symbol">${symbol}</span><h3>${name}</h3>${help(description, "О модели «" + name + "»")}</div>${pill(labels[status] || status, status === 'ready' ? '' : 'warning')}</article>`;
}
function renderAnalysis() {
  const analyzer = state.overview.health.analyzer;
  const model = name => state.session.demo ? 'demo' : analyzer.models?.find(m => m.id === name)?.state || 'unavailable';
  $('#content').innerHTML = head('Анализ видео', 'Загрузите запись или запишите фрагмент с веб-камеры. Оценка модели не является вероятностью правонарушения. Мимика не используется для оценки намерений.') +
    `<div class="model-grid">${modelCard('Возможная драка',model('fight') === 'ready' ? 'VD-MIL / MoViNet · обученная модель насилия, сигнал по последовательным окнам.' : model('vadclip') === 'ready' ? 'VadCLIP · резервная модель действий.' : 'VD-MIL / MoViNet · установите модель драк по инструкции проекта.','ϟ',model('fight') === 'ready' ? 'ready' : model('vadclip') === 'ready' ? 'ready' : model('fight'))}${modelCard('Возможная кража','VadCLIP · сигнал для просмотра и проверки оператором.','◈',model('vadclip'))}${modelCard('Мимика лица','MediaPipe · движения бровей, глаз и рта, без идентификации.','◉',model('face'))}</div><div class="source-grid"><div class="upload-zone"><div class="heading-with-help"><h3>Видеофайл</h3>${help("MP4, MOV, WebM, MKV, AVI · до 200 МБ · до 120 секунд · до 4K. Если сервис моделей выключен, запись останется в очереди.", "Форматы и ограничения видео")}</div><input id="video-file" type="file" accept="video/*,.mp4,.mov,.webm,.mkv,.avi" aria-label="Выбрать видео" ${state.session.demo ? 'disabled' : ''}><button class="primary" data-action="upload" ${state.session.demo ? 'disabled' : ''}>Запустить анализ</button></div><div class="panel webcam-entry"><div class="heading-with-help"><h3>Веб-камера</h3>${help("Камера ноутбука, USB-камера или камера телефона. Предпросмотр и анализ записи до 120 секунд. Требуется HTTPS.", "О записи с веб-камеры")}</div><span class="source-symbol" aria-hidden="true">▣</span><button class="primary" data-action="webcam" ${state.session.demo ? 'disabled' : ''}>Открыть веб-камеру</button></div></div><div class="section-heading"><h2>История анализа</h2></div><div class="panel">${state.jobs.length ? state.jobs.map(j => `<div class="job-card"><div><strong>${escapeHTML(cameraName(j.camera))}</strong><p>${dateLabel(j.created)}</p>${j.error ? `<p class="error">${escapeHTML(j.error)}</p>` : ""}</div>${pill(jobStatuses[j.state],j.state === 'failed' ? 'alert' : j.state === 'succeeded' ? '' : 'warning')}${j.state === 'succeeded' ? `<button class="secondary" data-job="${j.id}">Результат ↗</button>` : ''}${j.state === 'failed' && j.has_file ? `<button class="secondary" data-retry="${j.id}">Повторить</button>` : ''}</div>`).join('') : empty('Записей ещё нет','Загрузите видео или включите анализ событий камеры.')}</div>`;
}
function renderSystem() {
  const o = state.overview, online = o.health.frigate === 'online';
  const monitor = o.health.fight_monitor || {};
  const monitorLabels = {waiting:'Ожидание', model_missing:'Модель драк не подключена', nvr_offline:'Нет связи с NVR', monitoring:'Проверяем фрагменты с людьми', no_cameras:'Включите анализ у камеры', backlog:'Очередь задерживает проверку'};
  $('#content').innerHTML = head('Состояние системы', 'Подключения, модели и история действий оператора.') +
    `<div class="system-grid"><div class="panel"><div class="heading-with-help"><h3>Подключения</h3>${help("Видеоархив: Frigate 0.18.2. Пропущено окон анализа после сбоя или перегрузки: " + Number(monitor.skipped_windows || 0) + ". Разрывы записываются в аудит.", "Диагностика подключений")}</div><div class="status-row">Панель Glazza ${pill('Работает')}</div><div class="status-row">Видеоархив ${pill(state.session.demo ? 'Демонстрация' : online ? 'Подключён' : 'Нет связи',online ? '' : 'warning')}</div><div class="status-row">Сервис анализа ${pill(o.health.analyzer.state === 'ready' ? 'Готов' : 'Не готов',o.health.analyzer.state === 'ready' ? '' : 'warning')}</div><div class="status-row">Контроль драк ${pill(state.session.demo ? 'Демонстрация' : monitorLabels[monitor.state] || 'Ожидание', monitor.state === 'monitoring' ? '' : 'warning')}</div><div class="status-row"><span class="heading-with-help">Хранение журнала ${help("Видео для анализа удаляется через 24 часа после завершения задания. Архив Frigate хранится отдельно.", "О сроках хранения")}</span><span>${o.retention_days} дней</span></div></div><div class="panel"><div class="heading-with-help"><h3>Настройки камер</h3>${help("После добавления камеры или изменения зон примените конфигурацию. Frigate перезапустится; соединение восстановится автоматически. Драки проверяются по коротким фрагментам во время события с задержкой записи и очереди. Кража и мимика — после завершения события.", "О применении настроек")}</div><button class="primary" data-action="apply" ${state.session.demo ? 'disabled' : ''}>Применить и перезапустить Frigate</button></div></div><div class="section-heading"><h2>Журнал действий</h2></div><div id="audit-list" class="panel"><p>Загрузка…</p></div>`;
  api('/audit').then(items => { if (!$('#audit-list')) return; $('#audit-list').innerHTML = items.length ? items.slice(0,30).map(i => `<div class="status-row"><span>${escapeHTML(i.actor)} · ${escapeHTML(i.action)}</span><small>${dateLabel(i.at)}</small></div>`).join('') : 'Действий пока нет'; }).catch(e => toast(e.message));
}
function refreshImages() {
  if (state.session?.demo || document.hidden) return;
  $$('[data-camera-img]').forEach(img => {
    if (img.dataset.loading === 'true') return;
    img.dataset.loading = 'true';
    img.onload = () => {
      img.hidden = false; img.dataset.loading = 'false';
      const label = $(`[data-camera-label="${img.dataset.cameraImg}"]`);
      if (label) label.textContent = state.cameraStatus[img.dataset.cameraImg]?.signal === 'online' ? 'СИГНАЛ · ' + new Date().toLocaleTimeString('ru-RU') : 'ПОСЛЕДНИЙ КАДР';
    };
    img.onerror = () => { img.hidden = true; img.dataset.loading = 'false'; const label = $(`[data-camera-label="${img.dataset.cameraImg}"]`); if (label) label.textContent = 'НЕТ СИГНАЛА'; };
    img.src = '/api/cameras/' + img.dataset.cameraImg + '/snapshot?t=' + Date.now();
  });
}
function modal(title, body, explanation = '') { hideHelp(); stopWebcam(); $('#modal-title').textContent = title; $('#modal-help').innerHTML = explanation ? help(explanation, 'Помощь: ' + title) : ''; $('#modal-body').innerHTML = body; if (!$('#modal').open) $('#modal').showModal(); }
function addCamera(location = null) {
  if (state.session.demo) return toast('Подключение доступно в рабочем режиме. Демонстрация не управляет оборудованием.');
  const position = location && hasCameraLocation(location) ? {latitude: location.latitude, longitude: location.longitude} : {};
  modal(location ? 'Камера в выбранной точке' : 'Подключить камеру', `<form id="camera-form"><div class="form-row"><label>Название<input name="name" placeholder="Главный вход" required maxlength="80"></label><label>Идентификатор<input name="id" placeholder="entrance" pattern="[a-z][a-z0-9_]{1,39}" required></label></div><label>Адрес видеопотока<input name="stream" placeholder="rtsp://user:password@192.168.1.50:554/stream" required autocomplete="off" type="password"></label><label><input type="checkbox" name="analyze">Анализ событий объекта</label><p class="small"><button type="button" class="text-button" data-camera-webcam>Тест веб-камеры →</button></p><p class="error" id="form-error" role="alert"></p><div class="form-actions"><button type="submit" class="primary">Сохранить камеру</button></div></form>`, "Поддерживаются RTSP/RTSPS, HTTP(S) и RTMP(S) любой марки. Для ONVIF нужен RTSP-адрес профиля, а не страница входа. Для анализа нужны установленные модели. Пароль камеры после сохранения не возвращается в браузер. Расположение можно указать в разделе «Карта Казахстана».");
  $('[data-camera-webcam]').onclick = openWebcam;
  $('#camera-form').onsubmit = async event => {
    event.preventDefault(); const form = new FormData(event.target); const button = $('button[type=submit]',event.target); button.disabled = true;
    try { await api('/cameras', {method:'POST',body:JSON.stringify({site_id:selectedSite||'default',id:form.get('id'),name:form.get('name'),stream:form.get('stream'),analyze:form.get('analyze') === 'on',zones:[],...position})}); $('#modal').close(); toast('Камера сохранена. Примените настройки, чтобы подключить поток.'); await refresh(); if (location && state.view === 'map') openCameraMarker(form.get('id')); }
    catch(error) { $('#form-error').textContent = error.message; } finally { button.disabled = false; }
  };
}
function cameraSettings(id) {
  const cam = state.cameras.find(c => c.id === id);
  if (state.session.demo) { modal(cam.name, '<div class="detail-media">Демонстрационная камера · реального потока нет</div>'); return; }
  let zones = structuredClone(cam.zones), points = [], background;
  modal(cam.name + ' · зоны', `<canvas id="zone-canvas" class="zone-canvas" width="960" height="540" aria-label="Редактор зоны"></canvas><div id="zone-list" class="zone-list"></div><div class="form-row"><label>Имя зоны<input id="zone-name" placeholder="restricted_area" value="restricted_area" pattern="[a-z][a-z0-9_]{1,39}"></label><div class="form-actions"><button id="zone-clear" class="secondary">Очистить</button><button id="zone-add" class="secondary">+ Добавить зону</button></div></div><p id="zone-error" class="error" role="alert"></p><div class="form-actions"><button id="camera-location" class="secondary">⌖ На карте</button><button id="camera-connection" class="secondary">Изменить подключение</button><button id="camera-delete" class="danger">Удалить</button><button id="zone-save" class="primary">Сохранить зоны</button></div>`, "Нажмите на кадр, чтобы поставить вершины запретной зоны (3–20 точек). Добавьте зону и сохраните изменения. После изменения зон примените настройки камер.");
  $('#camera-location').onclick = () => openCameraOnMap(id);
  const canvas = $('#zone-canvas'), ctx = canvas.getContext('2d');
  function draw() {
    ctx.clearRect(0,0,960,540); ctx.fillStyle = '#edf3fb'; ctx.fillRect(0,0,960,540);
    if (background) ctx.drawImage(background,0,0,960,540); else { ctx.fillStyle = '#516783'; ctx.font = '18px sans-serif'; ctx.fillText('Нет сигнала камеры',45,260); }
    for (const polygon of [...zones.map(z => z.points), points]) {
      if (!polygon.length) continue;
      ctx.beginPath(); polygon.forEach(([x,y],i) => i ? ctx.lineTo(x*960,y*540) : ctx.moveTo(x*960,y*540)); ctx.closePath(); ctx.fillStyle = '#63a9ff3b'; ctx.fill(); ctx.lineWidth = 3; ctx.strokeStyle = '#7bbaff'; ctx.stroke();
      polygon.forEach(([x,y]) => { ctx.beginPath();ctx.arc(x*960,y*540,5,0,Math.PI*2);ctx.fillStyle = '#d6eaff';ctx.fill(); });
    }
    $('#zone-list').innerHTML = zones.map((z,i) => `<button class="zone-chip" data-remove-zone="${i}">${escapeHTML(z.name)} ×</button>`).join('');
    $$('[data-remove-zone]').forEach(button => button.onclick = () => { zones.splice(Number(button.dataset.removeZone),1); draw(); });
  }
  const img = new Image(); img.onload = () => { background = img; draw(); }; img.src = '/api/cameras/' + id + '/snapshot'; draw();
  canvas.onpointerdown = event => { if(points.length >= 20) return; const rect = canvas.getBoundingClientRect(); points.push([Math.min(1,Math.max(0,(event.clientX-rect.left)/rect.width)),Math.min(1,Math.max(0,(event.clientY-rect.top)/rect.height))]); draw(); };
  $('#zone-clear').onclick = () => { points = []; draw(); };
  $('#zone-add').onclick = () => { const name = $('#zone-name').value; if (points.length < 3 || !/^[a-z][a-z0-9_]{1,39}$/.test(name) || zones.some(z => z.name === name)) { $('#zone-error').textContent = 'Поставьте минимум 3 точки и задайте уникальное имя латиницей.'; return; } zones.push({name,points,restricted:true}); points=[]; $('#zone-error').textContent=''; draw(); };
  $('#zone-save').onclick = async () => { try { if(points.length) throw new Error('Сначала добавьте нарисованную зону или очистите точки.'); await api('/cameras/'+id+'/zones',{method:'PUT',body:JSON.stringify({zones})}); $('#modal').close(); toast('Зоны сохранены. Примените настройки.'); refresh(); } catch(e) { $('#zone-error').textContent=e.message; } };
  $('#camera-delete').onclick = () => { modal('Удалить камеру?',`<p>Камера «${escapeHTML(cam.name)}» будет удалена из конфигурации. Записи в журнале сохранятся до окончания срока хранения.</p><div class="form-actions"><button id="confirm-delete" class="danger">Удалить камеру</button></div>`); $('#confirm-delete').onclick = async()=> { try { await api('/cameras/'+id,{method:'DELETE'});$('#modal').close();toast('Камера удалена. Примените настройки.');refresh(); } catch(e){toast(e.message);} }; };
  $('#camera-connection').onclick = () => { modal('Настройки подключения', `<form id="connection-form"><label>Название<input name="name" value="${escapeHTML(cam.name)}" required maxlength="80"></label><label>Новый адрес видеопотока<input name="stream" type="password" required autocomplete="off"></label><label><input type="checkbox" name="analyze" ${cam.analyze ? 'checked' : ''}>Анализ событий объекта</label><p id="form-error" class="error"></p><div class="form-actions"><button type="submit" class="primary">Сохранить</button></div></form>`); $('#connection-form').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);try{await api('/cameras/'+id,{method:'PUT',body:JSON.stringify({...cam,name:f.get('name'),stream:f.get('stream'),analyze:f.get('analyze')==='on'})});$('#modal').close();toast('Сохранено. Примените настройки.');refresh();}catch(error){$('#form-error').textContent=error.message;}}; };
}
async function openEvent(id) {
  let e = state.events.find(e => e.id === id);
  if (!e) { try { e = await api('/events/' + id); } catch (error) { toast(error.message); return; } }
  modal(kinds[e.kind] || e.kind, `<div class="detail-media">${e.source === 'demo' ? 'Пример события · видеозапись не создавалась' : `<video controls preload="metadata" src="/api/events/${e.id}/media/clip" aria-label="Видео события"></video>`}</div><p id="media-error" class="error"></p><div class="detail-meta">${pill(cameraName(e.camera))}${pill(dateLabel(e.started),'neutral')}${eventStatus(e)}</div>${help((e.kind === "person" ? "Обнаружение человека в кадре." : "Проверьте запись перед решением. Сигнал не является доказательством нарушения.") + (e.score == null ? "" : " Оценка модели: " + Math.round(e.score*100) + "%."), "О сигнале модели")}<label>Комментарий<textarea id="review-note" maxlength="2000" placeholder="Что произошло на самом деле?">${escapeHTML(e.note)}</textarea></label><div class="form-actions"><button class="secondary" data-review="new">На проверку</button><button class="secondary" data-review="dismissed">Отклонить</button><button class="primary" data-review="confirmed">Подтвердить</button></div>`);
  const video = $('video', $('#modal')); if(video) video.onerror=()=>$('#media-error').textContent='Запись ещё не готова, недоступна или удалена по сроку хранения.';
  $$('[data-review]').forEach(button=>button.onclick=async()=> { try{await api('/events/'+id,{method:'PATCH',body:JSON.stringify({status:button.dataset.review,note:$('#review-note').value})});$('#modal').close();toast('Решение сохранено');refresh();}catch(error){toast(error.message);} });
}
function openJob(id) {
  const j=state.jobs.find(j=>j.id===id), r=j?.result;if(!r)return;
  const face = r.face || {}, labels={mouthSmileLeft:'Улыбка слева',mouthSmileRight:'Улыбка справа',eyeBlinkLeft:'Моргание слева',eyeBlinkRight:'Моргание справа',browInnerUp:'Поднятие бровей',jawOpen:'Открытие рта'};
  modal('Результат анализа',`${j.has_file ? `<div class="detail-media"><video controls src="/api/jobs/${id}/video"></video></div><p id="job-media-error" class="small muted"></p><a class="text-button" href="/api/jobs/${id}/video" download>Скачать исходное видео</a>` : '<p class="muted">Исходное видео удалено по сроку хранения.</p>'}<div class="result-help">${help((r.model || "Анализ мимики") + " · " + (r.duration_seconds?.toFixed(1) || "—") + " с", "Модель и длительность анализа")}</div>${r.detections?.length ? r.detections.map(d=>`<div class="warning-box">${kinds[d.kind]} · ${Math.round(d.score*100)}%${d.start_seconds != null ? ' · ' + Number(d.start_seconds).toFixed(1) + '–' + Number(d.end_seconds).toFixed(1) + ' с' : ''} · требуется проверка</div>`).join('') : `<div class="success-box heading-with-help">Сигналы не обнаружены ${help("Подключённые модели не сформировали сигналов. Это не гарантирует отсутствие события.", "О результате анализа")}</div>`}${r.fight ? `${help("VD-MIL: " + Number(r.fight.windows_analyzed) + " окон · порог " + Math.round(r.fight.threshold*100) + "% · максимум " + Math.round(r.fight.peak_score*100) + "%", "Подробности анализа драки")}` : ''}<div class="heading-with-help"><h3>Мимика лица</h3>${help((face.frames_with_faces || 0) + " кадров с лицом. Максимальные значения по видимым лицам; без идентификации человека.", "Как читать показатели мимики")}</div><div class="blend-grid">${Object.entries(face.peak_blendshapes || {}).filter(([name])=>labels[name]).map(([name,v])=>`<div class="blend-item">${labels[name]}<strong>${Math.round(v*100)}%</strong></div>`).join('')}</div>${(r.warnings||[]).map(w=>`<p class="warning-box">${escapeHTML(w)}</p>`).join('')}`);
  const player = $('#modal video');
  if (player) player.onerror = () => { $('#job-media-error').textContent = 'Браузер не воспроизводит этот формат. Скачайте исходное видео; результат анализа доступен ниже.'; };
}
async function applySettings() {
  if(state.session.demo)return toast('Демонстрация не меняет оборудование.');
  modal('Применить настройки?', '<p>Frigate перезапустится с сохранёнными камерами и зонами. Потоки на короткое время прервутся.</p><div class="form-actions"><button class="primary" id="confirm-apply">Применить и перезапустить</button></div>');
  $('#confirm-apply').onclick=async()=>{const b=$('#confirm-apply');b.disabled=true;try{const r=await api('/config/apply',{method:'POST'});$('#modal').close();toast(r.restarted?'Настройки применены. Frigate перезапускается.':r.detail);refresh();}catch(e){toast(e.message);b.disabled=false;}};
}
async function uploadVideo(button) {
  const input=$('#video-file'), file=input?.files[0];if(!file)return toast('Выберите видео для анализа.');if(file.size>200*1024*1024)return toast('Максимальный размер — 200 МБ.');
  uploadBusy=true;button.disabled=true;button.textContent='Загружаем…';
  try{await api(scopeUrl('/jobs/upload'),{method:'POST',body:file,headers:{'Content-Type':file.type || 'application/octet-stream'}});input.value='';toast('Видео добавлено в очередь');}catch(e){toast(e.message);}finally{uploadBusy=false;button.disabled=false;button.textContent='Запустить анализ';refresh();}
}
function bindActions() {
  $$('[data-nav]').forEach(b=>b.onclick=()=>navigate(b.dataset.nav));
  $$('[data-camera]').forEach(b=>b.onclick=()=>cameraSettings(b.dataset.camera));
  $$('[data-event]').forEach(b=>{b.onclick=()=>openEvent(b.dataset.event);b.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();openEvent(b.dataset.event);}};});
  $$('[data-job]').forEach(b=>b.onclick=()=>openJob(b.dataset.job));
  $$('[data-retry]').forEach(b=>b.onclick=async()=>{try{await api('/jobs/'+b.dataset.retry+'/retry',{method:'POST'});refresh();}catch(e){toast(e.message);}});
  $$('[data-action]').forEach(button=>button.onclick=()=>{
    const action=button.dataset.action;
    if(action==='webcam')openWebcam();if(action==='add-camera')addCamera();if(action==='refresh')refresh();if(action==='apply')applySettings();if(action==='upload')uploadVideo(button);
    if(action==='export'){const a=document.createElement('a');const url=URL.createObjectURL(new Blob([JSON.stringify(state.events,null,2)],{type:'application/json'}));a.href=url;a.download='glazza-events.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  });
}
$('#site-selector').onchange=async e=>{if(refreshing||uploadBusy){e.target.value=selectedSite;return;}selectedSite=e.target.value;e.target.blur();filters={kind:'',status:'',camera:''};disposeCameraMap();await refresh();};
$('#login-form').onsubmit=async event=>{event.preventDefault();const f=new FormData(event.target),button=$('button',event.target);button.disabled=true;$('#login-error').textContent='';try{state.session=await api('/login',{method:'POST',body:JSON.stringify({username:f.get('username'),password:f.get('password')})});$('input[name=password]',event.target).value='';showWorkspace();}catch(e){$('#login-error').textContent=e.message;}finally{button.disabled=false;}};
$('#logout').onclick=async()=>{try{await api('/logout',{method:'POST'});}finally{showLogin();}};
$('#mobile-logout').onclick=$('#logout').onclick;
$$('.nav-item').forEach(b=>b.onclick=()=>navigate(b.dataset.view));
$('#modal-close').onclick=()=>$('#modal').close();
$('#modal').onclick=e=>{if(e.target===$('#modal')){const r=$('#modal').getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)$('#modal').close();}};
setInterval(()=>$('#clock').textContent=new Date().toLocaleString('ru-RU',{day:'numeric',month:'long',hour:'2-digit',minute:'2-digit'}),1000);
api('/session').then(session=>{state.session=session;showWorkspace();}).catch(()=>showLogin());
