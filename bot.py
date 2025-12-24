import os
import re
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
from google.oauth2.credentials import Credentials as UserCredentials
from google.auth.transport.requests import Request


# =====================
# ENV
# =====================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
SA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json")


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

# Заголовки в Google Sheet (кожен день = окрема вкладка)
# ДОДАЛИ: ShootTime, TelegramChatId, Status, NotifiedAt
HEADER = [
    "Nameprint",
    "DateSigned",
    "ShootDate",
    "ShootPlace",
    "ShootState",
    "ShootTime",
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

def missing_required(data: dict, keys: list[str]) -> bool:
    return any(k not in data or data.get(k) is None for k in keys)

def now_iso_utc() -> str:
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
# GOOGLE AUTH
# =====================
def sheets_service_creds():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return ServiceAccountCredentials.from_service_account_file(SA_JSON, scopes=scopes)

def drive_user_creds():
    scopes = ["https://www.googleapis.com/auth/drive.file"]
    creds = UserCredentials.from_authorized_user_file("token_drive.json", scopes)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open("token_drive.json", "w") as f:
            f.write(creds.to_json())
    return creds

def ensure_sheet_tab(gc: gspread.Client, sheet_id: str, shoot_date_mmddyyyy: str):
    sh = gc.open_by_key(sheet_id)
    tab = mmddyyyy_tab_name(shoot_date_mmddyyyy)
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=1000, cols=60)
        ws.append_row(HEADER)
        return ws

    # якщо вкладка вже є, але заголовки старі — додамо колонки, яких не вистачає
    header_row = ws.row_values(1)
    if header_row:
        missing = [h for h in HEADER if h not in header_row]
        if missing:
            # додати справа від існуючих
            ws.update_cell(1, len(header_row) + 1, missing[0])
            if len(missing) > 1:
                ws.update(f"{gspread.utils.rowcol_to_a1(1, len(header_row) + 1)}:{gspread.utils.rowcol_to_a1(1, len(header_row) + len(missing))}", [missing])

            # переставимо заголовок рівно як HEADER (аккуратно)
            header_row = ws.row_values(1)
    # В ідеалі: просто перезаписати рядок 1 на HEADER (безпечніше для майбутніх мапінгів)
    ws.update("1:1", [HEADER])
    return ws

def model_exists_in_tab(ws, model_name: str) -> bool:
    try:
        # ModelName = 7 колонка за нашим HEADER
        col = ws.col_values(7)
    except Exception:
        return False
    key = normalize_name_key(model_name)
    for v in col[1:]:
        if v and normalize_name_key(v) == key:
            return True
    return False


