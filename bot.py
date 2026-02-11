import os
import re
import json
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

import gspread
from google.oauth2.service_account import Credentials as ServiceAccountCredentials

from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload


# =====================
# ENV
# =====================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")  # може починатися з 0AF... для Shared Drive
SERVICE_ACCOUNT_JSON_B64 = os.getenv("SERVICE_ACCOUNT_JSON_B64")  # base64(service_account.json)

STATUS_CHECK_INTERVAL_SEC = int(os.getenv("STATUS_CHECK_INTERVAL_SEC", "20"))


# =====================
# CONFIG
# =====================
DATES = [
    "10.02.2026",
    "11.02.2026",
    "12.02.2026",
    "14.02.2026",
    "15.02.2026",
    "16.02.2026",
    "17.02.2026",
    "18.02.2026",
    "19.02.2026",
    "20.02.2026",
    "21.02.2026",
]

TIMES = ["10:20", "11:00", "11:40", "12:30", "13:20"]

NAMEPRINT_CONST = "Stanislav Maspanov"
SHOOTPLACE_CONST = "Ukraine"
SHOOTSTATE_CONST = "Kyiv"
COUNTRY_CONST = "Ukraine"

HEADER = [
    "Nameprint",
    "DateSigned",
    "ShootDate",
    "ShootPlace",
    "ShootState",
    "ModelName",
    "DateOfBirth",
    "ResidenceAddress",
    "City",
    "State",
    "Country",
    "ZipCode",
    "Phone",
    "Email",
    "GuardianName",
    "DateSigneded",
    "Photo",
    "ShootTime",
    "TelegramChatId",
    "Status",
    "NotifiedAt",
    "SubmittedAt",
]


# =====================
# TEXTS
# =====================
UA_INTRO = (
    "Привіт! 👋💛\n\n"
    "Тут ви можете записатись на фотозйомку 📸\n\n"
    "Умови зйомки:\n"
    "• ОПЛАТА — 350 грн одразу після зйомки\n"
    "• ЗАЙНЯТІСТЬ — приблизно 20 хвилин\n\n"
    "Якщо умови вам підходять — тут можна обрати зручну дату та час 😊\n\n"
    "Я поставлю кілька запитань, які потрібні для оформлення модельного релізу.\n\n"
    "Важливо:\n"
    "• Усі текстові відповіді потрібно писати англійською\n"
    "• Номер телефону — лише цифри у форматі 380XXXXXXXXX\n"
    "• Адреса проживання необовʼязкова — менеджер зможе уточнити це пізніше 💛\n\n"
    "До речі, можна приходити з родичами — будемо раді всім 😊"
)
UA_READY = "Коли будете готові — натисніть кнопку нижче 👇"

UA_FINISH = (
    "Дякуємо! 💛 Ваша заявка успішно надіслана.\n\n"
    "Менеджер опрацьовує списки ближче до дати зйомки.\n"
    "Інформацію по локації та точним деталям ми надішлемо ближче до зйомки.\n"
    "На майданчику вас зустріне адміністратор і підкаже все необхідне.\n\n"
    "Хочете подати ще одну людину?"
)

APPROVED_TEXT = (
    "Вітаю! Вашу заявку **ПІДТВЕРДЖЕНО** ✅\n"
    "📅 Дата зйомки: {shoot_date}\n\n"
)

REJECTED_TEXT = (
    "На жаль, цього разу вашу заявку **НЕ ПІДТВЕРДЖЕНО** 🙏\n\n"
    "Дякуємо, що подались 💛 Якщо з’являться нові слоти — ми напишемо."
)

# Локація тільки для 01/10/2026 та 01/11/2026 (MM/DD/YYYY у таблиці)
LOCATION_DATES_MMDDYYYY = {"01/10/2026", "01/11/2026"}

APPROVED_LOCATION_10_11 = (
    "\n\n"
    "📍 **Локація зйомки**\n"
    "Архітектурно-інженерний колегіум А+\n"
    "(м.Нивки ЖК Файна Таун)\n"
    "https://maps.app.goo.gl/gngnhGf3BgoLLaLS8\n\n"
    "⏰ **Приходьте вчасно**\n"
    "Точка збору для моделей: перед входом в школу.\n"
    "Чекаємо, поки вас забере ваш адміністратор.\n"
    "Самостійно не заходимо 💛"
)


