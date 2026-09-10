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

# Только 1 видео одновременно — 512MB RAM
SEMAPHORE = asyncio.Semaphore(1)

BANNER_DURATION = 4.4
MAX_VIDEO_DURATION = 120  # 2 минуты


def run_ffmpeg(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return result.returncode == 0, result.stderr


def get_video_info(path):
    result = subprocess.run([
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        path
    ], capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
    except:
        return None

    info = {"duration": 0, "width": 1280, "height": 720, "has_audio": False}

    for s in data.get("streams", []):
        if s["codec_type"] == "video":
            info["duration"] = float(s.get("duration", 0) or
                                     data.get("format", {}).get("duration", 0))
            info["width"] = int(s.get("width", 1280))
            info["height"] = int(s.get("height", 720))
        elif s["codec_type"] == "audio":
            info["has_audio"] = True

    if info["duration"] == 0:
        info["duration"] = float(data.get("format", {}).get("duration", 0))

    return info if info["duration"] > 0 else None


def get_insert_points(duration):
    """Точки вставки баннера по правилам CSDOG"""
    if duration <= 60:
        return [round(duration / 2, 3)]
    else:
        points = []
        t = 20.0
        while t < duration - 5:
            points.append(round(t, 3))
            t += 60.0
        return points


def check_duration(path):
    """Проверяем длительность результата"""
    info = get_video_info(path)
    if not info:
        return 0
    return info["duration"]


def prepare_input(input_path, tmp, info):
    """
    Приводим входное видео к формату 576x1024:
    - вертикальное → scale+crop
    - горизонтальное → размытый фон + оригинал по центру
    """
    W, H = info["width"], info["height"]
    prepared = os.path.join(tmp, "prepared.mp4")
    is_vertical = H >= W

    if is_vertical:
        vf = "scale=576:1024:force_original_aspect_ratio=increase,crop=576:1024"
    else:
        # Горизонтальное: размытый фон + оригинал по центру
        # Вписываем оригинал в ширину 576
        orig_h = int(576 * H / W)
        orig_y = (1024 - orig_h) // 2
        vf = (
            f"split=2[bg][fg];"
            f"[bg]scale=576:1024:force_original_aspect_ratio=increase,"
            f"crop=576:1024,gblur=sigma=30[blurred];"
            f"[fg]scale=576:{orig_h}[orig];"
            f"[blurred][orig]overlay=x=0:y={orig_y}"
        )

    if is_vertical:
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vf", vf,
            "-r", "30",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            prepared
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-filter_complex", vf,
            "-r", "30",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            prepared
        ]

    ok, err = run_ffmpeg(cmd)
    if not ok:
        return None, err
    return prepared, None


def extract_segment(source, start, end, out_path, has_audio=True):
    """Вырезаем сегмент видео"""
    duration = round(end - start, 3)
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", source,
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-r", "30",
    ]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", out_path]
    return run_ffmpeg(cmd)


def make_freeze_with_banner(source, freeze_at, out_path, W=576, H=1024):
    """
    Делаем стоп-кадр + баннер поверх:
    - замораживаем кадр на freeze_at
    - накладываем banner с хрома-кеем
    """
    bw = 576
    bh = int(576 / (1350 / 750))  # ~320px
    bx = (W - bw) // 2
    by = (H - bh) // 2

    filter_complex = (
        f"[0:v]trim={freeze_at}:{freeze_at+0.1},setpts=PTS-STARTPTS,"
        f"select='eq(n\\,0)',"
        f"tpad=stop_mode=clone:stop_duration={BANNER_DURATION}[frozen];"
        f"[1:v]scale={bw}:{bh},"
        f"chromakey=color=00FF00:similarity=0.30:blend=0.05[banner_k];"
        f"[frozen][banner_k]overlay=x={bx}:y={by}[outv]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", source,
        "-i", BANNER,
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-map", "1:a",
        "-t", str(BANNER_DURATION),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-r", "30",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out_path
    ]
    return run_ffmpeg(cmd)


def concat_segments(segments, out_path):
    """Склеиваем сегменты через concat demuxer"""
    list_file = out_path + "_list.txt"
    with open(list_file, "w") as f:
        for seg in segments:
            f.write(f"file '{seg}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_file,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-r", "30",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out_path
    ]
    ok, err = run_ffmpeg(cmd)
    try:
        os.remove(list_file)
    except:
        pass
    return ok, err


