import os
import re
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

import gspread
from google.oauth2.service_account import Credentials

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError


# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()  # optional but recommended
SERVICE_ACCOUNT_PATH = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json").strip()

# Fixed values for release sheet
FIXED_NAMEPRINT = "Stanislav Maspanov"
FIXED_SHOOTPLACE = "Ukraine"
FIXED_SHOOTSTATE = "Kyiv"

# Shoot dates + time slots (user-facing)
SHOOT_DATES = [
    "10.01.2026", "11.01.2026", "13.01.2026", "14.01.2026",
    "17.01.2026", "18.01.2026", "20.01.2026", "21.01.2026",
]
TIME_SLOTS = ["10:20", "11:00", "11:40", "12:30", "13:20"]

# Sheet columns (per tab/day)
HEADERS = [
    "Nameprint", "DateSigned", "ShootDate", "ShootPlace", "ShootState",
    "ModelName", "DateOfBirth", "ResidenceAddress", "City", "State", "Country",
    "ZipCode", "Phone", "Email", "GuardianName", "DateSigneded", "Photo",
    "TelegramChatId", "Status", "NotifiedAt",
]

STATUS_VALUES = ["pending", "approved", "rejected"]  # in sheet we keep simple + stable


# =========================
# HELPERS: validation + formatting
# =========================

ENGLISH_RE = re.compile(r"^[A-Za-z0-9\s\-\.'(),/]+$")  # allow basic punctuation
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^380\d{9}$")  # 380 + 9 digits

def is_english_like(s: str) -> bool:
    s = s.strip()
    return bool(s) and bool(ENGLISH_RE.match(s))

def to_mmddyyyy(ddmmyyyy: str) -> str:
    # dd.mm.yyyy -> mm/dd/yyyy
    dd, mm, yyyy = ddmmyyyy.split(".")
    return f"{mm}/{dd}/{yyyy}"

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# =========================
# Google clients
# =========================

def get_gspread_client() -> gspread.Client:
    if not os.path.exists(SERVICE_ACCOUNT_PATH):
        raise RuntimeError(f"service account file not found: {SERVICE_ACCOUNT_PATH}")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_PATH, scopes=scopes)
    return gspread.authorize(creds)

def get_sheets_service():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_PATH, scopes=scopes)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)

def get_drive_service():
    # uses same service account (simpler + stable for Railway)
    scopes = [
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_PATH, scopes=scopes)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# =========================
# Sheet setup: tabs + headers + dropdown validation
# =========================

def ensure_tab_and_headers(gclient: gspread.Client, sheets_service, tab_name: str) -> None:
    sh = gclient.open_by_key(GOOGLE_SHEET_ID)

    # Create tab if missing
    try:
        ws = sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=2000, cols=max(30, len(HEADERS) + 5))
        # Freeze header row
        ws.freeze(rows=1)

    # Ensure headers (row 1)
    current = ws.row_values(1)
    if current != HEADERS:
        ws.resize(rows=max(ws.row_count, 2000), cols=max(ws.col_count, len(HEADERS) + 2))
        ws.update("A1", [HEADERS])
        ws.freeze(rows=1)

    # Ensure dropdown for Status column (data validation)
    try:
        # Find sheetId + Status column index
        spreadsheet = sheets_service.spreadsheets().get(
            spreadsheetId=GOOGLE_SHEET_ID
        ).execute()
        sheet_id = None
        for s in spreadsheet.get("sheets", []):
            props = s.get("properties", {})
            if props.get("title") == tab_name:
                sheet_id = props.get("sheetId")
                break
        if sheet_id is None:
            return

        status_col_index = HEADERS.index("Status")  # 0-based
        # Apply validation to rows 2..2000 in that column
        req = {
            "requests": [{
                "setDataValidation": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": 2000,
                        "startColumnIndex": status_col_index,
                        "endColumnIndex": status_col_index + 1,
                    },
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_LIST",
                            "values": [{"userEnteredValue": v} for v in ["approved", "rejected", "pending"]],
                        },
                        "strict": True,
                        "showCustomUi": True
                    }
                }
            }]
        }
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=GOOGLE_SHEET_ID,
            body=req
        ).execute()
    except Exception:
        # Don't block bot if Google API validation fails
        pass


