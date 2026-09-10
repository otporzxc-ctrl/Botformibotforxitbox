# MusorDrop Bot

Телеграм бот — вставляет рекламный баннер MusorDrop в середину видео.

## Деплой на Railway

### 1. Подготовка файлов
Положи в папку проекта:
- `bot.py`
- `Dockerfile`
- `requirements.txt`
- `musordrop_animation_green-screen_sound_on.mp4` (переименуется в banner.mp4)

### 2. Создай бота в Telegram
1. Открой @BotFather в Telegram
2. Напиши `/newbot`
3. Придумай имя и юзернейм
4. Скопируй токен (типа `7123456789:AAF...`)

### 3. Залей на GitHub
```
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/ТВОЙ_НИК/musordrop-bot.git
git push -u origin main
```

### 4. Railway
1. Зайди на railway.app
2. New Project → Deploy from GitHub
3. Выбери репозиторий
4. Variables → добавь: `BOT_TOKEN` = твой токен от BotFather
5. Deploy → готово!

## Как работает
1. Скидываешь видео боту
2. Бот находит середину
3. Замораживает кадр + размывает фон
4. Накладывает баннер (27% экрана, хрома-кей зелёного)
5. Возвращает готовое видео
