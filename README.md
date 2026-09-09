# CSDOG Bot v13

Final FFmpeg robustness fix: every individual video/freeze segment is explicitly normalized to exactly 576x1024, SAR 1:1, 30 FPS and yuv420p immediately before concat. Supports portrait and landscape inputs, full banner animation without chromakey, centered at maximum uncropped width. Long videos get banners at 0:20, 1:20, 2:20, etc. Railway concurrency is limited to one FFmpeg job.
