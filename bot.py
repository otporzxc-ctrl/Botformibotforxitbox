import os
import math
import json
import asyncio
import tempfile
import subprocess
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]
BANNER = "/app/banner.mp4"

ALLOWED_USERS = set(map(int, os.environ.get("ALLOWED_USERS", "").split(","))) \
    if os.environ.get("ALLOWED_USERS") else set()

# Railway 512 MB: never run several heavy FFmpeg encodes at once.
SEMAPHORE = asyncio.Semaphore(1)

OUTPUT_W = 576
OUTPUT_H = 1024
BANNER_DURATION = 4.4
MAX_INPUT_MB = 50


def run_cmd(cmd, timeout=600):
    """Run a command without keeping an unlimited stdout/stderr buffer in RAM."""
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
        tail = (e.stderr or "")[-6000:]
        return None, f"timeout\n{tail}"
    return result.returncode, result.stderr[-10000:]


def get_video_info(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
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

    info = {"duration": 0.0, "width": 1280, "height": 720, "has_audio": False}
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            info["width"] = int(s.get("width") or 1280)
            info["height"] = int(s.get("height") or 720)
            try:
                info["duration"] = float(s.get("duration") or 0)
            except (TypeError, ValueError):
                pass
        elif s.get("codec_type") == "audio":
            info["has_audio"] = True

    if not info["duration"]:
        try:
            info["duration"] = float(data.get("format", {}).get("duration") or 0)
        except (TypeError, ValueError):
            pass

    return info if info["duration"] > 0 else None


def get_insert_point(duration):
    # CSDOG rules:
    # <= 60s -> exactly the middle
    # > 60s -> first banner at 00:20
    return round(duration / 2, 2) if duration <= 60 else 20.0


def process_video(input_path, output_path):
    info = get_video_info(input_path)
    if not info:
        return False, "Не удалось прочитать видео"

    duration = info["duration"]
    if duration < 3:
        return False, "Видео слишком короткое"
    if not info["has_audio"]:
        return False, "В видео нет аудиодорожки"
    if not os.path.isfile(BANNER):
        return False, "Не найден /app/banner.mp4"

    insert = get_insert_point(duration)

    # The old graph used split=3 and asplit=2. On a 512 MB Railway container
    # that can keep several frame queues alive. We use one graph with one
    # filter thread and normalize the final video to the required TikTok size.
    #
    # Banner is fitted INSIDE the 576x1024 canvas and is always centered.
    # Its source aspect ratio is preserved, so it can never stick outside the
    # left/right edges of the video.
    banner_w = OUTPUT_W
    banner_h = int(round(banner_w * 750 / 1350))  # 320px for 1350x750 source
    banner_x = 0
    banner_y = (OUTPUT_H - banner_h) // 2

    # Make the source TikTok 9:16 without black bars: scale to cover + crop.
    base = (
        f"scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=increase,"
        f"crop={OUTPUT_W}:{OUTPUT_H},setsar=1,fps=30"
    )

    # Freeze exactly one source frame at insertion. The background is blurred,
    # then the CSDOG banner is placed in the exact center of the TikTok canvas.
    # The 4.4s ad replaces a 1s slice of the original and playback resumes from
    # the frame immediately after that slice.
    ad_start = insert
    ad_end = min(insert + 1.0, duration)

    filter_complex = (
        f"[0:v]{base},split=2[src][freeze];"
        f"[src]trim=start=0:end={insert},setpts=PTS-STARTPTS[part1v];"
        f"[src]trim=start={ad_end},setpts=PTS-STARTPTS[part2v];"
        f"[freeze]trim=start={ad_start}:end={ad_end},setpts=PTS-STARTPTS,"
        f"select='eq(n,0)',gblur=sigma=20,"
        f"tpad=stop_mode=clone:stop_duration={BANNER_DURATION},"
        f"trim=duration={BANNER_DURATION},setpts=PTS-STARTPTS[frozen];"
        f"[1:v]scale={banner_w}:{banner_h}:force_original_aspect_ratio=decrease,"
        f"pad={banner_w}:{banner_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"chromakey=color=00FF00:similarity=0.30:blend=0.05[banner_k];"
        f"[frozen][banner_k]overlay=x={banner_x}:y={banner_y}:shortest=0:repeatlast=0[ad_v];"
        f"[part1v][ad_v][part2v]concat=n=3:v=1:a=0[outv];"
        f"[0:a]atrim=start=0:end={insert},asetpts=PTS-STARTPTS[part1a];"
        f"[0:a]atrim=start={ad_end},asetpts=PTS-STARTPTS[part2a];"
        f"[1:a]atrim=duration={BANNER_DURATION},asetpts=PTS-STARTPTS,"
        f"volume=1.0,apad=pad_dur={BANNER_DURATION},atrim=duration={BANNER_DURATION}[bannera];"
        f"[part1a][bannera][part2a]concat=n=3:v=0:a=1[outa]"
    )

    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-threads", "1",
        "-filter_threads", "1",
        "-filter_complex_threads", "1",
        "-i", input_path,
        "-stream_loop", "-1", "-i", BANNER,
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-map", "[outa]",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "30",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-shortest",
        output_path,
    ]

    returncode, stderr = run_cmd(cmd, timeout=600)
    if returncode is None:
        return False, stderr
    if returncode != 0:
        return False, f"FFmpeg returncode={returncode}\n{stderr}"

    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        return False, "FFmpeg завершился, но выходной файл пустой/отсутствует"

    # Post-render sanity check: video/audio must exist and durations should be close.
    check = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_entries", "stream=codec_type,duration", output_path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    try:
        streams = json.loads(check.stdout).get("streams", [])
        durations = {
            s.get("codec_type"): float(s.get("duration") or 0)
            for s in streams
            if s.get("codec_type") in ("video", "audio")
        }
        if "video" not in durations or "audio" not in durations:
            return False, "После рендера отсутствует video или audio"
        if abs(durations["video"] - durations["audio"]) > 0.5:
            return False, (
                f"Длительности расходятся: video={durations['video']:.2f}s, "
                f"audio={durations['audio']:.2f}s"
            )
    except Exception as e:
        return False, f"Не удалось проверить результат: {e}"

    return True, "ok"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Нет доступа")
        return
    await update.message.reply_text(
        "👋 Привет!\n\n"
        "🎬 Скидывай видео — вставлю баннер CSDOG\n\n"
        "📌 Правила:\n"
        "• До 1 мин → баннер строго в середине\n"
        "• Больше 1 мин → первый баннер на 0:20\n"
        "• Баннер по центру TikTok 9:16\n"
        "• Озвучка баннера сохраняется\n\n"
        "📦 Макс: 50MB"
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /id intentionally has NO ALLOWED_USERS restriction so the owner can
    # retrieve an ID even before adding it to ALLOWED_USERS.
    uid = update.message.from_user.id
    await update.message.reply_text(f"Твой Telegram ID: {uid}")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                file = await context.bot.get_file(video.file_id)
                await file.download_to_drive(input_path)
            except Exception as e:
                await status.edit_text(f"❌ Ошибка скачивания: {e}")
                return

            await status.edit_text("🎬 Обрабатываю... (очередь: 1 видео за раз)")

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
                        caption="✅ Готово! Баннер CSDOG вставлен по центру",
                        supports_streaming=True,
                    )
                await status.delete()
            except Exception as e:
                await status.edit_text(f"❌ Ошибка отправки: {e}")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.Document.VIDEO, handle_video
    ))
    print("✅ Bot started | Railway 512MB mode | FFmpeg concurrency=1")
    app.run_polling()


if __name__ == "__main__":
    main()
