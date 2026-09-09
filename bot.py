import os
import json
import asyncio
import tempfile
import subprocess
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]
BANNER = "/app/banner.mp4"

ALLOWED_USERS = (
    set(map(int, os.environ.get("ALLOWED_USERS", "").split(",")))
    if os.environ.get("ALLOWED_USERS") else set()
)

# Railway 512 MB: only one FFmpeg render at a time.
SEMAPHORE = asyncio.Semaphore(1)

# Prevent accidental double-processing of the same Telegram update inside one
# running process. This is an extra guard; Railway must still have only one
# active replica/process for the bot token.
SEEN_UPDATES = {}
SEEN_TTL = 120

OUTPUT_W = 576
OUTPUT_H = 1024
MAX_INPUT_MB = 50
BANNER_FALLBACK_DURATION = 4.4


def run_cmd(cmd, timeout=900):
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return None, f"timeout\n{(e.stderr or '')[-8000:]}"
    return result.returncode, result.stderr[-12000:]


def probe(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_streams", "-show_format",
            path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    try:
        data = json.loads(result.stdout)
    except Exception:
        return None

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not video:
        return None

    def duration_of(stream):
        try:
            return float(stream.get("duration") or 0)
        except (TypeError, ValueError):
            return 0.0

    duration = duration_of(video) or duration_of(audio)
    if not duration:
        try:
            duration = float(data.get("format", {}).get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0

    return {
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": audio is not None,
    }


def is_duplicate_update(update):
    update_id = getattr(update, "update_id", None)
    if update_id is None:
        return False

    now = asyncio.get_event_loop().time()
    # Small TTL cache; keep memory bounded.
    for key, timestamp in list(SEEN_UPDATES.items()):
        if now - timestamp > SEEN_TTL:
            SEEN_UPDATES.pop(key, None)

    if update_id in SEEN_UPDATES:
        return True

    SEEN_UPDATES[update_id] = now
    return False


def get_banner_duration():
    info = probe(BANNER)
    if not info or info["duration"] <= 0:
        return BANNER_FALLBACK_DURATION
    # Use the real animation length. This prevents cutting the banner animation
    # in half. A 4.4s fallback is used only if probing fails.
    return info["duration"]


def insert_points(duration, banner_duration):
    if duration <= 60:
        # Exactly the middle for videos up to one minute.
        return [round(max(0.0, duration / 2.0 - banner_duration / 2.0), 3)]

    # First banner at 00:20, then every 60 seconds.
    points = []
    t = 20.0
    while t + banner_duration <= duration - 0.05:
        points.append(round(t, 3))
        t += 60.0
    return points


def process_video(input_path, output_path):
    info = probe(input_path)
    if not info:
        return False, "Не удалось прочитать видео"

    duration = info["duration"]
    if duration < 3:
        return False, "Видео слишком короткое"
    if not info["has_audio"]:
        return False, "В видео нет аудиодорожки"
    if not os.path.isfile(BANNER):
        return False, "Не найден /app/banner.mp4"

    banner_duration = get_banner_duration()
    if banner_duration <= 0:
        return False, "Не удалось определить длительность banner.mp4"

    points = insert_points(duration, banner_duration)
    if not points:
        return False, "Для этого видео не помещается ни один баннер"

    # Input logic:
    # 9:16 (or close): fill 576x1024, crop only excess edges.
    # 16:9 / landscape: preserve the whole source, put it over a blurred
    # vertical background so there are no ugly black bars.
    aspect = info["width"] / max(info["height"], 1)
    if aspect < 1.0:
        base = (
            f"scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=increase,"
            f"crop={OUTPUT_W}:{OUTPUT_H},setsar=1,fps=30,format=yuv420p"
        )
    else:
        base = (
            f"split=2[sharp][bg];"
            f"[bg]scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=increase,"
            f"crop={OUTPUT_W}:{OUTPUT_H},gblur=sigma=28,"
            f"setsar=1,fps=30[bgv];"
            f"[sharp]scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=decrease,"
            f"setsar=1,fps=30[fgv];"
            f"[bgv][fgv]overlay=(W-w)/2:(H-h)/2,"
            f"format=yuv420p"
        )

    # Build one segment per banner. The original video is frozen during each
    # banner, while the banner video itself plays its COMPLETE animation.
    v_parts = []
    a_parts = []

    cursor = 0.0
    for i, point in enumerate(points):
        # Main video before banner.
        if point > cursor:
            v_parts.append(
                f"[src]trim=start={cursor}:end={point},setpts=PTS-STARTPTS[v{i}a]"
            )
            a_parts.append(
                f"[0:a]atrim=start={cursor}:end={point},"
                f"asetpts=PTS-STARTPTS,aresample=48000,"
                f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a{i}a]"
            )

        # Freeze one exact frame from the source at the banner start.
        freeze_idx = i + 100
        v_parts.append(
            f"[src]trim=start={point}:end={point + 1/30},"
            f"setpts=PTS-STARTPTS,select='eq(n,0)',"
            f"tpad=stop_mode=clone:stop_duration={banner_duration},"
            f"trim=duration={banner_duration},setpts=PTS-STARTPTS[f{freeze_idx}]"
        )

        # IMPORTANT: no chromakey. The banner is inserted as a normal video,
        # fully visible, maximized to the available 576px width and centered.
        banner_idx = i + 200
        v_parts.append(
            f"[1:v]trim=duration={banner_duration},setpts=PTS-STARTPTS,"
            f"scale={OUTPUT_W}:-2:force_original_aspect_ratio=decrease,"
            f"setsar=1,format=yuv420p[ban{banner_idx}]"
        )
        v_parts.append(
            f"[f{freeze_idx}][ban{banner_idx}]overlay="
            f"x=(W-w)/2:y=(H-h)/2:shortest=1,format=yuv420p[ad{i}]"
        )

        a_parts.append(
            f"[1:a]atrim=duration={banner_duration},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=sample_fmts=fltp:"
            f"sample_rates=48000:channel_layouts=stereo,"
            f"volume=1.0[ba{i}]"
        )

        cursor = point + banner_duration

    # Tail of the original video.
    if cursor < duration:
        tail_i = len(points) + 500
        v_parts.append(
            f"[src]trim=start={cursor},setpts=PTS-STARTPTS[tailv{tail_i}]"
        )
        a_parts.append(
            f"[0:a]atrim=start={cursor},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=sample_fmts=fltp:"
            f"sample_rates=48000:channel_layouts=stereo[taila{tail_i}]"
        )

    # Source normalization happens once before all cuts.
    # For landscape input base creates a blurred vertical canvas with the
    # complete 16:9 frame centered on it.
    source_prefix = f"[0:v]{base}[src];"

    # Concatenate video/audio in the exact same order.
    ordered_v = []
    ordered_a = []
    for i in range(len(points)):
        if i == 0:
            pass
        ordered_v.append(f"[v{i}a]")
        ordered_a.append(f"[a{i}a]")
        ordered_v.append(f"[ad{i}]")
        ordered_a.append(f"[ba{i}]")
    if cursor < duration:
        ordered_v.append(f"[tailv{len(points)+500}]")
        ordered_a.append(f"[taila{len(points)+500}]")

    filters = (
        source_prefix
        + ";".join(v_parts)
        + ";"
        + ";".join(a_parts)
        + ";"
        + "".join(ordered_v)
        + f"concat=n={len(ordered_v)}:v=1:a=0,settb=1/30[outv];"
        + "".join(ordered_a)
        + f"concat=n={len(ordered_a)}:v=0:a=1[outa]"
    )

    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-threads", "1",
        "-filter_threads", "1",
        "-filter_complex_threads", "1",
        "-i", input_path,
        "-stream_loop", "-1", "-i", BANNER,
        "-filter_complex", filters,
        "-map", "[outv]",
        "-map", "[outa]",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "30",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-s", f"{OUTPUT_W}x{OUTPUT_H}",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-shortest",
        output_path,
    ]

    returncode, stderr = run_cmd(cmd)
    if returncode is None:
        return False, stderr
    if returncode != 0:
        return False, f"FFmpeg returncode={returncode}\n{stderr}"

    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        return False, "FFmpeg завершился, но выходной файл пустой/отсутствует"

    check = probe(output_path)
    if not check or check["width"] != OUTPUT_W or check["height"] != OUTPUT_H:
        return False, "После рендера размер видео не 576x1024"
    if not check["has_audio"]:
        return False, "После рендера отсутствует audio"

    return True, "ok"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_duplicate_update(update):
        return
    user_id = update.message.from_user.id
    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Нет доступа")
        return
    await update.message.reply_text(
        "👋 Привет!\n\n"
        "🎬 Скидывай видео — вставлю баннер CSDOG\n\n"
        "📌 Правила:\n"
        "• До 1 мин → баннер по центру\n"
        "• Больше 1 мин → 0:20, 1:20, 2:20...\n"
        "• 9:16 → заполняется весь вертикальный кадр\n"
        "• 16:9 → сохраняется целиком на вертикальном фоне\n"
        "• Баннер → полностью видим, максимально широкий\n"
        "• Озвучка баннера сохраняется\n\n"
        "📦 Макс: 50MB"
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_duplicate_update(update):
        return
    await update.message.reply_text(
        f"Твой Telegram ID: {update.message.from_user.id}"
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_duplicate_update(update):
        return
    msg = update.message
    user_id = msg.from_user.id
    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        await msg.reply_text("⛔ Нет доступа")
        return

    video = msg.video or msg.document
    if not video:
        return

    if video.file_size and video.file_size > MAX_INPUT_MB * 1024 * 1024:
        await msg.reply_text(f"❌ Максимум {MAX_INPUT_MB}MB")
        return

    status = await msg.reply_text("⏳ Скачиваю...")

    async with SEMAPHORE:
        with tempfile.TemporaryDirectory(prefix="csdog_") as tmp:
            input_path = os.path.join(tmp, "input.mp4")
            output_path = os.path.join(tmp, "output.mp4")

            try:
                tg_file = await context.bot.get_file(video.file_id)
                await tg_file.download_to_drive(input_path)
            except Exception as e:
                await status.edit_text(f"❌ Ошибка скачивания: {e}")
                return

            await status.edit_text("🎬 Обрабатываю... (1 видео за раз)")

            loop = asyncio.get_running_loop()
            try:
                success, err = await loop.run_in_executor(
                    None, process_video, input_path, output_path
                )
            except Exception as e:
                await status.edit_text(f"❌ Ошибка ffmpeg: {e}")
                return

            if not success:
                await status.edit_text(f"❌ Ошибка:\n{err[-3500:]}")
                return

            await status.edit_text("📤 Отправляю...")

            try:
                with open(output_path, "rb") as f:
                    await msg.reply_video(
                        video=f,
                        caption="✅ Готово! Баннер CSDOG вставлен",
                        supports_streaming=True,
                    )
                await status.delete()
            except Exception as e:
                await status.edit_text(f"❌ Ошибка отправки: {e}")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))

    print("✅ CSDOG bot v10 | Railway 512MB | FFmpeg concurrency=1")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
