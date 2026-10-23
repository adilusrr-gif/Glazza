# Сторонние компоненты

Собственный код Glazza поставляется под MIT; это не меняет права на зависимости, веса моделей, датасеты и торговые марки.

| Компонент | Источник | Условия |
|---|---|---|
| Frigate 0.18.2 | https://github.com/blakeblackshear/frigate | MIT для кода; торговые марки регулируются отдельно |
| VadCLIP | https://github.com/nwpu-zxr/VadCLIP | Apache-2.0 для кода; происхождение и условия checkpoint / UCF-Crime проверяются отдельно |
| CLIP в VadCLIP | https://github.com/openai/CLIP | MIT; сохраняйте upstream notices |
| MediaPipe | https://github.com/google-ai-edge/mediapipe | Apache-2.0 для кода; model cards и условия артефактов — у поставщика |
| PyTorch / torchvision | https://github.com/pytorch/pytorch | BSD-style, с уведомлениями сторонних библиотек |
| FastAPI / Uvicorn / httpx | Официальные PyPI-пакеты и репозитории | MIT / BSD в зависимости от пакета |
| FFmpeg | https://ffmpeg.org/legal.html | Условия зависят от сборки (LGPL/GPL); используем пакет дистрибутива |

Модели не включены в Git и не переопубликованы. Установщик MediaPipe ссылается на официальный Google URL. Для VadCLIP оператор передаёт файл, полученный у авторов; сохранённая локальная контрольная сумма подтверждает неизменность после установки, а не авторство файла.

VD-MIL (CC BY-NC), OpenFace с отдельным коммерческим лицензированием и Digital Witness без подтверждённой лицензии не включены в продукт по умолчанию. Glazza не выдаёт разрешение на коммерческое распространение сторонних весов только на основании открытой лицензии их кода.
