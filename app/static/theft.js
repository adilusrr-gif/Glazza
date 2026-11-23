'use strict';

const theftStatuses = {new:'На проверке', confirmed:'Отмечено для разбора', dismissed:'Сигнал отклонён'};

function openTheftQueue(status = 'new') {
  filters = {kind:'theft', status, camera:''};
  navigate('events');
}

function retailOverview(site) {
  const routes = state.overview.retail_routes;
  const activeRoutes = routes?.active || 0;
  const routeSignals = (routes?.camera_ids || []).filter(id => state.cameraStatus[id]?.signal === 'online').length;
  const routeStatus = state.session.demo ? 'Демонстрационный режим'
    : !routes ? 'Состояние недоступно'
    : activeRoutes ? `Камер с маршрутом: ${activeRoutes} · с сигналом: ${routeSignals}`
    : 'Маршрут не настроен';
  const health = state.overview.health;
  const monitor = health.theft_monitor || {};
  const eligible = state.cameras.filter(camera => camera.analyze);
  const receiving = eligible.filter(camera => state.cameraStatus[camera.id]?.signal === 'online');
  const busy = eligible.some(camera => monitor.busy_cameras?.includes(camera.id));
  let status;
  if (state.session.demo) status = 'Демонстрационный режим';
  else if (!site.modules.includes('theft')) status = 'Модуль выключен на объекте';
  else if (!eligible.length) status = 'Включите анализ событий у камеры';
  else if (health.analyzer.models?.find(model => model.id === 'vadclip')?.state !== 'ready') status = 'Модель VadCLIP недоступна';
  else if (health.frigate !== 'online' || !receiving.length) status = 'Нет подтверждённого сигнала с камер';
  else if (busy) status = 'Анализ отстаёт, очередь заполнена';
  else if (monitor.state === 'error') status = 'Ошибка планирования анализа';
  else if (receiving.some(camera => monitor.camera_ids?.includes(camera.id)) && ['monitoring','backlog'].includes(monitor.state)) status = 'Проверяются фрагменты с людьми';
  else status = 'Ожидание запуска мониторинга';
  return `<section id="retail-monitor" class="panel">
    <div class="section-heading"><h2>Контроль потерь</h2><button class="text-button" data-action="theft-history">Журнал сигналов →</button></div>
    <div class="retail-coverage"><div><strong>Маршрут по зонам</strong><span>${escapeHTML(routeStatus)}</span>
      ${routes?.invalidated ? `<small>Проверьте изменённые зоны · камер: ${Number(routes.invalidated)}</small>` : ''}</div>
      <button type="button" class="secondary" data-action="retail-setup">Настроить маршрут по зонам</button></div>
    <div class="retail-coverage"><div><strong>Видеоанализ VadCLIP</strong><span>${escapeHTML(status)}</span><small>Анализ включён у ${eligible.length} из ${state.cameras.length} камер · с сигналом ${receiving.length}</small></div></div>
  </section>`;
}

