'use strict';

const kazakhstanBounds = [[40.5, 46.4], [55.5, 87.4]];
const cameraMap = {map: null, markers: new Map(), selected: null, preview: null, signature: '', busy: false, dragging: false};
const hasCameraLocation = camera => Number.isFinite(camera.latitude) && Number.isFinite(camera.longitude);

function disposeCameraMap() {
  document.body.classList.remove('map-expanded-body');
  if (cameraMap.map) cameraMap.map.remove();
  Object.assign(cameraMap, {map: null, markers: new Map(), selected: null, preview: null, signature: '', busy: false, dragging: false});
}

function renderCameraMap() {
  if (!$('#camera-map') || !cameraMap.map) {
    disposeCameraMap();
    $('#content').innerHTML = head('Карта Казахстана', 'Все добавленные камеры и их расположение. Подложка карты загружается из интернета. Выберите камеру в списке и укажите место кликом или вводом координат.', '<button class="primary" data-action="add-camera">+ Подключить камеру</button>') + `
      <div class="map-summary" id="map-summary"></div>
      <div class="map-layout">
        <section class="map-panel" aria-label="Расположение камер">
          <div class="map-toolbar"><div class="heading-with-help"><strong>Казахстан</strong><button type="button" id="map-hint" class="help-trigger" data-help="Перемещайте карту мышью или пальцем. Масштаб — колёсиком, кнопками или двумя пальцами. Нажмите на карту, чтобы разместить камеру. Отметки можно перетаскивать." aria-label="Как пользоваться картой">?</button></div><div><button class="secondary" id="map-country">Весь Казахстан</button><button class="secondary" id="map-fit">Все отметки</button><button class="secondary map-expand-button" id="map-expand" aria-label="Развернуть карту" aria-pressed="false" data-help="Развернуть карту">⛶</button></div></div>

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
      cameraMap.map = L.map('camera-map', {zoomControl: false, minZoom: 3, maxZoom: 19, zoomSnap: 0.25, worldCopyJump: true,
        dragging: true, scrollWheelZoom: true, doubleClickZoom: true, touchZoom: true, keyboard: true});
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
        if (cameraMap.busy || state.session.demo) return;
        if (cameraMap.selected) setMapDraft(event.latlng);
        else openMapPoint(event.latlng);
      });
      cameraMap.map.on('popupopen', refreshImages);
    }
    $('#map-search').oninput = () => { renderMapCameraList(); filterMapMarkers(); };
    $('#map-expand').onclick = () => expandCameraMap();
    $('#map-country').onclick = () => cameraMap.map?.fitBounds(kazakhstanBounds, {padding: [18, 18]});
    $('#map-fit').onclick = () => {
      const points = filteredMapCameras().filter(hasCameraLocation).map(c => [c.latitude, c.longitude]);
      if (points.length) cameraMap.map?.fitBounds(points, {padding: [45, 45], maxZoom: 15});
    };
  }
  if (cameraMap.dragging) return;
  const located = state.cameras.filter(hasCameraLocation).length;
  $('#map-summary').innerHTML = `<span><strong>${state.cameras.length}</strong> Всего камер</span><span><strong>${located}</strong> На карте</span><span><strong>${state.cameras.length - located}</strong> Без координат</span>`;
  if (cameraMap.selected && !state.cameras.some(c => c.id === cameraMap.selected)) clearMapSelection();
  const signature = JSON.stringify(state.cameras);
  if (signature !== cameraMap.signature) {
    cameraMap.signature = signature;
    updateMapMarkers();
    renderMapCameraList();
  }
  cameraMap.map?.invalidateSize({pan: true, animate: false});
}

function filteredMapCameras() {
  const query = $('#map-search').value.trim().toLocaleLowerCase('ru');
  return state.cameras.filter(c => (c.name + ' ' + c.id).toLocaleLowerCase('ru').includes(query));
}

function renderMapCameraList() {
  const cameras = filteredMapCameras();
  const group = (items, title) => items.length ? `<h3 class="map-list-heading">${title} · ${items.length}</h3>${items.map(c => `
    <button class="map-camera-row ${cameraMap.selected === c.id ? 'selected' : ''}" data-map-camera="${c.id}" aria-pressed="${cameraMap.selected === c.id}" data-help="${hasCameraLocation(c) ? c.latitude.toFixed(4) + ", " + c.longitude.toFixed(4) : "Указать место на карте"}">
      <span class="map-camera-symbol">${hasCameraLocation(c) ? '▣' : '+'}</span><span><strong>${escapeHTML(c.name)}</strong></span><span aria-hidden="true">↗</span>
    </button>`).join('')}` : '';
  $('#map-camera-list').innerHTML = cameras.length ? group(cameras.filter(hasCameraLocation), 'На карте') + group(cameras.filter(c => !hasCameraLocation(c)), 'Без координат') :
    `<p class="map-list-empty">${state.cameras.length ? 'Камеры не найдены.' : 'Нет добавленных камер'}</p>`;
  $$('[data-map-camera]').forEach(button => button.onclick = () => selectMapCamera(button.dataset.mapCamera));
}

function filterMapMarkers() {
  const ids = new Set(filteredMapCameras().map(c => c.id));
  let located = 0;
  for (const [id, marker] of cameraMap.markers) {
    if (ids.has(id)) { if (!cameraMap.map.hasLayer(marker)) marker.addTo(cameraMap.map); located++; }
    else marker.remove();
  }
  $('#map-fit').disabled = located === 0;
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
        title: camera.name, alt: camera.name, riseOnHover: true, draggable: !state.session.demo,
        autoPan: true
      }).addTo(cameraMap.map);
      marker.on('dragstart', () => {
        hideHelp(); cameraMap.map.closePopup(); cameraMap.dragging = true;
      });
      marker.on('dragend', () => {
        cameraMap.dragging = false;
        const destination = marker.getLatLng();
        const current = state.cameras.find(c => c.id === camera.id);
        if (!current) return renderCameraMap();
        marker.setLatLng([current.latitude, current.longitude]);
        selectMapCamera(camera.id, {pan: false});
        setMapDraft(destination);
      });
      cameraMap.markers.set(camera.id, marker);
    } else marker.setLatLng([camera.latitude, camera.longitude]);
    if (marker.getElement()) {
      marker.getElement().title = camera.name;
      marker.getElement().setAttribute('aria-label', camera.name);
    }
    const popup = document.createElement('div');
    popup.className = 'map-popup';
    popup.innerHTML = `<div class="heading-with-help"><strong>${escapeHTML(camera.name)}</strong>${help(camera.latitude.toFixed(5) + ', ' + camera.longitude.toFixed(5), 'Координаты камеры')}</div>
      <div class="map-camera-preview"><span>Нет сигнала</span>${state.session.demo ? '' : `<img data-camera-img="${camera.id}" alt="Кадр камеры ${escapeHTML(camera.name)}" hidden>`}<span class="map-preview-status" data-camera-label="${camera.id}">ОЖИДАНИЕ</span></div>
      <button class="primary" data-open>Открыть камеру</button><button class="text-button" data-locate>Изменить место</button>`;
    $('[data-open]', popup).onclick = () => cameraSettings(camera.id);
    $('[data-locate]', popup).onclick = () => { cameraMap.map.closePopup(); selectMapCamera(camera.id); };
    marker.bindPopup(popup, {maxWidth: 300, minWidth: 210});
  }
  filterMapMarkers();
  updateMapMarkerState();
}

function selectMapCamera(id, {pan = true} = {}) {
  if (cameraMap.busy) return;
  const camera = state.cameras.find(c => c.id === id);
  if (!camera) return;
  cameraMap.selected = id;
  cameraMap.preview?.remove(); cameraMap.preview = null;
  updateMapMarkerState();
  renderMapCameraList();
  $('#map-location').hidden = false;
  $('#map-location').innerHTML = `<div class="heading-with-help"><h2>${escapeHTML(camera.name)}</h2>${help("Нажмите на карту, перетащите отметку или введите координаты. Затем сохраните отметку.", "Как задать координаты")}</div><span id="map-draft-status" class="pill warning" hidden>Не сохранено</span>
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
  if (pan && hasCameraLocation(camera)) cameraMap.map?.setView([camera.latitude, camera.longitude], Math.max(cameraMap.map.getZoom(), 12));
}

function setMapDraft(latlng) {
  if (!$('#map-latitude') || cameraMap.busy) return;
  const point = latlng.wrap();
  $('#map-latitude').value = point.lat.toFixed(6);
  $('#map-longitude').value = point.lng.toFixed(6);
  updateMapPreview();
}

function updateMapMarkerState() {
  for (const [id, marker] of cameraMap.markers) {
    const hasDraft = id === cameraMap.selected && !!cameraMap.preview;
    marker.setOpacity(hasDraft ? 0.35 : 1);
    marker.getElement()?.classList.toggle('is-selected', id === cameraMap.selected);
    if (cameraMap.busy || state.session?.demo || hasDraft) marker.dragging.disable();
    else marker.dragging.enable();
  }
  if (cameraMap.preview) {
    if (cameraMap.busy) cameraMap.preview.dragging.disable();
    else cameraMap.preview.dragging.enable();
  }
}

function updateMapPreview() {
  if (!cameraMap.map || cameraMap.busy) return;
  const lat = $('#map-latitude'), lon = $('#map-longitude');
  cameraMap.preview?.remove(); cameraMap.preview = null;
  if (!lat.validity.valid || !lon.validity.valid) { updateMapMarkerState(); return; }
  cameraMap.preview = L.marker([lat.valueAsNumber, lon.valueAsNumber], {
    icon: L.divIcon({className: 'camera-map-marker map-draft-marker', html: '<span aria-hidden="true">▣</span>', iconSize: [38, 38], iconAnchor: [19, 19]}),
    draggable: true, autoPan: true, zIndexOffset: 1000, title: 'Перетащить новую отметку', alt: 'Новая отметка камеры'
  }).addTo(cameraMap.map);
  cameraMap.preview.on('dragstart', () => { cameraMap.dragging = true; hideHelp(); });
  cameraMap.preview.on('dragend', event => { cameraMap.dragging = false; setMapDraft(event.target.getLatLng()); });
  updateMapMarkerState();
  $('#map-draft-status').hidden = false;
  setHelp('#map-hint', 'Новая отметка выбрана. Нажмите «Сохранить отметку».');
}

async function saveMapLocation(id, location) {
  if (cameraMap.busy) return;
  const form = $('#map-location-form');
  cameraMap.busy = true;
  updateMapMarkerState();
  $('fieldset', form).disabled = true;
  $('#map-location-error').textContent = '';
  try {
    await api('/cameras/' + id + '/location', {method: 'PUT', body: JSON.stringify(location)});
    const camera = state.cameras.find(c => c.id === id);
    if (camera) Object.assign(camera, location);
    if (form.isConnected) {
      cameraMap.busy = false; clearMapSelection(); renderCameraMap();
      if (location.latitude !== null) cameraMap.map?.panTo([location.latitude, location.longitude]);
    }
    toast(location.latitude === null ? 'Отметка удалена. Камера осталась в списке.' : 'Место камеры сохранено. Перезапуск не требуется.');
  } catch (error) {
    if (form.isConnected) $('#map-location-error').textContent = error.message;
    else if (state.session) toast(error.message);
  } finally {
    cameraMap.busy = false;
    updateMapMarkerState();
    if (form.isConnected) $('fieldset', form).disabled = !!state.session?.demo;
  }
}

function clearMapSelection() {
  if (cameraMap.busy) return;
  cameraMap.selected = null;
  cameraMap.preview?.remove(); cameraMap.preview = null;
  updateMapMarkerState();
  if (!$('#camera-map')) return;
  $('#map-location').hidden = true;
  $('#camera-map').classList.remove('is-picking');
  setHelp('#map-hint', 'Нажмите на карту, чтобы добавить камеру в точке. Существующие отметки можно перетаскивать; сохраните новое положение после переноса.');
  renderMapCameraList();
}

function openMapPoint(latlng) {
  hideHelp();
  const point = latlng.wrap();
  const location = {latitude: Number(point.lat.toFixed(6)), longitude: Number(point.lng.toFixed(6))};
  const node = document.createElement('div');
  node.className = 'map-point-popup';
  node.innerHTML = `<div class="heading-with-help"><strong>Точка на карте</strong>${help(location.latitude + ', ' + location.longitude, 'Координаты точки')}</div>
    <button class="primary" data-new-camera>+ Добавить камеру</button>
    ${state.cameras.length ? `<select aria-label="Камера для точки" data-existing-camera><option value="">Выбрать камеру</option>${state.cameras.map(c => `<option value="${c.id}">${escapeHTML(c.name)}</option>`).join('')}</select><button class="secondary" data-place-camera disabled>Разместить здесь</button>` : ''}`;
  $('[data-new-camera]', node).onclick = () => { cameraMap.map.closePopup(); addCamera(location); };
  const select = $('[data-existing-camera]', node);
  if (select) {
    select.onchange = () => { $('[data-place-camera]', node).disabled = !select.value; };
    $('[data-place-camera]', node).onclick = () => {
      cameraMap.map.closePopup(); selectMapCamera(select.value, {pan: false}); setMapDraft(point);
    };
  }
  L.popup({maxWidth: 300, minWidth: 210}).setLatLng(point).setContent(node).openOn(cameraMap.map);
}

function expandCameraMap(expanded) {
  const layout = $('.map-layout');
  if (!layout) return;
  const active = expanded ?? !layout.classList.contains('is-expanded');
  hideHelp();
  layout.classList.toggle('is-expanded', active);
  document.body.classList.toggle('map-expanded-body', active);
  $('#map-expand').setAttribute('aria-pressed', String(active));
  $('#map-expand').setAttribute('aria-label', active ? 'Свернуть карту' : 'Развернуть карту');
  setHelp('#map-expand', active ? 'Свернуть карту · Escape' : 'Развернуть карту');
  requestAnimationFrame(() => cameraMap.map?.invalidateSize({pan: true, animate: false}));
}

document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !$('#modal').open && $('.map-layout.is-expanded')) expandCameraMap(false);
});

function openCameraOnMap(id) {
  $('#modal').close();
  navigate('map');
  selectMapCamera(id);
}
