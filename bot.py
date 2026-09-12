import os
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

BANNER_DURATION = 4.4        # точная длительность баннера
MAX_VIDEO_DURATION = 120     # 2 минуты
TARGET_W = 576
TARGET_H = 1024

# Размер баннера на экране: на всю ширину
BANNER_W = TARGET_W
BANNER_H = int(TARGET_W * 750 / 1350)  # 576 * (750/1350) = 320
BANNER_X = 0
BANNER_Y = (TARGET_H - BANNER_H) // 2  # по центру по вертикали


def run_ffmpeg(cmd, timeout=600):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
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
    except Exception:
        return None

    info = {"duration": 0, "width": 1280, "height": 720, "has_audio": False}
    for s in data.get("streams", []):
        if s["codec_type"] == "video":
            dur = float(s.get("duration") or 0)
            if dur == 0:
                dur = float(data.get("format", {}).get("duration", 0))
            info["duration"] = dur
            info["width"] = int(s.get("width", 1280))
            info["height"] = int(s.get("height", 720))
        elif s["codec_type"] == "audio":
            info["has_audio"] = True

    if info["duration"] == 0:
        info["duration"] = float(data.get("format", {}).get("duration", 0))

    return info if info["duration"] > 0 else None


def get_insert_point(duration):
    """
    Одна точка вставки — ровно середина видео.
    Для видео > 60 сек можно добавить несколько, но пока одна.
    """
    if duration <= 60:
        return [round(duration / 2, 3)]
    else:
        points = []
        t = 20.0
        while t < duration - 5:
            points.append(round(t, 3))
            t += 60.0
        return points


def prepare_input(input_path, tmp, info):
    """
    Приводим входное видео к формату 576x1024 30fps AAC.
    Горизонтальное → размытый фон + оригинал по центру.
    Вертикальное   → scale+crop.
    Выходной файл всегда имеет аудиодорожку (тишина если нет звука).
    """
    W, H = info["width"], info["height"]
    prepared = os.path.join(tmp, "prepared.mp4")
    is_vertical = H >= W

    if is_vertical:
        vf = (
            f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{TARGET_H}"
        )
        video_filter_arg = ["-vf", vf]
    else:
        orig_h = int(TARGET_W * H / W)
        if orig_h % 2 != 0:
            orig_h -= 1
        orig_y = (TARGET_H - orig_h) // 2
        filter_complex = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{TARGET_H},gblur=sigma=30[blurred];"
            f"[fg]scale={TARGET_W}:{orig_h}[orig];"
            f"[blurred][orig]overlay=x=0:y={orig_y}[outv]"
        )
        video_filter_arg = ["-filter_complex", filter_complex, "-map", "[outv]"]

    # Базовые аргументы
    cmd = ["ffmpeg", "-y", "-i", input_path]
    cmd += video_filter_arg

    # Аудио: если есть — перекодируем, нет — генерируем тишину
    if info["has_audio"] and is_vertical:
        cmd += ["-map", "0:a"]
    elif info["has_audio"] and not is_vertical:
        cmd += ["-map", "0:a"]
    else:
        # Генерируем тишину нужной длины
        cmd = ["ffmpeg", "-y", "-i", input_path,
               "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        cmd += video_filter_arg if is_vertical else ["-filter_complex",
            (f"[0:v]split=2[bg][fg];"
             f"[bg]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
             f"crop={TARGET_W}:{TARGET_H},gblur=sigma=30[blurred];"
             f"[fg]scale={TARGET_W}:{int(TARGET_W * H / W) - (int(TARGET_W * H / W) % 2)}[orig];"
             f"[blurred][orig]overlay=x=0:y={(TARGET_H - (int(TARGET_W * H / W) - (int(TARGET_W * H / W) % 2))) // 2}[outv]"),
            "-map", "[outv]"]
        cmd += ["-map", "1:a", "-shortest"]

    cmd += [
        "-r", "30",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        prepared
    ]

    ok, err = run_ffmpeg(cmd)
    if not ok:
        return None, err
    return prepared, None


def extract_segment(source, start, end, out_path):
    """
    Вырезаем сегмент.
    -ss ПЕРЕД -i = быстрый seek к ближайшему keyframe.
    Перекодирование (-c:v libx264) всё равно происходит, поэтому
    итоговая точность достаточная, а скорость — максимальная.
    """
    duration = round(end - start, 6)
    if duration <= 0:
        return False, f"Нулевой сегмент: start={start} end={end}"

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),   # быстрый seek ДО -i
        "-i", source,
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        out_path
    ]
    return run_ffmpeg(cmd)