def process_video(input_path, output_path):
    with tempfile.TemporaryDirectory() as tmp:
        # 1. Получаем инфо
        info = get_video_info(input_path)
        if not info:
            return False, "Не удалось прочитать видео"

        duration = info["duration"]

        if duration < 3:
            return False, "Видео слишком короткое (меньше 3 секунд)"

        if duration > MAX_VIDEO_DURATION:
            return False, f"Видео слишком длинное. Максимум 2 минуты"

        # 2. Приводим к 576x1024
        prepared, err = prepare_input(input_path, tmp, info)
        if not prepared:
            return False, f"Ошибка подготовки: {err[-200:]}"

        # 3. Точки вставки
        points = get_insert_points(duration)
        banner_count = len(points)

        # 4. Нарезаем сегменты и вставляем баннеры
        segments = []
        prev = 0.0

        for i, pt in enumerate(points):
            # Сегмент до баннера
            seg_path = os.path.join(tmp, f"seg_{i}.mp4")
            ok, err = extract_segment(prepared, prev, pt, seg_path)
            if not ok:
                return False, f"Ошибка сегмента {i}: {err[-200:]}"
            segments.append(seg_path)

            # Баннер (стоп-кадр + баннер)
            ban_path = os.path.join(tmp, f"ban_{i}.mp4")
            ok, err = make_freeze_with_banner(prepared, pt, ban_path)
            if not ok:
                return False, f"Ошибка баннера {i}: {err[-200:]}"
            segments.append(ban_path)

            prev = pt

        # Последний сегмент после всех баннеров
        last_path = os.path.join(tmp, f"seg_last.mp4")
        ok, err = extract_segment(prepared, prev, duration, last_path)
        if not ok:
            return False, f"Ошибка последнего сегмента: {err[-200:]}"
        segments.append(last_path)

        # 5. Склеиваем всё
        ok, err = concat_segments(segments, output_path)
        if not ok:
            return False, f"Ошибка склейки: {err[-200:]}"

        # 6. Проверяем длительность
        result_duration = check_duration(output_path)
        expected = round(duration + BANNER_DURATION * banner_count, 1)
        if abs(result_duration - expected) > 2.0:
            return False, (
                f"Ошибка длительности: ожидалось ~{expected}с, "
                f"получилось {result_duration:.1f}с"
            )

        return True, "ok"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Нет доступа")
        return
    await update.message.reply_text(
        "👋 Скидывай видео!\n\n"
        "📌 Что делаю:\n"
        "• 9:16 → вертикальный кадр\n"
        "• 16:9 → целиком на размытом фоне\n"
        "• Баннер CSDOG целиком, максимально широкий\n"
        "• До 1 мин → баннер по центру\n"
        "• Длиннее → 0:20, 1:20, 2:20...\n"
        "• Аудио баннера сохраняется\n\n"
        "📦 Макс: 50MB, 2 минуты"
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    await update.message.reply_text(
        f"Твой Telegram ID: `{uid}`", parse_mode="Markdown"
    )


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
        with tempfile.TemporaryDirectory() as tmp:
            input_path = os.path.join(tmp, "input.mp4")
            output_path = os.path.join(tmp, "output.mp4")

            try:
                file = await context.bot.get_file(video.file_id)
                await file.download_to_drive(input_path)
            except Exception as e:
                await status.edit_text(f"❌ Ошибка скачивания: {e}")
                return

            await status.edit_text("🎬 Обрабатываю видео...")

            loop = asyncio.get_event_loop()
            try:
                success, err = await loop.run_in_executor(
                    None, process_video, input_path, output_path
                )
            except Exception as e:
                await status.edit_text(f"❌ Неожиданная ошибка: {e}")
                return

            if not success:
                await status.edit_text(f"❌ Ошибка: {err}")
                return

            if not os.path.exists(output_path) or \
               os.path.getsize(output_path) == 0:
                await status.edit_text("❌ Файл не создался")
                return

            await status.edit_text("📤 Отправляю...")

            try:
                with open(output_path, "rb") as f:
                    await msg.reply_video(
                        video=f,
                        caption="✅ Готово! Баннер CSDOG вставлен",
                        supports_streaming=True
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
    print("✅ Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
