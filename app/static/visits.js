'use strict';

const visitStatuses = {observed:'В кадре', transit:'Между камерами', uncertain:'Связь неясна', exited:'Вышел', expired:'Наблюдение завершено'};
let visitScreen = null;

function stopVisitMonitor() {
  if (visitScreen) clearTimeout(visitScreen.timer);
  visitScreen = null;
}

function visitTime(at) {
  return Number.isFinite(at) ? new Date(at * 1000).toLocaleString('ru-RU', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—';
}

function visitPath(visitId, item) {
  if (!Array.isArray(item?.path)) return '';
  const points = item.path.filter(sample => Array.isArray(sample?.[0]) && sample[0].length === 2 &&
    sample[0].every(value => Number.isFinite(value) && value >= 0 && value <= 1) && Number.isFinite(sample[1]));
  if (!points.length) return '';
  const coords = points.map(([[x, y]]) => [(x * 640).toFixed(1), (y * 360).toFixed(1)]);
  const [x, y] = coords[coords.length - 1].map(Number);
  const shortId = String(visitId).length > 16 ? String(visitId).slice(0, 12) + '…' : visitId;
  return `<figure class="visit-path"><figcaption><strong>Траектория в кадре · ${escapeHTML(cameraName(item.camera))}</strong>
    <small>Последняя точка · ${visitTime(points[points.length - 1][1])}</small></figcaption>
    <svg viewBox="0 0 640 360" role="img" aria-label="Положение и движение в координатах кадра">
      <polyline points="${coords.map(point => point.join(',')).join(' ')}" fill="none" stroke="#335dae" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="${x}" cy="${y}" r="8" fill="#1f50ac" stroke="white" stroke-width="3"/>
      <text x="${x > 320 ? x-12 : x+12}" y="${Math.max(23, Math.min(347, y-9))}" text-anchor="${x > 320 ? 'end' : 'start'}" font-size="18" fill="#17315c">ID ${escapeHTML(shortId)}</text>
    </svg></figure>`;
}

function renderVisits() {
  const site = state.platform?.sites.find(item => item.id === selectedSite && item.profile === 'retail');
  if (!site) {
    stopVisitMonitor();
    $('#content').innerHTML = head('Визиты посетителей', '', '<button class="secondary" data-nav="overview">В обзор</button>') + empty('Выберите магазин в фильтре объектов');
    return;
  }
  if (visitScreen?.siteId === site.id && visitScreen.panel.isConnected) return;
  stopVisitMonitor();
  $('#content').innerHTML = head('Визиты посетителей', 'При входе через любую настроенную дверь система выдаёт временный ID визита. Переход между камерами оценивается по цвету одежды и времени в пути. При неоднозначном совпадении проверьте записи камер.', '<button class="secondary" data-nav="overview">В обзор</button><button class="primary" id="visit-setup">Камеры и переходы</button>') +
    `<section id="visit-monitor"><div class="visit-toolbar"><div role="group" aria-label="Фильтр визитов">
       <button class="secondary" data-visit-filter="active" aria-pressed="true">Активные</button>
       <button class="secondary" data-visit-filter="all" aria-pressed="false">Все визиты</button></div>
       <span id="visit-updated" role="status">Загрузка…</span></div>
       <p id="visit-alert" class="warning-box" role="status" hidden></p><div class="visit-columns">
       <section class="panel"><h2>Последние визиты</h2><div id="visit-list"></div></section>
       <section class="panel" id="visit-detail" aria-label="Маршрут визита">Выберите визит, чтобы увидеть его маршрут.</section>
       </div></section>`;
  const panel = $('#visit-monitor');
  const screen = {siteId:site.id, panel, timer:null, active:true, selected:null, selectedPath:null, lastVisit:null, generation:0,
                  listHTML:null, detailHTML:null};
  visitScreen = screen;
  const endpoint = '/retail/visits/' + encodeURIComponent(site.id);
  const current = generation => visitScreen === screen && generation === screen.generation && panel.isConnected
    && state.session && state.view === 'visits' && selectedSite === site.id;
  const alert = $('#visit-alert', panel);
  const updated = $('#visit-updated', panel);
  const list = $('#visit-list', panel);
  const detail = $('#visit-detail', panel);

  function updateContent(node, key, html) {
    if (html === screen[key]) return;
    const focused = node.contains(document.activeElement) ? document.activeElement : null;
    const focusId = focused && (focused.dataset.visitId || focused.dataset.visitClip || focused.dataset.visitPath || focused.dataset.visitCamera);
    node.innerHTML = html;
    screen[key] = html;
    if (focusId) [...node.querySelectorAll('button')].find(item => Object.values(item.dataset).includes(focusId))?.focus({preventScroll:true});
  }

  function visitRows(visits, configured) {
    if (!visits.length) return empty(configured ? screen.active ? 'Активных визитов пока нет' : 'Визитов пока нет' : 'Сначала настройте входы и переходы');
    return visits.map(visit => {
      const camera = visit.active_camera || visit.last_camera;
      const cameraLabel = camera ? escapeHTML(cameraName(camera)) : 'Камера неизвестна';
      return `<button class="visit-row" data-visit-id="${escapeHTML(visit.id)}" aria-pressed="${screen.selected === visit.id}">
        <strong>ID ${escapeHTML(visit.id)}</strong><span>${escapeHTML(visitStatuses[visit.status] || visit.status)} · ${cameraLabel}</span>
        <small>Последнее наблюдение · ${visitTime(visit.last_seen_at)}</small></button>`;
    }).join('');
  }

  function routeMarkup(visit) {
    const liveCamera = visit.status === 'observed' && visit.active_camera;
    const camera = liveCamera || visit.last_camera;
    const cameraInSite = liveCamera && state.cameras.some(item => item.id === liveCamera && item.site_id === site.id);
    const observations = visit.timeline || [];
    const entrance = observations.find(item => item.role === 'entrance');
    const selected = observations.find(item => item.origin_event && item.origin_event === screen.selectedPath);
    const path = visitPath(visit.id, selected || observations[observations.length - 1]);
    const timeline = observations.map(item => {
      const title = {entrance:'Вход',observation:'Наблюдение',exit:'Выход'}[item.role] || 'Наблюдение';
      return `<li><div><strong>${title} · ${escapeHTML(cameraName(item.camera))}</strong><span>${visitTime(item.started_at)}${Number.isFinite(item.ended_at) ? ' — ' + visitTime(item.ended_at) : ''}${item.zone ? ' · ' + escapeHTML(item.zone) : ''}</span></div>
        <div class="visit-timeline-actions">${item.path?.length && item.origin_event ? `<button class="text-button" data-visit-path="${escapeHTML(item.origin_event)}">Траектория</button>` : ''}
        ${item.has_clip && item.origin_event ? `<button class="text-button" data-visit-clip="${escapeHTML(item.origin_event)}">Запись</button>` : ''}</div></li>`;
    }).join('');
    return `<h2 class="visit-id">ID ${escapeHTML(visit.id)}</h2>${pill(visitStatuses[visit.status] || visit.status, visit.status === 'uncertain' ? 'warning' : 'neutral')}
      <p>${entrance?.camera ? `<span data-visit-origin>Вход · <strong>${escapeHTML(cameraName(entrance.camera))}</strong></span><br>` : ''}${liveCamera ? 'Текущая камера' : 'Последняя камера'} · <strong>${escapeHTML(camera ? cameraName(camera) : '—')}</strong><br>
      Последнее наблюдение · ${visitTime(visit.last_seen_at)}</p>
      ${cameraInSite ? `<button type="button" class="secondary" data-visit-camera="${escapeHTML(liveCamera)}">Открыть кадр камеры</button>` : ''}
      ${path}<h3>Маршрут визита</h3><ol class="visit-timeline">${timeline}</ol>`;
  }

  async function load() {
    clearTimeout(screen.timer);
    const generation = ++screen.generation;
    let interval = 5;
    if (!current(generation)) return;
    try {
      const data = await api(endpoint + '?limit=100&active_only=' + screen.active);
      if (!current(generation)) return;
      const visits = data.visits || [];
      interval = Number.isFinite(data.poll_seconds) ? Math.max(1, Math.min(30, data.poll_seconds)) : 5;
      if (!visits.some(visit => visit.id === screen.selected)) screen.selected = visits[0]?.id || null;
      const message = !data.configured ? 'Укажите входы магазина и направления переходов между камерами.'
        : data.state === 'invalidated' ? 'Отслеживание приостановлено. Проверьте камеры и переходы.' : '';
      alert.hidden = !message;
      alert.textContent = message;
      updated.textContent = 'Обновлено · ' + visitTime(data.checked_at);
      updateContent(list, 'listHTML', visitRows(visits, data.configured));
      if (screen.selected) {
        const visit = await api(endpoint + '/' + encodeURIComponent(screen.selected));
        if (current(generation)) {
          screen.lastVisit = visit;
          updateContent(detail, 'detailHTML', routeMarkup(visit));
        }
      } else updateContent(detail, 'detailHTML', 'Здесь появится маршрут выбранного визита.');
    } catch (error) {
      if (!current(generation)) return;
      updated.textContent = 'Нет актуальных данных';
      alert.textContent = 'Не удалось обновить визиты. ' + error.message;
      alert.hidden = false;
      updateContent(list, 'listHTML', '');
      updateContent(detail, 'detailHTML', 'Ожидание связи с сервером.');
    } finally {
      if (current(generation)) screen.timer = setTimeout(load, interval * 1000);
    }
  }

  $('#visit-setup').onclick = () => openVisitSetup(site.id, load);
  panel.onclick = event => {
    const button = event.target.closest('button');
    if (!button || !panel.contains(button)) return;
    if (button.hasAttribute('data-visit-filter')) {
      screen.active = button.dataset.visitFilter === 'active';
      $$('[data-visit-filter]', panel).forEach(item => item.setAttribute('aria-pressed', String(item === button)));
      load();
    } else if (button.dataset.visitId) {
      screen.selected = button.dataset.visitId;
      screen.selectedPath = null;
      load();
    } else if (button.dataset.visitPath && screen.lastVisit?.id === screen.selected) {
      screen.selectedPath = button.dataset.visitPath;
      updateContent(detail, 'detailHTML', routeMarkup(screen.lastVisit));
    } else if (button.dataset.visitClip) {
      const url = '/api/events/' + encodeURIComponent(button.dataset.visitClip) + '/media/clip';
      modal('Запись наблюдения', `<div class="detail-media"><video controls preload="metadata" src="${url}"></video></div><p class="error" data-visit-media-error role="status" hidden>Запись недоступна.</p>`);
      const error = $('[data-visit-media-error]', $('#modal'));
      $('#modal video').onerror = () => error.hidden = false;
    } else if (button.dataset.visitCamera) {
      const id = button.dataset.visitCamera;
      modal(cameraName(id), `<div class="camera-picture"><span class="camera-offline">Ожидание кадра</span><img data-camera-img="${escapeHTML(id)}" alt="Кадр камеры" hidden></div>`);
      refreshImages();
    }
  };
  load();
}
