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
