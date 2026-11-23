# Сторонние компоненты

Собственный код Glazza поставляется под MIT; это не меняет права на зависимости, веса моделей, датасеты и торговые марки.

| Компонент | Источник | Условия |
|---|---|---|
| Frigate 0.18.2 | https://github.com/blakeblackshear/frigate | MIT для кода; торговые марки регулируются отдельно |
| Paho MQTT Python 2.1.0 | https://github.com/eclipse-paho/paho.mqtt.python | EPL-2.0 OR BSD-3-Clause; используется для текущих состояний ресторанных зон |
| Eclipse Mosquitto 2.0.22 | https://github.com/eclipse-mosquitto/mosquitto | EPL-2.0 OR EDL-1.0 (BSD-3-Clause); отдельный контейнер во внутренней сети Compose |
| Leaflet 1.9.4 | https://github.com/Leaflet/Leaflet | BSD-2-Clause; лицензия сохранена в `app/static/vendor/leaflet` |
| Leaflet.markercluster 1.5.3 | https://github.com/Leaflet/Leaflet.markercluster | MIT; лицензия сохранена в `app/static/vendor/markercluster` |
| VD-MIL · Roger Figueroa Quintero, 2025–2026 | https://github.com/roggerfq/violence-detection-mil | CC BY-NC 4.0; коммерческое использование только с письменным разрешением автора |
| MoViNet-pytorch · Lorenzo Atzeni / fork roggerfq | https://github.com/roggerfq/MoViNet-pytorch | MIT для кода; оригинальный LICENSE сохраняется установщиком |
| VadCLIP | https://github.com/nwpu-zxr/VadCLIP | Apache-2.0 для кода; происхождение и условия checkpoint / UCF-Crime проверяются отдельно |
| CLIP в VadCLIP | https://github.com/openai/CLIP | MIT; сохраняйте upstream notices |
| MediaPipe | https://github.com/google-ai-edge/mediapipe | Apache-2.0 для кода; model cards и условия артефактов — у поставщика |
| PyTorch / torchvision | https://github.com/pytorch/pytorch | BSD-style, с уведомлениями сторонних библиотек |
| FastAPI / Uvicorn / httpx | Официальные PyPI-пакеты и репозитории | MIT / BSD в зависимости от пакета |
| FFmpeg | https://ffmpeg.org/legal.html | Условия зависят от сборки (LGPL/GPL); используем пакет дистрибутива |

Модели не включены в Git и не переопубликованы. Установщик MediaPipe ссылается на официальный Google URL. Для VadCLIP оператор передаёт UCF-Crime checkpoint из колонки Model в таблице авторов. Контрольная сумма позволяет воспроизводить установку и проверять целостность файла. Авторы отдельно публикуют признаки CLIP, которые для запуска адаптера не нужны. Условия коммерческого использования и распространения весов следует подтвердить у правообладателя перед включением в поставку. [Публикация весов](https://github.com/nwpu-zxr/VadCLIP/tree/c41067f07d252efcda18008bea367886070c33b0)

VD-MIL подключается отдельной командой `scripts/models.py fight`, которая скачивает оригинальные файлы с закреплённых commits. Файлы `VD-MIL-README.md` и `MoViNet-LICENSE` сохраняются рядом с весами. Собственный адаптер Glazza сохраняет атрибуцию. Лицензия MIT Glazza не отменяет CC BY-NC и права на данные, на которых обучены модели. OpenFace и Digital Witness не включены. Glazza не выдаёт разрешение на коммерческое распространение сторонних весов только на основании открытой лицензии их кода.

YuNet и SFace: OpenCV Zoo, commit `48b10fa505aeb51fbd127fc60a5c660040e823b7`. YuNet directory — MIT; SFace directory — Apache-2.0. Оригинальные LICENSE скачиваются рядом с весами `scripts/models.py identity`. Источники и контрольные суммы: `analyzer/identity_artifacts.json`. Соблюдение условий конкретного применения и оснований обработки биометрии требуется отдельно.
