'use strict';
let webcam = null;

function stopWebcam() {
  const current = webcam;
  webcam = null;
  if (!current) return;
  clearInterval(current.timer);
  current.stream?.getTracks().forEach(track => track.stop());
  if (current.recorder?.state === 'recording') current.recorder.stop();
}

async function openWebcam() {
  if (state.session?.demo) return toast('Веб-камера недоступна в демонстрации.');
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    const url = new URL(location.href);
    url.protocol = 'https:';
    url.port = '8443';
    modal('Веб-камера: защищённое подключение', `<p>Для веб-камеры требуется HTTPS.</p><a class="primary" href="${escapeHTML(url.href)}">Открыть HTTPS-панель</a>`, "Браузер разрешает доступ к камере через HTTPS или localhost. После перехода войдите снова. Для локального сертификата может потребоваться подтверждение браузера или установка сертификата на устройство.");
    return;
  }
  if (!window.MediaRecorder) return toast('Запись камеры не поддерживается этим браузером. Загрузите видеофайл.');
  modal('Тест с веб-камеры', `<video id="webcam-preview" autoplay muted playsinline class="webcam-preview"></video><div class="form-row"><label>Камера<select id="webcam-device" disabled><option>Запрашиваем доступ…</option></select></label><label>Длительность записи<select id="webcam-duration">${[5,10,20,30,60,120].map(n=>`<option value="${n}" ${n===10?'selected':''}>${n} секунд</option>`).join('')}</select></label></div><p id="webcam-status" role="status">Ожидание доступа к камере</p><p id="webcam-error" class="error" role="alert"></p><div class="form-actions"><button id="webcam-start" class="primary" disabled>Записать и анализировать</button><button id="webcam-stop" class="secondary" disabled>Завершить и анализировать</button></div>`, "Видео отправляется только после записи. Закрытие окна отменяет запись и выключает камеру. Микрофон не используется. Результат появится в истории анализа.");
  const current = {stream:null, recorder:null, timer:null, switching:false};
  webcam = current;
  const fail = error => {
    if (webcam !== current) return;
    const messages = {NotAllowedError:'Доступ к камере запрещён. Разрешите его в настройках сайта и откройте окно снова.', NotFoundError:'Камера не найдена. Подключите USB-камеру или откройте панель на телефоне.', NotReadableError:'Камера занята другой программой или недоступна.'};
    $('#webcam-error').textContent = messages[error.name] || error.message || 'Не удалось записать видео.';
  };
  async function connect(deviceId) {
    current.switching = true;
    $('#webcam-start').disabled = true;
    $('#webcam-device').disabled = true;
    current.stream?.getTracks().forEach(t=>t.stop());
    try {
      const stream = await navigator.mediaDevices.getUserMedia({audio:false, video:{width:{ideal:640},height:{ideal:360},frameRate:{ideal:15,max:30},...(deviceId?{deviceId:{exact:deviceId}}:{})}});
      if (webcam !== current) { stream.getTracks().forEach(t=>t.stop()); return; }
      current.stream = stream;
      $('#webcam-preview').srcObject = stream;
      const devices = (await navigator.mediaDevices.enumerateDevices()).filter(d=>d.kind==='videoinput');
      if (webcam !== current) return;
      const selected = stream.getVideoTracks()[0].getSettings().deviceId;
      $('#webcam-device').innerHTML = devices.map((d,i)=>`<option value="${escapeHTML(d.deviceId)}" ${d.deviceId===selected?'selected':''}>${escapeHTML(d.label || 'Камера '+(i+1))}</option>`).join('');
      $('#webcam-error').textContent = '';
      $('#webcam-status').textContent = 'Камера готова';
      $('#webcam-start').disabled = false;
    } catch(error) { fail(error); }
    finally { current.switching = false; if (webcam===current) $('#webcam-device').disabled = false; }
  }
  $('#webcam-device').onchange = e => connect(e.target.value);
  $('#webcam-start').onclick = () => {
    if (webcam!==current || current.switching || !current.stream?.active) return;
    try {
      const mime = ['video/webm;codecs=vp8','video/webm;codecs=vp9','video/mp4'].find(t=>MediaRecorder.isTypeSupported(t));
      if (!mime) throw new Error('Браузер не поддерживает запись WebM или MP4. Загрузите видеофайл.');
      const recorder = new MediaRecorder(current.stream,{mimeType:mime,videoBitsPerSecond:1500000});
      current.recorder = recorder;
      const chunks = [];
      let bytes = 0, recordingError = null;
      const started = performance.now(), seconds = Number($('#webcam-duration').value);
      $('#webcam-start').disabled = true;
      $('#webcam-device').disabled = true;
      $('#webcam-duration').disabled = true;
      $('#webcam-stop').disabled = false;
      $('#webcam-error').textContent = '';
      const finish = () => { if (recorder.state==='recording') recorder.stop(); };
      $('#webcam-stop').onclick = finish;
      recorder.ondataavailable = e => {
        if (e.data.size) { bytes += e.data.size; chunks.push(e.data); }
        if (bytes > 200*1024*1024) { recordingError = new Error('Запись превысила 200 МБ. Выберите меньшую длительность.'); finish(); }
      };
      recorder.onerror = e => { recordingError = e.error || new Error('Ошибка записи'); finish(); };
      recorder.onstop = async () => {
        clearInterval(current.timer);
        current.stream.getTracks().forEach(t=>t.stop());
        if (webcam!==current) return;
        $('#webcam-stop').disabled = true;
        try {
          if (recordingError) throw recordingError;
          if (performance.now()-started < 1000) throw new Error('Запись слишком короткая. Запишите хотя бы 5 секунд.');
          const blob = new Blob(chunks,{type:recorder.mimeType});
          $('#webcam-status').textContent = 'Отправляем запись на анализ…';
          const job = await api(scopeUrl('/jobs/upload?source=webcam'),{method:'POST',body:blob,headers:{'Content-Type':recorder.mimeType}});
          if (webcam!==current) return;
          $('#webcam-status').textContent = 'В очереди на анализ';
          $('#webcam-start').textContent = 'Перейти к результату';
          $('#webcam-start').disabled = false;
          $('#webcam-start').onclick = () => { $('#modal').close(); navigate('analysis'); };
          current.jobId = job.id;
          toast('Видео с веб-камеры отправлено на анализ');
        } catch(error) {
          fail(error);
          $('#webcam-status').textContent = 'Откройте веб-камеру снова, чтобы повторить запись.';
        }
      };
      recorder.start(1000);
      $('#webcam-status').textContent = 'Запись: 0 / '+seconds+' с';
      current.timer = setInterval(()=>{
        if (webcam!==current) return;
        const elapsed = (performance.now()-started)/1000;
        $('#webcam-status').textContent = 'Запись: '+Math.floor(elapsed)+' / '+seconds+' с';
        if (elapsed>=Math.min(seconds,119.5)) finish();
      },200);
    } catch(error) { fail(error); $('#webcam-start').disabled=false; }
  };
  await connect();
}
document.getElementById('modal').addEventListener('close',stopWebcam);
window.addEventListener('pagehide',stopWebcam);
