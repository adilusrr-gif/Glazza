# Сторонние компоненты

Собственный код Glazza поставляется под MIT; это не меняет права на зависимости, веса моделей, датасеты и торговые марки.

| Компонент | Источник | Условия |
|---|---|---|
| Frigate 0.18.2 | https://github.com/blakeblackshear/frigate | MIT для кода; торговые марки регулируются отдельно |
| VD-MIL · Roger Figueroa Quintero, 2025–2026 | https://github.com/roggerfq/violence-detection-mil | CC BY-NC 4.0; коммерческое использование только с письменным разрешением автора |
| MoViNet-pytorch · Lorenzo Atzeni / fork roggerfq | https://github.com/roggerfq/MoViNet-pytorch | MIT для кода; оригинальный LICENSE сохраняется установщиком |
| VadCLIP | https://github.com/nwpu-zxr/VadCLIP | Apache-2.0 для кода; происхождение и условия checkpoint / UCF-Crime проверяются отдельно |
| CLIP в VadCLIP | https://github.com/openai/CLIP | MIT; сохраняйте upstream notices |
| MediaPipe | https://github.com/google-ai-edge/mediapipe | Apache-2.0 для кода; model cards и условия артефактов — у поставщика |
| PyTorch / torchvision | https://github.com/pytorch/pytorch | BSD-style, с уведомлениями сторонних библиотек |
| FastAPI / Uvicorn / httpx | Официальные PyPI-пакеты и репозитории | MIT / BSD в зависимости от пакета |
| FFmpeg | https://ffmpeg.org/legal.html | Условия зависят от сборки (LGPL/GPL); используем пакет дистрибутива |

Модели не включены в Git и не переопубликованы. Установщик MediaPipe ссылается на официальный Google URL. Для VadCLIP оператор передаёт файл, полученный у авторов; сохранённая локальная контрольная сумма подтверждает неизменность после установки, а не авторство файла.

VD-MIL подключается отдельной командой `scripts/models.py fight`, которая скачивает оригинальные файлы с закреплённых commits. Файлы `VD-MIL-README.md` и `MoViNet-LICENSE` сохраняются рядом с весами. Собственный адаптер Glazza сохраняет атрибуцию. Лицензия MIT Glazza не отменяет CC BY-NC и права на данные, на которых обучены модели. OpenFace и Digital Witness не включены. Glazza не выдаёт разрешение на коммерческое распространение сторонних весов только на основании открытой лицензии их кода.