def ensure_all_tabs_once():
    gclient = get_gspread_client()
    sheets_service = get_sheets_service()
    for d in SHOOT_DATES:
        ensure_tab_and_headers(gclient, sheets_service, d)


# =========================
# Drive upload
# =========================

async def upload_photo_to_drive(bot: Bot, file_id: str, filename: str) -> str:
    """
    Upload Telegram photo to Google Drive folder.
    Returns Drive webViewLink OR fileId if link unavailable.
    """
    if not GOOGLE_DRIVE_FOLDER_ID:
        # fallback: store Telegram file_id
        return file_id

    drive = get_drive_service()

    # download from telegram to tmp
    tg_file = await bot.get_file(file_id)
    tmp_path = f"/tmp/{filename}"
    await bot.download_file(tg_file.file_path, destination=tmp_path)

    media = MediaFileUpload(tmp_path, mimetype="image/jpeg", resumable=False)
    body = {
        "name": filename,
        "parents": [GOOGLE_DRIVE_FOLDER_ID],
    }
    created = drive.files().create(
        body=body,
        media_body=media,
        fields="id, webViewLink"
    ).execute()

    # optional: make it readable by link (comment out if you want private)
    try:
        drive.permissions().create(
            fileId=created["id"],
            body={"type": "anyone", "role": "reader"},
        ).execute()
    except Exception:
        pass

    return created.get("webViewLink") or created.get("id")


# =========================
# FSM
# =========================

class Form(StatesGroup):
    pick_date = State()
    pick_time = State()
    model_name = State()
    dob = State()
    phone = State()
    email = State()
    country = State()
    guardian = State()
    address = State()
    city = State()
    photo = State()


@dataclass
class Draft:
    shoot_date_ddmmyyyy: str = ""
    shoot_time: str = ""
    model_name: str = ""
    dob_ddmmyyyy: str = ""
    phone: str = ""
    email: str = ""
    country: str = ""
    guardian: str = ""
    address: str = ""
    city: str = ""


# =========================
# UI builders (NO persistent keyboard)
# =========================

def kb_start():
    b = InlineKeyboardBuilder()
    b.button(text="📝 Подати заявку на зйомку", callback_data="apply")
    b.button(text="ℹ️ Як це працює", callback_data="info")
    b.adjust(1)
    return b.as_markup()

def kb_dates():
    b = InlineKeyboardBuilder()
    for d in SHOOT_DATES:
        b.button(text=d, callback_data=f"date:{d}")
    b.adjust(2)
    return b.as_markup()

def kb_times():
    b = InlineKeyboardBuilder()
    for t in TIME_SLOTS:
        b.button(text=t, callback_data=f"time:{t}")
    b.button(text="⬅️ Назад до дат", callback_data="back:dates")
    b.adjust(2)
    return b.as_markup()

def kb_skip_address():
    b = InlineKeyboardBuilder()
    b.button(text="ДАЛІ", callback_data="skip:address")
    b.adjust(1)
    return b.as_markup()

def kb_restart_end():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Подати ще одну людину", callback_data="apply")
    b.button(text="✅ Завершити", callback_data="done")
    b.adjust(1)
    return b.as_markup()


# =========================
# Bot texts (nice + Ukrainian)
# =========================

WELCOME = (
    "Привіт! 💛\n\n"
    "Це бот для подачі заявки на зйомку.\n"
    "Я зберу дані для модельного релізу та допоможу обрати день і час.\n\n"
    "Натисніть кнопку нижче 👇"
)

