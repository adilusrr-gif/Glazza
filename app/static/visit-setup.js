'use strict';

async function openVisitSetup(siteId, onSaved) {
  if (!siteId || siteId !== selectedSite) return;
  const cameras = state.cameras.filter(camera => camera.site_id === siteId);
  modal('Камеры и переходы посетителей', '<div id="visit-setup-body">Загрузка настроек…</div>');
  const panel = $('#visit-setup-body');
  const endpoint = '/retail/visits/' + encodeURIComponent(siteId) + '/config';
  if (cameras.length < 2) {
    panel.textContent = 'Подключите входную камеру и как минимум одну камеру внутри магазина.';
    return;
  }
  let config;
  try { ({config} = await api(endpoint)); }
  catch (error) { panel.textContent = error.message; return; }
  if (!panel.isConnected) return;
  const cameraOptions = id => '<option value="">Выберите камеру</option>' + cameras.map(camera =>
    `<option value="${escapeHTML(camera.id)}" ${camera.id === id ? 'selected' : ''}>${escapeHTML(camera.name)} · ${escapeHTML(camera.id)}</option>`).join('');
  panel.innerHTML = `<form id="visit-config-form">
    <p>Укажите камеру и зону, где посетитель впервые попадает в кадр. Для каждого прохода между камерами задайте направление и время в пути.</p>
    ${config?.state === 'invalidated' ? '<p class="warning-box">Настройки камеры изменились. Проверьте зоны и переходы перед включением.</p>' : ''}
    <div class="form-row"><label>Входная камера<select name="entrance_camera" required>${cameraOptions(config?.entrance_camera)}</select></label>
    <label>Зона входа<select name="entrance_zone" required></select></label></div>
    <label>Зона внутри зала после двери<select name="inside_zone"></select></label>
    <p class="small">Если одна дверь служит входом и выходом, укажите две зоны на входной камере. Визит начнётся при движении от двери в зал.</p>
    <p class="small">Зона входа размечается в настройках камеры как зона наблюдения. Для прохода в обе стороны добавьте два направления.</p>
    <div data-visit-edges></div>
    <button class="secondary" type="button" data-add-edge>Добавить переход</button>
    <p class="error" role="alert" data-visit-config-error></p>
    <div class="form-actions"><button class="primary" type="submit">Сохранить и включить</button>
    ${config ? '<button class="secondary" data-disable-visits type="button">Отключить отслеживание</button>' : ''}</div></form>`;
  const form = $('#visit-config-form', panel);
  const entrance = form.elements.entrance_camera;
  const zone = form.elements.entrance_zone;
  const edges = $('[data-visit-edges]', form);
  const error = $('[data-visit-config-error]', form);
  function updateZones(value, inside) {
    const zones = cameras.find(camera => camera.id === entrance.value)?.zones || [];
    zone.innerHTML = '<option value="">Выберите зону</option>' + zones.filter(z => !z.restricted).map(z =>
      `<option value="${escapeHTML(z.name)}" ${z.name === value ? 'selected' : ''}>${escapeHTML(z.name)}</option>`).join('');
    form.elements.inside_zone.innerHTML = '<option value="">Отдельный вход в одну сторону</option>' +
      zones.filter(z => !z.restricted).map(z => `<option value="${escapeHTML(z.name)}" ${z.name === inside ? 'selected' : ''}>${escapeHTML(z.name)}</option>`).join('');
  }
  entrance.onchange = () => updateZones('');
  updateZones(config?.entrance_zone, config?.inside_zone);
  function addEdge(edge = {}) {
    const row = document.createElement('fieldset');
    row.innerHTML = `<legend>Переход</legend><div class="form-row">
      <label>Откуда<select data-from required>${cameraOptions(edge.from_camera)}</select></label>
      <label>Куда<select data-to required>${cameraOptions(edge.to_camera)}</select></label></div>
      <div class="form-row"><label>Минимум, с<input data-min type="number" required min="0" max="599" value="${Number(edge.min_seconds ?? 2)}"></label>
      <label>Максимум, с<input data-max type="number" required min="1" max="600" value="${Number(edge.max_seconds ?? 60)}"></label></div>
      <button type="button" class="text-button">Удалить переход</button>`;
    $('button', row).onclick = () => row.remove();
    edges.append(row);
  }
  for (const edge of config?.transitions || [{}]) addEdge(edge);
  $('[data-add-edge]', form).onclick = () => addEdge();
  let busy = false;
  async function save(method, body) {
    if (busy) return;
    busy = true;
    const controls = $$('button,input,select', form);
    controls.forEach(control => control.disabled = true);
    try {
      await api(endpoint, {method, ...(body && {body: JSON.stringify(body)})});
      if (panel.isConnected && $('#modal').open) $('#modal').close();
      toast(method === 'DELETE' ? 'Отслеживание визитов отключено' : 'Переходы сохранены');
      if (siteId === selectedSite && onSaved) await onSaved();
    } catch (e) { error.textContent = e.message; }
    finally { busy = false; controls.forEach(control => control.disabled = false); }
  }
  form.onsubmit = event => {
    event.preventDefault();
    const transitions = $$('fieldset', edges).map(row => ({
      from_camera: $('[data-from]', row).value, to_camera: $('[data-to]', row).value,
      min_seconds: $('[data-min]', row).valueAsNumber, max_seconds: $('[data-max]', row).valueAsNumber
    }));
    if (!transitions.length || transitions.some(t => t.from_camera === t.to_camera || t.min_seconds >= t.max_seconds)) {
      error.textContent = 'Укажите переход между разными камерами и интервал времени от меньшего к большему.';
      return;
    }
    const camera_ids = [...new Set([entrance.value, ...transitions.flatMap(t => [t.from_camera, t.to_camera])])];
    save('PUT', {entrance_camera: entrance.value, entrance_zone: zone.value,
                 inside_zone: form.elements.inside_zone.value || null, camera_ids, transitions});
  };
  const disable = $('[data-disable-visits]', form);
  if (disable) disable.onclick = () => save('DELETE');
}