# =====================
# DRIVE UPLOAD
# =====================
def normalize_filename(shoot_date_ddmmyyyy: str, shoot_time: str, model_name: str, phone: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", model_name.strip()).strip("_")
    safe_phone = re.sub(r"[^0-9]+", "", phone.strip())
    safe_time = shoot_time.replace(":", "-")
    safe_date = shoot_date_ddmmyyyy.replace(".", "-")
    return f"{safe_date}_{safe_time}_{safe_name}_{safe_phone}.jpg"

async def upload_photo_to_drive(bot: Bot, file_id: str, filename: str) -> str:
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
# STATUS CHECKER (Manager approves in Sheet -> bot notifies user)
# =====================
APPROVED = {"approved", "approve", "ok", "yes"}
REJECTED = {"rejected", "reject", "no"}

def header_index_map(header_row: list[str]) -> dict[str, int]:
    # повертає індекси (1-based як у gspread)
    m = {}
    for i, h in enumerate(header_row, start=1):
        if h:
            m[h.strip()] = i
    return m

async def status_watcher(bot: Bot, interval_sec: int = 25):
    """
    Кожні interval_sec:
      - проходить по вкладках з датами
      - шукає рядки де Status = approved/rejected і NotifiedAt пустий
      - шле повідомлення в TelegramChatId і ставить NotifiedAt
    """
    await asyncio.sleep(3)  # щоб бот встиг стартанути
    while True:
        try:
            gc = gspread.authorize(sheets_service_creds())
            sh = gc.open_by_key(SHEET_ID)

            for d in DATES:
                shoot_date_mmddyyyy = ddmmyyyy_to_mmddyyyy(d)
                tab = mmddyyyy_tab_name(shoot_date_mmddyyyy)
                try:
                    ws = sh.worksheet(tab)
                except Exception:
                    continue

                values = ws.get_all_values()
                if not values or len(values) < 2:
                    continue

                hdr = values[0]
                idx = header_index_map(hdr)

                # потрібні колонки
                if "TelegramChatId" not in idx or "Status" not in idx or "NotifiedAt" not in idx:
                    # спробуємо вирівняти заголовок
                    ws.update("1:1", [HEADER])
                    values = ws.get_all_values()
                    if not values or len(values) < 2:
                        continue
                    hdr = values[0]
                    idx = header_index_map(hdr)
                    if "TelegramChatId" not in idx or "Status" not in idx or "NotifiedAt" not in idx:
                        continue

                chat_col = idx["TelegramChatId"]
                status_col = idx["Status"]
                notified_col = idx["NotifiedAt"]
                name_col = idx.get("ModelName", 7)
                time_col = idx.get("ShootTime", 6)

                # rows start from 2
                for r_i in range(2, len(values) + 1):
                    row = values[r_i - 1]
                    # safe getters
                    def getc(c):
                        return row[c - 1].strip() if c - 1 < len(row) and row[c - 1] else ""

                    chat_id = getc(chat_col)
                    status_raw = getc(status_col).lower()
                    notified_at = getc(notified_col)

                    if not chat_id:
                        continue
                    if notified_at:
                        continue

                    # only act on approved/rejected
                    verdict = None
                    if status_raw in APPROVED:
                        verdict = "approved"
                    elif status_raw in REJECTED:
                        verdict = "rejected"
                    else:
                        continue

                    model_name = getc(name_col) or "your application"
                    shoot_time = getc(time_col)
                    shoot_date_human = d

                    if verdict == "approved":
                        text = (
                            "Є новини по вашій заявці 💛\n\n"
                            f"Статус: Погоджено ✅\n"
                            f"Дата: {shoot_date_human}\n"
                            f"Час: {shoot_time or '—'}\n"
                            f"Імʼя: {model_name}\n\n"
                            "Деталі по локації та зустрічі ми надішлемо ближче до зйомки."
                        )
                    else:
                        text = (
                            "Є новини по вашій заявці 💛\n\n"
                            f"Статус: На жаль, не погоджено ❌\n"
                            f"Дата: {shoot_date_human}\n"
                            f"Час: {shoot_time or '—'}\n"
                            f"Імʼя: {model_name}\n\n"
                            "Дякуємо за заявку! Можна податись ще на інший день."
                        )

                    try:
                        await bot.send_message(int(chat_id), text)
                        ws.update_cell(r_i, notified_col, now_iso_utc())
                    except Exception:
                        # якщо не змогли надіслати — не ставимо NotifiedAt
                        pass

        except Exception:
            # не валимо бота через проблеми з Google — просто повторимо
            pass

        await asyncio.sleep(interval_sec)


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

    # Якщо людина пише ДАЛІ — пропускаємо адресу і місто
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
    if missing_required(data, required):
        await call.message.answer("Форма не активна 🙈 Почнемо спочатку: /start")
        await state.clear()
        return

    shoot_date_mmddyyyy = ddmmyyyy_to_mmddyyyy(data["shoot_date"])
    guardian = (data.get("guardian_name") or "").strip()
    city_val = (data.get("city") or "").strip()

    gc = gspread.authorize(sheets_service_creds())
    ws = ensure_sheet_tab(gc, SHEET_ID, shoot_date_mmddyyyy)

    # Перевірка повтору імені
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
        NAMEPRINT_CONST,
        shoot_date_mmddyyyy,       # DateSigned = ShootDate (як ти просила)
        shoot_date_mmddyyyy,       # ShootDate
        SHOOTPLACE_CONST,
        SHOOTSTATE_CONST,
        data["shoot_time"].strip(),  # ShootTime
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
        shoot_date_mmddyyyy,       # DateSigneded
        data["photo_drive_url"].strip(),
        chat_id,                   # TelegramChatId
        "pending",                 # Status
        "",                        # NotifiedAt
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
# MAIN
# =====================
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty")
    if not SHEET_ID:
        raise RuntimeError("GOOGLE_SHEET_ID is empty")
    if not DRIVE_FOLDER_ID:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID is empty")
    if not os.path.exists("service_account.json"):
        raise RuntimeError("service_account.json not found in project folder")
    if not os.path.exists("token_drive.json"):
        raise RuntimeError("token_drive.json not found")

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

    # Запускаємо фоновий чекер статусів
    asyncio.create_task(status_watcher(bot, interval_sec=25))

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