INFO = (
    "Як це працює 💡\n\n"
    "1) Ви обираєте дату та час.\n"
    "2) Заповнюєте дані англійською (як у документі).\n"
    "3) Додаєте портретне фото.\n\n"
    "Після подачі заявки менеджер опрацює списки ближче до дати зйомки.\n"
    "Локацію та фінальні деталі ми надішлемо окремо ✅"
)

ASK_DATE = "Оберіть, будь ласка, дату зйомки 📅"
ASK_TIME = "Чудово! Тепер оберіть час 🕒"
ASK_NAME = (
    "Дякую 💛\n"
    "Тепер введіть, будь ласка, імʼя та прізвище **англійською**.\n"
    "Приклад: Ivan Petrenko"
)
ASK_DOB = (
    "Супер!\n"
    "Тепер дата народження 🗓\n"
    "Введіть у форматі: 17.05.1994"
)
ASK_PHONE = (
    "Дякую!\n"
    "Тепер номер телефону 📞\n"
    "Введіть ТІЛЬКИ цифри у форматі: 380931111111"
)
ASK_EMAIL = (
    "Чудово!\n"
    "Тепер електронна пошта ✉️\n"
    "Приклад: name@example.com"
)
ASK_COUNTRY = (
    "Дякую 💛\n"
    "Вкажіть, будь ласка, країну проживання **англійською**.\n"
    "Приклад: Ukraine"
)
ASK_GUARDIAN = (
    "Якщо заявка для дитини 👶 — вкажіть, будь ласка, імʼя та прізвище опікуна **англійською**.\n"
    "Якщо опікун не потрібен — напишіть: None"
)
ASK_ADDRESS = (
    "Тепер адреса проживання 🏡\n"
    "Якщо вам комфортно — додайте адресу **англійською** (вулиця, будинок).\n"
    "Якщо не хочете — це абсолютно ок 😊\n"
    "Натисніть «ДАЛІ», і менеджер уточнить це питання пізніше."
)
ASK_CITY = (
    "Дякую! 💛\n"
    "Тепер місто проживання **англійською**.\n"
    "Приклад: Kyiv"
)
ASK_PHOTO = (
    "Майже готово ✨\n"
    "Надішліть, будь ласка, портретне фото (селфі або портрет).\n"
    "Без фільтрів — як вам комфортно 💛"
)

FINAL_TEXT = (
    "Дякуємо! 💛 Ваша заявка успішно надіслана.\n\n"
    "Менеджер опрацьовує списки ближче до дати зйомки.\n"
    "Інформацію по локації та підтвердження ми надішлемо окремо ✅\n\n"
    "Хочете подати ще одну людину?"
)


# =========================
# Google Sheet write + name-duplicates
# =========================

def open_ws_for_date(gclient: gspread.Client, sheets_service, date_ddmmyyyy: str):
    ensure_tab_and_headers(gclient, sheets_service, date_ddmmyyyy)
    sh = gclient.open_by_key(GOOGLE_SHEET_ID)
    return sh.worksheet(date_ddmmyyyy)

def name_exists(ws: gspread.Worksheet, model_name: str) -> bool:
    try:
        # Find ModelName col index
        col = HEADERS.index("ModelName") + 1
        vals = ws.col_values(col)[1:]  # skip header
        target = model_name.strip().lower()
        return any(v.strip().lower() == target for v in vals if v)
    except Exception:
        return False

def append_row(ws: gspread.Worksheet, row: list):
    ws.append_row(row, value_input_option="USER_ENTERED")


# =========================
# Notifications loop (manager sets Status in sheet)
# =========================

