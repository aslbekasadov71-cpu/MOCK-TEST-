"""
Test tekshiruvchi Telegram bot.
Ustoz: test yaratadi va to'g'ri javoblarni kiritadi, bot test kodini beradi.
O'quvchi: test kodi + javoblarini yuboradi, bot qaysi savol to'g'ri yoki xato ekanini ko'rsatadi.
"""
import asyncio
import html
import logging
import os
import random
import re
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

# ---------------- Sozlamalar ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DB_PATH = os.getenv("DB_PATH", "test_bot.db")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")  # masalan: https://test-bot.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "test-bot-secret")
PORT = int(os.getenv("PORT", "8080"))

logging.basicConfig(level=logging.INFO)
router = Router()


def esc(s) -> str:
    return html.escape(str(s or ""))


def is_teacher(uid: int) -> bool:
    return uid in ADMIN_IDS


# ---------------- Ma'lumotlar bazasi ----------------
async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tests(
                code TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                answers TEXT NOT NULL,
                owner_id INTEGER NOT NULL,
                is_open INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS submissions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                answers TEXT NOT NULL,
                correct INTEGER NOT NULL,
                total INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(test_code, user_id)
            );
            """
        )
        await db.commit()


async def db_exec(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.lastrowid


async def db_one(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return await cur.fetchone()


async def db_all(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return await cur.fetchall()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------------- Javoblarni o'qish va tekshirish ----------------
# Kirill harflarda yozilsa ham tushunadi (А, В, С, Д ...)
CYR = str.maketrans({"а": "a", "б": "b", "в": "b", "с": "c", "ц": "c",
                     "д": "d", "е": "e", "э": "e", "ф": "f"})
VALID = set("abcdef")


def parse_answers(text: str):
    """'abcdab...' yoki '1a 2b 3c' / '1-a, 2-b' formatlarini qabul qiladi.
    Qaytaradi: (javoblar_qatori, xato_matni)"""
    t = (text or "").lower().translate(CYR)
    if re.search(r"\d", t):
        pairs = re.findall(r"(\d+)\s*[\)\.\-:=]?\s*([a-z])", t)
        if not pairs:
            return None, "Javoblarni o'qib bo'lmadi. Masalan: <code>1a2b3c4d</code>"
        d = {int(n): letter for n, letter in pairs}
        n = max(d)
        missing = [i for i in range(1, n + 1) if i not in d]
        if missing:
            shown = ", ".join(map(str, missing[:15]))
            return None, f"Quyidagi savollarga javob yo'q: {shown}"
        res = [d[i] for i in range(1, n + 1)]
    else:
        res = [c for c in t if c.isalpha()]
    if not res or any(c not in VALID for c in res):
        return None, "Faqat A, B, C, D (E, F) harflaridan foydalaning."
    return "".join(res), None


def format_result(test, ans: str, full_name: str, show_key: bool) -> str:
    key = test["answers"]
    correct = sum(a == k for a, k in zip(ans, key))
    total = len(key)
    lines = [
        f"📝 <b>{esc(test['title'])}</b> (kod: <code>{test['code']}</code>)",
        f"👤 {esc(full_name)}",
        f"✅ To'g'ri: <b>{correct}</b> / {total}  ({correct * 100 / total:.1f}%)",
        f"❌ Xato: <b>{total - correct}</b>",
        "",
    ]
    for i, (a, k) in enumerate(zip(ans, key), 1):
        if a == k:
            lines.append(f"{i}. {a.upper()} ✅")
        else:
            tail = f"  (to'g'ri: {k.upper()})" if show_key else ""
            lines.append(f"{i}. {a.upper()} ❌{tail}")
    if not show_key:
        lines.append("\nℹ️ To'g'ri javoblar test yopilgandan keyin yuboriladi.")
    return "\n".join(lines)


async def send_long(m: Message, text: str, **kw):
    """Telegram 4096 belgidan uzun xabarni bo'lib yuboradi."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3900:
            await m.answer(chunk, **kw)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await m.answer(chunk, **kw)


