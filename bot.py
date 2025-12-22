import os
import re
import io
import asyncio
import datetime as dt
from typing import Dict, Any, Optional, List

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload


# =========================
# CONFIG (редагуй тут, якщо треба)
# =========================

# Дні зйомок (вкладки у Google Sheet) — як ти просила: "10.01.2026" тощо
SHOOT_DATES = [
    "10.01.2026",
    "11.01.2026",
    "13.01.2026",
    "14.01.2026",
    "17.01.2026",
    "18.01.2026",
    "20.01.2026",
    "21.01.2026",
]

# Тайм-слоти
SHOOT_TIMES = ["10:20", "11:00", "11:40", "12:30", "13:20"]

# Константи для релізів
NAMEPRINT_CONST = "Stanislav Maspanov"
SHOOTPLACE_CONST = "Ukraine"
SHOOTSTATE_CONST = "Kyiv"

# Статуси, які менеджер може виставляти в таблиці
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

# Як часто перевіряти таблицю на нові апруви/реджекти (сек)
POLL_SECONDS = 30


# =========================
# ENV + Google clients
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()  # можна лишити пустим, тоді фото не вантажимо
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is empty in .env / Railway Variables")
if not GOOGLE_SHEET_ID:
    raise RuntimeError("GOOGLE_SHEET_ID is empty in .env / Railway Variables")

# Права для Sheets + Drive
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

if not os.path.exists(GOOGLE_SERVICE_ACCOUNT_JSON):
    raise RuntimeError(f"{GOOGLE_SERVICE_ACCOUNT_JSON} not found in project folder")

creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES)
gc = gspread.authorize(creds)
sheets_doc = gc.open_by_key(GOOGLE_SHEET_ID)

drive = build("drive", "v3", credentials=creds, cache_discovery=False)


# =========================
# Helpers
# =========================

def ua_date_to_mmddyyyy(dotted: str) -> str:
    # "17.05.1994" -> "05/17/1994"
    m = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*", dotted)
    if not m:
        raise ValueError("bad date")
    dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{mm:02d}/{dd:02d}/{yyyy:04d}"

def shootdate_to_mmddyyyy(dotted: str) -> str:
    # вкладки у нас "10.01.2026" -> "01/10/2026"
    return ua_date_to_mmddyyyy(dotted)

def now_iso() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def only_english(text: str) -> bool:
    # дозволяємо латиницю, пробіли, дефіси, апостроф, крапки, коми
    return bool(re.fullmatch(r"[A-Za-z0-9\s\-\.'\,/]+", text.strip()))

def clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())

def is_phone_ua(s: str) -> bool:
    return bool(re.fullmatch(r"380\d{9}", s.strip()))

def ensure_tab_exists(tab_name: str):
    try:
        sheets_doc.worksheet(tab_name)
        return
    except Exception:
        sheets_doc.add_worksheet(title=tab_name, rows=2000, cols=40)

def ensure_headers(tab_name: str, required_headers: List[str]):
    ws = sheets_doc.worksheet(tab_name)
    row1 = ws.row_values(1)
    if not row1:
        ws.update("A1", [required_headers])
        return

    # якщо колонки частково є — додаємо відсутні в кінець
    existing = [h.strip() for h in row1]
    to_add = [h for h in required_headers if h not in existing]
    if to_add:
        new_headers = existing + to_add
        ws.update("A1", [new_headers])

def ensure_all_tabs_and_headers():
    # базовий набір колонок релізу + наші службові
    headers = [
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
        # Нові, як ти просила:
        "TelegramChatId",
        "Status",
        "NotifiedAt",
    ]

    for d in SHOOT_DATES:
        ensure_tab_exists(d)
        ensure_headers(d, headers)

