import os
import json
import asyncio
import tempfile
import subprocess
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]
BANNER = "/app/banner.mp4"
ALLOWED_USERS = set(map(int, os.environ.get("ALLOWED_USERS", "").split(","))) if os.environ.get("ALLOWED_USERS") else set()
SEMAPHORE = asyncio.Semaphore(1)
SEEN_UPDATES = {}
SEEN_TTL = 120
OUTPUT_W, OUTPUT_H = 576, 1024
MAX_INPUT_MB = 50
BANNER_FALLBACK_DURATION = 4.4


def run_cmd(cmd, timeout=900):
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace", timeout=timeout)
        return p.returncode, p.stderr[-12000:]
    except subprocess.TimeoutExpired as e:
        return None, f"timeout\n{(e.stderr or '')[-8000:]}"


def probe(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    try:
        d = json.loads(p.stdout)
    except Exception:
        return None
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        return None
    def dur(s):
        try: return float(s.get("duration") or 0)
        except Exception: return 0.0
    duration = dur(v) or dur(a) or float(d.get("format", {}).get("duration") or 0)
    return {"duration": duration, "width": int(v.get("width") or 0), "height": int(v.get("height") or 0), "has_audio": a is not None}


def duplicate(update):
    uid = getattr(update, "update_id", None)
    if uid is None: return False
    now = asyncio.get_event_loop().time()
    for k, t in list(SEEN_UPDATES.items()):
        if now - t > SEEN_TTL: SEEN_UPDATES.pop(k, None)
    if uid in SEEN_UPDATES: return True
    SEEN_UPDATES[uid] = now
    return False


def banner_duration():
    x = probe(BANNER)
    return x["duration"] if x and x["duration"] > 0 else BANNER_FALLBACK_DURATION


def points_for(duration, bd):
    if duration <= 60:
        start = duration / 2 - bd / 2
        return [max(0.0, start)] if start + bd <= duration else []
    out, t = [], 20.0
    while t + bd <= duration - 0.01:
        out.append(t)
        t += 60.0
    return out


def process_video(inp, out):
    info = probe(inp)
    if not info: return False, "Не удалось прочитать видео"
    d = info["duration"]
    if d < 3: return False, "Видео слишком короткое"
    if not info["has_audio"]: return False, "В видео нет аудиодорожки"
    if not os.path.isfile(BANNER): return False, "Не найден /app/banner.mp4"
    bd = banner_duration()
    points = points_for(d, bd)
    if not points: return False, "Баннер не помещается в это видео"

    # First normalize the ORIGINAL video exactly once. Every later trim comes
    # from this already-normalized stream, so concat can never receive 720x1280,
    # 568x1280, odd SAR, or another source format.
    if info["width"] < info["height"]:
        source_norm = f"[0:v]scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=increase,crop={OUTPUT_W}:{OUTPUT_H},setsar=1,fps=30,format=yuv420p[src];"
    else:
        source_norm = (
            f"[0:v]split=2[fg0][bg0];"
            f"[bg0]scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=increase,crop={OUTPUT_W}:{OUTPUT_H},gblur=sigma=28,setsar=1,fps=30,format=yuv420p[bg];"
            f"[fg0]scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=decrease,setsar=1,fps=30,format=yuv420p[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1,fps=30,format=yuv420p[src];"
        )

    # Build exact timeline intervals. No branch counter, no 2/3 failure.
    intervals = []
    cursor = 0.0
    for p in points:
        if p > cursor + 0.001: intervals.append(("video", cursor, p))
        intervals.append(("banner", p, min(p + bd, d)))
        cursor = p + bd
    if cursor < d - 0.001: intervals.append(("video", cursor, d))

    # Split normalized source into exactly the number of video intervals that need it.
    n_video = sum(1 for x in intervals if x[0] == "video")
    split_labels = [f"sv{i}" for i in range(n_video)]
    if n_video == 1:
        split_expr = f"[src]null[{split_labels[0]}];"
    else:
        split_expr = f"[src]split={n_video}" + "".join(f"[{x}]" for x in split_labels) + ";"

    vparts, aparts = [], []
    vi = 0
    for i, (kind, start, end) in enumerate(intervals):
        if kind == "video":
            lab = f"v{i}"
            vparts.append(f"[{split_labels[vi]}]trim=start={start}:end={end},setpts=PTS-STARTPTS[{lab}]")
            aparts.append(f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a{i}]")
            vi += 1
        else:
            # Freeze one frame from the video immediately before the banner.
            # The banner itself is scaled to maximum width while preserving all
            # of its 1350x750 animation, so no part of it is cropped.
            fv = f"freeze{i}"
            av = f"ad{i}"
            b = f"banner{i}"
            vparts.append(
                f"[src]trim=start={max(0,start-1/30)}:end={min(d,start+1/30)},setpts=PTS-STARTPTS,select='eq(n,0)',"
                f"tpad=stop_mode=clone:stop_duration={end-start},trim=duration={end-start},setpts=PTS-STARTPTS[{fv}]"
            )
            vparts.append(
                f"[1:v]trim=duration={end-start},setpts=PTS-STARTPTS,"
                f"scale={OUTPUT_W}:-2:force_original_aspect_ratio=decrease,setsar=1,format=yuv420p[{b}];"
                f"[{fv}][{b}]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1,setsar=1,fps=30,format=yuv420p[{av}]"
            )
            aparts.append(f"[1:a]atrim=duration={end-start},asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,volume=1.0[aa{i}]")

    ordered_v = [f"[{('v' if kind=='video' else 'ad')}{i}]" for i,(kind,_,_) in enumerate(intervals)]
    ordered_a = [f"[{('a' if kind=='video' else 'aa')}{i}]" for i,(kind,_,_) in enumerate(intervals)]
    fc = source_norm + split_expr + ";".join(vparts) + ";" + ";".join(aparts) + ";" + "".join(ordered_v) + f"concat=n={len(intervals)}:v=1:a=0,settb=1/30[outv];" + "".join(ordered_a) + f"concat=n={len(intervals)}:v=0:a=1[outa]"

    cmd = ["ffmpeg","-hide_banner","-y","-threads","1","-filter_threads","1","-filter_complex_threads","1","-i",inp,"-stream_loop","-1","-i",BANNER,"-filter_complex",fc,"-map","[outv]","-map","[outa]","-c:v","libx264","-preset","ultrafast","-crf","30","-pix_fmt","yuv420p","-r","30","-s","576x1024","-c:a","aac","-b:a","128k","-movflags","+faststart","-shortest",out]
    rc, err = run_cmd(cmd)
    if rc is None: return False, err
    if rc != 0: return False, f"FFmpeg returncode={rc}\n{err}"
    check = probe(out)
    if not check or check["width"] != 576 or check["height"] != 1024 or not check["has_audio"]:
        return False, "Результат не прошёл проверку 576x1024 + audio"
    return True, "ok"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if duplicate(update): return
    await update.message.reply_text("👋 Скидывай видео.\n\n• 9:16 → вертикальный кадр\n• 16:9 → целиком на вертикальном фоне\n• Баннер целиком и максимально широкий\n• До 1 мин → по центру\n• Длиннее → 0:20, 1:20, 2:20...\n• Аудио баннера сохраняется")

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if duplicate(update): return
    await update.message.reply_text(f"Твой Telegram ID: {update.message.from_user.id}")

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if duplicate(update): return
    msg = update.message
    video = msg.video or msg.document
    if not video: return
    if video.file_size and video.file_size > MAX_INPUT_MB * 1024 * 1024:
        await msg.reply_text(f"❌ Максимум {MAX_INPUT_MB}MB"); return
    status = await msg.reply_text("⏳ Скачиваю...")
    async with SEMAPHORE:
        with tempfile.TemporaryDirectory(prefix="csdog_") as tmp:
            inp, out = os.path.join(tmp,"input.mp4"), os.path.join(tmp,"output.mp4")
            try:
                f = await context.bot.get_file(video.file_id); await f.download_to_drive(inp)
                await status.edit_text("🎬 Обрабатываю... (1 видео за раз)")
                loop = asyncio.get_running_loop()
                ok, err = await loop.run_in_executor(None, process_video, inp, out)
            except Exception as e:
                await status.edit_text(f"❌ Ошибка: {e}"); return
            if not ok:
                await status.edit_text(f"❌ Ошибка:\n{err[-3500:]}"); return
            try:
                with open(out,"rb") as f:
                    await msg.reply_video(video=f, caption="✅ Готово!", supports_streaming=True)
                await status.delete()
            except Exception as e:
                await status.edit_text(f"❌ Ошибка отправки: {e}")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    print("✅ CSDOG bot v12 FINAL | Railway 512MB")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__": main()