# ---------------- Klaviaturalar ----------------
teacher_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="➕ Yangi test"), KeyboardButton(text="📋 Testlarim")]],
    resize_keyboard=True,
)
student_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="✍️ Javob yuborish"), KeyboardButton(text="📊 Natijalarim")]],
    resize_keyboard=True,
)


# ---------------- Holatlar ----------------
class Reg(StatesGroup):
    name = State()


class NewTest(StatesGroup):
    title = State()
    answers = State()


class Submit(StatesGroup):
    code = State()
    answers = State()


# ---------------- Umumiy buyruqlar ----------------
@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    if is_teacher(uid):
        await m.answer(
            "Assalomu alaykum, ustoz! 👋\n\n"
            "➕ <b>Yangi test</b> — test yaratib, to'g'ri javoblarni kiritish\n"
            "📋 <b>Testlarim</b> — natijalarni ko'rish va testni yopish",
            reply_markup=teacher_kb,
        )
        return
    user = await db_one("SELECT full_name FROM users WHERE id=?", (uid,))
    if user:
        await m.answer(f"Assalomu alaykum, {esc(user['full_name'])}! 👋\n"
                       "Javob yuborish uchun «✍️ Javob yuborish» tugmasini bosing.",
                       reply_markup=student_kb)
    else:
        await state.set_state(Reg.name)
        await m.answer("Assalomu alaykum! 👋\nIsm va familiyangizni yozing (masalan: <i>Aliyev Vali</i>):")


@router.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Sizning Telegram ID: <code>{m.from_user.id}</code>")


