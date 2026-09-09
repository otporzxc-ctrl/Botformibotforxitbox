# CSDOG Bot v11

Telegram bot for inserting the CSDOG animated banner into videos.

## Processing
- Final output: 576x1024 (9:16), 30 FPS.
- Portrait input (9:16 and similar): fills the vertical frame; only excess edges are cropped.
- Landscape input (16:9 and similar): the complete original frame is preserved and centered over a blurred vertical background.
- Banner: the COMPLETE `banner.mp4` animation is used, with no chromakey and no cutting of the animation.
- Banner is scaled to the maximum available width (576 px) while preserving its original 1350x750 aspect ratio, then centered.
- Video up to 60 s: banner is centered in time.
- Video over 60 s: banners start at 0:20, 1:20, 2:20, etc., when there is enough remaining video time.
- Main video is frozen while the banner animation plays.
- Main audio is normalized to stereo 48 kHz; banner audio is 100% and preserved.
- Only one FFmpeg render runs at a time for Railway 512 MB.
- Telegram updates are deduplicated inside a process.

## Files
Put your real `banner.mp4` next to these files before building/deploying.

## Railway
Required variable:
- `BOT_TOKEN`

Optional:
- `ALLOWED_USERS` — comma-separated Telegram user IDs.
