'use strict';

async function openRetailSetup() {
  const site = state.platform?.sites.find(item => item.id === selectedSite && item.profile === 'retail');
  if (!site) return toast('Выберите магазин в фильтре объектов.');
  const cameras = state.cameras.filter(camera => camera.site_id === site.id);
  if (!cameras.length) {
    modal('Маршрут от полки до выхода', '<p>Сначала подключите камеру, в кадре которой видны полка, касса и выход.</p>');
    return;
  }
  const options = cameras.map(camera => `<option value="${escapeHTML(camera.id)}">${escapeHTML(camera.name)}</option>`).join('');
  modal('Маршрут от полки до выхода', `<p>Сигнал появится, если камера проследила человека от полки до выхода без входа в зону кассы. Видео проверяет оператор.</p>
    <label>Камера магазина<select id="route-camera">${options}</select></label><div id="route-settings" aria-live="polite"></div>`);
  const select = $('#route-camera');
  const panel = $('#route-settings');

  async function loadRule() {
    const camera = cameras.find(item => item.id === select.value);
    const endpoint = '/retail/routes/' + encodeURIComponent(camera.id) + '?site_id=' + encodeURIComponent(site.id);
    panel.textContent = 'Загрузка настроек…';
    try {
      const {rule} = await api(endpoint);
      if (!panel.isConnected || select.value !== camera.id) return;
      const zones = camera.zones.filter(zone => !zone.restricted);
      const field = (title, key) => `<label>${title}<select name="${key}" required><option value="">Выберите зону</option>${zones.map(zone =>
        `<option value="${escapeHTML(zone.name)}" ${rule?.[key] === zone.name ? 'selected' : ''}>${escapeHTML(zone.name)}</option>`).join('')}</select></label>`;
      const active = rule?.state === 'active';
      const status = active ? 'Маршрут включён' : rule ? 'Маршрут приостановлен' : 'Маршрут выключен';
      const reasons = {camera_changed: 'Изменились подключение камеры, зоны или привязка к магазину.',
                       profile_changed: 'Изменился профиль объекта.'};
      const reason = rule?.reason ? `<p>${reasons[rule.reason] || 'Проверьте настройки камеры и маршрута.'}</p>` : '';
      panel.innerHTML = `<p><strong>${status}</strong></p>${reason}
        <p>Используйте три отдельные зоны наблюдения на одной камере. После изменения разметки примените настройки Frigate и заново сохраните маршрут.</p>
        <button type="button" class="secondary" data-route-zones>Разметить зоны камеры</button>
        ${zones.length >= 3 ? `<form id="route-form">${field('Полка', 'shelf_zone')}${field('Касса', 'checkout_zone')}${field('Выход', 'exit_zone')}
          <button class="primary" ${state.session.demo ? 'disabled' : ''}>${active ? 'Сохранить маршрут' : 'Включить маршрут'}</button></form>`
          : '<p>Чтобы включить маршрут, нарисуйте полку, кассу и выход. Для каждой укажите тип «Наблюдение за маршрутом».</p>'}
        ${rule ? '<button type="button" class="secondary" data-route-disable>Отключить маршрут</button>' : ''}<p class="error" data-route-error role="alert"></p>`;
      $('[data-route-zones]', panel).onclick = () => cameraSettings(camera.id);
      async function save(method, body) {
        const controls = [...panel.querySelectorAll('button,select'), select];
        controls.forEach(node => node.disabled = true);
        try {
          await api(endpoint, {method, ...(body ? {body: JSON.stringify(body)} : {})});
          if (panel.isConnected) {
            await loadRule();
            toast(method === 'DELETE' ? 'Маршрут отключён' : 'Маршрут сохранён');
          }
        } catch (error) {
          if (panel.isConnected) $('[data-route-error]', panel).textContent = error.message;
        } finally {
          controls.forEach(node => node.disabled = false);
        }
      }
      const form = $('#route-form', panel);
      if (form) form.onsubmit = event => {
        event.preventDefault();
        const body = Object.fromEntries(new FormData(form));
        if (new Set(Object.values(body)).size !== 3) {
          $('[data-route-error]', panel).textContent = 'Выберите разные зоны полки, кассы и выхода.';
          return;
        }
        save('PUT', body);
      };
      const disable = $('[data-route-disable]', panel);
      if (disable) disable.onclick = () => save('DELETE');
    } catch (error) {
      if (panel.isConnected && select.value === camera.id) panel.textContent = error.message;
    }
  }
  select.onchange = loadRule;
  await loadRule();
}

