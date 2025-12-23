import os
import re
import json
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

# Railway: файл service_account.json ми відновлюємо через init_secrets.py
SA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json").strip()


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

# БАЗОВИЙ хедер для релізів (як у тебе)
HEADER_BASE = [
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

# НОВЕ: додаємо час, щоб менеджеру було зручно групувати
# (додаємо в кінець, щоб не ламати твої існуючі процеси)
EXTRA_HEADERS = [
    "ShootTime",
    "TelegramChatId",
    "Status",
    "NotifiedAt",
]

HEADER = HEADER_BASE + EXTRA_HEADERS


# =====================
# TEXTS (милі)
# =====================
UA_INTRO = (
    "Привіт! 👋💛\n\n"
    "Тут можна податись на фотозйомку.\n"
    "Я задам кілька коротких запитань — це потрібно лише для оформлення модельного релізу.\n\n"
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


# =====================
# VALIDATION + HELPERS
# =====================
EN_TEXT_RE = re.compile(r"^[A-Za-z0-9\s\-\.'\,/#]+$")
PHONE_RE = re.compile(r"^380\d{9}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DOB_RE = re.compile(r"^\d{2}[./]\d{2}[./]\d{4}$")

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
    return bool(DOB_RE.fullmatch(text.strip()))

def dob_ua_to_mmddyyyy(text: str) -> str:
    t = text.strip().replace("/", ".")
    d, m, y = t.split(".")
    return f"{m}/{d}/{y}"

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def safe_get(lst, idx, default=""):
    try:
        return lst[idx]
    except Exception:
        return default


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
# GOOGLE AUTH
# =====================
def service_account_creds(scopes: list[str]):
    if not os.path.exists(SA_JSON):
        raise RuntimeError("service_account.json not found in project folder")
    return ServiceAccountCredentials.from_service_account_file(SA_JSON, scopes=scopes)

def sheets_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return gspread.authorize(service_account_creds(scopes))

def drive_service():
    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = service_account_creds(scopes)
    return build("drive", "v3", credentials=creds)

def ensure_sheet_tab_and_headers(sh, tab_title: str):
    """
    Створює вкладку якщо нема.
    Додає заголовки якщо порожньо.
    Додає відсутні колонки (TelegramChatId/Status/NotifiedAt/ShootTime) якщо ще нема.
    """
    try:
        ws = sh.worksheet(tab_title)
    except Exception:
        ws = sh.add_worksheet(title=tab_title, rows=1000, cols=60)

    values = ws.get_all_values()
    if not values:
        ws.append_row(HEADER)
        return ws

    current_header = values[0]
    # якщо перший рядок не схожий на хедер — вважаємо що хедера нема
    if "ModelName" not in current_header and "Phone" not in current_header:
        ws.insert_row(HEADER, 1)
        return ws

    # додаємо відсутні колонки в кінець
    missing = [h for h in HEADER if h not in current_header]
    if missing:
        new_header = current_header + missing
        ws.update("A1", [new_header])

    return ws

def model_exists_in_tab(ws, model_name: str) -> bool:
    # ModelName = колонка з назвою "ModelName"
    header = ws.row_values(1)
    try:
        idx = header.index("ModelName") + 1
    except ValueError:
        return False

    col = ws.col_values(idx)
    key = normalize_name_key(model_name)
    for v in col[1:]:
        if v and normalize_name_key(v) == key:
            return True
    return False


# =====================
# DRIVE UPLOAD (через Service Account, без RefreshError)
# =====================
def normalize_filename(shoot_date_ddmmyyyy: str, shoot_time: str, model_name: str, phone: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", model_name.strip()).strip("_")
    safe_phone = re.sub(r"[^0-9]+", "", phone.strip())
    safe_time = shoot_time.replace(":", "-")
    safe_date = shoot_date_ddmmyyyy.replace(".", "-")
    return f"{safe_date}_{safe_time}_{safe_name}_{safe_phone}.jpg"

async def upload_photo_to_drive(bot: Bot, file_id: str, filename: str) -> str:
    if not DRIVE_FOLDER_ID:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID is empty")

    drive = drive_service()

    tg_file = await bot.get_file(file_id)
    file_bytes = await bot.download_file(tg_file.file_path)
    data = file_bytes.read()

    media = MediaInMemoryUpload(data, mimetype="image/jpeg", resumable=False)
    metadata = {"name": filename, "parents": [DRIVE_FOLDER_ID]}

    created = drive.files().create(
        body=metadata,
        media_body=media,
        fields="id, webViewLink"
    ).execute()

    file_id_drive = created["id"]

    # Робимо доступ "anyone with link can view" (щоб твій генератор релізів завжди міг стягнути фото)
    drive.permissions().create(
        fileId=file_id_drive,
        body={"role": "reader", "type": "anyone"},
    ).execute()

    # Після permissions webViewLink інколи не оновлюється, але зазвичай ок.
    # Підстрахуємось — прочитаємо link ще раз:
    meta2 = drive.files().get(fileId=file_id_drive, fields="webViewLink").execute()
    return meta2.get("webViewLink") or created.get("webViewLink") or ""


# =====================
# MANAGER NOTIFICATIONS
# =====================
def status_norm(s: str) -> str:
    return (s or "").strip().lower()

async def notifier_loop(bot: Bot):
    """
    Кожну хвилину проходиться по вкладках-датах,
    якщо Status = approved/rejected і NotifiedAt порожнє,
    відправляє повідомлення користувачу і пише NotifiedAt.
    """
    if not SHEET_ID:
        return

    while True:
        try:
            gc = sheets_client()
            sh = gc.open_by_key(SHEET_ID)

            for d in DATES:
                tab = mmddyyyy_tab_name(ddmmyyyy_to_mmddyyyy(d))
                try:
                    ws = sh.worksheet(tab)
                except Exception:
                    continue

                values = ws.get_all_values()
                if not values or len(values) < 2:
                    continue

                header = values[0]
                def hidx(name):
                    try:
                        return header.index(name)
                    except ValueError:
                        return -1

                idx_chat = hidx("TelegramChatId")
                idx_status = hidx("Status")
                idx_notif = hidx("NotifiedAt")
                idx_name = hidx("ModelName")
                idx_time = hidx("ShootTime")

                if idx_chat < 0 or idx_status < 0 or idx_notif < 0:
                    continue

                for r_i in range(1, len(values)):
                    row = values[r_i]
                    chat_id = safe_get(row, idx_chat, "").strip()
                    st = status_norm(safe_get(row, idx_status, ""))
                    notified = safe_get(row, idx_notif, "").strip()

                    if not chat_id or notified:
                        continue

                    if st not in {"approved", "rejected"}:
                        continue

                    model_name = safe_get(row, idx_name, "your application").strip()
                    shoot_time = safe_get(row, idx_time, "").strip()

                    if st == "approved":
                        msg = (
                            "Привіт! 💛\n\n"
                            f"Є оновлення по заявці: {model_name}\n"
                            "Статус: Погоджено ✅\n\n"
                            "Деталі по локації та точному місцю ми надішлемо ближче до зйомки.\n"
                        )
                        if shoot_time:
                            msg += f"\nОрієнтовний час (який ви обрали): {shoot_time}"
                    else:
                        msg = (
                            "Привіт! 💛\n\n"
                            f"Є оновлення по заявці: {model_name}\n"
                            "Статус: На жаль, цього разу не підійшло ❌\n\n"
                            "Дякуємо, що подались! Будемо раді бачити вас в наступних зйомках 😊"
                        )

                    try:
                        await bot.send_message(chat_id, msg)
                        # записуємо NotifiedAt
                        cell = gspread.utils.rowcol_to_a1(r_i + 1, idx_notif + 1)
                        ws.update_acell(cell, utc_now_iso())
                    except Exception:
                        # якщо Telegram не доставив — не ламаємо цикл
                        pass

        except Exception:
            pass

        await asyncio.sleep(60)


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
    text = message.text.strip()
    if not is_en(text):
        await message.answer("Трошки не так 🙂 Введіть, будь ласка, англійською. Приклад: Anna Ivanova")
        return

    # перевірка дублікатів на дату
    data = await state.get_data()
    shoot_date_mmddyyyy = ddmmyyyy_to_mmddyyyy(data["shoot_date"])
    tab = mmddyyyy_tab_name(shoot_date_mmddyyyy)

    try:
        gc = sheets_client()
        sh = gc.open_by_key(SHEET_ID)
        ws = ensure_sheet_tab_and_headers(sh, tab)
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

    # як ти хотіла: якщо ДАЛІ — нічого більше по адресі/місту не питаємо
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
    for k in ["shoot_date", "shoot_time", "model_name", "phone"]:
        if not data.get(k):
            await message.answer("Ой 🙈 анкета перервалася. Почнемо спочатку: /start")
            await state.clear()
            return

    filename = normalize_filename(data["shoot_date"], data["shoot_time"], data["model_name"], data["phone"])
    await message.answer("Дякую! 💛 Завантажую фото…")

    try:
        drive_url = await upload_photo_to_drive(bot, file_id, filename)
        if not drive_url:
            raise RuntimeError("Drive link is empty")
    except Exception as e:
        await message.answer(
            "Не вдалося завантажити фото в Google Drive 😔\n"
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

    required = ["shoot_date", "shoot_time", "model_name", "dob", "phone", "email", "photo_drive_url"]
    for k in required:
        if not data.get(k):
            await call.message.answer("Форма не активна 🙈 Почнемо спочатку: /start")
            await state.clear()
            return

    shoot_date_mmddyyyy = ddmmyyyy_to_mmddyyyy(data["shoot_date"])
    tab = mmddyyyy_tab_name(shoot_date_mmddyyyy)

    # готуємо рядок
    guardian = (data.get("guardian_name") or "").strip()
    city_val = (data.get("city") or "").strip()

    row_base = [
        NAMEPRINT_CONST,
        shoot_date_mmddyyyy,            # DateSigned = day of shoot
        shoot_date_mmddyyyy,            # ShootDate = same
        SHOOTPLACE_CONST,
        SHOOTSTATE_CONST,
        data["model_name"].strip(),
        data["dob"].strip(),
        (data.get("residence_address") or "").strip(),
        city_val,
        "",                             # State (не питаємо)
        COUNTRY_CONST,
        "",                             # ZipCode (не питаємо)
        data["phone"].strip(),
        data["email"].strip(),
        guardian,
        shoot_date_mmddyyyy,            # DateSigneded
        data["photo_drive_url"].strip(),
    ]

    shoot_time = data["shoot_time"].strip()
    chat_id = str(call.from_user.id)
    status = ""        # менеджер вручну ставить approved / rejected
    notified_at = ""   # бот заповнить сам

    row = row_base + [shoot_time, chat_id, status, notified_at]

    # записуємо в Sheets
    gc = sheets_client()
    sh = gc.open_by_key(SHEET_ID)
    ws = ensure_sheet_tab_and_headers(sh, tab)

    # дублікати
    if model_exists_in_tab(ws, data["model_name"]):
        await call.message.answer(
            "Схоже, ця людина вже є у списку на цю дату 🙂\n"
            "Якщо це інша людина з таким самим ім’ям — подайте ще раз з middle name/ініціалом.\n\n"
            "Натисніть: Подати ще одну людину",
            reply_markup=kb_more()
        )
        await state.clear()
        return

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
# MAIN
# =====================
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty")
    if not SHEET_ID:
        raise RuntimeError("GOOGLE_SHEET_ID is empty")
    if not os.path.exists(SA_JSON):
        raise RuntimeError("service_account.json not found in project folder")
    if not DRIVE_FOLDER_ID:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID is empty")

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

    # менеджерські нотифікації
    asyncio.create_task(notifier_loop(bot))

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