# =====================
# VALIDATION + HELPERS
# =====================
EN_TEXT_RE = re.compile(r"^[A-Za-z0-9\s\-\.'\,/#]+$")
PHONE_RE = re.compile(r"^380\d{9}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def is_en(s: str) -> bool:
    s = (s or "").strip()
    return bool(s) and bool(EN_TEXT_RE.fullmatch(s))

def is_phone(s: str) -> bool:
    return bool(PHONE_RE.fullmatch((s or "").strip()))

def is_email(s: str) -> bool:
    return bool(EMAIL_RE.fullmatch((s or "").strip()))

def is_next_ua(s: str) -> bool:
    s = (s or "").strip().lower()
    return s in {"далі", "дали", "далi", "next"}

def normalize_name_key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()

def ddmmyyyy_to_mmddyyyy(ddmmyyyy: str) -> str:
    d, m, y = ddmmyyyy.split(".")
    return f"{m}/{d}/{y}"

def mmddyyyy_tab_name(mmddyyyy: str) -> str:
    return mmddyyyy.replace("/", "-")

def is_dob_ua(text: str) -> bool:
    return bool(re.fullmatch(r"\d{2}[./]\d{2}[./]\d{4}", (text or "").strip()))

def dob_ua_to_mmddyyyy(text: str) -> str:
    t = (text or "").strip().replace("/", ".")
    d, m, y = t.split(".")
    return f"{m}/{d}/{y}"

def missing_required(data: dict, keys: list[str]) -> bool:
    return any(k not in data or data.get(k) is None for k in keys)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def safe_lower(s: str) -> str:
    return (s or "").strip().lower()

def kyiv_submitted_at() -> str:
    return datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%m/%d/%Y %H:%M")

def b64_to_bytes(b64: str) -> bytes:
    import base64
    # прибираємо пробіли/переноси — Railway інколи вставляє з переносами
    b64_clean = "".join((b64 or "").split())
    return base64.b64decode(b64_clean.encode("utf-8"))


# =====================
# KEYBOARDS
# =====================
def kb_begin():
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Подати заявку на зйомку", callback_data="begin:yes")
    return kb.as_markup()

def kb_dates():
    kb = InlineKeyboardBuilder()
    for d in DATES:
        kb.button(text=d, callback_data=f"date:{d}")
    kb.adjust(2)
    return kb.as_markup()

def kb_times():
    kb = InlineKeyboardBuilder()
    for t in TIMES:
        kb.button(text=t, callback_data=f"time:{t}")
    kb.adjust(2)
    return kb.as_markup()

def kb_minor():
    kb = InlineKeyboardBuilder()
    kb.button(text="Так, мені менше 18", callback_data="minor:yes")
    kb.button(text="Ні, мені 18+", callback_data="minor:no")
    kb.adjust(1)
    return kb.as_markup()

def kb_consent():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Погоджуюсь", callback_data="consent:yes")
    return kb.as_markup()

def kb_more():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Подати ще одну людину", callback_data="more:yes")
    kb.button(text="✅ Завершити", callback_data="more:no")
    kb.adjust(1)
    return kb.as_markup()


# =====================
# STATES
# =====================
class Form(StatesGroup):
    shoot_date = State()
    shoot_time = State()

    model_name = State()
    dob = State()

    residence_address = State()
    city = State()

    phone = State()
    email = State()

    minor = State()
    guardian_name = State()

    photo = State()
    consent = State()


# =====================
# GOOGLE AUTH (Service Account ONLY)
# =====================
def service_account_info() -> dict:
    if not SERVICE_ACCOUNT_JSON_B64:
        raise RuntimeError("SERVICE_ACCOUNT_JSON_B64 is empty in Railway Variables")
    raw = b64_to_bytes(SERVICE_ACCOUNT_JSON_B64)
    try:
        txt = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RuntimeError(f"SERVICE_ACCOUNT_JSON_B64 decode failed (not valid UTF-8 json). {e}")
    return json.loads(txt)

def sheets_client() -> gspread.Client:
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = ServiceAccountCredentials.from_service_account_info(service_account_info(), scopes=scopes)
    return gspread.authorize(creds)

def drive_service():
    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_service_account_info(service_account_info(), scopes=scopes)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# =====================
# SHEETS HELPERS
# =====================
def ensure_sheet_tab(gc: gspread.Client, sheet_id: str, shoot_date_mmddyyyy: str):
    sh = gc.open_by_key(sheet_id)
    tab = mmddyyyy_tab_name(shoot_date_mmddyyyy)

    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=2000, cols=60)
        ws.append_row(HEADER)

    current_header = ws.row_values(1)
    if not current_header:
        ws.append_row(HEADER)
        current_header = HEADER

    missing = [h for h in HEADER if h not in current_header]
    if missing:
        new_header = current_header + missing
        ws.resize(rows=max(ws.row_count, 2000), cols=max(ws.col_count, len(new_header) + 5))
        ws.update("1:1", [new_header])

    return ws