def append_row(tab_name: str, row: Dict[str, Any]):
    ws = sheets_doc.worksheet(tab_name)
    headers = ws.row_values(1)
    # підстраховка: якщо раптом заголовки не ті
    if not headers:
        ensure_all_tabs_and_headers()
        headers = ws.row_values(1)

    values = []
    for h in headers:
        values.append(row.get(h, ""))

    ws.append_row(values, value_input_option="USER_ENTERED")

def find_duplicate_name(tab_name: str, model_name: str) -> bool:
    ws = sheets_doc.worksheet(tab_name)
    headers = ws.row_values(1)
    if not headers or "ModelName" not in headers:
        return False
    col = headers.index("ModelName") + 1
    col_vals = ws.col_values(col)[1:]  # без заголовка
    norm = model_name.strip().lower()
    return any((v or "").strip().lower() == norm for v in col_vals)

def upload_photo_to_drive(file_bytes: bytes, filename: str) -> str:
    if not GOOGLE_DRIVE_FOLDER_ID:
        return ""  # фото не вантажимо
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="image/jpeg", resumable=False)
    body = {"name": filename, "parents": [GOOGLE_DRIVE_FOLDER_ID]}
    created = drive.files().create(body=body, media_body=media, fields="id").execute()
    return created.get("id", "")

def set_cell(ws, row_idx: int, col_name: str, value: str):
    headers = ws.row_values(1)
    if col_name not in headers:
        return
    col_idx = headers.index(col_name) + 1
    ws.update_cell(row_idx, col_idx, value)

def get_col(ws, col_name: str) -> Optional[int]:
    headers = ws.row_values(1)
    if not headers or col_name not in headers:
        return None
    return headers.index(col_name) + 1


# =========================
# Bot state (простий словник по chat_id)
# =========================

FORM: Dict[int, Dict[str, Any]] = {}

def reset_form(chat_id: int):
    FORM[chat_id] = {
        "ShootDateHuman": "",
        "ShootTime": "",
        "ModelName": "",
        "DateOfBirth": "",
        "Phone": "",
        "Email": "",
        "ResidenceAddress": "",
        "City": "",
        "ZipCode": "",
        "GuardianName": "",
        "PhotoFileId": "",
        "PhotoDriveId": "",
        "SkipAddress": False,
    }

def kb_start():
    rb = ReplyKeyboardBuilder()
    rb.button(text="📝 Подати заявку на зйомку")
    rb.adjust(1)
    return rb.as_markup(resize_keyboard=True)

def ikb_dates():
    kb = InlineKeyboardBuilder()
    for d in SHOOT_DATES:
        kb.button(text=d, callback_data=f"date:{d}")
    kb.adjust(2)
    return kb.as_markup()

def ikb_times():
    kb = InlineKeyboardBuilder()
    for t in SHOOT_TIMES:
        kb.button(text=t, callback_data=f"time:{t}")
    kb.adjust(3)
    return kb.as_markup()

def rb_next_only():
    rb = ReplyKeyboardBuilder()
    rb.button(text="ДАЛІ")
    rb.adjust(1)
    return rb.as_markup(resize_keyboard=True)

def rb_submit_more():
    rb = ReplyKeyboardBuilder()
    rb.button(text="➕ Подати ще одну людину")
    rb.button(text="✅ Готово")
    rb.adjust(1)
    return rb.as_markup(resize_keyboard=True)


# =========================
# Aiogram setup
# =========================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# =========================
# Handlers
# =========================

@dp.message(CommandStart())
async def start(m: Message):
    reset_form(m.chat.id)
    await m.answer(
        "Привіт 💛\n"
        "Я допоможу подати заявку на зйомку.\n\n"
        "Натисніть кнопку нижче, щоб почати 👇",
        reply_markup=kb_start()
    )

@dp.message(F.text == "📝 Подати заявку на зйомку")
async def apply_start(m: Message):
    reset_form(m.chat.id)
    await m.answer(
        "Супер 😊\n"
        "Оберіть, будь ласка, дату зйомки (кожен день — окрема вкладка в таблиці):",
        reply_markup=ikb_dates()
    )

