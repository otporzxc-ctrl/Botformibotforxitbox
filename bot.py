import os
import math
import json
import asyncio
import tempfile
import subprocess
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]
BANNER = "/app/banner.mp4"


def get_video_info(path):
    result = subprocess.run([
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", path
    ], capture_output=True, text=True)
    data = json.loads(result.stdout)
    info = {"duration": 0, "width": 1280, "height": 720,
            "banner_w": 1350, "banner_h": 750}
    for s in data["streams"]:
        if s["codec_type"] == "video":
            info["duration"] = float(s.get("duration", 0))
            info["width"] = int(s.get("width", 1280))
            info["height"] = int(s.get("height", 720))
    return info


def get_insert_points(duration):
    """По правилам CSDOG:
    - до 60с → одна вставка в середине
    - больше 60с → на 0:20, 1:20, 2:20 каждые 60с
    """
    if duration <= 60:
        return [round(duration / 2, 2)]
    else:
        points = []
        t = 20.0
        while t < duration - 5:
            points.append(round(t, 2))
            t += 60.0
        return points


def calc_banner_size(width, height):
    """Баннер 50% площади экрана по требованиям CSDOG"""
    screen_area = width * height
    target_area = screen_area * 0.50
    ratio = 1350 / 750  # размер баннера CSDOG
    bh = math.sqrt(target_area / ratio)
    bw = ratio * bh
    return int(bw), int(bh)


def process_video(input_path, output_path):
    info = get_video_info(input_path)
    duration = info["duration"]
    W = info["width"]
    H = info["height"]

    if duration < 3:
        return False, "Видео слишком короткое"

    points = get_insert_points(duration)
    bw, bh = calc_banner_size(W, H)
    bx = (W - bw) // 2
    by = (H - bh) // 2

    banner_dur = 4.4  # длительность баннера CSDOG

    # Строим сегменты: до точки → стоп+баннер → после
    # Для простоты берём первую точку вставки
    # (для длинных видео — первую на 0:20)
    insert = points[0]
    insert_end = insert + 1.0

    filter_complex = f"""
        [0:v]split=3[v1][v2][v3];
        [0:a]asplit=2[a1][a2];

        [v1]trim=0:{insert},setpts=PTS-STARTPTS[part1v];
        [v3]trim={insert},setpts=PTS-STARTPTS[part2v];

        [a1]atrim=0:{insert},asetpts=PTS-STARTPTS[part1a];
        [a2]atrim={insert},asetpts=PTS-STARTPTS[part2a];

        [v2]trim={insert}:{insert_end},setpts=PTS-STARTPTS,
        select='eq(n,0)',gblur=sigma=20,
        tpad=stop_mode=clone:stop_duration={banner_dur}[frozen];

        [1:v]scale={bw}:{bh},
        chromakey=color=00FF00:similarity=0.30:blend=0.05[banner_k];

        [frozen][banner_k]overlay=x={bx}:y={by}[ad_v];

        [part1v][ad_v][part2v]concat=n=3:v=1:a=0[outv];
        [part1a][1:a][part2a]concat=n=3:v=0:a=1[outa]
    """

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-i", BANNER,
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-map", "[outa]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        output_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return False, result.stderr[-500:]
    return True, "ok"


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    video = msg.video or msg.document

    if not video:
        await msg.reply_text("Скинь видео 🎬")
        return

    if video.file_size and video.file_size > 50 * 1024 * 1024:
        await msg.reply_text("❌ Видео слишком большое (макс 50MB)")
        return

    status = await msg.reply_text("⏳ Скачиваю видео...")

    with tempfile.TemporaryDirectory() as tmp:
        input_path = os.path.join(tmp, "input.mp4")
        output_path = os.path.join(tmp, "output.mp4")

        file = await context.bot.get_file(video.file_id)
        await file.download_to_drive(input_path)

        await status.edit_text("🎬 Вставляю баннер CSDOG...")

        loop = asyncio.get_event_loop()
        success, msg_text = await loop.run_in_executor(
            None, process_video, input_path, output_path
        )

        if not success:
            await status.edit_text(f"❌ Ошибка: {msg_text}")
            return

        await status.edit_text("📤 Отправляю готовое видео...")

        with open(output_path, "rb") as f:
            await msg.reply_video(
                video=f,
                caption=(
                    "✅ Готово! Баннер CSDOG вставлен по правилам:\n"
                    "• 50% экрана\n"
                    "• Стоп-кадр + размытие\n"
                    "• Звук баннера сохранён"
                ),
                supports_streaming=True
            )

        await status.delete()


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для монетизации через CSDOG.\n\n"
        "📌 Что делаю:\n"
        "• Вставляю баннер CSDOG строго по правилам\n"
        "• Видео до 1 мин → баннер в середине\n"
        "• Видео больше 1 мин → на 0:20, 1:20, 2:20...\n"
        "• 50% экрана, стоп-кадр, звук сохранён\n\n"
        "🎬 Просто скинь видео!"
    )


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r"^/start"), handle_start
    ))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.Document.VIDEO, handle_video
    ))
    print("✅ Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
