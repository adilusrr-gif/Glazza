'use strict';

const kazakhstanBounds = [[40.5, 46.4], [55.5, 87.4]];
const cameraMap = {map: null, markers: new Map(), selected: null, preview: null, signature: '', busy: false};
const hasCameraLocation = camera => Number.isFinite(camera.latitude) && Number.isFinite(camera.longitude);

function disposeCameraMap() {
  if (cameraMap.map) cameraMap.map.remove();
  Object.assign(cameraMap, {map: null, markers: new Map(), selected: null, preview: null, signature: '', busy: false});
}

function renderCameraMap() {
  if (!$('#camera-map') || !cameraMap.map) {
    disposeCameraMap();
    $('#content').innerHTML = head('Карта Казахстана', 'Все добавленные камеры и их расположение. Подложка карты загружается из интернета. Выберите камеру в списке и укажите место кликом или вводом координат.', '<button class="primary" data-action="add-camera">+ Подключить камеру</button>') + `
      <div class="map-summary" id="map-summary"></div>
      <div class="map-layout">
        <section class="map-panel" aria-label="Расположение камер">
          <div class="map-toolbar"><div class="heading-with-help"><strong>Казахстан</strong><button type="button" id="map-hint" class="help-trigger" data-help="Выберите камеру в списке, чтобы указать её место." aria-label="Как отметить камеру">?</button></div><div><button class="secondary" id="map-country">Весь Казахстан</button><button class="secondary" id="map-fit">Все отметки</button></div></div>

          <div id="camera-map" class="camera-map" role="region" aria-label="Интерактивная карта Казахстана"></div>
          <p id="map-tile-error" class="warning-box" role="status" hidden>Не удалось загрузить часть карты. Проверьте доступ к интернету. Список камер и ввод координат доступны.</p>
          <div class="map-legend"><span><i class="map-dot"></i> Место камеры</span><span><i class="map-dot draft"></i> Новая отметка</span></div>
        </section>
        <aside class="map-sidebar">
          <section class="panel map-cameras"><h2>Камеры</h2><input id="map-search" type="search" aria-label="Поиск камер" placeholder="Поиск камер" autocomplete="off"><div id="map-camera-list" class="map-camera-list"></div></section>
          <section class="panel map-location" id="map-location" hidden></section>
        </aside>
      </div>`;
    if (typeof L === 'undefined') {
      $('#camera-map').textContent = 'Не удалось открыть карту. Обновите страницу.';
    } else {
      cameraMap.map = L.map('camera-map', {zoomControl: false, minZoom: 3, maxZoom: 19, zoomSnap: 0.25, worldCopyJump: true});
      L.control.zoom({position: 'topright', zoomInTitle: 'Приблизить', zoomOutTitle: 'Отдалить'}).addTo(cameraMap.map);
      L.control.scale({imperial: false}).addTo(cameraMap.map);
      const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19, referrerPolicy: 'strict-origin-when-cross-origin',
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors'
      });
      let tileFailures = 0;
      tiles.on('loading', () => { tileFailures = 0; });
      tiles.on('tileerror', () => { tileFailures++; if ($('#map-tile-error')) $('#map-tile-error').hidden = false; });
      tiles.on('load', () => { if ($('#map-tile-error')) $('#map-tile-error').hidden = tileFailures === 0; });
      tiles.addTo(cameraMap.map);
      cameraMap.map.fitBounds(kazakhstanBounds, {padding: [18, 18], animate: false});
      cameraMap.map.on('click', event => {
        if (!cameraMap.selected || cameraMap.busy || state.session.demo) return;
        $('#map-latitude').value = event.latlng.lat.toFixed(6);
        $('#map-longitude').value = event.latlng.wrap().lng.toFixed(6);
        updateMapPreview();
      });
    }
    $('#map-search').oninput = renderMapCameraList;
    $('#map-country').onclick = () => cameraMap.map?.fitBounds(kazakhstanBounds, {padding: [18, 18]});
    $('#map-fit').onclick = () => {
      const points = state.cameras.filter(hasCameraLocation).map(c => [c.latitude, c.longitude]);
      if (points.length) cameraMap.map?.fitBounds(points, {padding: [45, 45], maxZoom: 15});
    };
  }
  const located = state.cameras.filter(hasCameraLocation).length;
  $('#map-summary').innerHTML = `<span><strong>${state.cameras.length}</strong> Всего камер</span><span><strong>${located}</strong> На карте</span><span><strong>${state.cameras.length - located}</strong> Без координат</span>`;
  $('#map-fit').disabled = located === 0;
  if (cameraMap.selected && !state.cameras.some(c => c.id === cameraMap.selected)) clearMapSelection();
  const signature = JSON.stringify(state.cameras);
  if (signature !== cameraMap.signature) {
    cameraMap.signature = signature;
    updateMapMarkers();
    renderMapCameraList();
  }
  cameraMap.map?.invalidateSize({pan: false});
}