@dp.callback_query(F.data.startswith("date:"))
async def pick_date(cq: CallbackQuery):
    d = cq.data.split(":", 1)[1]
    FORM.setdefault(cq.message.chat.id, {})
    FORM[cq.message.chat.id]["ShootDateHuman"] = d
    await cq.message.answer(
        f"Чудово! Дата: {d}\n\nТепер оберіть час:",
        reply_markup=ikb_times()
    )
    await cq.answer()

@dp.callback_query(F.data.startswith("time:"))
async def pick_time(cq: CallbackQuery):
    t = cq.data.split(":", 1)[1]
    FORM[cq.message.chat.id]["ShootTime"] = t

    await cq.message.answer(
        "Тепер ім’я та прізвище англійською (як у закордонному паспорті).\n"
        "Приклад: Anastasiia Svitylko",
        reply_markup=None
    )
    await cq.answer()

@dp.message(F.text)
async def text_router(m: Message):
    chat_id = m.chat.id
    if chat_id not in FORM:
        reset_form(chat_id)

    data = FORM[chat_id]
    text = (m.text or "").strip()

    # якщо ще не вибрали дату/час — ігноруємо
    if not data.get("ShootDateHuman") or not data.get("ShootTime"):
        return

    # 1) ModelName
    if not data.get("ModelName"):
        if not only_english(text):
            await m.answer("Будь ласка, введіть ім’я та прізвище лише англійськими літерами 😊")
            return
        model_name = clean_spaces(text)
        # дублікати по імені у вибраній вкладці-дні
        if find_duplicate_name(data["ShootDateHuman"], model_name):
            await m.answer(
                "Здається, заявка з таким ім’ям у цей день уже є 🤍\n"
                "Будь ласка, уточніть ім’я (наприклад додайте середній ініціал) і надішліть ще раз англійською."
            )
            return
        data["ModelName"] = model_name
        await m.answer(
            "Дякую 💛\n\n"
            "Дата народження 🗓\n"
            "Введіть у форматі: день.місяць.рік\n"
            "Приклад: 05.07.1996"
        )
        return

    # 2) DateOfBirth
    if not data.get("DateOfBirth"):
        try:
            dob_mmddyyyy = ua_date_to_mmddyyyy(text)
        except Exception:
            await m.answer("Трішки не той формат 🙏 Спробуйте так: 05.07.1996")
            return
        data["DateOfBirth"] = dob_mmddyyyy
        await m.answer(
            "Супер 😊\n\n"
            "Номер телефону у форматі 380931111111 (без +, без пробілів):"
        )
        return

    # 3) Phone
    if not data.get("Phone"):
        if not is_phone_ua(text):
            await m.answer("Потрібен формат рівно так: 380931111111 🙏 Спробуйте ще раз.")
            return
        data["Phone"] = text
        await m.answer(
            "Електронна пошта ✉️\n"
            "Приклад: name@example.com"
        )
        return

    # 4) Email
    if not data.get("Email"):
        email = text.strip()
        if "@" not in email or "." not in email:
            await m.answer("Здається, email написаний з помилкою 😊 Спробуйте ще раз.")
            return
        data["Email"] = email
        await m.answer(
            "Адреса проживання 🏡\n"
            "Якщо вам комфортно — додайте, будь ласка, адресу англійською (вулиця, будинок).\n"
            "Якщо не хочете — це абсолютно ок 😊 менеджер зможе уточнити це пізніше.\n\n"
            "Напишіть адресу англійською або натисніть ДАЛІ:",
            reply_markup=rb_next_only()
        )
        return

    # 5) ResidenceAddress (optional)
    if data.get("ResidenceAddress") == "" and not data.get("City"):
        # ми ще на кроці адреси
        if text.upper() == "ДАЛІ":
            data["SkipAddress"] = True
            data["ResidenceAddress"] = ""
            # якщо адреса пропущена — не питаємо місто/індекс (як ти просила)
            data["City"] = ""
            data["ZipCode"] = ""
            await m.answer(
                "Добре 💛\n\n"
                "І ще одне питання: ім’я та прізвище опікуна (якщо модель неповнолітня).\n"
                "Якщо повнолітня — напишіть: NONE"
            )
            return

        if not only_english(text):
            await m.answer("Адресу, будь ласка, англійською 😊 Або натисніть ДАЛІ.")
            return
        data["ResidenceAddress"] = clean_spaces(text)
        # якщо адреса є — питаємо лише місто (як ти просила), без області
        await m.answer(
            "Дякую 💛\n\nМісто проживання англійською.\nПриклад: Kyiv"
        )
        return

    # 6) City (тільки якщо адресу ввели)
    if data.get("ResidenceAddress") and not data.get("City"):
        if not only_english(text):
            await m.answer("Місто, будь ласка, англійською 😊 Приклад: Kyiv")
            return
        data["City"] = clean_spaces(text)
        await m.answer(
            "Поштовий індекс (Zip Code) — якщо маєте.\n"
            "Якщо не знаєте — напишіть: NONE"
        )
        return

    # 7) ZipCode (тільки якщо адресу ввели)
    if data.get("ResidenceAddress") and not data.get("ZipCode"):
        z = text.strip()
        if z.upper() == "NONE":
            z = ""
        data["ZipCode"] = z
        await m.answer(
            "Ім’я та прізвище опікуна (якщо модель неповнолітня).\n"
            "Якщо повнолітня — напишіть: NONE"
        )
        return

    # 8) GuardianName
    if not data.get("GuardianName"):
        g = clean_spaces(text)
        if g.upper() == "NONE":
            g = ""
        else:
            if not only_english(g):
                await m.answer("Опікуна, будь ласка, англійською 😊 Або NONE")
                return
        data["GuardianName"] = g
        await m.answer(
            "Останній крок 📸\n"
            "Надішліть, будь ласка, портретне фото (селфі або портрет), без фільтрів бажано 😊"
        )
        return

    # якщо ми вже попросили фото — текст ігноруємо
    return


