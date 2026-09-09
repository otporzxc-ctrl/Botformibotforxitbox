# CSDOG Bot v10

- Output: 576x1024 (9:16)
- 9:16 input: fills the vertical frame, cropping only excess edges.
- 16:9 input: full original frame is preserved and centered over a blurred vertical background.
- Banner: no chromakey; full animation; maximized to available 576px width; centered; no cropping.
- <=60 sec: banner centered in time.
- >60 sec: banners at 0:20, 1:20, 2:20, ... when there is enough time.
- Main video is frozen during each banner.
- Banner audio is 100%.
- Source audio is normalized to stereo/48k for reliable concat.
- One FFmpeg job at a time for Railway 512MB.
- /start and /id handlers are registered once.
- drop_pending_updates=True prevents old queued updates from causing duplicate replies after restart.

Put the existing `banner.mp4` next to these files before Docker build.
Required Railway variable: BOT_TOKEN
Optional: ALLOWED_USERS
