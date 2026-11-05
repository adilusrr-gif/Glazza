/* Login presentation only: credentials and sessions stay in the existing API. */
(() => {
  const root = document.getElementById('login');
  const form = document.getElementById('login-form');
  const password = document.getElementById('login-password');
  const toggle = document.getElementById('login-password-toggle');
  const submit = document.getElementById('login-submit');
  const error = document.getElementById('login-error');
  const copy = {
    ru: {heroFirst:'Видеть больше.',heroSecond:'Реагировать вовремя.',heroSubtitle:'Интеллектуальное видеонаблюдение',eyebrow:'GLAZZA / ПАНЕЛЬ УПРАВЛЕНИЯ',title:'С возвращением',subtitle:'Войдите в систему видеонаблюдения',username:'Логин',password:'Пароль',submit:'Войти в Glazza',busy:'Входим…',show:'Показать пароль',hide:'Скрыть пароль',help:'Помощь со входом',hint:'Войдите в панель наблюдения. Данные для первого входа находятся в файле credentials.txt на сервере.',secure:'Защищённое подключение',local:'Локальная система видеонаблюдения',invalid:'Неверный логин или пароль',limited:'Слишком много попыток. Подождите 5 минут',unavailable:'Не удалось подключиться. Проверьте соединение и повторите вход.'},
    kk: {heroFirst:'Көбірек көру.',heroSecond:'Уақытында әрекет ету.',heroSubtitle:'Зияткерлік бейнебақылау',eyebrow:'GLAZZA / БАСҚАРУ ПАНЕЛІ',title:'Қайта оралуыңызбен',subtitle:'Бейнебақылау жүйесіне кіріңіз',username:'Логин',password:'Құпиясөз',submit:'Glazza жүйесіне кіру',busy:'Кіру…',show:'Құпиясөзді көрсету',hide:'Құпиясөзді жасыру',help:'Кіру бойынша көмек',hint:'Бақылау панеліне кіріңіз. Алғашқы кіруге арналған деректер сервердегі credentials.txt файлында сақталған.',secure:'Қорғалған қосылым',local:'Жергілікті бейнебақылау жүйесі',invalid:'Логин немесе құпиясөз қате',limited:'Кіру әрекеттері тым көп. 5 минут күтіңіз',unavailable:'Қосылу мүмкін болмады. Байланысты тексеріп, қайта кіріңіз.'}
  };
  let language = 'ru', busy = false, errorKey = '';
  try { if (localStorage.getItem('glazza.login.language') === 'kk') language = 'kk'; } catch (_) { /* Storage is optional. */ }
  function update() {
    root.lang = language;
    const c = copy[language];
    root.querySelectorAll('[data-login-i18n]').forEach(node => { node.textContent = c[node.dataset.loginI18n]; });
    submit.querySelector('[data-login-i18n]').textContent = busy ? c.busy : c.submit;
    toggle.setAttribute('aria-label',password.type === 'password' ? c.show : c.hide);
    const help = root.querySelector('.login-help');
    help.setAttribute('aria-label',c.help);help.dataset.help = c.hint;
    root.querySelectorAll('[data-login-language]').forEach(button => button.setAttribute('aria-pressed',String(button.dataset.loginLanguage === language)));
    // The label reflects the actual browser transport rather than promising TLS on HTTP.
    document.getElementById('login-connection-text').textContent = location.protocol === 'https:' ? c.secure : c.local;
    if (errorKey) error.textContent = c[errorKey];
  }
  toggle.onclick = () => {
    const show = password.type === 'password';
    password.type = show ? 'text' : 'password';
    toggle.setAttribute('aria-pressed',String(show));
    update();
  };
  root.querySelectorAll('[data-login-language]').forEach(button => {
    button.onclick = () => {
      language = button.dataset.loginLanguage;
      try { localStorage.setItem('glazza.login.language',language); } catch (_) { /* Optional. */ }
      update();
    };
  });
  window.glazzaLogin = {
    begin() {
      if (busy) return false;
      busy = true;errorKey = '';error.textContent = '';
      submit.disabled = true;form.setAttribute('aria-busy','true');update();return true;
    },
    finish() { busy = false;submit.disabled = false;form.removeAttribute('aria-busy');update(); },
    fail(message) {
      errorKey = message === copy.ru.invalid ? 'invalid' : message === copy.ru.limited ? 'limited' : 'unavailable';
      error.textContent = copy[language][errorKey];
      password.setAttribute('aria-invalid','true');password.setAttribute('aria-describedby','login-error');
    },
    conceal() { password.type = 'password';toggle.setAttribute('aria-pressed','false');update(); }
  };
  password.addEventListener('input', () => { password.removeAttribute('aria-invalid');password.removeAttribute('aria-describedby'); });
  update();
})();