function reviewTheft(event) {
  const details = event.details || {};
  const route = event.source === 'retail_route';
  const demo = event.source === 'demo';
  const start = details.start_seconds, end = details.end_seconds;
  const hasStart = Number.isFinite(start) && start >= 0;
  const hasEnd = hasStart && Number.isFinite(end) && end >= start;
  const clock = value => value.toLocaleString('ru-RU', {maximumFractionDigits:1});
  const evidence = '/api/events/' + encodeURIComponent(event.id) + '/media/clip';
  const labels = {Shoplifting:'Возможная магазинная кража', Stealing:'Возможное хищение', Robbery:'Возможное ограбление', Burglary:'Возможное проникновение'};
  const reason = route ? 'Переход от полки к выходу' : 'Модель выделила фрагмент для просмотра';
  const routeDetails = route ? `<p>Посещение кассовой зоны не зарегистрировано. Такой маршрут возможен и у покупателя, который ушёл без покупки.</p>
    <dl class="theft-facts"><div><dt>Полка</dt><dd>${escapeHTML(details.shelf_zone)}</dd></div><div><dt>Кассовая зона</dt><dd>${escapeHTML(details.checkout_zone)}</dd></div><div><dt>Выход</dt><dd>${escapeHTML(details.exit_zone)}</dd></div></dl>` : '';
  const modelDetails = [
    details.model && escapeHTML(details.model),
    details.label && escapeHTML(labels[details.label] || details.label),
    Number.isFinite(event.score) && 'Оценка модели ' + Math.round(event.score * 100) + '%'
  ].filter(Boolean).join(' · ');
  const signal = demo ? '<p>Демонстрационный сигнал</p>' : `<strong>${reason}</strong>${route ? routeDetails : `<p>${modelDetails}</p>`}`;
  const reviewMedia = demo ? 'Пример события · видеозапись не создавалась' : `<video controls preload="metadata" src="${evidence}" aria-label="Видео события"></video>`;
  const interval = hasStart ? `<div class="theft-interval"><span>Фрагмент · ${clock(start)}${hasEnd ? '–' + clock(end) : ''} с</span>
    ${!demo ? '<button type="button" class="text-button" id="theft-seek" disabled>К началу фрагмента</button>' : ''}</div>` : '';
  const site = state.platform?.sites.find(site => site.id === event.site_id);
  const recorded = ['upload','webcam'].includes(event.camera) ? 'По времени загрузки · ' : '';
  modal('Проверка возможной кражи', `<div class="detail-meta">${site ? pill(site.name,'neutral') : ''}${pill(cameraName(event.camera))}${pill(recorded + dateLabel(event.started),'neutral')}${eventStatus(event)}</div>
    <div class="detail-media">${reviewMedia}</div>
    <p id="media-error" class="error" role="status" hidden></p>
    ${!demo ? `<a class="text-button" id="theft-download" href="${evidence}" download hidden>Скачать запись</a>` : ''}
    ${interval}<div class="theft-signal">${signal}</div>
    <label>Комментарий оператора<textarea id="review-note" maxlength="2000" placeholder="Что видно на записи?">${escapeHTML(event.note)}</textarea></label>
    <p id="theft-review-error" class="error" role="alert" hidden></p>
    <button class="text-button" id="theft-reload" hidden>Обновить решение</button>
    <div class="form-actions"><button class="secondary" data-review="new">Оставить на проверке</button><button class="secondary" data-review="dismissed">Отклонить сигнал</button><button class="primary" data-review="confirmed">Отметить для разбора</button></div>`);
  const video = $('#modal video');
  const seek = $('#theft-seek');
  if (video) {
    const metadata = () => { if (seek) seek.disabled = !Number.isFinite(video.duration) || start >= video.duration; };
    video.onloadedmetadata = metadata;
    video.oncanplay = metadata;
    if (video.readyState >= 1) metadata();
    if (seek) seek.onclick = () => { video.currentTime = start; video.focus(); };
    const mediaError = $('#media-error');
    const download = $('#theft-download');
    video.onerror = () => {
      mediaError.textContent = 'Видео недоступно для просмотра. Проверьте наличие записи и поддержку формата.';
      mediaError.hidden = false;
      download.hidden = false;
      if (seek) seek.disabled = true;
    };
  }
  const errorBox = $('#theft-review-error');
  const reload = $('#theft-reload');
  const buttons = $$('[data-review]', $('#modal'));
  reload.onclick = async () => {
    try { reviewTheft(await api('/events/' + encodeURIComponent(event.id))); }
    catch (error) { errorBox.hidden = false; errorBox.textContent = error.message; }
  };
  buttons.forEach(button => button.onclick = async () => {
    buttons.forEach(item => item.disabled = true);
    let conflict = false;
    try {
      await api('/events/' + encodeURIComponent(event.id), {method:'PATCH', body:JSON.stringify({
        status:button.dataset.review, expected_status:event.status, expected_note:event.note || '', note:$('#review-note').value
      })});
      $('#modal').close();
      toast('Решение сохранено');
      refresh();
    } catch (error) {
      conflict = error.status === 409;
      errorBox.textContent = conflict ? 'Другой оператор уже изменил решение. Обновите карточку, чтобы увидеть его запись.' : error.message;
      errorBox.hidden = false;
      reload.hidden = !conflict;
    } finally {
      if (!conflict) buttons.forEach(item => item.disabled = false);
    }
  });
}