def make_banner_segment(source, freeze_at, out_path):
    """
    Создаём сегмент с баннером:
    1. Замораживаем кадр на freeze_at (стоп-кадр)
    2. Накладываем баннер с хрома-кеем поверх
    3. Аудио = только звук баннера (оригинальное аудио заглушается)
    
    Выход: ровно BANNER_DURATION секунд.
    """
    bw = BANNER_W
    bh = BANNER_H
    bx = BANNER_X
    by = BANNER_Y

    # Берём один кадр на freeze_at и растягиваем его на BANNER_DURATION
    # Баннер накладываем поверх с хрома-кеем
    filter_complex = (
        # Стоп-кадр: берём один кадр, дублируем на всю длину баннера
        f"[0:v]trim=start={freeze_at}:duration=0.1,"
        f"setpts=PTS-STARTPTS,"
        f"loop=loop=-1:size=1:start=0,"
        f"trim=duration={BANNER_DURATION},"
        f"setpts=PTS-STARTPTS[frozen];"
        # Баннер: масштабируем и убираем хрома-кей
        f"[1:v]scale={bw}:{bh}[banner_scaled];"
        f"[banner_scaled]chromakey=color=0x00FF00:similarity=0.25:blend=0.0[banner_key];"
        # Накладываем баннер на стоп-кадр
        f"[frozen][banner_key]overlay=x={bx}:y={by}[outv]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", source,           # 0: подготовленное видео (для стоп-кадра)
        "-i", BANNER,           # 1: баннер
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-map", "1:a",          # аудио ТОЛЬКО от баннера
        "-t", str(BANNER_DURATION),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        out_path
    ]
    return run_ffmpeg(cmd)


def concat_segments(segments, out_path):
    """
    Склейка через concat demuxer.
    Все сегменты уже в одинаковом формате (libx264, 30fps, aac 48k).
    """
    list_file = out_path + "_list.txt"
    with open(list_file, "w") as f:
        for seg in segments:
            f.write(f"file '{seg}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_file,
        # Копируем потоки — они уже одинаковые, перекодировать не нужно
        "-c", "copy",
        "-movflags", "+faststart",
        out_path
    ]
    ok, err = run_ffmpeg(cmd)
    try:
        os.remove(list_file)
    except Exception:
        pass
    return ok, err


def process_video(input_path, output_path):
    with tempfile.TemporaryDirectory() as tmp:
        # 1. Получаем инфо об оригинале
        info = get_video_info(input_path)
        if not info:
            return False, "Не удалось прочитать видео"

        duration = info["duration"]
        if duration < 3:
            return False, "Видео слишком короткое (меньше 3 секунд)"
        if duration > MAX_VIDEO_DURATION:
            return False, "Видео слишком длинное. Максимум 2 минуты"

        # 2. Приводим к 576x1024 30fps
        prepared, err = prepare_input(input_path, tmp, info)
        if not prepared:
            return False, f"Ошибка подготовки: {err[-300:]}"

        # 3. Получаем реальную длительность подготовленного видео
        prepared_info = get_video_info(prepared)
        if not prepared_info:
            return False, "Не удалось прочитать подготовленное видео"
        prepared_duration = prepared_info["duration"]

        # 4. Точки вставки баннера (по prepared_duration)
        points = get_insert_point(prepared_duration)

        # 5. Нарезаем сегменты
        segments = []
        prev = 0.0

        for i, pt in enumerate(points):
            # Сегмент ДО баннера
            seg_path = os.path.join(tmp, f"seg_{i}.mp4")
            ok, err = extract_segment(prepared, prev, pt, seg_path)
            if not ok:
                return False, f"Ошибка вырезки сегмента {i}: {err[-300:]}"
            segments.append(seg_path)

            # Баннерный сегмент (стоп-кадр + баннер поверх)
            ban_path = os.path.join(tmp, f"ban_{i}.mp4")
            ok, err = make_banner_segment(prepared, pt, ban_path)
            if not ok:
                return False, f"Ошибка баннера {i}: {err[-300:]}"
            segments.append(ban_path)

            prev = pt

        # Финальный сегмент после последнего баннера
        last_path = os.path.join(tmp, "seg_last.mp4")
        ok, err = extract_segment(prepared, prev, prepared_duration, last_path)
        if not ok:
            return False, f"Ошибка финального сегмента: {err[-300:]}"
        segments.append(last_path)

        # 6. Склеиваем всё
        ok, err = concat_segments(segments, output_path)
        if not ok:
            return False, f"Ошибка склейки: {err[-300:]}"

        # 7. Проверка результата
        result_info = get_video_info(output_path)
        if not result_info:
            return False, "Результирующий файл повреждён"

        expected = round(prepared_duration + BANNER_DURATION * len(points), 1)
        got = result_info["duration"]
        if abs(got - expected) > 2.0:
            return False, (
                f"Некорректная длительность: ожидалось ~{expected:.1f}с, "
                f"получилось {got:.1f}с"
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
        "• Баннер CSDOG на всю ширину, по центру\n"
        "• До 1 мин → баннер посередине\n"
        "• Длиннее → каждые 60 сек с 0:20\n"
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

            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
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
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