async function openMulticamSetup() {
  const site = state.platform?.sites.find(item => item.id === selectedSite && item.profile === 'retail');
  if (!site) return toast('Выберите магазин в фильтре объектов.');
  const cameras = state.cameras.filter(camera => camera.site_id === site.id);
  const endpoint = '/retail/multicam/' + encodeURIComponent(site.id);
  const roles = {shelf:'Полка', checkout:'Касса', exit:'Выход'};
  modal('Маршрут между камерами', '<div id="multicam-settings" aria-live="polite">Загрузка настроек…</div>');
  const panel = $('#multicam-settings');

  function draw(rule) {
    const status = !rule ? 'Связь камер выключена' : rule.state === 'active' ? 'Связь камер включена' : 'Связь камер приостановлена';
    const cameraField = ([role, title]) => {
      const value = rule?.[role + '_camera'];
      return `<fieldset><legend>${title}</legend><div class="form-row"><label>Камера
        <select name="${role}_camera" aria-label="${title} · камера" required><option value="">Выберите камеру</option>
        ${cameras.map(camera => `<option value="${escapeHTML(camera.id)}" ${camera.id === value ? 'selected' : ''}>${escapeHTML(camera.name)}</option>`).join('')}</select></label>
        <label>Зона наблюдения<select name="${role}_zone" aria-label="${title} · зона" required></select></label></div></fieldset>`;
    };
    panel.innerHTML = `<p><strong>${status}</strong></p>
      ${rule?.state === 'invalidated' ? '<p>Проверьте изменившиеся настройки камер и сохраните маршрут заново.</p>' : ''}
      ${cameras.length < 3 ? '<p>Для маршрута нужны три разные камеры одного магазина. Подключите камеры полки, кассы и выхода в разделе «Камеры».</p>'
        : `<form id="multicam-form"><p>Выберите по одной зоне на трёх разных камерах. Камера кассы должна охватывать путь к оплате.</p>
          ${Object.entries(roles).map(cameraField).join('')}
          <fieldset><legend>Время перехода от полки до выхода</legend><div class="form-row">
            <label>От, секунд<input name="min_seconds" type="number" required min="0" max="599" step="1" value="${Number(rule?.min_seconds ?? 2)}"></label>
            <label>До, секунд<input name="max_seconds" type="number" required min="1" max="600" step="1" value="${Number(rule?.max_seconds ?? 60)}"></label>
          </div></fieldset><button class="primary" ${state.session.demo ? 'disabled' : ''}>${rule ? 'Сохранить связь камер' : 'Включить связь камер'}</button></form>`}
      ${rule ? `<button type="button" class="secondary" data-multicam-disable ${state.session.demo ? 'disabled' : ''}>Отключить связь камер</button>` : ''}
      <p class="error" data-multicam-error role="alert"></p>`;
    const form = $('#multicam-form', panel);
    const errorBox = $('[data-multicam-error]', panel);
    async function save(method, body) {
      const controls = [...panel.querySelectorAll('button,input,select')].filter(field => !field.disabled);
      controls.forEach(field => field.disabled = true);
      try {
        const response = await api(endpoint, {method, ...(body ? {body:JSON.stringify(body)} : {})});
        if (panel.isConnected) draw(response.rule);
        refresh();
      } catch (error) {
        errorBox.textContent = error.message;
        controls.forEach(field => field.disabled = false);
      }
    }
    if (form) {
      for (const role of Object.keys(roles)) {
        const camera = form.elements[role + '_camera'];
        const zone = form.elements[role + '_zone'];
        function updateZones(value) {
          const selected = cameras.find(item => item.id === camera.value);
          const zones = selected?.zones.filter(item => !item.restricted) || [];
          const placeholder = !selected ? 'Сначала выберите камеру' : zones.length ? 'Выберите зону' : 'Добавьте зону в настройках камеры';
          zone.innerHTML = `<option value="">${placeholder}</option>` + zones.map(item =>
            `<option value="${escapeHTML(item.name)}" ${item.name === value ? 'selected' : ''}>${escapeHTML(item.name)}</option>`).join('');
        }
        camera.onchange = () => updateZones('');
        updateZones(rule?.[role + '_zone']);
      }
      form.onsubmit = event => {
        event.preventDefault();
        const body = Object.fromEntries(new FormData(form));
        if (new Set(Object.keys(roles).map(role => body[role + '_camera'])).size !== 3) {
          errorBox.textContent = 'Выберите три разные камеры.';
          return;
        }
        body.min_seconds = Number(body.min_seconds);
        body.max_seconds = Number(body.max_seconds);
        if (body.min_seconds >= body.max_seconds) {
          errorBox.textContent = 'Максимальное время должно быть больше минимального.';
          return;
        }
        save('PUT', body);
      };
    }
    const disable = $('[data-multicam-disable]', panel);
    if (disable) disable.onclick = () => save('DELETE');
  }

  try {
    const result = await api(endpoint);
    if (panel.isConnected) draw(result.rule);
  } catch (error) {
    if (panel.isConnected) panel.textContent = error.message;
  }
}