@router.message(Command("bekor"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    kb = teacher_kb if is_teacher(m.from_user.id) else student_kb
    await m.answer("Bekor qilindi.", reply_markup=kb)


@router.message(Reg.name, F.text)
async def reg_name(m: Message, state: FSMContext):
    name = " ".join(m.text.split())
    if len(name) < 3 or len(name) > 60 or name.startswith("/"):
        await m.answer("Iltimos, ism va familiyangizni to'g'ri yozing:")
        return
    await db_exec("INSERT OR REPLACE INTO users(id, full_name) VALUES(?, ?)", (m.from_user.id, name))
    data = await state.get_data()
    await state.clear()
    await m.answer(f"✅ Rahmat, {esc(name)}!", reply_markup=student_kb)
    if data.get("after_reg") == "submit":
        await ask_code(m, state)


# ---------------- USTOZ ----------------
@router.message(F.text == "➕ Yangi test")
@router.message(Command("yangi"))
async def new_test(m: Message, state: FSMContext):
    if not is_teacher(m.from_user.id):
        return
    await state.clear()
    await state.set_state(NewTest.title)
    await m.answer("Test nomini yozing (masalan: <i>9-sinf Matematika, 1-chorak</i>):\n\nBekor qilish: /bekor")


@router.message(NewTest.title, F.text)
async def new_test_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip()[:100])
    await state.set_state(NewTest.answers)
    await m.answer(
        "Endi <b>to'g'ri javoblarni</b> yuboring. Ikki xil formatda bo'ladi:\n\n"
        "1) Ketma-ket: <code>abcdabcdab</code>\n"
        "2) Raqam bilan: <code>1a 2b 3c 4d</code>\n\n"
        "Savollar soni javoblar soniga qarab aniqlanadi."
    )


@router.message(NewTest.answers, F.text)
async def new_test_answers(m: Message, state: FSMContext):
    key, err = parse_answers(m.text)
    if err:
        await m.answer(f"⚠️ {err}\nQaytadan yuboring:")
        return
    data = await state.get_data()
    while True:
        code = str(random.randint(1000, 9999))
        if not await db_one("SELECT 1 FROM tests WHERE code=?", (code,)):
            break
    await db_exec(
        "INSERT INTO tests(code, title, answers, owner_id, created_at) VALUES(?,?,?,?,?)",
        (code, data["title"], key, m.from_user.id, now()),
    )
    await state.clear()
    key_view = "  ".join(f"{i}{c.upper()}" for i, c in enumerate(key, 1))
    await m.answer(
        f"✅ Test yaratildi!\n\n"
        f"📝 <b>{esc(data['title'])}</b>\n"
        f"🔑 Test kodi: <code>{code}</code>\n"
        f"❓ Savollar soni: {len(key)}\n\n"
        f"Javoblar: {key_view}\n\n"
        f"O'quvchilar botda «✍️ Javob yuborish» tugmasini bosib, kodni kiritadi.\n"
        f"Yoki bitta xabarda shunday yuboradi:\n<code>{code}*abcd...</code>\n\n"
        f"Natijalar: /natija_{code}\nTestni yopish: /yopish_{code}",
        reply_markup=teacher_kb,
    )


@router.message(F.text == "📋 Testlarim")
@router.message(Command("testlar"))
async def my_tests(m: Message, state: FSMContext):
    if not is_teacher(m.from_user.id):
        return
    await state.clear()
    rows = await db_all(
        """SELECT t.code, t.title, t.is_open, length(t.answers) AS n,
                  (SELECT COUNT(*) FROM submissions s WHERE s.test_code = t.code) AS c
           FROM tests t WHERE t.owner_id = ? ORDER BY t.created_at DESC LIMIT 30""",
        (m.from_user.id,),
    )
    if not rows:
        await m.answer("Hali test yo'q. «➕ Yangi test» tugmasini bosing.")
        return
    lines = ["📋 <b>Testlaringiz:</b>\n"]
    for r in rows:
        status = "🟢 ochiq" if r["is_open"] else "🔴 yopilgan"
        lines.append(
            f"<b>{esc(r['title'])}</b> — kod <code>{r['code']}</code>\n"
            f"{status} · {r['n']} savol · {r['c']} o'quvchi\n"
            f"Natija: /natija_{r['code']}" + (f"   Yopish: /yopish_{r['code']}" if r["is_open"] else "")
            + "\n"
        )
    await send_long(m, "\n".join(lines))


async def get_own_test(m: Message, code: str):
    test = await db_one("SELECT * FROM tests WHERE code=?", (code,))
    if not test or test["owner_id"] != m.from_user.id:
        await m.answer("Bunday test topilmadi.")
        return None
    return test


@router.message(F.text.regexp(r"^/natija_?(\d+)"))
async def test_results(m: Message):
    if not is_teacher(m.from_user.id):
        return
    code = re.match(r"^/natija_?(\d+)", m.text).group(1)
    test = await get_own_test(m, code)
    if not test:
        return
    subs = await db_all(
        """SELECT COALESCE(u.full_name, '—') AS name, s.correct, s.total, s.answers
           FROM submissions s LEFT JOIN users u ON u.id = s.user_id
           WHERE s.test_code = ? ORDER BY s.correct DESC, s.created_at ASC""",
        (code,),
    )
    head = f"📊 <b>{esc(test['title'])}</b> (kod <code>{code}</code>)\n"
    if not subs:
        await m.answer(head + "\nHali hech kim javob yubormagan.")
        return
    key = test["answers"]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [head, f"Ishtirokchilar: {len(subs)}"]
    avg = sum(s["correct"] for s in subs) / len(subs)
    lines.append(f"O'rtacha: {avg:.1f} / {len(key)}\n")
    for i, s in enumerate(subs, 1):
        pct = s["correct"] * 100 / s["total"]
        lines.append(f"{medals.get(i, f'{i}.')} {esc(s['name'])} — <b>{s['correct']}/{s['total']}</b> ({pct:.0f}%)")

    # Qaysi savollarda ko'p xato qilingan
    wrong = [0] * len(key)
    for s in subs:
        for q, (a, k) in enumerate(zip(s["answers"], key)):
            if a != k:
                wrong[q] += 1
    top = sorted(((w, q + 1) for q, w in enumerate(wrong) if w), reverse=True)[:5]
    if top:
        lines.append("\n❗️ <b>Eng ko'p xato qilingan savollar:</b>")
        lines += [f"{q}-savol — {w} ta o'quvchi xato qildi" for w, q in top]
    if test["is_open"]:
        lines.append(f"\nTestni yopish: /yopish_{code}")
    await send_long(m, "\n".join(lines))


@router.message(F.text.regexp(r"^/yopish_?(\d+)"))
async def close_test(m: Message, bot: Bot):
    if not is_teacher(m.from_user.id):
        return
    code = re.match(r"^/yopish_?(\d+)", m.text).group(1)
    test = await get_own_test(m, code)
    if not test:
        return
    if not test["is_open"]:
        await m.answer("Bu test allaqachon yopilgan.")
        return
    await db_exec("UPDATE tests SET is_open = 0 WHERE code = ?", (code,))
    test = await db_one("SELECT * FROM tests WHERE code=?", (code,))
    subs = await db_all(
        """SELECT s.user_id, s.answers, COALESCE(u.full_name, '') AS name
           FROM submissions s LEFT JOIN users u ON u.id = s.user_id WHERE s.test_code = ?""",
        (code,),
    )
    await m.answer(f"🔴 Test yopildi. {len(subs)} ta o'quvchiga to'g'ri javoblar yuborilmoqda...")
    sent = 0
    for s in subs:
        try:
            await bot.send_message(s["user_id"], "🔔 Test yopildi, to'g'ri javoblar bilan natijangiz:\n\n"
                                   + format_result(test, s["answers"], s["name"], show_key=True))
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"✅ {sent} ta o'quvchiga yuborildi.\nYakuniy natijalar: /natija_{code}")