function renderMapCameraList() {
  const query = $('#map-search').value.trim().toLocaleLowerCase('ru');
  const cameras = state.cameras.filter(c => (c.name + ' ' + c.id).toLocaleLowerCase('ru').includes(query));
  const group = (items, title) => items.length ? `<h3 class="map-list-heading">${title} · ${items.length}</h3>${items.map(c => `
    <button class="map-camera-row ${cameraMap.selected === c.id ? 'selected' : ''}" data-map-camera="${c.id}" aria-pressed="${cameraMap.selected === c.id}" data-help="${hasCameraLocation(c) ? c.latitude.toFixed(4) + ", " + c.longitude.toFixed(4) : "Указать место на карте"}">
      <span class="map-camera-symbol">${hasCameraLocation(c) ? '▣' : '+'}</span><span><strong>${escapeHTML(c.name)}</strong></span><span aria-hidden="true">↗</span>
    </button>`).join('')}` : '';
  $('#map-camera-list').innerHTML = cameras.length ? group(cameras.filter(hasCameraLocation), 'На карте') + group(cameras.filter(c => !hasCameraLocation(c)), 'Без координат') :
    `<p class="map-list-empty">${state.cameras.length ? 'Камеры не найдены.' : 'Нет добавленных камер'}</p>`;
  $$('[data-map-camera]').forEach(button => button.onclick = () => selectMapCamera(button.dataset.mapCamera));
}

function updateMapMarkers() {
  if (!cameraMap.map) return;
  const cameras = state.cameras.filter(hasCameraLocation);
  const ids = new Set(cameras.map(c => c.id));
  for (const [id, marker] of cameraMap.markers) {
    if (!ids.has(id)) { marker.remove(); cameraMap.markers.delete(id); }
  }
  for (const camera of cameras) {
    let marker = cameraMap.markers.get(camera.id);
    if (!marker) {
      marker = L.marker([camera.latitude, camera.longitude], {
        icon: L.divIcon({className: 'camera-map-marker', html: '<span aria-hidden="true">▣</span>', iconSize: [34, 34], iconAnchor: [17, 17]}),
        title: camera.name, alt: camera.name, riseOnHover: true
      }).addTo(cameraMap.map);
      cameraMap.markers.set(camera.id, marker);
    } else marker.setLatLng([camera.latitude, camera.longitude]);
    marker.getElement().title = camera.name;
    marker.getElement().setAttribute('aria-label', camera.name);
    const popup = document.createElement('div');
    popup.className = 'map-popup';
    popup.innerHTML = `<strong>${escapeHTML(camera.name)}</strong><p>${camera.latitude.toFixed(5)}, ${camera.longitude.toFixed(5)}</p><button class="primary" data-open>Открыть камеру</button><button class="text-button" data-locate>Изменить место</button>`;
    $('[data-open]', popup).onclick = () => cameraSettings(camera.id);
    $('[data-locate]', popup).onclick = () => { cameraMap.map.closePopup(); selectMapCamera(camera.id); };
    marker.bindPopup(popup);
  }
}

