'use strict';

async function openVisitSetup(siteId, onSaved) {
  if (!siteId || siteId !== selectedSite) return;
  const cameras = state.cameras.filter(camera => camera.site_id === siteId);
  modal('Камеры и переходы посетителей', '<div id="visit-setup-body">Загрузка настроек…</div>');
  const panel = $('#visit-setup-body');
  const endpoint = '/retail/visits/' + encodeURIComponent(siteId) + '/config';
  if (cameras.length < 2) {
    panel.textContent = 'Подключите как минимум две камеры магазина, включая камеру у входа.';
    return;
  }
  let config;
  try { ({config} = await api(endpoint)); }
  catch (error) { panel.textContent = error.message; return; }
  if (!panel.isConnected) return;
  const cameraOptions = id => '<option value="">Выберите камеру</option>' + cameras.map(camera =>
    `<option value="${escapeHTML(camera.id)}" ${camera.id === id ? 'selected' : ''}>${escapeHTML(camera.name)} · ${escapeHTML(camera.id)}</option>`).join('');
  panel.innerHTML = `<form id="visit-config-form">
    <h3>Входы магазина</h3><p>Для каждой двери выберите камеру и зону входа. Зоны размечаются в настройках камеры.</p>
    ${config?.state === 'invalidated' ? '<p class="warning-box">Настройки камеры изменились. Проверьте зоны и переходы перед включением.</p>' : ''}
    <div data-visit-entrances></div>
    <button class="secondary" type="button" data-add-entrance>Добавить вход</button>
    <p class="small">Для двусторонней двери укажите также зону внутри зала. Визит начнётся при движении от двери в зал.</p>
    <h3>Переходы между камерами</h3><p class="small">Укажите направление и время в пути. Для прохода в обе стороны добавьте два перехода. Если нужны только независимые входы, оставьте список переходов пустым.</p>
    <div data-visit-edges></div>
    <button class="secondary" type="button" data-add-edge>Добавить переход</button>
    <p class="error" role="alert" data-visit-config-error></p>
    <div class="form-actions"><button class="primary" type="submit">Сохранить и включить</button>
    ${config ? '<button class="secondary" data-disable-visits type="button">Отключить отслеживание</button>' : ''}</div></form>`;
  const form = $('#visit-config-form', panel);
  const entrances = $('[data-visit-entrances]', form);
  const addEntranceButton = $('[data-add-entrance]', form);
  const edges = $('[data-visit-edges]', form);
  const error = $('[data-visit-config-error]', form);
  function updateEntranceControls() {
    const rows = [...entrances.children];
    rows.forEach((row, index) => {
      const label = 'Вход ' + (index + 1);
      row.setAttribute('aria-label', label);
      $('[data-entrance-title]', row).textContent = label;
      const remove = $('[data-remove-entrance]', row);
      remove.hidden = rows.length === 1;
      remove.setAttribute('aria-label', 'Удалить вход ' + (index + 1));
    });
    addEntranceButton.disabled = rows.length >= Math.min(cameras.length, 64);
  }

  function addEntrance(saved = {}) {
    const row = document.createElement('div');
    row.setAttribute('role', 'group');
    row.innerHTML = `<div class="section-heading"><strong data-entrance-title></strong>
      <button type="button" class="text-button" data-remove-entrance>Удалить вход</button></div>
      <div class="form-row"><label>Входная камера<select name="entrance_camera" required>${cameraOptions(saved.camera)}</select></label>
      <label>Зона входа<select name="entrance_zone" required></select></label></div>
      <label>Зона внутри зала после двери<select name="inside_zone"></select></label>`;
    const camera = $('[name=entrance_camera]', row);
    const zone = $('[name=entrance_zone]', row);
    const inside = $('[name=inside_zone]', row);
    const zones = () => (cameras.find(item => item.id === camera.value)?.zones || []).filter(item => !item.restricted);
    const options = items => items.map(item => `<option value="${escapeHTML(item.name)}">${escapeHTML(item.name)}</option>`).join('');
    function updateInside(selected) {
      const available = zones().filter(item => item.name !== zone.value);
      inside.innerHTML = '<option value="">Односторонний вход</option>' + options(available);
      const invalid = selected && !available.some(item => item.name === selected);
      if (invalid) {
        const missing = new Option('Перепроверьте зону · ' + selected, selected);
        missing.disabled = true;
        inside.add(missing);
      }
      inside.value = selected || '';
      inside.setCustomValidity(invalid ? 'Укажите другую внутреннюю зону или выберите односторонний вход.' : '');
    }
    function updateZones(entranceZone, insideZone) {
      const available = zones();
      const placeholder = camera.value && !available.length ? 'Добавьте зону в настройках камеры' : 'Выберите зону';
      zone.innerHTML = `<option value="">${placeholder}</option>` + options(available);
      zone.value = available.some(item => item.name === entranceZone) ? entranceZone : '';
      updateInside(insideZone);
    }
    camera.onchange = () => updateZones('', '');
    zone.onchange = () => updateInside(inside.value);
    inside.onchange = () => inside.setCustomValidity('');
    $('[data-remove-entrance]', row).onclick = () => {
      row.remove();
      updateEntranceControls();
      addEntranceButton.focus();
    };
    entrances.append(row);
    updateZones(saved.entrance_zone, saved.inside_zone);
    updateEntranceControls();
    return camera;
  }

  const initialEntrances = config?.entrances?.length ? config.entrances : [{
    camera:config?.entrance_camera, entrance_zone:config?.entrance_zone, inside_zone:config?.inside_zone
  }];
  initialEntrances.forEach(addEntrance);
  addEntranceButton.onclick = () => addEntrance().focus();
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
    const controls = $$('button,input,select', form).filter(control => !control.disabled);
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
    if (transitions.some(t => t.from_camera === t.to_camera || t.min_seconds >= t.max_seconds)) {
      error.textContent = 'Укажите переход между разными камерами и интервал времени от меньшего к большему.';
      return;
    }
    const doors = [...entrances.children].map(row => ({
      camera: $('[name=entrance_camera]', row).value,
      entrance_zone: $('[name=entrance_zone]', row).value,
      inside_zone: $('[name=inside_zone]', row).value || null
    }));
    const entranceCameras = doors.map(door => door.camera);
    if (new Set(entranceCameras).size !== doors.length) {
      error.textContent = 'Выберите отдельную камеру для каждого входа.';
      return;
    }
    if (!transitions.length && doors.length < 2) {
      error.textContent = 'Для одной входной камеры добавьте переход к другой камере.';
      return;
    }
    const camera_ids = [...new Set([...entranceCameras, ...transitions.flatMap(t => [t.from_camera, t.to_camera])])];
    save('PUT', {entrances: doors, camera_ids, transitions});
  };
  const disable = $('[data-disable-visits]', form);
  if (disable) disable.onclick = () => save('DELETE');
}
