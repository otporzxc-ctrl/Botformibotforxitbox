# CSDOG bot v9

Railway 512 MB optimized Telegram bot.

- Output: 576x1024 TikTok/Reels
- FFmpeg concurrency: 1
- Banner: full 576px canvas width, centered vertically, source aspect ratio preserved
- Video branches are independently normalized to 576x1024 / 30 FPS / SAR 1:1 before concat
- `/id` is available independently of ALLOWED_USERS
- `banner.mp4` must be present next to Dockerfile

## Deploy

Required Railway variable:
- `BOT_TOKEN`

Optional:
- `ALLOWED_USERS=123,456`
