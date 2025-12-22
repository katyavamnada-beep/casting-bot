import os
import re
import json
import base64
import asyncio
from datetime import datetime, timezone

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


# =====================
# ENV
# =====================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")  # папка для фото (може бути пустою, якщо не треба)
SA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json")

# Railway / secrets base64 (не обов'язково локально, але корисно на хостингу)
SERVICE_ACCOUNT_JSON_B64 = os.getenv("SERVICE_ACCOUNT_JSON_B64", "")
TOKEN_DRIVE_JSON_B64 = os.getenv("TOKEN_DRIVE_JSON_B64", "")  # якщо використовуєш token_drive.json


# =====================
# CONFIG
# =====================
DATES = [
    "10.01.2026",
    "11.01.2026",
    "13.01.2026",
    "14.01.2026",
    "17.01.2026",
    "18.01.2026",
    "20.01.2026",
    "21.01.2026",
]
TIMES = ["10:20", "11:00", "11:40", "12:30", "13:20"]

NAMEPRINT_CONST = "Stanislav Maspanov"
SHOOTPLACE_CONST = "Ukraine"
SHOOTSTATE_CONST = "Kyiv"
COUNTRY_CONST = "Ukraine"

# Базові колонки (як у тебе)
BASE_HEADER = [
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
]

# Менеджерські колонки (додаємо кодом)
MANAGER_HEADER = ["TelegramChatId", "Status", "NotifiedAt"]

# Повний заголовок
HEADER = BASE_HEADER + MANAGER_HEADER

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

# Як часто бот перевіряє таблицю на статуси (сек)
STATUS_POLL_SECONDS = 30


# =====================
# TEXTS (милі)
# =====================
UA_INTRO = (
    "Привіт! 👋💛\n\n"
    "Тут ви можете податись на фотозйомку.\n"
    "Я поставлю кілька запитань — це потрібно лише для оформлення модельного релізу.\n\n"
    "Важливо:\n"
    "• Всі текстові відповіді (імʼя, місто, адреса, email) — англійською\n"
    "• Телефон — тільки цифри у форматі 380931111111\n"
    "• Адреса (вулиця/будинок) — необовʼязкова: можна написати ДАЛІ\n\n"
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

UA_APPROVED = (
    "Є новини 💛\n\n"
    "Ваша заявка Погоджена ✅\n"
    "Деталі по локації та організації ми надішлемо ближче до зйомки.\n"
    "Якщо потрібно щось уточнити — менеджер з вами звʼяжеться."
)

UA_REJECTED = (
    "Дякуємо 💛\n\n"
    "Цього разу, на жаль, не виходить ❌\n"
    "Але ми збережемо контакт — можемо написати вам щодо наступних зйомок."
)


# =====================
# VALIDATION + HELPERS
# =====================
EN_TEXT_RE = re.compile(r"^[A-Za-z0-9\s\-\.'\,/#]+$")
PHONE_RE = re.compile(r"^380\d{9}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def is_en(s: str) -> bool:
    s = s.strip()
    return bool(s) and bool(EN_TEXT_RE.fullmatch(s))

def is_phone(s: str) -> bool:
    return bool(PHONE_RE.fullmatch(s.strip()))

def is_email(s: str) -> bool:
    return bool(EMAIL_RE.fullmatch(s.strip()))

def is_next_ua(s: str) -> bool:
    s = s.strip().lower()
    return s in {"далі", "дали", "далi", "next"}

def normalize_name_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).lower()

def ddmmyyyy_to_mmddyyyy(ddmmyyyy: str) -> str:
    d, m, y = ddmmyyyy.split(".")
    return f"{m}/{d}/{y}"

def mmddyyyy_tab_name(mmddyyyy: str) -> str:
    # Таб-нейм в тебе уже був у різних форматах.
    # Тут робимо таб як дата у форматі: 10.01.2026 (як тобі зручно по дням)
    # Але самі значення в колонках DateSigned/ShootDate — MM/DD/YYYY (як ти просила).
    # Тобто: таб = dd.mm.yyyy, а в клітинках = mm/dd/yyyy
    return None  # не використовується

def is_dob_ua(text: str) -> bool:
    return bool(re.fullmatch(r"\d{2}[./]\d{2}[./]\d{4}", text.strip()))

def dob_ua_to_mmddyyyy(text: str) -> str:
    t = text.strip().replace("/", ".")
    d, m, y = t.split(".")
    return f"{m}/{d}/{y}"

def missing_required(data: dict, keys: list[str]) -> bool:
    return any(k not in data or data.get(k) is None for k in keys)

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
# GOOGLE (Sheets)
# =====================
def ensure_local_secrets_if_needed():
    """
    На Railway файлів може не бути (бо ми їх не пушили).
    Тому якщо немає service_account.json / token_drive.json — пробуємо відновити з BASE64 env.
    """
    if (not os.path.exists(SA_JSON)) and SERVICE_ACCOUNT_JSON_B64.strip():
        raw = base64.b64decode(SERVICE_ACCOUNT_JSON_B64.strip().encode("utf-8"))
        with open(SA_JSON, "wb") as f:
            f.write(raw)

    if (not os.path.exists("token_drive.json")) and TOKEN_DRIVE_JSON_B64.strip():
        raw = base64.b64decode(TOKEN_DRIVE_JSON_B64.strip().encode("utf-8"))
        with open("token_drive.json", "wb") as f:
            f.write(raw)

def sheets_service_creds():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return ServiceAccountCredentials.from_service_account_file(SA_JSON, scopes=scopes)

def open_sheet(gc: gspread.Client):
    return gc.open_by_key(SHEET_ID)

def ensure_day_tab_and_headers(sh, day_ddmmyyyy: str):
    """
    Кожен день = окрема вкладка з назвою як у кнопці: 10.01.2026
    Додає заголовок HEADER, якщо треба, і доганяє менеджерські колонки.
    """
    try:
        ws = sh.worksheet(day_ddmmyyyy)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=day_ddmmyyyy, rows=1000, cols=60)
        ws.append_row(HEADER)
        return ws

    # Якщо вкладка є — перевіримо заголовок
    try:
        existing = ws.row_values(1)
    except Exception:
        existing = []

    if not existing:
        ws.append_row(HEADER)
        return ws

    # Якщо старий заголовок без менеджерських колонок — дозапишемо їх в кінець
    need_append = []
    for col in HEADER:
        if col not in existing:
            need_append.append(col)

    if need_append:
        # Додаємо відсутні заголовки в кінець
        ws.update(f"{gspread.utils.rowcol_to_a1(1, len(existing)+1)}:{gspread.utils.rowcol_to_a1(1, len(existing)+len(need_append))}",
                  [need_append])

    return ws

