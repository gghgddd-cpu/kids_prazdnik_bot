import asyncio
import logging
import os
import re
import json
import sqlite3
from datetime import datetime
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, FSInputFile, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PROXYAPI_API_KEY = os.getenv("PROXYAPI_API_KEY", "").strip()
PROXYAPI_BASE_URL = os.getenv("PROXYAPI_BASE_URL", "https://api.proxyapi.ru").rstrip("/")
SPEAKING_MODEL = os.getenv("PROXYAPI_SPEAKING_MODEL", "heygen/heygen-video-1").strip()
CARTOON_MODEL = os.getenv("PROXYAPI_CARTOON_MODEL", "x-ai/grok-imagine-video-1.5-lite").strip()
TTS_MODEL = os.getenv("PROXYAPI_TTS_MODEL", "openai/gpt-4o-mini-tts").strip()
TTS_VOICE = os.getenv("PROXYAPI_TTS_VOICE", "alloy").strip()
VIDEO_DURATION = int(os.getenv("VIDEO_DURATION_SECONDS", "10"))
VIDEO_RESOLUTION = os.getenv("VIDEO_RESOLUTION", "480p").strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "8"))
MAX_WAIT_SECONDS = int(os.getenv("MAX_WAIT_SECONDS", "900"))
MAX_TELEGRAM_VIDEO_BYTES = 49 * 1024 * 1024
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
VIDEO_PRICE_RUB = int(os.getenv("VIDEO_PRICE_RUB", "149"))
DB_PATH = Path(os.getenv("BOT_DB_PATH", "kids_video_bot.sqlite3"))
DEMO_GENERATION_ENABLED = os.getenv("DEMO_GENERATION_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
MAINTENANCE_MODE = os.getenv("MAINTENANCE_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("kids-video-bot")

if not BOT_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в файле .env")
if not PROXYAPI_API_KEY:
    raise RuntimeError("Не задан PROXYAPI_API_KEY в файле .env")

OCCASIONS = {
    "birthday": ("🎂 День рождения",),
    "newyear": ("🎄 Новый год",),
    "success": ("🏆 Достижение",),
    "support": ("🌟 Поддержка и пожелание",),
}
CHARACTERS = {
    "spiderman": "🕷 Человек-паук",
    "batman": "🦇 Бэтмен",
    "minecraft_steve": "⛏ Стив из Minecraft",
    "minecraft_creeper": "💚 Крипер из Minecraft",
    "sonic": "💨 Соник",
    "pikachu": "⚡ Пикачу",
    "ironman": "🤖 Железный человек",
    "elsa": "❄️ Эльза",
    "dinosaur": "🦖 Весёлый динозавр",
    "space_robot": "🚀 Космический робот",
    "ded_moroz": "🎅 Дед Мороз",
    "snegurochka": "❄️ Снегурочка",
    "mermaid_princess": "🧜‍♀️ Русалочка",
    "cinderella": "👑 Золушка",
    "rapunzel": "🌸 Рапунцель",
    "unicorn_princess": "🦄 Принцесса-единорог",
    "matroskin": "🐱 Кот Матроскин",
    "carlson": "🚁 Карлсон",
    "cheburashka": "🐻 Чебурашка",
    "gena_crocodile": "🐊 Крокодил Гена",
    "neznaika": "🎩 Незнайка",
    "baba_yaga_kind": "🧙‍♀️ Добрая Баба-яга",
}

OCCASION_SCENES = {
    "birthday": (
        "A joyful birthday party background: colorful balloons floating behind the character, "
        "festive garlands, confetti drifting gently, a decorated birthday cake in the distant background, "
        "bright celebratory colors and warm soft lights."
    ),
    "newyear": (
        "A magical New Year winter scene: beautifully decorated fir trees with glowing lights and ornaments, "
        "soft snow falling slowly, sparkling snowflakes, warm golden bokeh lights and a cozy festive atmosphere."
    ),
    "success": (
        "A triumphant celebration scene: sparkling golden stars, colorful celebratory confetti, "
        "glowing achievement badges and a bright uplifting stage-like background, joyful but not cluttered."
    ),
    "support": (
        "A warm magical encouragement scene: glowing stars, soft sparkling particles, gentle rainbow-colored light, "
        "friendly clouds and a hopeful dreamy background that feels safe, comforting and uplifting."
    ),
}

class Order(StatesGroup):
    mode = State()
    name = State()
    character = State()
    occasion = State()
    custom_occasion = State()
    custom_character = State()
    custom_script = State()
    admin_broadcast = State()
    admin_price = State()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, first_seen TEXT, last_seen TEXT
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, created_at TEXT NOT NULL,
            status TEXT NOT NULL, payload TEXT NOT NULL, price_rub INTEGER NOT NULL DEFAULT 0
        )''')
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('demo_generation', ?)", ('1' if DEMO_GENERATION_ENABLED else '0',))
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('maintenance', ?)", ('1' if MAINTENANCE_MODE else '0',))
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('video_price_rub', ?)", (str(VIDEO_PRICE_RUB),))


def setting(key: str, default: str = '0') -> str:
    with db() as conn:
        row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return str(row['value']) if row else default


def set_setting(key: str, value: str):
    with db() as conn:
        conn.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))


def current_video_price() -> int:
    try:
        return max(0, int(setting('video_price_rub', str(VIDEO_PRICE_RUB))))
    except (TypeError, ValueError):
        return VIDEO_PRICE_RUB


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def main_menu_keyboard(user_id: int):
    rows = [
        [KeyboardButton(text='🎬 Создать поздравление')],
        [KeyboardButton(text='ℹ️ Как это работает'), KeyboardButton(text='❌ Отменить')],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(text='🛠 Панель администратора')])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def register_user(user):
    now = datetime.now().isoformat(timespec='seconds')
    with db() as conn:
        conn.execute('''INSERT INTO users(user_id,username,first_name,first_seen,last_seen) VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_seen=excluded.last_seen''',
            (user.id, user.username or '', user.first_name or '', now, now))


def create_order(user_id: int, payload: dict) -> int:
    with db() as conn:
        cur = conn.execute('INSERT INTO orders(user_id,created_at,status,payload,price_rub) VALUES(?,?,?,?,?)',
                           (user_id, datetime.now().isoformat(timespec='seconds'), 'awaiting_payment', json.dumps(payload, ensure_ascii=False), current_video_price()))
        return int(cur.lastrowid)


def get_order(order_id: int, user_id: int | None = None):
    with db() as conn:
        if user_id is None:
            row = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
        else:
            row = conn.execute('SELECT * FROM orders WHERE id=? AND user_id=?', (order_id, user_id)).fetchone()
        if not row:
            return None
        result = dict(row)
        result['payload'] = json.loads(result['payload'])
        return result


def update_order_status(order_id: int, status: str):
    with db() as conn:
        conn.execute('UPDATE orders SET status=? WHERE id=?', (status, order_id))


def admin_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text='📊 Статистика', callback_data='admin:stats')
    kb.button(text='👥 Пользователи', callback_data='admin:users')
    kb.button(text='🧾 Последние заказы', callback_data='admin:orders')
    kb.button(text='🧪 Тестовая генерация', callback_data='admin:demo')
    kb.button(text='🛠 Обслуживание', callback_data='admin:maintenance')
    kb.button(text='📣 Рассылка', callback_data='admin:broadcast')
    kb.button(text='💰 Изменить стоимость', callback_data='admin:price')
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


async def show_myid(message: Message):
    register_user(message.from_user)
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>\nДобавь это число в параметр ADMIN_IDS в файле .env.", parse_mode="HTML")


async def show_admin_menu(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer('⛔ У тебя нет доступа к панели администратора.')
        return
    demo = 'включена' if setting('demo_generation') == '1' else 'выключена'
    maintenance = 'включён' if setting('maintenance') == '1' else 'выключен'
    await message.answer(
        f'🛠 <b>Панель администратора</b>\n\nТекущая стоимость: <b>{current_video_price()} ₽</b> за видео\n'
        f'Тестовая генерация: <b>{demo}</b>\nРежим обслуживания: <b>{maintenance}</b>\n\nВыбери действие:',
        reply_markup=admin_keyboard(), parse_mode='HTML')


def characters_keyboard():
    kb = InlineKeyboardBuilder()
    for key, label in CHARACTERS.items():
        kb.button(text=label, callback_data=f"char:{key}")
    kb.adjust(2)
    return kb.as_markup()


def occasion_keyboard():
    kb = InlineKeyboardBuilder()
    for key, (label,) in OCCASIONS.items():
        kb.button(text=label, callback_data=f"occasion:{key}")
    kb.adjust(1)
    return kb.as_markup()


def mode_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎉 Готовый шаблон", callback_data="mode:standard")
    kb.button(text="✨ Своё поздравление", callback_data="mode:custom")
    kb.adjust(1)
    return kb.as_markup()


def clean_name(value: str) -> str:
    value = re.sub(r"[^A-Za-zА-Яа-яЁё -]", "", value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value[:35]


def build_script(name: str, occasion: str) -> str:
    scripts = {
        "birthday": f"Привет, {name}! С днём рождения! Желаю тебе ярких приключений, верных друзей и исполнения самых смелых мечтаний. Продолжай творить, улыбаться и верить в себя!",
        "newyear": f"Привет, {name}! С Новым годом! Пусть тебя ждут волшебные открытия, весёлые приключения и приятные сюрпризы. Желаю радости, отличного настроения и исполнения желаний!",
        "success": f"Привет, {name}! Поздравляю тебя с твоим достижением! Ты здорово постарался. Продолжай пробовать новое, не бойся сложных задач и верь в себя. Так держать!",
        "support": f"Привет, {name}! Это послание специально для тебя. У тебя есть свои таланты, идеи и суперспособности. Не сдавайся, если что-то не получилось с первого раза. У тебя обязательно получится!",
    }
    return scripts[occasion]


def auth_headers(json_content: bool = False):
    headers = {"Authorization": f"Bearer {PROXYAPI_API_KEY}"}
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


async def api_json(method: str, path_or_url: str, payload=None, timeout_seconds=90):
    url = path_or_url if path_or_url.startswith("https://") else f"{PROXYAPI_BASE_URL}{path_or_url}"
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, url, headers=auth_headers(payload is not None), json=payload) as resp:
            text_body = await resp.text()
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = {"raw": text_body}
            if resp.status >= 400:
                # Never include request headers or API keys in the error text.
                detail = body.get("error") or body.get("message") or body.get("detail") or text_body
                if isinstance(detail, dict):
                    detail = detail.get("message") or str(detail)
                raise RuntimeError(f"ProxyAPI HTTP {resp.status}: {str(detail)[:500]}")
            return body


async def create_video(model: str, prompt: str, generate_audio: bool) -> str:
    # Video models differ in which optional parameters they accept.
    # Retry with simpler payloads when ProxyAPI rejects an unsupported option.
    candidates = [
        {"model": model, "prompt": prompt, "resolution": VIDEO_RESOLUTION,
         "duration": VIDEO_DURATION, "generate_audio": generate_audio, "aspect_ratio": "9:16"},
        {"model": model, "prompt": prompt, "resolution": VIDEO_RESOLUTION, "duration": VIDEO_DURATION},
        {"model": model, "prompt": prompt, "resolution": VIDEO_RESOLUTION},
        {"model": model, "prompt": prompt},
    ]
    last_error = None
    result = None
    for index, payload in enumerate(candidates):
        try:
            result = await api_json("POST", "/v1/videos", payload, timeout_seconds=120)
            if index:
                log.warning("Video model %s accepted fallback payload variant %s", model, index + 1)
            break
        except RuntimeError as exc:
            last_error = exc
            if "HTTP 400" not in str(exc) or "unsupported" not in str(exc).lower():
                raise
            log.warning("Video option set rejected by %s (variant %s); trying simpler options", model, index + 1)
    if result is None:
        raise RuntimeError(
            f"Не удалось подобрать поддерживаемые параметры для модели {model}. "
            f"Последняя ошибка ProxyAPI: {last_error}"
        )
    video_id = result.get("id") or result.get("video_id") or (result.get("data") or {}).get("id")
    if not video_id:
        raise RuntimeError(f"ProxyAPI не вернул id задачи. Ответ: {str(result)[:500]}")
    log.info("Video task created: id=%s model=%s", video_id, model)
    return str(video_id)


async def wait_for_video(video_id: str, progress_message=None) -> tuple[str, str | None]:
    waited = 0
    while waited <= MAX_WAIT_SECONDS:
        result = await api_json("GET", f"/v1/videos/{video_id}", timeout_seconds=60)
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        status = str(data.get("status", "")).lower()
        if status in {"completed", "complete", "success", "succeeded"}:
            urls = data.get("unsigned_urls") or data.get("urls") or []
            if isinstance(urls, str):
                urls = [urls]
            url = data.get("video_url") or data.get("url") or (urls[0] if urls else None)
            # Some responses return a result URL only after the task is complete.
            if not url:
                url = f"{PROXYAPI_BASE_URL}/v1/videos/{video_id}/content?index=0"
            return str(url), str(data.get("polling_url") or result.get("polling_url") or "") or None
        if status in {"failed", "error", "failure", "cancelled"}:
            err = data.get("error") or data.get("message") or data
            raise RuntimeError(f"Генерация видео завершилась ошибкой: {str(err)[:500]}")
        await asyncio.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
        if progress_message is not None and waited % 16 == 0:
            try:
                dots = "·" * ((waited // 16) % 4 + 1)
                await progress_message.edit_text(
                    f"✨ <b>Персонаж оживает{dots}</b>\n\n"
                    f"Модель создаёт видео и речь. Прошло примерно {waited} сек. Это нормально — можно просто подождать.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
    raise TimeoutError(f"Видео не готово за {MAX_WAIT_SECONDS} секунд. Задача могла продолжить работу на стороне ProxyAPI.")


async def download_url(url: str, destination: Path):
    timeout = aiohttp.ClientTimeout(total=240)
    headers = auth_headers()
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status >= 400:
                body = (await resp.text())[:300]
                raise RuntimeError(f"Не удалось скачать результат: HTTP {resp.status}. {body}")
            with destination.open("wb") as f:
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    f.write(chunk)


async def create_speech(script: str, destination: Path):
    payload = {"model": TTS_MODEL, "input": script, "voice": TTS_VOICE, "response_format": "mp3"}
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{PROXYAPI_BASE_URL}/v1/audio/speech", headers=auth_headers(True), json=payload) as resp:
            if resp.status >= 400:
                body = (await resp.text())[:500]
                raise RuntimeError(f"ProxyAPI TTS HTTP {resp.status}: {body}")
            with destination.open("wb") as f:
                async for chunk in resp.content.iter_chunked(128 * 1024):
                    f.write(chunk)


def make_name_overlay(name: str, destination: Path):
    image = Image.new("RGBA", (720, 1280), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # A semi-transparent strip keeps the name legible across different video backgrounds.
    draw.rounded_rectangle((36, 1050, 684, 1188), radius=28, fill=(15, 23, 42, 190), outline=(255, 255, 255, 230), width=3)
    font = None
    for font_path in [
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]:
        try:
            font = ImageFont.truetype(font_path, 54)
            break
        except OSError:
            pass
    if font is None:
        font = ImageFont.load_default()
    text = name
    box = draw.textbbox((0, 0), text, font=font, stroke_width=1)
    while box[2] - box[0] > 590 and len(text) > 4:
        text = text[:-1]
        box = draw.textbbox((0, 0), text + "…", font=font, stroke_width=1)
    if text != name:
        text += "…"
        box = draw.textbbox((0, 0), text, font=font, stroke_width=1)
    x = (720 - (box[2] - box[0])) // 2
    y = 1090 + (70 - (box[3] - box[1])) // 2
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255), stroke_width=1, stroke_fill=(0, 0, 0, 180))
    image.save(destination)


async def combine_video_audio(video_path: Path, audio_path: Path, overlay_path: Path, output_path: Path):
    # FFmpeg is required for this post-processing step.
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path), "-i", str(audio_path), "-loop", "1", "-i", str(overlay_path),
        "-filter_complex", "[0:v]scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1[v];[v][2:v]overlay=0:0:shortest=1[vout]",
        "-map", "[vout]", "-map", "1:a:0", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p", "-shortest", "-movflags", "+faststart", str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-1200:]
        raise RuntimeError("FFmpeg не смог собрать видео. Убедись, что FFmpeg установлен и доступен в PATH. " + detail)


async def start(message: Message, state: FSMContext):
    register_user(message.from_user)
    if setting('maintenance') == '1' and not is_admin(message.from_user.id):
        await message.answer('🛠 Бот временно на техническом обслуживании. Попробуй немного позже.')
        return
    await state.clear()
    await message.answer(
        "🎬 <b>Создадим персональное видеопоздравление!</b>\n\n"
        "Выбери, как будем делать видео:\n\n"
        "🎉 <b>Готовый шаблон</b> — выбираешь героя и праздник, бот сам подготовит текст.\n"
        "✨ <b>Своё поздравление</b> — задаёшь свой праздник, описываешь персонажа и пишешь текст поздравления самостоятельно.\n\n"
        "Видео обычно готовится за 1–2 минуты.",
        reply_markup=mode_keyboard(), parse_mode="HTML"
    )
    await message.answer("\uFE0F", reply_markup=main_menu_keyboard(message.from_user.id))
    await state.set_state(Order.mode)


async def choose_mode(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not callback.data or not callback.data.startswith("mode:"):
        return
    mode = callback.data.split(":", 1)[1]
    if mode not in {"standard", "custom"}:
        return
    await state.update_data(mode=mode)
    await callback.message.answer("✍️ Для кого готовим поздравление? Напиши имя ребёнка.")
    await state.set_state(Order.name)


async def receive_name(message: Message, state: FSMContext):
    name = clean_name(message.text or "")
    if len(name) < 2:
        await message.answer("Напиши имя длиной хотя бы 2 символа, пожалуйста.")
        return
    data = await state.get_data()
    await state.update_data(child_name=name)
    if data.get("mode") == "custom":
        await message.answer(
            "🎈 <b>Какой праздник или повод отмечаем?</b>\n"
            "Например: первый гол в футболе, окончание детского сада, день рождения бабушки, победа на соревнованиях.\n\n"
            "Опиши своими словами, до 120 символов.", parse_mode="HTML"
        )
        await state.set_state(Order.custom_occasion)
    else:
        await message.answer("🦸 <b>Выбери персонажа</b>", reply_markup=characters_keyboard(), parse_mode="HTML")
        await state.set_state(Order.character)


async def choose_character(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not callback.data or not callback.data.startswith("char:"):
        return
    character = callback.data.split(":", 1)[1]
    if character not in CHARACTERS:
        await callback.message.answer("Не удалось распознать персонажа. Нажми /start и попробуй ещё раз.")
        return
    await state.update_data(character=character)
    await callback.message.answer("🎉 Теперь выбери повод для видео:", reply_markup=occasion_keyboard())
    await state.set_state(Order.occasion)


async def run_video_order(message: Message, bot: Bot, name: str, character_label: str,
                          occasion_label: str, scene: str, script: str, order_id: int | None = None):
    progress_message = await message.answer(
        f"🎬 <b>Готовим поздравление для {name}</b>\n"
        f"Персонаж: {character_label}\nПовод: {occasion_label}\n\n"
        "⏳ Шаг 1 из 3: запускаю генерацию…\n"
        "Это может занять 1–2 минуты. Я сообщу, когда видео будет готово.",
        parse_mode="HTML"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="kids_video_") as tmp:
            work = Path(tmp)
            raw_video = work / "raw_video.mp4"
            final_video = work / "greeting.mp4"
            prompt = (
                "Create a polished, family-friendly personalized greeting video for a child in LANDSCAPE widescreen 16:9 format. "
                f"The main character is {character_label}. Keep the character visually consistent throughout. "
                "Show the character facing the camera in a medium close-up, with face and mouth clearly visible, "
                "positioned around the center of the widescreen frame with shoulders visible. "
                "The character speaks directly to camera in natural Russian and says exactly this text, "
                f"without changing or adding words: {script!r}. "
                f"The background must clearly match the occasion and remain visible around the character: {scene} "
                "Make the background rich, detailed and lively, with gentle animated environmental motion, "
                "but keep it behind the character and do not obscure the face. "
                "Use high-quality cheerful cinematic 3D animation, expressive facial animation, gentle hand gestures, "
                "smooth movement and stable composition. Wide horizontal cinematic framing, 16:9, not vertical, not portrait. "
                "No captions, no subtitles, no text, no name labels, no logos, no black bars."
            )
            await progress_message.edit_text(
                f"🎬 <b>Готовим поздравление для {name}</b>\nПерсонаж: {character_label}\nПовод: {occasion_label}\n\n"
                "🧠 Шаг 1 из 3: создаю сцену и речь персонажа…", parse_mode="HTML"
            )
            video_id = await create_video(SPEAKING_MODEL, prompt, generate_audio=True)
            await progress_message.edit_text(
                f"🎬 <b>Готовим поздравление для {name}</b>\nПерсонаж: {character_label}\nПовод: {occasion_label}\n\n"
                "✨ Шаг 2 из 3: персонаж оживает, подготавливаю видео…", parse_mode="HTML"
            )
            video_url, _ = await wait_for_video(video_id, progress_message=progress_message)
            await download_url(video_url, raw_video)
            await progress_message.edit_text("🎞 Шаг 3 из 3: подготавливаю горизонтальное видео к отправке…", parse_mode="HTML")
            cmd = [
                "ffmpeg", "-y", "-i", str(raw_video),
                "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30",
                "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(final_video)
            ]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError("FFmpeg не смог обработать видео. Проверь установку FFmpeg. " + stderr.decode("utf-8", errors="replace")[-900:])
            size = final_video.stat().st_size
            if size > MAX_TELEGRAM_VIDEO_BYTES:
                await message.answer(f"Видео готово, но файл весит {size / 1024 / 1024:.1f} МБ и превышает лимит отправки. Попробуй уменьшить VIDEO_RESOLUTION или VIDEO_DURATION_SECONDS в .env.")
                return
            await bot.send_video(
                chat_id=message.chat.id,
                video=FSInputFile(final_video),
                caption=f"🎉 Твоё видеопоздравление готово!\nПерсонаж: {character_label}",
                supports_streaming=True,
            )
            if order_id is not None:
                update_order_status(order_id, 'completed')
            await progress_message.edit_text("✅ Видео готово и отправлено выше! Хочешь создать ещё одно? Нажми кнопку «🎬 Создать поздравление» внизу экрана.")
    except Exception as exc:
        log.exception("Video generation failed")
        if order_id is not None:
            update_order_status(order_id, 'failed')
        await message.answer(
            "Не получилось создать видео 😕\n\n"
            f"Ошибка: {str(exc)[:900]}\n\n"
            "Пришли текст ошибки без API-ключа — поправим настройки."
        )


async def choose_occasion(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    if not callback.data or not callback.data.startswith("occasion:"):
        return
    occasion = callback.data.split(":", 1)[1]
    if occasion not in OCCASIONS:
        await callback.message.answer("Не удалось распознать повод. Нажми /start и попробуй ещё раз.")
        return
    data = await state.get_data()
    name = data.get("child_name")
    character = data.get("character")
    if not name or character not in CHARACTERS:
        await callback.message.answer("Начни заново командой /start.")
        await state.clear()
        return
    payload = {
        'name': name, 'character_label': CHARACTERS[character], 'occasion_label': OCCASIONS[occasion][0],
        'scene': OCCASION_SCENES[occasion], 'script': build_script(name, occasion),
    }
    await state.clear()
    await present_payment_stub(callback.message, callback.from_user.id, payload)


async def receive_custom_occasion(message: Message, state: FSMContext):
    occasion = (message.text or "").strip()
    if len(occasion) < 3:
        await message.answer("Опиши повод чуть подробнее, пожалуйста (минимум 3 символа).")
        return
    if len(occasion) > 120:
        await message.answer("Слишком длинное описание. Сократи его до 120 символов, чтобы сцена получилась точнее.")
        return
    await state.update_data(custom_occasion=occasion)
    await message.answer(
        "🦸 <b>Кого должен изображать персонаж?</b>\n"
        "Опиши героя своими словами — например: добрый космический рыцарь в синем костюме, "
        "говорящий динозавр с зелёными шипами или волшебница с ледяными силами.\n\n"
        "До 100 символов.", parse_mode="HTML"
    )
    await state.set_state(Order.custom_character)


async def receive_custom_character(message: Message, state: FSMContext):
    character = (message.text or "").strip()
    if len(character) < 3:
        await message.answer("Опиши персонажа чуть подробнее, пожалуйста.")
        return
    if len(character) > 100:
        await message.answer("Сократи описание персонажа до 100 символов.")
        return
    await state.update_data(custom_character=character)
    await message.answer(
        "✍️ <b>Теперь напиши текст, который персонаж произнесёт.</b>\n\n"
        "Можно начать с обращения по имени. Пиши готовый текст целиком — бот не будет придумывать поздравление за тебя.\n"
        "Рекомендуем 2–5 предложений, до 500 символов.", parse_mode="HTML"
    )
    await state.set_state(Order.custom_script)


async def receive_custom_script(message: Message, state: FSMContext, bot: Bot):
    script = (message.text or "").strip()
    if len(script) < 10:
        await message.answer("Текст слишком короткий. Напиши поздравление хотя бы из нескольких слов.")
        return
    if len(script) > 500:
        await message.answer("Текст слишком длинный. Сократи поздравление до 500 символов, чтобы оно поместилось в короткое видео.")
        return
    data = await state.get_data()
    name = data.get("child_name")
    occasion = data.get("custom_occasion")
    character = data.get("custom_character")
    if not name or not occasion or not character:
        await state.clear()
        await message.answer("Не хватает данных для заказа. Начни заново командой /start.")
        return
    scene = (
        f"A festive animated background for this specific occasion: {occasion}. "
        "Show clear, recognizable visual details related to the described occasion, with warm cinematic lighting, "
        "gentle animated particles and decorations around the edges. The background must be visible and not cluttered."
    )
    payload = {'name': name, 'character_label': character, 'occasion_label': occasion, 'scene': scene, 'script': script}
    await state.clear()
    await present_payment_stub(message, message.from_user.id, payload)


def payment_keyboard(order_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text='💳 Оплата — скоро', callback_data=f'pay:soon:{order_id}')
    if setting('demo_generation') == '1':
        kb.button(text='🧪 Тестовая генерация', callback_data=f'pay:demo:{order_id}')
    kb.button(text='❌ Отменить заказ', callback_data=f'pay:cancel:{order_id}')
    kb.adjust(1)
    return kb.as_markup()


async def present_payment_stub(message: Message, user_id: int, payload: dict):
    order_id = create_order(user_id, payload)
    await message.answer(
        f"🧾 <b>Заказ №{order_id}</b>\n\n"
        f"🎁 Поздравление для: <b>{payload['name']}</b>\n"
        f"🎭 Персонаж: {payload['character_label']}\n"
        f"🎉 Повод: {payload['occasion_label']}\n\n"
        f"💰 Стоимость: <b>{current_video_price()} ₽</b>\n\n"
        "Оплата пока находится в разработке. Это заглушка — деньги сейчас не списываются. "
        "Для тестирования можно запустить генерацию без оплаты, если этот режим включён администратором.",
        reply_markup=payment_keyboard(order_id), parse_mode='HTML')


async def payment_callback(callback: CallbackQuery, bot: Bot):
    parts = (callback.data or '').split(':')
    if len(parts) != 3:
        return
    action, order_raw = parts[1], parts[2]
    if not order_raw.isdigit():
        return
    order_id = int(order_raw)
    order = get_order(order_id, callback.from_user.id)
    if not order:
        await callback.message.answer('Заказ не найден. Начни заново через /start.')
        return
    if action == 'soon':
        await callback.answer('Оплата ещё не подключена. Ничего не списано.', show_alert=True)
        return
    await callback.answer()
    if action == 'cancel':
        update_order_status(order_id, 'cancelled')
        await callback.message.edit_text(f'Заказ №{order_id} отменён. Оплата не производилась.')
        return
    if action == 'demo':
        if setting('demo_generation') != '1' and not is_admin(callback.from_user.id):
            await callback.answer('Тестовая генерация сейчас отключена.', show_alert=True)
            return
        if order['status'] not in {'awaiting_payment', 'failed'}:
            await callback.answer('Этот заказ уже запущен или закрыт.', show_alert=True)
            return
        update_order_status(order_id, 'generating')
        await callback.message.edit_text(f'🧪 Тестовый запуск заказа №{order_id}. Оплата не списывается.')
        payload = order['payload']
        proxy_message = callback.message
        # run_video_order needs a Message to send to the same chat; this callback message is suitable.
        await run_video_order(proxy_message, bot, payload['name'], payload['character_label'],
                              payload['occasion_label'], payload['scene'], payload['script'], order_id=order_id)


async def admin_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer('Нет доступа.', show_alert=True)
        return
    await callback.answer()
    action = (callback.data or '').split(':', 1)[1]
    if action == 'stats':
        with db() as conn:
            users = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
            total = conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
            paid = conn.execute("SELECT COUNT(*) FROM orders WHERE status='paid'").fetchone()[0]
            generated = conn.execute("SELECT COUNT(*) FROM orders WHERE status='completed'").fetchone()[0]
            failed = conn.execute("SELECT COUNT(*) FROM orders WHERE status='failed'").fetchone()[0]
            cancelled = conn.execute("SELECT COUNT(*) FROM orders WHERE status='cancelled'").fetchone()[0]
        await callback.message.answer(
            f'📊 <b>Статистика</b>\nПользователей: {users}\nЗаказов: {total}\n'
            f'Видео успешно: {generated}\nОшибки генерации: {failed}\nОтменено: {cancelled}\nОплачено: {paid}\n'
            f'Текущая стоимость: {current_video_price()} ₽', parse_mode='HTML')
    elif action == 'users':
        with db() as conn:
            rows = conn.execute('SELECT user_id, username, first_name, last_seen FROM users ORDER BY last_seen DESC LIMIT 15').fetchall()
        if not rows:
            await callback.message.answer('Пользователей пока нет.')
        else:
            lines = [f"{r['first_name'] or 'Без имени'} (@{r['username'] or '—'}) — ID {r['user_id']} — {r['last_seen']}" for r in rows]
            await callback.message.answer('<b>Последние пользователи</b>\n' + '\n'.join(lines), parse_mode='HTML')
    elif action == 'orders':
        with db() as conn:
            rows = conn.execute('SELECT id,user_id,created_at,status,price_rub FROM orders ORDER BY id DESC LIMIT 15').fetchall()
        if not rows:
            await callback.message.answer('Заказов пока нет.')
        else:
            lines = [f"№{r['id']} | user {r['user_id']} | {r['status']} | {r['price_rub']} ₽ | {r['created_at']}" for r in rows]
            await callback.message.answer('<b>Последние заказы</b>\n' + '\n'.join(lines), parse_mode='HTML')
    elif action == 'demo':
        new_value = '0' if setting('demo_generation') == '1' else '1'
        set_setting('demo_generation', new_value)
        await callback.message.answer('Тестовая генерация: ' + ('включена' if new_value == '1' else 'выключена'))
    elif action == 'maintenance':
        new_value = '0' if setting('maintenance') == '1' else '1'
        set_setting('maintenance', new_value)
        await callback.message.answer('Режим обслуживания: ' + ('включён' if new_value == '1' else 'выключен'))
    elif action == 'price':
        await state.set_state(Order.admin_price)
        await callback.message.answer(
            f'💰 Текущая стоимость видео: {current_video_price()} ₽\n\n'
            'Отправь новую цену целым числом в рублях, например 199. Для отмены нажми «❌ Отменить».')
    elif action == 'broadcast':
        await state.set_state(Order.admin_broadcast)
        await callback.message.answer('📣 Отправь текст рассылки одним сообщением. Для отмены — /cancel.')


async def receive_admin_price(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or '').strip().replace(' ', '').replace('₽', '')
    if not raw.isdigit() or not 1 <= int(raw) <= 100000:
        await message.answer('Введи целую сумму от 1 до 100000 рублей, например 199.')
        return
    price = int(raw)
    set_setting('video_price_rub', str(price))
    await state.clear()
    await message.answer(f'✅ Стоимость одного видео изменена: {price} ₽.', reply_markup=main_menu_keyboard(message.from_user.id))


async def receive_broadcast(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or '').strip()
    if not text:
        await message.answer('Нужен текст рассылки. Попробуй ещё раз или /cancel.')
        return
    with db() as conn:
        ids = [r[0] for r in conn.execute('SELECT user_id FROM users').fetchall()]
    sent = failed = 0
    await message.answer(f'Начинаю рассылку для {len(ids)} пользователей…')
    for user_id in ids:
        try:
            await bot.send_message(user_id, text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await state.clear()
    await message.answer(f'Рассылка завершена. Отправлено: {sent}; не доставлено: {failed}.')


async def menu_create(message: Message, state: FSMContext):
    await start(message, state)


async def menu_help(message: Message):
    await message.answer(
        'ℹ️ <b>Как это работает</b>\n\n'
        '1. Нажми «🎬 Создать поздравление».\n'
        '2. Выбери готовый шаблон или свой текст.\n'
        '3. Укажи имя ребёнка и выбери персонажа.\n'
        '4. Подтверди заказ и дождись видео.\n\n'
        'Если что-то пошло не так, нажми «❌ Отменить» и начни заново.',
        parse_mode='HTML', reply_markup=main_menu_keyboard(message.from_user.id))


async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Текущий шаг отменён. Чтобы начать заново, нажми «🎬 Создать поздравление».", reply_markup=main_menu_keyboard(message.from_user.id))


async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    init_db()
    dp.message.register(start, CommandStart())
    dp.message.register(show_admin_menu, Command("admin"))
    dp.message.register(show_myid, Command("myid"))
    dp.message.register(cancel, Command("cancel"))
    dp.message.register(menu_create, F.text == '🎬 Создать поздравление')
    dp.message.register(menu_help, F.text == 'ℹ️ Как это работает')
    dp.message.register(show_admin_menu, F.text == '🛠 Панель администратора')
    dp.message.register(cancel, F.text == '❌ Отменить')
    dp.message.register(receive_admin_price, Order.admin_price, F.text)
    dp.message.register(receive_broadcast, Order.admin_broadcast, F.text)
    dp.message.register(receive_name, Order.name, F.text)
    dp.message.register(receive_custom_occasion, Order.custom_occasion, F.text)
    dp.message.register(receive_custom_character, Order.custom_character, F.text)
    dp.message.register(receive_custom_script, Order.custom_script, F.text)
    dp.callback_query.register(admin_callback, F.data.startswith("admin:"))
    dp.callback_query.register(payment_callback, F.data.startswith("pay:"))
    dp.callback_query.register(choose_mode, F.data.startswith("mode:"))
    dp.callback_query.register(choose_character, F.data.startswith("char:"))
    dp.callback_query.register(choose_occasion, F.data.startswith("occasion:"))
    log.info("Bot started. Speaking model=%s; character choices=%s; admins=%s", SPEAKING_MODEL, len(CHARACTERS), len(ADMIN_IDS))
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