async def notify_loop(bot: Bot):
    """
    Every minute checks all tabs:
      - Status == approved/rejected
      - NotifiedAt empty
      - TelegramChatId present
    Sends message and writes NotifiedAt.
    """
    await asyncio.sleep(5)  # small delay after startup
    while True:
        try:
            gclient = get_gspread_client()
            sheets_service = get_sheets_service()
            sh = gclient.open_by_key(GOOGLE_SHEET_ID)

            for tab in SHOOT_DATES:
                try:
                    ws = sh.worksheet(tab)
                except Exception:
                    continue

                # pull all rows (could be optimized later)
                rows = ws.get_all_values()
                if not rows or rows[0] != HEADERS:
                    continue

                # indices
                i_chat = HEADERS.index("TelegramChatId")
                i_status = HEADERS.index("Status")
                i_notif = HEADERS.index("NotifiedAt")
                i_time = None
                # time isn't a header, so we don't store time in a separate column; it lives in no header
                # We'll include it in message from stored shoot date only (таб-дата).
                # If you want time in sheet too later — we can add "ShootTime" column.

                updates = []
                for r_idx in range(1, len(rows)):
                    r = rows[r_idx]
                    # pad
                    if len(r) < len(HEADERS):
                        r += [""] * (len(HEADERS) - len(r))

                    chat_id = r[i_chat].strip()
                    status = r[i_status].strip().lower()
                    notified = r[i_notif].strip()

                    if not chat_id or notified:
                        continue
                    if status not in ("approved", "rejected"):
                        continue

                    # notify
                    if status == "approved":
                        text = (
                            "Привіт! 💛\n\n"
                            "Ваша заявка попередньо ПІДТВЕРДЖЕНА ✅\n"
                            "Деталі по локації та часу ми надішлемо окремо трохи ближче до зйомки.\n\n"
                            "Дякуємо!"
                        )
                    else:
                        text = (
                            "Привіт! 💛\n\n"
                            "На жаль, цього разу ми не можемо вас підтвердити ❌\n"
                            "Але будемо раді бачити вас на наступних зйомках.\n\n"
                            "Дякуємо за заявку!"
                        )

                    try:
                        await bot.send_message(chat_id=int(chat_id), text=text)
                        # write NotifiedAt in sheet
                        cell = gspread.utils.rowcol_to_a1(r_idx + 1, i_notif + 1)
                        updates.append((cell, now_iso()))
                    except Exception:
                        # ignore send failures (user blocked bot etc.)
                        pass

                if updates:
                    # batch update
                    ws.update([[v] for _, v in updates], range_name=f"{gspread.utils.rowcol_to_a1(2, i_notif+1)}:{gspread.utils.rowcol_to_a1(2000, i_notif+1)}")
                    # The above is a simple update; to be precise per-cell we do:
                    for cell, value in updates:
                        ws.update(cell, value)

        except Exception:
            pass

        await asyncio.sleep(60)


# =========================
# Handlers
# =========================

async def start_new_form(state: FSMContext):
    await state.set_state(Form.pick_date)
    await state.update_data(draft=Draft().__dict__)

def get_draft(data: dict) -> Draft:
    d = data.get("draft", {})
    return Draft(**d)

async def save_draft(state: FSMContext, draft: Draft):
    await state.update_data(draft=draft.__dict__)


async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME, reply_markup=kb_start())