function selectMapCamera(id) {
  if (cameraMap.busy) return;
  const camera = state.cameras.find(c => c.id === id);
  if (!camera) return;
  cameraMap.selected = id;
  cameraMap.preview?.remove(); cameraMap.preview = null;
  renderMapCameraList();
  $('#map-location').hidden = false;
  $('#map-location').innerHTML = `<div class="heading-with-help"><h2>${escapeHTML(camera.name)}</h2>${help("Нажмите на карту или введите координаты. Затем сохраните отметку.", "Как задать координаты")}</div><span id="map-draft-status" class="pill warning" hidden>Не сохранено</span>
    <form id="map-location-form"><fieldset ${state.session.demo ? 'disabled' : ''}><div class="map-coordinate-fields">
      <label>Широта<input id="map-latitude" type="number" min="-90" max="90" step="any" required placeholder="51.1694" value="${camera.latitude ?? ''}"></label>
      <label>Долгота<input id="map-longitude" type="number" min="-180" max="180" step="any" required placeholder="71.4491" value="${camera.longitude ?? ''}"></label>
      </div><p class="error" id="map-location-error" role="alert"></p><button class="primary" type="submit">Сохранить отметку</button>
      <button class="text-button" type="button" id="map-remove-location" ${hasCameraLocation(camera) ? '' : 'hidden'}>Убрать отметку</button></fieldset></form>
    <div class="map-location-actions"><button class="secondary" id="map-open-camera">Открыть камеру</button><button class="text-button" id="map-cancel">Отмена</button></div>`;
  setHelp('#map-hint', state.session.demo ? 'В демонстрации расположение камер изменить нельзя.' : 'Выбрана камера «' + camera.name + '». Нажмите на карту, чтобы поставить отметку.');
  $('#camera-map').classList.toggle('is-picking', !state.session.demo);
  $('#map-latitude').oninput = updateMapPreview;
  $('#map-longitude').oninput = updateMapPreview;
  $('#map-location-form').onsubmit = event => {
    event.preventDefault();
    saveMapLocation(id, {latitude: $('#map-latitude').valueAsNumber, longitude: $('#map-longitude').valueAsNumber});
  };
  $('#map-remove-location').onclick = () => saveMapLocation(id, {latitude: null, longitude: null});
  $('#map-open-camera').onclick = () => cameraSettings(id);
  $('#map-cancel').onclick = clearMapSelection;
  if (hasCameraLocation(camera)) cameraMap.map?.setView([camera.latitude, camera.longitude], Math.max(cameraMap.map.getZoom(), 12));
}

function updateMapPreview() {
  if (!cameraMap.map || cameraMap.busy) return;
  const lat = $('#map-latitude'), lon = $('#map-longitude');
  cameraMap.preview?.remove(); cameraMap.preview = null;
  if (!lat.validity.valid || !lon.validity.valid) return;
  cameraMap.preview = L.circleMarker([lat.valueAsNumber, lon.valueAsNumber], {radius: 13, color: '#efbc6b', fillColor: '#efbc6b', fillOpacity: 0.25, weight: 3, dashArray: '4 4', interactive: false}).addTo(cameraMap.map);
  $('#map-draft-status').hidden = false;
  setHelp('#map-hint', 'Новая отметка выбрана. Нажмите «Сохранить отметку».');
}

async function saveMapLocation(id, location) {
  if (cameraMap.busy) return;
  const form = $('#map-location-form');
  cameraMap.busy = true;
  $('fieldset', form).disabled = true;
  $('#map-location-error').textContent = '';
  try {
    await api('/cameras/' + id + '/location', {method: 'PUT', body: JSON.stringify(location)});
    const camera = state.cameras.find(c => c.id === id);
    if (camera) Object.assign(camera, location);
    if (form.isConnected) { cameraMap.busy = false; clearMapSelection(); renderCameraMap(); }
    toast(location.latitude === null ? 'Отметка удалена. Камера осталась в списке.' : 'Место камеры сохранено. Перезапуск не требуется.');
  } catch (error) {
    if (form.isConnected) $('#map-location-error').textContent = error.message;
    else if (state.session) toast(error.message);
  } finally {
    cameraMap.busy = false;
    if (form.isConnected) $('fieldset', form).disabled = !!state.session?.demo;
  }
}

function clearMapSelection() {
  if (cameraMap.busy) return;
  cameraMap.selected = null;
  cameraMap.preview?.remove(); cameraMap.preview = null;
  if (!$('#camera-map')) return;
  $('#map-location').hidden = true;
  $('#camera-map').classList.remove('is-picking');
  setHelp('#map-hint', 'Выберите камеру в списке, чтобы указать её место.');
  renderMapCameraList();
}

function openCameraOnMap(id) {
  $('#modal').close();
  navigate('map');
  selectMapCamera(id);
}
