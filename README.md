# CSDOG Bot v12 FINAL

- 9:16 input -> fills 576x1024.
- 16:9 input -> preserved on a blurred vertical background.
- Complete banner animation, maximum width without cropping/distortion.
- <=60s -> banner centered in timeline.
- >60s -> 0:20, 1:20, 2:20... when the full banner fits.
- Main video freezes during banner; banner audio is preserved.
- Dynamic timeline; no fragile branch counter.
- Duplicate Telegram updates ignored.
- One FFmpeg render at a time for Railway 512MB.

Put your own `banner.mp4` next to the Dockerfile before deployment.