@dp.message(F.photo)
async def got_photo(m: Message):
    chat_id = m.chat.id
    if chat_id not in FORM:
        reset_form(chat_id)
    data = FORM[chat_id]

    if not data.get("GuardianName"):
        await m.answer("Спочатку відповімо на питання вище 😊")
        return

    # беремо найбільший розмір фото
    ph = m.photo[-1]
    file = await bot.get_file(ph.file_id)
    file_bytes = await bot.download_file(file.file_path)

    # upload to Drive (optional)
    drive_id = ""
    try:
        drive_id = upload_photo_to_drive(file_bytes.read(), f"{data['ModelName'].replace(' ', '_')}.jpg")
    except Exception:
        drive_id = ""

    data["PhotoDriveId"] = drive_id

    # готуємо рядок для Google Sheet
    shoot_date_tab = data["ShootDateHuman"]
    shoot_mmddyyyy = shootdate_to_mmddyyyy(shoot_date_tab)

    row = {
        "Nameprint": NAMEPRINT_CONST,
        "DateSigned": shoot_mmddyyyy,       # як ти просила: DateSigned = ShootDate (день зйомки)
        "ShootDate": shoot_mmddyyyy,
        "ShootPlace": SHOOTPLACE_CONST,
        "ShootState": SHOOTSTATE_CONST,
        "ModelName": data["ModelName"],
        "DateOfBirth": data["DateOfBirth"],  # уже у MM/DD/YYYY
        "ResidenceAddress": data["ResidenceAddress"],
        "City": data["City"],
        "State": "",                         # ми не питаємо область
        "Country": "Ukraine",
        "ZipCode": data["ZipCode"],
        "Phone": data["Phone"],
        "Email": data["Email"],
        "GuardianName": data["GuardianName"],
        "DateSigneded": shoot_mmddyyyy,
        "Photo": drive_id,                   # тут збережемо Drive fileId (або пусто)
        "TelegramChatId": str(chat_id),
        "Status": "",                        # менеджер поставить approved/rejected
        "NotifiedAt": "",                    # бот заповнить коли повідомить
    }

    # гарантуємо вкладки + заголовки перед записом
    ensure_all_tabs_and_headers()
    append_row(shoot_date_tab, row)

    await m.answer(
        "Дякуємо! 💛 Ваша заявка успішно надіслана.\n\n"
        "Менеджер опрацьовує списки ближче до дати зйомки.\n"
        "Інформація по локації та деталях буде надіслана ближче до зйомки.\n"
        "На майданчику вас зустріне адміністратор і підкаже все необхідне 😊\n\n"
        "Хочете подати ще одну людину?",
        reply_markup=rb_submit_more()
    )

    # підготувати форму на наступну людину (але не стартувати автоматом)
    reset_form(chat_id)
    # залишимо дату/час пустими, щоб вона знов натиснула "Подати заявку"