def find_col_index(headers: list[str], col_name: str) -> int:
    # 1-based index
    for i, v in enumerate(headers, start=1):
        if v.strip() == col_name:
            return i
    return -1

def model_exists_in_tab(ws, model_name: str) -> bool:
    # ModelName = 6 колонка в BASE_HEADER, але краще знайдемо по заголовку
    headers = ws.row_values(1)
    idx = find_col_index(headers, "ModelName")
    if idx < 1:
        return False
    col = ws.col_values(idx)
    key = normalize_name_key(model_name)
    for v in col[1:]:
        if v and normalize_name_key(v) == key:
            return True
    return False


# =====================
# DRIVE UPLOAD (опційно)
# =====================
# Якщо у тебе фото вже нормально заливається в Drive у твоїй робочій версії — залишаємо як є:
# тут ми не міняємо твою логіку, тільки не падаємо якщо DRIVE_FOLDER_ID не заданий.

from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload
from google.oauth2.credentials import Credentials as UserCredentials
from google.auth.transport.requests import Request

def drive_user_creds():
    scopes = ["https://www.googleapis.com/auth/drive.file"]
    creds = UserCredentials.from_authorized_user_file("token_drive.json", scopes)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open("token_drive.json", "w") as f:
            f.write(creds.to_json())
    return creds

