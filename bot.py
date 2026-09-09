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
    points = insert_points(duration, banner_duration)
    if not points:
        return False, "Для этого видео не помещается ни один баннер"

    # IMPORTANT: every video branch gets its own normalization chain.
    # Reusing one filter output in several trim filters can make FFmpeg connect
    # one concat input to the ORIGINAL dimensions (e.g. 720x1280), which was the
    # cause of returncode=234. We therefore split the raw input explicitly and
    # normalize every branch independently before concat.
    aspect = info["width"] / max(info["height"], 1)
    branch_count = 2 * len(points) + 1
    split_labels = [f"s{i}" for i in range(branch_count)]

    if aspect < 1.0:
        source_prefix = (
            f"[0:v]split={branch_count}" + "".join(f"[{x}]" for x in split_labels) + ";"
        )
        def norm(label, out):
            return (
                f"[{label}]scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=increase,"
                f"crop={OUTPUT_W}:{OUTPUT_H},setsar=1,fps=30,format=yuv420p[{out}]"
            )
    else:
        source_prefix = (
            f"[0:v]split={branch_count}" + "".join(f"[{x}]" for x in split_labels) + ";"
        )
        def norm(label, out):
            return (
                f"[{label}]split=2[fg_{out}][bg_{out}];"
                f"[bg_{out}]scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=increase,"
                f"crop={OUTPUT_W}:{OUTPUT_H},gblur=sigma=28,setsar=1,fps=30,format=yuv420p[bgv_{out}];"
                f"[fg_{out}]scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=decrease,"
                f"setsar=1,fps=30,format=yuv420p[fgv_{out}];"
                f"[bgv_{out}][fgv_{out}]overlay=(W-w)/2:(H-h)/2:shortest=1,"
                f"setsar=1,format=yuv420p[{out}]"
            )

    v_parts = []
    a_parts = []
    ordered_v = []
    ordered_a = []
    cursor = 0.0
    branch_i = 0

    for i, point in enumerate(points):
        if point > cursor:
            src_label = split_labels[branch_i]
            branch_i += 1
            vlabel = f"pre{i}"
            v_parts.append(norm(src_label, f"rawpre{i}") + ";" +
                           f"[rawpre{i}]trim=start={cursor}:end={point},setpts=PTS-STARTPTS,{'' if aspect < 1.0 else ''}format=yuv420p[{vlabel}]")
            a_parts.append(
                f"[0:a]atrim=start={cursor}:end={point},asetpts=PTS-STARTPTS,"
                f"aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a{i}]"
            )
            ordered_v.append(f"[{vlabel}]")
            ordered_a.append(f"[a{i}]")

        src_label = split_labels[branch_i]
        branch_i += 1
        rawfreeze = f"rawfreeze{i}"
        freeze = f"freeze{i}"
        v_parts.append(
            norm(src_label, rawfreeze) + ";" +
            f"[{rawfreeze}]trim=start={point}:end={point + 1/30},"
            f"setpts=PTS-STARTPTS,select='eq(n,0)',"
            f"tpad=stop_mode=clone:stop_duration={banner_duration},"
            f"trim=duration={banner_duration},setpts=PTS-STARTPTS[{freeze}]"
        )

        ban = f"ban{i}"
        ad = f"ad{i}"
        v_parts.append(
            f"[1:v]trim=duration={banner_duration},setpts=PTS-STARTPTS,"
            f"scale={OUTPUT_W}:-2:force_original_aspect_ratio=decrease,"
            f"setsar=1,format=yuv420p[{ban}]"
        )
        v_parts.append(
            f"[{freeze}][{ban}]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1,"
            f"setsar=1,format=yuv420p[{ad}]"
        )
        a_parts.append(
            f"[1:a]atrim=duration={banner_duration},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"volume=1.0[ba{i}]"
        )
        ordered_v.append(f"[{ad}]")
        ordered_a.append(f"[ba{i}]")

        cursor = point + banner_duration

    if cursor < duration:
        src_label = split_labels[branch_i]
        tail_raw = "tailraw"
        tail = "tailv"
        v_parts.append(norm(src_label, tail_raw) + ";" +
                       f"[{tail_raw}]trim=start={cursor},setpts=PTS-STARTPTS[{tail}]")
        a_parts.append(
            f"[0:a]atrim=start={cursor},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[taila]"
        )
        ordered_v.append(f"[{tail}]")
        ordered_a.append("[taila]")

    if branch_i != branch_count:
        return False, f"Внутренняя ошибка ветвления видео: {branch_i}/{branch_count}"

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

    print("✅ CSDOG bot v11 | Railway 512MB | FFmpeg concurrency=1")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