# ---------------- O'QUVCHI ----------------
async def ask_code(m: Message, state: FSMContext):
    await state.set_state(Submit.code)
    await m.answer("Test kodini yuboring (ustoz bergan 4 xonali raqam):\n\nBekor qilish: /bekor")


@router.message(F.text == "✍️ Javob yuborish")
async def submit_start(m: Message, state: FSMContext):
    await state.clear()
    if not await db_one("SELECT 1 FROM users WHERE id=?", (m.from_user.id,)):
        await state.set_state(Reg.name)
        await state.update_data(after_reg="submit")
        await m.answer("Avval ism va familiyangizni yozing:")
        return
    await ask_code(m, state)


async def check_test_available(m: Message, code: str):
    test = await db_one("SELECT * FROM tests WHERE code=?", (code,))
    if not test:
        await m.answer("❌ Bunday kodli test topilmadi. Kodni tekshirib qayta yuboring:")
        return None
    if not test["is_open"]:
        await m.answer("🔴 Bu test yopilgan, javob qabul qilinmaydi.")
        return None
    if await db_one("SELECT 1 FROM submissions WHERE test_code=? AND user_id=?", (code, m.from_user.id)):
        await m.answer("⚠️ Siz bu testga allaqachon javob yuborgansiz. Javobni faqat bir marta yuborish mumkin.")
        return None
    return test


@router.message(Submit.code, F.text)
async def submit_code(m: Message, state: FSMContext):
    code = m.text.strip()
    test = await check_test_available(m, code)
    if not test:
        if not await db_one("SELECT 1 FROM tests WHERE code=?", (code,)):
            return  # kod noto'g'ri — qayta kiritsin
        await state.clear()
        return
    await state.update_data(code=code)
    await state.set_state(Submit.answers)
    await m.answer(
        f"📝 <b>{esc(test['title'])}</b>\n❓ Savollar soni: <b>{len(test['answers'])}</b>\n\n"
        f"Javoblaringizni yuboring:\n<code>abcdabcd...</code>  yoki  <code>1a 2b 3c ...</code>"
    )