def normalize_filename(shoot_date_ddmmyyyy: str, shoot_time: str, model_name: str, phone: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", model_name.strip()).strip("_")
    safe_phone = re.sub(r"[^0-9]+", "", phone.strip())
    safe_time = shoot_time.replace(":", "-")
    safe_date = shoot_date_ddmmyyyy.replace(".", "-")
    return f"{safe_date}_{safe_time}_{safe_name}_{safe_phone}.jpg"

async def upload_photo_to_drive(bot: Bot, file_id: str, filename: str) -> str:
    if not DRIVE_FOLDER_ID:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID is empty")
    if not os.path.exists("token_drive.json"):
        raise RuntimeError("token_drive.json not found")

    creds = drive_user_creds()
    drive = build("drive", "v3", credentials=creds)

    tg_file = await bot.get_file(file_id)
    file_bytes = await bot.download_file(tg_file.file_path)
    data = file_bytes.read()

    media = MediaInMemoryUpload(data, mimetype="image/jpeg", resumable=False)
    metadata = {"name": filename, "parents": [DRIVE_FOLDER_ID]}
    created = drive.files().create(
        body=metadata,
        media_body=media,
        fields="webViewLink"
    ).execute()

    return created["webViewLink"]


# =====================
# HANDLERS (старий UX)
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
    text = message.text.strip()
    if not is_en(text):
        await message.answer("Трошки не так 🙂 Введіть, будь ласка, англійською. Приклад: Anna Ivanova")
        return

    data = await state.get_data()
    shoot_date_ddmmyyyy = data.get("shoot_date")

    # Перевірка дубля по імені в табі цього дня
    try:
        gc = gspread.authorize(sheets_service_creds())
        sh = open_sheet(gc)
        ws = ensure_day_tab_and_headers(sh, shoot_date_ddmmyyyy)
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
    text = message.text.strip()
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
    text = message.text.strip()

    # Якщо ДАЛІ — пропускаємо адресу + місто (як ти хотіла)
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
    text = message.text.strip()
    if not is_en(text):
        await message.answer("Будь ласка, англійською 💛 Приклад: Kyiv")
        return

    await state.update_data(city=text)
    await message.answer("І ще номер телефону 📞 Тільки цифри у форматі: 380931111111")
    await state.set_state(Form.phone)

async def on_phone(message: Message, state: FSMContext):
    text = message.text.strip()
    if not is_phone(text):
        await message.answer("Майже 🙂 Номер має виглядати ось так: 380931111111 (тільки цифри)")
        return
    await state.update_data(phone=text)
    await message.answer("Тепер email ✉️ Приклад: name@gmail.com")
    await state.set_state(Form.email)

async def on_email(message: Message, state: FSMContext):
    text = message.text.strip()
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
    text = message.text.strip()
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
        drive_url = await upload_photo_to_drive(bot, file_id, filename)
    except Exception as e:
        await message.answer(
            "Не вдалося завантажити фото 😔\n"
            "Спробуйте ще раз або напишіть адміну.\n\n"
            f"Технічна помилка: {type(e).__name__}"
        )
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

    required = ["shoot_date", "model_name", "dob", "phone", "email", "photo_drive_url"]
    if missing_required(data, required):
        await call.message.answer("Форма не активна 🙈 Почнемо спочатку: /start")
        await state.clear()
        return

    shoot_date_ddmmyyyy = data["shoot_date"]
    shoot_date_mmddyyyy = ddmmyyyy_to_mmddyyyy(shoot_date_ddmmyyyy)

    guardian = (data.get("guardian_name") or "").strip()
    city_val = (data.get("city") or "").strip()
    address_val = (data.get("residence_address") or "").strip()

    # Пишемо в sheet
    gc = gspread.authorize(sheets_service_creds())
    sh = open_sheet(gc)
    ws = ensure_day_tab_and_headers(sh, shoot_date_ddmmyyyy)

    # Дубль ще раз перед записом
    if model_exists_in_tab(ws, data["model_name"]):
        await call.message.answer(
            "Схоже, ця людина вже є у списку на цю дату 🙂\n"
            "Якщо це інша людина з таким самим ім’ям — подайте ще раз з middle name/ініціалом.\n\n"
            "Натисніть: Подати ще одну людину",
            reply_markup=kb_more()
        )
        await state.clear()
        return

    chat_id = str(call.message.chat.id)

    row = [
        NAMEPRINT_CONST,             # Nameprint
        shoot_date_mmddyyyy,         # DateSigned = день зйомки
        shoot_date_mmddyyyy,         # ShootDate = день зйомки
        SHOOTPLACE_CONST,            # ShootPlace
        SHOOTSTATE_CONST,            # ShootState
        data["model_name"].strip(),  # ModelName
        data["dob"].strip(),         # DateOfBirth
        address_val,                 # ResidenceAddress
        city_val,                    # City
        "",                          # State (не питаємо)
        COUNTRY_CONST,               # Country (константа)
        "",                          # ZipCode (не питаємо)
        data["phone"].strip(),       # Phone
        data["email"].strip(),       # Email
        guardian,                    # GuardianName
        shoot_date_mmddyyyy,         # DateSigneded (як було)
        data["photo_drive_url"].strip(),  # Photo (url)
        chat_id,                     # TelegramChatId
        STATUS_PENDING,              # Status
        "",                          # NotifiedAt
    ]

    ws.append_row(row)

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
# MANAGER STATUS WATCHER
# =====================
async def status_watcher(bot: Bot):
    """
    Менеджер у таблиці міняє Status на approved / rejected.
    Бот знаходить рядки, де:
      - Status in {approved, rejected}
      - NotifiedAt пусто
      - TelegramChatId не пусто
    і надсилає повідомлення, потім ставить NotifiedAt.
    """
    while True:
        try:
            gc = gspread.authorize(sheets_service_creds())
            sh = open_sheet(gc)

            for day in DATES:
                try:
                    ws = sh.worksheet(day)
                except gspread.WorksheetNotFound:
                    continue

                headers = ws.row_values(1)
                if not headers:
                    continue

                idx_chat = find_col_index(headers, "TelegramChatId")
                idx_status = find_col_index(headers, "Status")
                idx_notif = find_col_index(headers, "NotifiedAt")

                if idx_chat < 1 or idx_status < 1 or idx_notif < 1:
                    # Якщо вкладка стара — доганяємо заголовки
                    ws = ensure_day_tab_and_headers(sh, day)
                    headers = ws.row_values(1)
                    idx_chat = find_col_index(headers, "TelegramChatId")
                    idx_status = find_col_index(headers, "Status")
                    idx_notif = find_col_index(headers, "NotifiedAt")
                    if idx_chat < 1 or idx_status < 1 or idx_notif < 1:
                        continue

                # Беремо всі значення (обережно, але у тебе там не мільйони рядків)
                values = ws.get_all_values()
                if len(values) < 2:
                    continue

                for r_i in range(2, len(values) + 1):  # 1-based rows, start from row 2
                    row = values[r_i - 1]
                    def get_cell(ci: int) -> str:
                        return row[ci - 1].strip() if ci - 1 < len(row) and row[ci - 1] else ""

                    chat_id = get_cell(idx_chat)
                    status = get_cell(idx_status).lower()
                    notified = get_cell(idx_notif)

                    if not chat_id:
                        continue
                    if notified:
                        continue
                    if status not in {STATUS_APPROVED, STATUS_REJECTED}:
                        continue

                    # Надіслати повідомлення
                    try:
                        if status == STATUS_APPROVED:
                            await bot.send_message(int(chat_id), UA_APPROVED)
                        else:
                            await bot.send_message(int(chat_id), UA_REJECTED)

                        # Поставити NotifiedAt
                        ws.update_cell(r_i, idx_notif, utc_now_iso())
                    except Exception:
                        # якщо не змогли написати (юзер заблокував бота тощо) — не валимо цикл
                        pass

        except Exception:
            pass

        await asyncio.sleep(STATUS_POLL_SECONDS)


# =====================
# MAIN
# =====================
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty")
    if not SHEET_ID:
        raise RuntimeError("GOOGLE_SHEET_ID is empty")

    # Секрети (Railway)
    ensure_local_secrets_if_needed()

    if not os.path.exists(SA_JSON):
        raise RuntimeError("service_account.json not found (provide SERVICE_ACCOUNT_JSON_B64 or a file)")
    # token_drive.json потрібен тільки якщо ти реально заливаєш фото в Drive
    # Якщо хочеш — можна тимчасово вимкнути upload і все одно збирати анкети.
    # Але у твоєму сценарії фото треба, тому залишаю як вимогу:
    if not DRIVE_FOLDER_ID:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID is empty")
    if not os.path.exists("token_drive.json"):
        raise RuntimeError("token_drive.json not found (provide TOKEN_DRIVE_JSON_B64 or upload file)")

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

    # Фоновий таск для статусів
    asyncio.create_task(status_watcher(bot))

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
