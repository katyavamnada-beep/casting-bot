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

from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload


# =====================
# ENV
# =====================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
SERVICE_ACCOUNT_JSON_B64 = os.getenv("SERVICE_ACCOUNT_JSON_B64", "").strip()


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

# Додали ShootTime + manager-поля
HEADER = [
    "Nameprint",
    "DateSigned",
    "ShootDate",
    "ShootTime",
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
    "TelegramChatId",
    "Status",
    "NotifiedAt",
]


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
    "• Адреса (вулиця/будинок) — необовʼязкова, можна написати ДАЛІ\n\n"
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
    return mmddyyyy.replace("/", "-")

def is_dob_ua(text: str) -> bool:
    return bool(re.fullmatch(r"\d{2}[./]\d{2}[./]\d{4}", text.strip()))

def dob_ua_to_mmddyyyy(text: str) -> str:
    t = text.strip().replace("/", ".")
    d, m, y = t.split(".")
    return f"{m}/{d}/{y}"

def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def missing_required(data: dict, keys: list[str]) -> bool:
    return any(k not in data or data.get(k) is None for k in keys)


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
def _service_account_info() -> dict:
    if not SERVICE_ACCOUNT_JSON_B64:
        raise RuntimeError("SERVICE_ACCOUNT_JSON_B64 is empty in Railway Variables")

    try:
        raw = base64.b64decode(SERVICE_ACCOUNT_JSON_B64).decode("utf-8")
        return json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"Cannot decode SERVICE_ACCOUNT_JSON_B64: {type(e).__name__}")

def sheets_service_creds():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    info = _service_account_info()
    return ServiceAccountCredentials.from_service_account_info(info, scopes=scopes)

def drive_service_creds():
    # Drive scope потрібен для Shared Drive
    scopes = ["https://www.googleapis.com/auth/drive"]
    info = _service_account_info()
    return ServiceAccountCredentials.from_service_account_info(info, scopes=scopes)

def ensure_sheet_tab(gc: gspread.Client, sheet_id: str, shoot_date_mmddyyyy: str):
    sh = gc.open_by_key(sheet_id)
    tab = mmddyyyy_tab_name(shoot_date_mmddyyyy)

    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=1000, cols=60)
        ws.append_row(HEADER)
        return ws

    # якщо вкладка існувала раніше — оновимо заголовки (додамо відсутні)
    try:
        existing = ws.row_values(1)
        if existing:
            changed = False
            merged = existing[:]
            for h in HEADER:
                if h not in existing:
                    merged.append(h)
                    changed = True
            if changed:
                ws.update("1:1", [merged])
    except Exception:
        pass

    return ws

def model_exists_in_tab(ws, model_name: str) -> bool:
    try:
        col = ws.col_values(7)  # ModelName тепер 7-а колонка (бо додали ShootTime)
    except Exception:
        return False
    key = normalize_name_key(model_name)
    for v in col[1:]:
        if v and normalize_name_key(v) == key:
            return True
    return False


# =====================
# DRIVE UPLOAD (Service Account)
# =====================
def normalize_filename(shoot_date_ddmmyyyy: str, shoot_time: str, model_name: str, phone: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", model_name.strip()).strip("_")
    safe_phone = re.sub(r"[^0-9]+", "", phone.strip())
    safe_time = shoot_time.replace(":", "-")
    safe_date = shoot_date_ddmmyyyy.replace(".", "-")
    return f"{safe_date}_{safe_time}_{safe_name}_{safe_phone}.jpg"

async def upload_photo_to_drive(bot: Bot, file_id: str, filename: str) -> str:
    if not DRIVE_FOLDER_ID:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID is empty in Railway Variables")

    creds = drive_service_creds()
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    tg_file = await bot.get_file(file_id)
    file_bytes = await bot.download_file(tg_file.file_path)
    data = file_bytes.read()

    media = MediaInMemoryUpload(data, mimetype="image/jpeg", resumable=False)
    metadata = {"name": filename, "parents": [DRIVE_FOLDER_ID]}

    # IMPORTANT: supportsAllDrives=True для Shared Drive
    created = drive.files().create(
        body=metadata,
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True
    ).execute()

    return created["webViewLink"]


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
        gc = gspread.authorize(sheets_service_creds())
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
        drive_url = await upload_photo_to_drive(bot, file_id, filename)
    except Exception as e:
        await message.answer(
            "Не вдалося завантажити фото в Google Drive 😔\n"
            "Спробуйте ще раз або напишіть адміну.\n\n"
            f"Технічна помилка: {type(e).__name__}\n{str(e)[:800]}"
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

    required = ["shoot_date", "shoot_time", "model_name", "dob", "phone", "email", "photo_drive_url"]
    if missing_required(data, required):
        await call.message.answer("Форма не активна 🙈 Почнемо спочатку: /start")
        await state.clear()
        return

    shoot_date_mmddyyyy = ddmmyyyy_to_mmddyyyy(data["shoot_date"])

    guardian = (data.get("guardian_name") or "").strip()
    city_val = (data.get("city") or "").strip()

    gc = gspread.authorize(sheets_service_creds())
    ws = ensure_sheet_tab(gc, SHEET_ID, shoot_date_mmddyyyy)

    if model_exists_in_tab(ws, data["model_name"]):
        await call.message.answer(
            "Схоже, ця людина вже є у списку на цю дату 🙂\n"
            "Якщо це інша людина з таким самим ім’ям — подайте ще раз з middle name/ініціалом.\n\n"
            "Натисніть: Подати ще одну людину",
            reply_markup=kb_more()
        )
        await state.clear()
        return

    row = [
        NAMEPRINT_CONST,
        shoot_date_mmddyyyy,
        shoot_date_mmddyyyy,
        data["shoot_time"].strip(),
        SHOOTPLACE_CONST,
        SHOOTSTATE_CONST,
        data["model_name"].strip(),
        data["dob"].strip(),
        (data.get("residence_address") or "").strip(),
        city_val,
        "",
        COUNTRY_CONST,
        "",
        data["phone"].strip(),
        data["email"].strip(),
        guardian,
        shoot_date_mmddyyyy,
        data["photo_drive_url"].strip(),
        str(call.message.chat.id),
        "NEW",
        "",
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

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
