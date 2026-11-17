# Проверка стабильности ресторанного модуля

Результаты от 17 ноября 2026 относятся к продуктовому коду коммита
`98a22174f44097d53c12e1febb870951ff914aba`. Ветка аудита добавляет тесты и отчёты;
продуктовые файлы не изменены.

Полный отчёт: [report.md](../../docs/audits/restaurant-stability-2026-11-17/report.md).
Удобный просмотр: [report.html](../../docs/audits/restaurant-stability-2026-11-17/report.html).

## Среда и границы

Все сценарии используют временные SQLite-базы и локальные процессы. Камеры
синтетические, HTTP Telegram подменён. Реальные сообщения не отправляются.
Для нагрузки используется настоящий Uvicorn; для MQTT — настоящий TCP и Paho.
Ни один сценарий не предназначен для запуска против рабочего ресторана.

Использован Python 3.12.14 и уже установленные зависимости из
`requirements-dev.txt`; дополнительно в проверочной среде доступны
`amqtt==0.12.2` и `psutil==7.2.2`. Node нужен для VM-проверок JavaScript.
Новые зависимости не включены в production requirements.

Команды ниже запускаются из корня репозитория. Перед повторением сохраните
предыдущие evidence: отдельные сценарии пишут в папку отчёта по умолчанию.
Метаданные этого аудита нельзя переносить на изменённый продуктовый код.

## Базовая регрессия

```bash
.venv/bin/python -m pytest -q
node --check app/static/restaurant.js
node --check app/static/staff.js
```

## Реальная нагрузка

```bash
.venv/bin/python tests/stability/restaurant_load.py \
  --duration 600 \
  --output docs/audits/restaurant-stability-2026-11-17/evidence/load-results.json \
  --commit 98a22174f44097d53c12e1febb870951ff914aba

.venv/bin/python tests/stability/load_connections.py
.venv/bin/python tests/stability/load_history.py
```

Основной сценарий длится минимум 10 минут и затем выполняет ограниченные
burst/history проверки. Дополнительный сценарий соединений длится 120 секунд.
Все HTTP 200 означают доступность ответов; приемлемая задержка и точность камеры
из этого не следуют. Нагрузочный CSV содержит исходные замеры.

## Перезапуски и целостность

```bash
PYTHONPATH=. .venv/bin/python tests/stability/restaurant_state.py
```

Выполняются настоящие SIGKILL дочерних процессов на заданных границах
транзакции. 72 часа истории моделируются ускоренно через логическое время;
это отдельная проверка от 10 минут реальной нагрузки. Перепады питания,
повреждение диска и полный production startup здесь не моделируются.

## Транспорт и уведомления

```bash
.venv/bin/python tests/stability/restaurant_transport.py
# При необходимости отдельно:
.venv/bin/python tests/stability/restaurant_transport.py --scope mqtt
.venv/bin/python tests/stability/restaurant_transport.py --scope delivery
```

В проверенном коде transport-сценарий возвращает **exit 1**, поскольку
воспроизводит три подтверждённых дефекта. Это ожидаемый результат аудита,
который нельзя переименовывать в PASS.

## Точные репродукции и независимая проверка

```bash
node tests/stability/restaurant_ui_faults.js
.venv/bin/python tests/stability/restaurant_alert_visibility.py
.venv/bin/python tests/stability/restaurant_loop_stall.py
.venv/bin/python tests/stability/restaurant_verify_transport.py
PYTHONPATH=. .venv/bin/python tests/stability/state_review_alerts.py
node tests/stability/state_review_ui.js
PYTHONPATH=. .venv/bin/python tests/stability/state_review_passes.py
```

Эти диагностические репродукции проверяют наблюдавшееся поведение старого
коммита. Их exit 0 означает, что репродукция выполнилась и записала результат,
а не что продукт исправлен. После исправлений необходимо заменить ожидание
дефекта на соответствующий положительный regression test.

VM-проверки исполняют исходный JavaScript с синтетическими DOM, fetch и часами.
Они проверяют управление запросами, но не являются проверкой рендеринга в
настоящем браузере.

## Сборка отчёта

```bash
.venv/bin/python tests/stability/build_report.py
```

Сборщик валидирует файлы доказательств, сохраняет сырые заключения, проверяет
ссылки на исходники и формирует Markdown, HTML, JSON, SARIF и JUnit. Он не
подменяет отсутствующий официальный runtime Project Sentinel и не вычисляет
канонический балл качества.

JUnit содержит 211 исходных тестов и шесть отдельных FAIL для подтверждённых
причин сбоев. Это не утверждение, что запускались 217 обычных unit-тестов.