def header_map(ws) -> dict:
    hdr = ws.row_values(1)
    return {name: (i + 1) for i, name in enumerate(hdr)}  # 1-based

def model_exists_in_tab(ws, model_name: str) -> bool:
    try:
        hm = header_map(ws)
        col_idx = hm.get("ModelName")
        if not col_idx:
            return False
        col = ws.col_values(col_idx)
    except Exception:
        return False

    key = normalize_name_key(model_name)
    for v in col[1:]:
        if v and normalize_name_key(v) == key:
            return True
    return False

def append_row_by_header(ws, row_dict: dict):
    hdr = ws.row_values(1)
    row = [row_dict.get(h, "") for h in hdr]
    ws.append_row(row, value_input_option="RAW")


# =====================
# DRIVE UPLOAD
# =====================
def normalize_filename(shoot_date_ddmmyyyy: str, shoot_time: str, model_name: str, phone: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", (model_name or "").strip()).strip("_")
    safe_phone = re.sub(r"[^0-9]+", "", (phone or "").strip())
    safe_time = (shoot_time or "").replace(":", "-")
    safe_date = (shoot_date_ddmmyyyy or "").replace(".", "-")
    return f"{safe_date}_{safe_time}_{safe_name}_{safe_phone}.jpg"

async def upload_photo_to_drive_service_account(bot: Bot, file_id: str, filename: str) -> str:
    if not DRIVE_FOLDER_ID:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID is empty in Railway Variables")

    drive = drive_service()

    tg_file = await bot.get_file(file_id)
    file_bytes = await bot.download_file(tg_file.file_path)
    data = file_bytes.read()

    media = MediaInMemoryUpload(data, mimetype="image/jpeg", resumable=False)
    metadata = {"name": filename, "parents": [DRIVE_FOLDER_ID]}

    created = drive.files().create(
        body=metadata,
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute()

    return created.get("webViewLink") or f"https://drive.google.com/file/d/{created['id']}/view"


# =====================
# STATUS WATCHER
# =====================
async def status_watcher(bot: Bot):
    await asyncio.sleep(3)

    while True:
        try:
            gc = sheets_client()
            sh = gc.open_by_key(SHEET_ID)

            for ws in sh.worksheets():
                hdr = ws.row_values(1)
                if not hdr:
                    continue
                if "Status" not in hdr or "TelegramChatId" not in hdr or "NotifiedAt" not in hdr:
                    continue

                hm = header_map(ws)
                status_col = hm.get("Status")
                notified_col = hm.get("NotifiedAt")
                chat_col = hm.get("TelegramChatId")
                date_col = hm.get("ShootDate")
                time_col = hm.get("ShootTime")

                if not (status_col and notified_col and chat_col):
                    continue

                all_rows = ws.get_all_values()

                for r_i in range(2, len(all_rows) + 1):  # 1-based row index
                    row = all_rows[r_i - 1]

                    def get_by_col(col_num: int) -> str:
                        return row[col_num - 1] if col_num - 1 < len(row) else ""

                    status = safe_lower(get_by_col(status_col))
                    notified = (get_by_col(notified_col) or "").strip()
                    chat_id = (get_by_col(chat_col) or "").strip()

                    if not chat_id:
                        continue
                    if notified:
                        continue
                    if status not in {"approved", "rejected"}:
                        continue

                    shoot_date = (get_by_col(date_col) or "").strip() if date_col else ""
                    shoot_time = (get_by_col(time_col) or "").strip() if time_col else ""

                    if status == "approved":
                        text = APPROVED_TEXT.format(shoot_date=shoot_date, shoot_time=shoot_time)
                        if shoot_date in LOCATION_DATES_MMDDYYYY:
                            text += APPROVED_LOCATION_10_11
                    else:
                        text = REJECTED_TEXT

                    try:
                        await bot.send_message(int(chat_id), text, parse_mode="Markdown")
                    except Exception:
                        continue

                    ws.update_cell(r_i, notified_col, now_iso())

        except Exception as e:
            # щоб не валився процес у Railway
            print("status_watcher error:", type(e).__name__, str(e))

        await asyncio.sleep(STATUS_CHECK_INTERVAL_SEC)


# =====================
# HANDLERS
# =====================
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(UA_INTRO)
    await message.answer(UA_READY, reply_markup=kb_begin())

async def on_begin(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await call.message.answer("Чудово! 😊 Почнемо.\n\nОберіть, будь ласка, дату зйомки 📅", reply_markup=kb_dates())
    await state.set_state(Form.shoot_date)

async def on_date(call: CallbackQuery, state: FSMContext):
    date_val = call.data.split(":", 1)[1]
    await state.update_data(shoot_date=date_val)
    await call.message.answer("Супер! ✨ Тепер оберіть зручний час ⏰", reply_markup=kb_times())
    await state.set_state(Form.shoot_time)
    await call.answer()

async def on_time(call: CallbackQuery, state: FSMContext):
    time_val = call.data.split(":", 1)[1]
    await state.update_data(shoot_time=time_val)
    await call.message.answer(
        "Чудово 😊\n"
        "Напишіть, будь ласка, імʼя та прізвище англійською (як у паспорті).\n"
        "Це потрібно для модельного релізу 💛"
    )
    await state.set_state(Form.model_name)
    await call.answer()

async def on_model_name(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not is_en(text):
        await message.answer("Трошки не так 🙂 Введіть, будь ласка, англійською. Приклад: Anna Ivanova")
        return

    data = await state.get_data()
    shoot_date_mmddyyyy = ddmmyyyy_to_mmddyyyy(data["shoot_date"])

    try:
        gc = sheets_client()
        ws = ensure_sheet_tab(gc, SHEET_ID, shoot_date_mmddyyyy)
        if model_exists_in_tab(ws, text):
            await message.answer(
                "Схоже, така людина вже подана на цю дату 🙂\n"
                "Якщо це інша людина з таким самим ім’ям — додайте middle name або ініціал.\n\n"
                "Спробуйте ще раз, будь ласка 💛"
            )
            return
    except Exception:
        pass

    await state.update_data(model_name=text)
    await message.answer(
        "Тепер дата народження 🗓\n"
        "Будь ласка, введіть у форматі: день.місяць.рік\n"
        "Наприклад: 22.12.1998"
    )
    await state.set_state(Form.dob)

async def on_dob(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not is_dob_ua(text):
        await message.answer("Майже 🙂 Формат має бути: день.місяць.рік. Приклад: 22.12.1998")
        return

    await state.update_data(dob=dob_ua_to_mmddyyyy(text))

    await message.answer(
        "Дякую 💛\n\n"
        "Тепер адреса проживання 🏡\n"
        "Якщо вам комфортно — додайте, будь ласка, адресу англійською (вулиця, будинок).\n"
        "Якщо не хочете заповнювати — це абсолютно ок 😊 менеджер зможе уточнити це питання пізніше.\n\n"
        "Якщо пропускаєте — просто напишіть: ДАЛІ"
    )
    await state.set_state(Form.residence_address)

async def on_residence_address(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if is_next_ua(text):
        await state.update_data(residence_address="", city="")
        await message.answer(
            "Ок 💛 Тоді йдемо далі.\n\n"
            "Напишіть, будь ласка, номер телефону 📞\n"
            "Тільки цифри у форматі: 380931111111"
        )
        await state.set_state(Form.phone)
        return

    if not is_en(text):
        await message.answer(
            "Трошки не так 🙂\n"
            "Адресу, будь ласка, введіть англійською (наприклад: 12 Khreshchatyk St).\n"
            "А якщо не хочете заповнювати — просто напишіть: ДАЛІ 💛"
        )
        return

    await state.update_data(residence_address=text)
    await message.answer("Супер, дякую! ✨ Тепер напишіть місто проживання англійською. Приклад: Kyiv")
    await state.set_state(Form.city)

async def on_city(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not is_en(text):
        await message.answer("Будь ласка, англійською 💛 Приклад: Kyiv")
        return

    await state.update_data(city=text)
    await message.answer("І ще номер телефону 📞 Тільки цифри у форматі: 380931111111")
    await state.set_state(Form.phone)

async def on_phone(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not is_phone(text):
        await message.answer("Майже 🙂 Номер має виглядати ось так: 380931111111 (тільки цифри)")
        return
    await state.update_data(phone=text)
    await message.answer("Тепер email ✉️ Приклад: name@gmail.com")
    await state.set_state(Form.email)

async def on_email(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not is_email(text):
        await message.answer("Схоже, email написаний з помилкою 🙂 Приклад: name@gmail.com")
        return
    await state.update_data(email=text)
    await message.answer("Вам менше 18 років?", reply_markup=kb_minor())
    await state.set_state(Form.minor)

async def on_minor(call: CallbackQuery, state: FSMContext):
    choice = call.data.split(":", 1)[1]
    await call.answer()

    if choice == "yes":
        await state.update_data(minor=True)
        await call.message.answer(
            "Добре 💛\n"
            "Тоді, будь ласка, напишіть імʼя та прізвище опікуна англійською.\n"
            "Це потрібно для дитячого модельного релізу 👨‍👩‍👧"
        )
        await state.set_state(Form.guardian_name)
    else:
        await state.update_data(minor=False, guardian_name="")
        await call.message.answer(
            "Супер ✨ Тепер завантажте, будь ласка, портретне фото 📸\n"
            "Можна як фото або як файл."
        )
        await state.set_state(Form.photo)

async def on_guardian_name(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not is_en(text):
        await message.answer("Будь ласка, англійською 💛 Приклад: Olha Ivanova")
        return
    await state.update_data(guardian_name=text)
    await message.answer("Дякую! ✨ Тепер завантажте, будь ласка, портретне фото 📸")
    await state.set_state(Form.photo)

async def on_photo(message: Message, state: FSMContext, bot: Bot):
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_id = message.document.file_id

    if not file_id:
        await message.answer("Це не схоже на фото 🙂 Надішліть, будь ласка, портретне фото.")
        return

    data = await state.get_data()
    required = ["shoot_date", "shoot_time", "model_name", "phone"]
    if missing_required(data, required):
        await message.answer("Ой 🙈 анкета перервалася. Почнемо спочатку: /start")
        await state.clear()
        return

    filename = normalize_filename(data["shoot_date"], data["shoot_time"], data["model_name"], data["phone"])
    await message.answer("Дякую! 💛 Завантажую фото…")

    try:
        drive_url = await upload_photo_to_drive_service_account(bot, file_id, filename)
    except Exception as e:
        await message.answer(
            "Не вдалося завантажити фото в Google Drive 😔\n"
            "Спробуйте ще раз або напишіть адміну.\n\n"
            f"Технічна помилка: {type(e).__name__}"
        )
        print("upload error:", type(e).__name__, str(e))
        return

    await state.update_data(photo_drive_url=drive_url)

    await message.answer(
        "Майже готово ✅\n"
        "Підтвердіть, будь ласка, що ви погоджуєтесь на використання цих даних для оформлення модельного релізу 💛",
        reply_markup=kb_consent()
    )
    await state.set_state(Form.consent)

async def on_consent(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()

    required = ["shoot_date", "shoot_time", "model_name", "dob", "phone", "email", "photo_drive_url"]
    if missing_required(data, required):
        await call.message.answer("Форма не активна 🙈 Почнемо спочатку: /start")
        await state.clear()
        return

    shoot_date_mmddyyyy = ddmmyyyy_to_mmddyyyy(data["shoot_date"])
    guardian = (data.get("guardian_name") or "").strip()
    city_val = (data.get("city") or "").strip()

    gc = sheets_client()
    ws = ensure_sheet_tab(gc, SHEET_ID, shoot_date_mmddyyyy)

    # дубль по імені
    if model_exists_in_tab(ws, data["model_name"]):
        await call.message.answer(
            "Схоже, ця людина вже є у списку на цю дату 🙂\n"
            "Якщо це інша людина з таким самим ім’ям — подайте ще раз з middle name/ініціалом.\n\n"
            "Натисніть: Подати ще одну людину",
            reply_markup=kb_more()
        )
        await state.clear()
        return

    submitted_at = kyiv_submitted_at()

    row_dict = {
        "Nameprint": NAMEPRINT_CONST,
        "DateSigned": shoot_date_mmddyyyy,
        "ShootDate": shoot_date_mmddyyyy,
        "ShootPlace": SHOOTPLACE_CONST,
        "ShootState": SHOOTSTATE_CONST,
        "ModelName": data["model_name"].strip(),
        "DateOfBirth": data["dob"].strip(),
        "ResidenceAddress": (data.get("residence_address") or "").strip(),
        "City": city_val,
        "State": "",
        "Country": COUNTRY_CONST,
        "ZipCode": "",
        "Phone": data["phone"].strip(),
        "Email": data["email"].strip(),
        "GuardianName": guardian,
        "DateSigneded": shoot_date_mmddyyyy,
        "Photo": data["photo_drive_url"].strip(),
        "ShootTime": data["shoot_time"].strip(),
        "TelegramChatId": str(call.from_user.id),
        "Status": "",
        "NotifiedAt": "",
        "SubmittedAt": submitted_at,
    }

    append_row_by_header(ws, row_dict)

    await call.message.answer(UA_FINISH, reply_markup=kb_more())
    await state.clear()

async def on_more(call: CallbackQuery, state: FSMContext):
    await call.answer()
    choice = call.data.split(":", 1)[1]
    await state.clear()

    if choice == "yes":
        await call.message.answer("Супер! 😊 Подамо ще одну людину ✨")
        await call.message.answer("Оберіть, будь ласка, дату зйомки 📅", reply_markup=kb_dates())
        await state.set_state(Form.shoot_date)
    else:
        await call.message.answer("Готово 💛 Гарного дня! Якщо що — просто напишіть /start")


# =====================
# MAIN
# =====================
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty in Railway Variables")
    if not SHEET_ID:
        raise RuntimeError("GOOGLE_SHEET_ID is empty in Railway Variables")
    if not DRIVE_FOLDER_ID:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID is empty in Railway Variables")
    if not SERVICE_ACCOUNT_JSON_B64:
        raise RuntimeError("SERVICE_ACCOUNT_JSON_B64 is empty in Railway Variables")

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.register(cmd_start, CommandStart())
    dp.callback_query.register(on_begin, F.data == "begin:yes")

    dp.callback_query.register(on_date, F.data.startswith("date:"), Form.shoot_date)
    dp.callback_query.register(on_time, F.data.startswith("time:"), Form.shoot_time)

    dp.message.register(on_model_name, Form.model_name)
    dp.message.register(on_dob, Form.dob)

    dp.message.register(on_residence_address, Form.residence_address)
    dp.message.register(on_city, Form.city)

    dp.message.register(on_phone, Form.phone)
    dp.message.register(on_email, Form.email)

    dp.callback_query.register(on_minor, F.data.startswith("minor:"), Form.minor)
    dp.message.register(on_guardian_name, Form.guardian_name)

    dp.message.register(on_photo, Form.photo)
    dp.callback_query.register(on_consent, F.data == "consent:yes", Form.consent)

    dp.callback_query.register(on_more, F.data.startswith("more:"))

    asyncio.create_task(status_watcher(bot))

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())