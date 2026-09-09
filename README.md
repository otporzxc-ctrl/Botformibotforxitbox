# CSDOG Telegram Video Bot — Railway 512MB

Версия оптимизирована под Railway Free / 512 MB RAM.

## Главное изменение

Старая версия запускала до 5 FFmpeg одновременно и использовала `split=3/asplit=2` внутри одного большого filter graph. Это очень легко выбивает 512 MB RAM.

Новая версия:

- максимум **1 FFmpeg одновременно**;
- `split=3/asplit=2` убраны;
- pre/post/freeze берутся отдельными входами исходного файла;
- `threads=1`, `filter_threads=1`, `filter_complex_threads=1`;
- stderr FFmpeg ограниченно хранится в памяти вместо `capture_output=True`;
- после рендера проверяется наличие файла и его длительность;
- реальный `returncode` и хвост stderr отправляются пользователю при ошибке;
- временные файлы создаются через `TemporaryDirectory`.

## Файлы

В репозитории должны быть:

- `bot.py`
- `Dockerfile`
- `requirements.txt`
- `musordrop_animation_green-screen_sound_on.mp4`

Баннер Dockerfile автоматически копирует в `/app/banner.mp4`.

## Railway Variables

Обязательно:

`BOT_TOKEN=...`

Опционально:

`ALLOWED_USERS=123456789,987654321`
`FFMPEG_TIMEOUT=600`

## Почему не Cloudflare Workers

Cloudflare Workers не является хорошей заменой для тяжёлого локального FFmpeg-рендера. Для этого бота проще оставить Python worker там, где доступна память и процессор.

Если 512 MB всё равно окажется недостаточно для конкретных тяжёлых роликов, следующий шаг — вынести **только FFmpeg worker** на машину с 1–2 GB RAM, оставив Telegram-бота отдельно.