@dp.message(F.text == "➕ Подати ще одну людину")
async def submit_more(m: Message):
    reset_form(m.chat.id)
    await m.answer("Супер 😊 Оберіть дату зйомки:", reply_markup=ikb_dates())

@dp.message(F.text == "✅ Готово")
async def done(m: Message):
    reset_form(m.chat.id)
    await m.answer("Домовились 💛 Гарного дня!", reply_markup=kb_start())


# =========================
# Status polling (менеджер ставить Status у таблиці)
# =========================

async def poll_status_changes():
    await asyncio.sleep(3)
    while True:
        try:
            ensure_all_tabs_and_headers()

            for tab in SHOOT_DATES:
                ws = sheets_doc.worksheet(tab)

                col_status = get_col(ws, "Status")
                col_notified = get_col(ws, "NotifiedAt")
                col_chat = get_col(ws, "TelegramChatId")

                if not col_status or not col_notified or not col_chat:
                    continue

                statuses = ws.col_values(col_status)[1:]
                notified = ws.col_values(col_notified)[1:]
                chats = ws.col_values(col_chat)[1:]

                # рядки в таблиці починаються з 2 (бо 1 — заголовок)
                for i, status in enumerate(statuses, start=2):
                    st = (status or "").strip().lower()
                    if st not in (STATUS_APPROVED, STATUS_REJECTED):
                        continue

                    already = (notified[i - 2] or "").strip()
                    if already:
                        continue

                    chat_id_str = (chats[i - 2] or "").strip()
                    if not chat_id_str.isdigit():
                        set_cell(ws, i, "NotifiedAt", now_iso())
                        continue

                    chat_id = int(chat_id_str)

                    if st == STATUS_APPROVED:
                        text = (
                            "Є хороші новини 💛\n"
                            "Ваша заявка погоджена ✅\n\n"
                            "Локацію та деталі менеджер надішле ближче до зйомки."
                        )
                    else:
                        text = (
                            "Дякуємо за заявку 💛\n"
                            "На жаль, цього разу не виходить ❌\n\n"
                            "Будемо раді бачити вас у наступних зйомках 😊"
                        )

                    try:
                        await bot.send_message(chat_id, text)
                    except Exception:
                        pass

                    set_cell(ws, i, "NotifiedAt", now_iso())

        except Exception:
            # не валимо бота через тимчасові помилки API
            pass

        await asyncio.sleep(POLL_SECONDS)


# =========================
# MAIN
# =========================

async def main():
    # 1) на старті створюємо вкладки і заголовки (з новими колонками)
    ensure_all_tabs_and_headers()

    # 2) запускаємо фон-перевірку статусів
    asyncio.create_task(poll_status_changes())

    # 3) стартуємо бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