async def process_submission(m: Message, code: str, raw: str) -> bool:
    """True — jarayon tugadi, False — o'quvchi qayta yuborishi kerak."""
    test = await check_test_available(m, code)
    if not test:
        return True
    user = await db_one("SELECT full_name FROM users WHERE id=?", (m.from_user.id,))
    if not user:
        await m.answer("Avval /start bosib ro'yxatdan o'ting.")
        return True
    ans, err = parse_answers(raw)
    if err:
        await m.answer(f"⚠️ {err}\nQaytadan yuboring:")
        return False
    key = test["answers"]
    if len(ans) != len(key):
        await m.answer(f"⚠️ Test <b>{len(key)}</b> ta savoldan iborat, siz <b>{len(ans)}</b> ta javob yubordingiz.\n"
                       f"Qaytadan to'liq yuboring:")
        return False
    correct = sum(a == k for a, k in zip(ans, key))
    try:
        await db_exec(
            "INSERT INTO submissions(test_code, user_id, answers, correct, total, created_at) VALUES(?,?,?,?,?,?)",
            (code, m.from_user.id, ans, correct, len(key), now()),
        )
    except aiosqlite.IntegrityError:
        await m.answer("⚠️ Siz bu testga allaqachon javob yuborgansiz.")
        return True
    await m.answer(format_result(test, ans, user["full_name"], show_key=False), reply_markup=student_kb)
    try:  # ustozga qisqa xabar
        await m.bot.send_message(
            test["owner_id"],
            f"📥 {esc(user['full_name'])} — <b>{correct}/{len(key)}</b> · {esc(test['title'])} (/natija_{code})",
        )
    except Exception:
        pass
    return True


@router.message(Submit.answers, F.text)
async def submit_answers(m: Message, state: FSMContext):
    data = await state.get_data()
    if await process_submission(m, data["code"], m.text):
        await state.clear()


@router.message(F.text == "📊 Natijalarim")
async def my_results(m: Message, state: FSMContext):
    await state.clear()
    rows = await db_all(
        """SELECT t.title, t.code, s.correct, s.total, s.created_at
           FROM submissions s JOIN tests t ON t.code = s.test_code
           WHERE s.user_id = ? ORDER BY s.created_at DESC LIMIT 20""",
        (m.from_user.id,),
    )
    if not rows:
        await m.answer("Siz hali test ishlamagansiz.")
        return
    lines = ["📊 <b>Natijalaringiz:</b>\n"]
    for r in rows:
        lines.append(f"📝 {esc(r['title'])} — <b>{r['correct']}/{r['total']}</b> ({r['created_at']})")
    await send_long(m, "\n".join(lines))


# Bitta xabarda: 1234*abcd...
@router.message(StateFilter(None), F.text.regexp(r"^\s*(\d{3,6})\s*\*\s*(.+)$"))
async def quick_submit(m: Message, state: FSMContext):
    match = re.match(r"^\s*(\d{3,6})\s*\*\s*(.+)$", m.text, re.S)
    if not await db_one("SELECT 1 FROM users WHERE id=?", (m.from_user.id,)):
        await state.set_state(Reg.name)
        await m.answer("Avval ism va familiyangizni yozing, keyin javobni qayta yuboring:")
        return
    await process_submission(m, match.group(1), match.group(2))


@router.message(StateFilter(None))
async def fallback(m: Message):
    if is_teacher(m.from_user.id):
        await m.answer("Quyidagi tugmalardan foydalaning 👇", reply_markup=teacher_kb)
    else:
        await m.answer("Javob yuborish uchun «✍️ Javob yuborish» tugmasini bosing\n"
                       "yoki shunday yuboring: <code>1234*abcd...</code>", reply_markup=student_kb)


# ---------------- Ishga tushirish ----------------
async def on_startup(bot: Bot):
    await db_init()
    if WEBHOOK_URL:
        await bot.set_webhook(f"{WEBHOOK_URL}/webhook", secret_token=WEBHOOK_SECRET,
                              drop_pending_updates=False)
    else:
        await bot.delete_webhook(drop_pending_updates=False)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN o'rnatilmagan")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)

    if WEBHOOK_URL:  # Render (web service) uchun
        from aiohttp import web
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

        app = web.Application()
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path="/webhook")
        setup_application(app, dp, bot=bot)

        async def health(_):
            return web.Response(text="OK")

        app.router.add_get("/", health)
        web.run_app(app, host="0.0.0.0", port=PORT)
    else:  # kompyuterda yoki VPS'da
        asyncio.run(dp.start_polling(bot))


if __name__ == "__main__":
    main()