async def cb_apply(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await start_new_form(state)
    await call.message.answer(ASK_DATE, reply_markup=kb_dates())

async def cb_info(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await call.message.answer(INFO, reply_markup=kb_start())

async def cb_done(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await call.message.answer("Готово 💛 Якщо захочете — просто натисніть «Подати заявку» ще раз.", reply_markup=kb_start())

async def cb_back_dates(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(Form.pick_date)
    await call.message.answer(ASK_DATE, reply_markup=kb_dates())

async def cb_pick_date(call: CallbackQuery, state: FSMContext):
    await call.answer()
    date_ddmmyyyy = call.data.split("date:", 1)[1]
    data = await state.get_data()
    draft = get_draft(data)
    draft.shoot_date_ddmmyyyy = date_ddmmyyyy
    await save_draft(state, draft)

    await state.set_state(Form.pick_time)
    await call.message.answer(f"Дата: {date_ddmmyyyy} ✅\n\n{ASK_TIME}", reply_markup=kb_times())

async def cb_pick_time(call: CallbackQuery, state: FSMContext):
    await call.answer()
    t = call.data.split("time:", 1)[1]
    data = await state.get_data()
    draft = get_draft(data)
    draft.shoot_time = t
    await save_draft(state, draft)

    await state.set_state(Form.model_name)
    await call.message.answer(f"Час: {t} ✅\n\n{ASK_NAME}")

async def on_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not is_english_like(name):
        await message.answer("Ой, схоже тут не англійською 🙈\nБудь ласка, введіть імʼя та прізвище англійською.\nПриклад: Ivan Petrenko")
        return

    data = await state.get_data()
    draft = get_draft(data)
    draft.model_name = name
    await save_draft(state, draft)

    await state.set_state(Form.dob)
    await message.answer(ASK_DOB)

async def on_dob(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", txt):
        await message.answer("Будь ласка, формат: 17.05.1994")
        return
    data = await state.get_data()
    draft = get_draft(data)
    draft.dob_ddmmyyyy = txt
    await save_draft(state, draft)

    await state.set_state(Form.phone)
    await message.answer(ASK_PHONE)

async def on_phone(message: Message, state: FSMContext):
    txt = (message.text or "").strip().replace(" ", "")
    if not PHONE_RE.match(txt):
        await message.answer("Телефон має бути тільки цифри у форматі 380931111111. Спробуйте ще раз 💛")
        return
    data = await state.get_data()
    draft = get_draft(data)
    draft.phone = txt
    await save_draft(state, draft)

    await state.set_state(Form.email)
    await message.answer(ASK_EMAIL)

async def on_email(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not EMAIL_RE.match(txt):
        await message.answer("Здається, пошта написана з помилкою 🙈\nПриклад: name@example.com")
        return
    data = await state.get_data()
    draft = get_draft(data)
    draft.email = txt
    await save_draft(state, draft)

    await state.set_state(Form.country)
    await message.answer(ASK_COUNTRY)

async def on_country(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not is_english_like(txt):
        await message.answer("Будь ласка, напишіть країну англійською. Приклад: Ukraine")
        return
    data = await state.get_data()
    draft = get_draft(data)
    draft.country = txt
    await save_draft(state, draft)

    await state.set_state(Form.guardian)
    await message.answer(ASK_GUARDIAN)

async def on_guardian(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if txt.lower() != "none" and not is_english_like(txt):
        await message.answer("Будь ласка, імʼя опікуна англійською або напишіть None")
        return

    data = await state.get_data()
    draft = get_draft(data)
    draft.guardian = txt
    await save_draft(state, draft)

    await state.set_state(Form.address)
    await message.answer(ASK_ADDRESS, reply_markup=kb_skip_address())

async def cb_skip_address(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    draft = get_draft(data)
    draft.address = ""
    draft.city = ""
    await save_draft(state, draft)

    # skip city/state/zip as requested
    await state.set_state(Form.photo)
    await call.message.answer(ASK_PHOTO)

async def on_address(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    # If user types "ДАЛІ" manually
    if txt.upper() == "ДАЛІ":
        data = await state.get_data()
        draft = get_draft(data)
        draft.address = ""
        draft.city = ""
        await save_draft(state, draft)
        await state.set_state(Form.photo)
        await message.answer(ASK_PHOTO)
        return

    if not is_english_like(txt):
        await message.answer("Будь ласка, адреса англійською 😊\nАбо натисніть «ДАЛІ».", reply_markup=kb_skip_address())
        return

    data = await state.get_data()
    draft = get_draft(data)
    draft.address = txt
    await save_draft(state, draft)

    await state.set_state(Form.city)
    await message.answer(ASK_CITY)

async def on_city(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not is_english_like(txt):
        await message.answer("Будь ласка, місто англійською. Приклад: Kyiv")
        return

    data = await state.get_data()
    draft = get_draft(data)
    draft.city = txt
    await save_draft(state, draft)

    await state.set_state(Form.photo)
    await message.answer(ASK_PHOTO)

async def on_photo(message: Message, state: FSMContext, bot: Bot):
    if not message.photo:
        await message.answer("Потрібно саме фото 🙏 Надішліть портретним фото, будь ласка.")
        return

    data = await state.get_data()
    draft = get_draft(data)

    # Ensure tabs + headers
    gclient = get_gspread_client()
    sheets_service = get_sheets_service()
    ws = open_ws_for_date(gclient, sheets_service, draft.shoot_date_ddmmyyyy)

    # block duplicates by name
    if name_exists(ws, draft.model_name):
        await message.answer(
            "Ой 🙈 Схоже, заявка з таким імʼям уже є в цей день.\n"
            "Якщо це інша людина з таким самим імʼям — додайте, будь ласка, середню літеру або друге імʼя англійською.\n"
            "Приклад: Ivan P. Petrenko\n\n"
            "Введіть імʼя ще раз:"
        )
        await state.set_state(Form.model_name)
        return

    # Upload photo
    biggest = message.photo[-1]
    file_id = biggest.file_id
    safe_name = re.sub(r"[^A-Za-z0-9_]+", "_", draft.model_name).strip("_")
    filename = f"{draft.shoot_date_ddmmyyyy}_{draft.shoot_time}_{safe_name}.jpg"
    try:
        photo_ref = await upload_photo_to_drive(bot, file_id, filename)
    except Exception:
        photo_ref = file_id  # fallback

    # Build row
    shoot_mmddyyyy = to_mmddyyyy(draft.shoot_date_ddmmyyyy)
    dob_mmddyyyy = to_mmddyyyy(draft.dob_ddmmyyyy)

    # Residence fields: if address skipped, city/state/zip empty as requested
    residence_address = draft.address
    city = draft.city if draft.address else ""
    state = ""  # residence state not asked in this flow
    zipcode = ""  # zip not asked in this flow

    row = [
        FIXED_NAMEPRINT, shoot_mmddyyyy, shoot_mmddyyyy, FIXED_SHOOTPLACE, FIXED_SHOOTSTATE,
        draft.model_name, dob_mmddyyyy, residence_address, city, state, draft.country,
        zipcode, draft.phone, draft.email, draft.guardian,
        shoot_mmddyyyy, photo_ref,
        str(message.chat.id), "pending", ""
    ]

    append_row(ws, row)

    await state.clear()
    await message.answer(FINAL_TEXT, reply_markup=kb_restart_end())


# =========================
# Main
# =========================

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty (set Railway Variable BOT_TOKEN)")
    if not GOOGLE_SHEET_ID:
        raise RuntimeError("GOOGLE_SHEET_ID is empty (set Railway Variable GOOGLE_SHEET_ID)")

    # Create tabs once on startup (safe)
    try:
        ensure_all_tabs_once()
    except Exception:
        pass

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.register(cmd_start, CommandStart())
    dp.callback_query.register(cb_apply, F.data == "apply")
    dp.callback_query.register(cb_info, F.data == "info")
    dp.callback_query.register(cb_done, F.data == "done")
    dp.callback_query.register(cb_back_dates, F.data == "back:dates")
    dp.callback_query.register(cb_pick_date, F.data.startswith("date:"))
    dp.callback_query.register(cb_pick_time, F.data.startswith("time:"))
    dp.callback_query.register(cb_skip_address, F.data == "skip:address")

    dp.message.register(on_name, Form.model_name)
    dp.message.register(on_dob, Form.dob)
    dp.message.register(on_phone, Form.phone)
    dp.message.register(on_email, Form.email)
    dp.message.register(on_country, Form.country)
    dp.message.register(on_guardian, Form.guardian)
    dp.message.register(on_address, Form.address)
    dp.message.register(on_city, Form.city)
    dp.message.register(lambda m, s: on_photo(m, s, bot), Form.photo)

    # Notifications background
    asyncio.create_task(notify_loop(bot))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
