import os
import math
import json
import asyncio
import tempfile
import subprocess
import shutil
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]
BANNER = "/app/banner.mp4"
ALLOWED_USERS = set(map(int, os.environ.get("ALLOWED_USERS", "").split(","))) if os.environ.get("ALLOWED_USERS") else set()

# Railway 512 MB: никогда не запускаем несколько тяжёлых ffmpeg одновременно.
SEMAPHORE = asyncio.Semaphore(1)
FFMPEG_TIMEOUT = int(os.environ.get("FFMPEG_TIMEOUT", "600"))
BANNER_DURATION = 4.4


def run_cmd(cmd, timeout=FFMPEG_TIMEOUT):
    """Run ffmpeg without capture_output buffering. Return code + tail of stderr."""
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        stderr = p.stderr or ""
        return p.returncode, stderr[-6000:]
    except subprocess.TimeoutExpired as e:
        stderr = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return -124, "FFmpeg timeout\n" + stderr[-6000:]


def get_video_info(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_entries", "format=duration:stream=index,codec_type,codec_name,width,height,duration",
            path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        data = json.loads(result.stdout)
    except Exception:
        return None

    duration = float(data.get("format", {}).get("duration", 0) or 0)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not video or duration <= 0:
        return None
    return {
        "duration": duration,
        "width": int(video.get("width") or 1280),
        "height": int(video.get("height") or 720),
        "has_audio": audio is not None,
    }


def get_insert_point(duration):
    return round(duration / 2, 2) if duration <= 60 else 20.0


def calc_banner_size(width, height):
    # Оставляем исходную логику проекта: 50% площади, соотношение 1350/750.
    screen_area = width * height
    target_area = screen_area * 0.50
    ratio = 1350 / 750
    bh = math.sqrt(target_area / ratio)
    bw = ratio * bh
    return int(bw), int(bh)


def probe_duration(path):
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        return float(p.stdout.strip())
    except Exception:
        return 0.0


def process_video(input_path, output_path):
    info = get_video_info(input_path)
    if not info:
        return False, "Не удалось прочитать видео"

    duration = info["duration"]
    W, H = info["width"], info["height"]
    if duration < 3:
        return False, "Видео слишком короткое"
    if not os.path.exists(BANNER):
        return False, f"Не найден баннер: {BANNER}"

    insert = get_insert_point(duration)
    if insert <= 0 or insert >= duration:
        return False, "Некорректная точка вставки"

    insert_end = min(insert + 1.0, duration)
    bw, bh = calc_banner_size(W, H)
    bx, by = max(0, (W - bw) // 2), max(0, (H - bh) // 2)

    # ВАЖНО: вместо split=3/asplit=2 используем отдельные входы одного файла.
    # Это позволяет FFmpeg декодировать только нужные диапазоны и не держать 3
    # ветки полного видеопотока в filter graph. На 512 MB это существенно стабильнее.
    if info["has_audio"]:
        audio_maps = "[a_pre][a_ad][a_post]concat=n=3:v=0:a=1[outa]"
        audio_filter = (
            f"[0:a]atrim=duration={insert},asetpts=PTS-STARTPTS[a_pre];"
            f"[1:a]atrim=duration={BANNER_DURATION},asetpts=PTS-STARTPTS[a_ad];"
            f"[2:a]atrim=start=0,asetpts=PTS-STARTPTS[a_post];"
            f"{audio_maps};"
        )
        audio_input = ["-i", input_path]
    else:
        audio_filter = ""
        audio_maps = ""
        audio_input = []

    # Один FFmpeg, но без split полного потока. Входы 0/1/2 — независимые
    # декодеры pre/post/freeze; вход 3 — зацикленный banner.
    if info["has_audio"]:
        cmd = [
            "ffmpeg", "-y",
            "-threads", "1", "-filter_threads", "1", "-filter_complex_threads", "1",
            "-ss", "0", "-t", f"{insert:.3f}", "-i", input_path,
            "-stream_loop", "-1", "-i", BANNER,
            "-ss", f"{insert:.3f}", "-i", input_path,
            "-ss", f"{insert:.3f}", "-frames:v", "1", "-i", input_path,
            "-filter_complex",
            (
                f"[0:v]setpts=PTS-STARTPTS[pre];"
                f"[2:v]setpts=PTS-STARTPTS[post];"
                f"[3:v]gblur=sigma=20,tpad=stop_mode=clone:stop_duration={BANNER_DURATION},"
                f"trim=duration={BANNER_DURATION},setpts=PTS-STARTPTS[frozen];"
                f"[1:v]scale={bw}:{bh}:flags=fast_bilinear,"
                f"chromakey=color=00FF00:similarity=0.30:blend=0.05[banner_k];"
                f"[frozen][banner_k]overlay=x={bx}:y={by}:shortest=1[ad];"
                f"[pre][ad][post]concat=n=3:v=1:a=0[outv];"
                f"[0:a]asetpts=PTS-STARTPTS,atrim=duration={insert}[a_pre];"
                f"[1:a]asetpts=PTS-STARTPTS,atrim=duration={BANNER_DURATION}[a_ad];"
                f"[2:a]asetpts=PTS-STARTPTS[a_post];"
                f"[a_pre][a_ad][a_post]concat=n=3:v=0:a=1[outa]"
            ),
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-threads", "1", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", output_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-threads", "1", "-filter_threads", "1", "-filter_complex_threads", "1",
            "-ss", "0", "-t", f"{insert:.3f}", "-i", input_path,
            "-stream_loop", "-1", "-i", BANNER,
            "-ss", f"{insert:.3f}", "-i", input_path,
            "-ss", f"{insert:.3f}", "-frames:v", "1", "-i", input_path,
            "-filter_complex",
            (
                f"[0:v]setpts=PTS-STARTPTS[pre];"
                f"[2:v]setpts=PTS-STARTPTS[post];"
                f"[3:v]gblur=sigma=20,tpad=stop_mode=clone:stop_duration={BANNER_DURATION},"
                f"trim=duration={BANNER_DURATION},setpts=PTS-STARTPTS[frozen];"
                f"[1:v]scale={bw}:{bh}:flags=fast_bilinear,chromakey=color=00FF00:similarity=0.30:blend=0.05[banner_k];"
                f"[frozen][banner_k]overlay=x={bx}:y={by}:shortest=1[ad];"
                f"[pre][ad][post]concat=n=3:v=1:a=0[outv]"
            ),
            "-map", "[outv]", "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-threads", "1", "-movflags", "+faststart", output_path,
        ]

    rc, stderr = run_cmd(cmd)
    if rc != 0:
        return False, f"returncode={rc}\n{stderr}"

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        return False, "FFmpeg завершился, но выходной файл пустой/не создан"

    out_duration = probe_duration(output_path)
    expected = duration + BANNER_DURATION
    if abs(out_duration - expected) > 0.8:
        return False, f"Проверка длительности не пройдена: input={duration:.2f}s output={out_duration:.2f}s expected≈{expected:.2f}s"

    return True, "ok"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Нет доступа")
        return
    await update.message.reply_text(
        "👋 Привет!\n\n🎬 Скидывай видео — вставлю баннер CSDOG\n\n"
        "📌 До 1 мин → баннер в середине\n"
        "📌 Больше 1 мин → на 0:20\n\n📦 Макс: 50MB"
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    await update.message.reply_text(f"Твой Telegram ID: `{uid}`", parse_mode="Markdown")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        await msg.reply_text("⛔ Нет доступа")
        return

    video = msg.video or msg.document
    if not video:
        return
    if video.file_size and video.file_size > 50 * 1024 * 1024:
        await msg.reply_text("❌ Максимум 50MB")
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

            await status.edit_text("🎬 Обрабатываю...\nПараллельная обработка отключена для экономии RAM.")
            loop = asyncio.get_running_loop()
            try:
                success, err = await loop.run_in_executor(None, process_video, input_path, output_path)
            except Exception as e:
                await status.edit_text(f"❌ Ошибка ffmpeg: {e}")
                return

            if not success:
                await status.edit_text(f"❌ Ошибка FFmpeg:\n{err[-3500:]}")
                return
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                await status.edit_text("❌ Файл не создался")
                return

            await status.edit_text("📤 Отправляю...")
            try:
                with open(output_path, "rb") as f:
                    await msg.reply_video(video=f, caption="✅ Готово! Баннер CSDOG вставлен", supports_streaming=True)
                await status.delete()
            except Exception as e:
                await status.edit_text(f"❌ Ошибка отправки: {e}")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    print("✅ Bot started | FFmpeg concurrency=1")
    app.run_polling()


if __name__ == "__main__":
    main()
