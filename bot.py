"""
Mock imtihon boti (Rasch modeli bilan).

Ustoz: test yaratadi -> kod (mock002) -> to'g'ri javoblar -> boshlanish va tugash vaqti.
O'quvchi: belgilangan vaqt oralig'ida kod + javoblarini yuboradi.
Test tugagach: bot Rasch modeli bo'yicha ball (T-ball), foiz va daraja hisoblaydi,
har bir o'quvchiga ✅/❌ bilan natija yuboradi va kanalga reytingni joylaydi.
"""
import asyncio
import html
import logging
import math
import os
import random
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()  # @kanal_nomi yoki -100...; bot kanalga admin bo'lishi kerak
DB_PATH = os.getenv("DB_PATH", "mock_bot.db")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mock-bot-secret")
PORT = int(os.getenv("PORT", "8080"))
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Tashkent"))
CHANNEL_TOP = int(os.getenv("CHANNEL_TOP", "100"))  # kanalga nechta o'rin chiqarilsin

# Daraja chegaralari (T-ball bo'yicha). Kerak bo'lsa o'zgartiring.
GRADES = [(70, "A+"), (65, "A"), (60, "B+"), (55, "B"), (50, "C+"), (46, "C")]

FMT = "%Y-%m-%d %H:%M"
logging.basicConfig(level=logging.INFO)
router = Router()
_finalize_lock = asyncio.Lock()
_bg_tasks = set()


def esc(s) -> str:
    return html.escape(str(s or ""))


def is_teacher(uid: int) -> bool:
    return uid in ADMIN_IDS


def now_dt() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)


def now() -> str:
    return now_dt().strftime(FMT)


def show_dt(s: str) -> str:
    """'2026-10-12 12:30' -> '12.10 12:30'"""
    return f"{s[8:10]}.{s[5:7]} {s[11:16]}" if s else "—"


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
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
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
                tscore REAL,
                grade TEXT,
                rank INTEGER,
                UNIQUE(test_code, user_id)
            );
            """
        )
        await db.commit()


async def db_exec(sql, params=()):
    """O'zgargan qatorlar sonini qaytaradi."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.rowcount


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


# ---------------- Javoblar va vaqtni o'qish ----------------
CYR = str.maketrans({"а": "a", "б": "b", "в": "b", "с": "c", "ц": "c",
                     "д": "d", "е": "e", "э": "e", "ф": "f"})
VALID = set("abcdef")


def parse_answers(text: str):
    """'abcdab...' yoki '1a 2b 3c' / '1-a, 2-b'. Qaytaradi: (javoblar, xato_matni)"""
    t = (text or "").lower().translate(CYR)
    if re.search(r"\d", t):
        pairs = re.findall(r"(\d+)\s*[\)\.\-:=]?\s*([a-z])", t)
        if not pairs:
            return None, "Javoblarni o'qib bo'lmadi. Masalan: <code>1a2b3c4d</code>"
        d = {int(n): letter for n, letter in pairs}
        n = max(d)
        missing = [i for i in range(1, n + 1) if i not in d]
        if missing:
            return None, f"Quyidagi savollarga javob yo'q: {', '.join(map(str, missing[:15]))}"
        res = [d[i] for i in range(1, n + 1)]
    else:
        res = [c for c in t if c.isalpha()]
    if not res or any(c not in VALID for c in res):
        return None, "Faqat A, B, C, D (E, F) harflaridan foydalaning."
    return "".join(res), None


def parse_when(text: str, start: datetime = None):
    """'hozir', '10:00', '15.10 10:00', '15.10.2026 10:00'; tugash uchun daqiqa ham: '150'."""
    t = (text or "").strip().lower()
    n = now_dt()
    if start is None and t in ("hozir", "now", "0"):
        return n
    if start is not None and re.fullmatch(r"\d{1,4}", t):
        return start + timedelta(minutes=int(t))
    m = re.fullmatch(r"(?:(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s+)?(\d{1,2})[:.](\d{2})", t)
    if not m:
        return None
    d, mo, y, h, mi = m.groups()
    try:
        return datetime(int(y) if y else n.year, int(mo) if d else n.month,
                        int(d) if d else n.day, int(h), int(mi))
    except ValueError:
        return None


# ---------------- Rasch modeli ----------------
def rasch(matrix, iters=300, tol=1e-4):
    """Dixotomik Rasch modeli, JML usuli.
    matrix: o'quvchilar x savollar (1 - to'g'ri, 0 - xato).
    Qaytaradi: (o'quvchi qobiliyati theta, savol qiyinligi b)."""
    n, k = len(matrix), len(matrix[0])
    r = [sum(row) for row in matrix]
    s = [sum(matrix[p][i] for p in range(n)) for i in range(k)]
    # 0 yoki maksimal ball uchun 0.5 tuzatish (aks holda cheksizlik chiqadi)
    r = [min(max(x, 0.5), k - 0.5) for x in r]
    s = [min(max(x, 0.5), n - 0.5) for x in s] if n > 1 else [0.5] * k
    theta = [math.log(x / (k - x)) for x in r]
    b = [math.log((n - x) / x) if n > 1 else 0.0 for x in s]

    def prob(t, d):
        z = max(-30.0, min(30.0, t - d))
        return 1.0 / (1.0 + math.exp(-z))

    for _ in range(iters):
        maxd = 0.0
        if n > 1:
            for i in range(k):
                ps = [prob(theta[p], b[i]) for p in range(n)]
                var = sum(p * (1 - p) for p in ps) or 1e-9
                step = max(-1.0, min(1.0, (sum(ps) - s[i]) / var))
                b[i] += step
                maxd = max(maxd, abs(step))
            mean_b = sum(b) / k
            b = [x - mean_b for x in b]
        for p in range(n):
            ps = [prob(theta[p], b[i]) for i in range(k)]
            var = sum(q * (1 - q) for q in ps) or 1e-9
            step = max(-1.0, min(1.0, (r[p] - sum(ps)) / var))
            theta[p] += step
            maxd = max(maxd, abs(step))
        if maxd < tol:
            break
    return theta, b


def t_scores(theta):
    """T-ball: 50 + 10*Z (o'rtacha 50, standart og'ish 10)."""
    n = len(theta)
    mean = sum(theta) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in theta) / n)
    if sd < 1e-9:
        return [50.0] * n
    return [round(max(0.0, 50 + 10 * (x - mean) / sd), 1) for x in theta]


def grade_of(score: float) -> str:
    for limit, g in GRADES:
        if score >= limit:
            return g
    return "—"


# ---------------- Matnlar ----------------
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def final_result_text(test, s, participants: int) -> str:
    key, ans = test["answers"], s["answers"]
    total = len(key)
    lines = [
        f"🏁 <b>{esc(test['title'])}</b> — yakuniy natija",
        f"👤 {esc(s['name'])}",
        "",
        f"✅ To'g'ri: <b>{s['correct']}</b> / {total}  ({s['correct'] * 100 / total:.1f}%)",
        f"❌ Xato: <b>{total - s['correct']}</b>",
        f"🎯 Ball (Rasch): <b>{s['tscore']:.1f}</b>",
        f"🏅 Daraja: <b>{s['grade']}</b>",
        f"🏆 O'rin: <b>{s['rank']}</b> / {participants}",
        "",
    ]
    for i, (a, k) in enumerate(zip(ans, key), 1):
        lines.append(f"{i}. {a.upper()} ✅" if a == k else f"{i}. {a.upper()} ❌  (to'g'ri: {k.upper()})")
    return "\n".join(lines)


def ranking_text(test, subs, limit=None) -> str:
    total = len(test["answers"])
    lines = [
        f"🏁 <b>{esc(test['title'])}</b> natijalari",
        f"🔑 Kod: <code>{esc(test['code'])}</code> · ❓ {total} savol",
        f"👥 Ishtirokchilar: {len(subs)}",
        "",
    ]
    shown = subs[:limit] if limit else subs
    for s in shown:
        medal = MEDALS.get(s["rank"], f"{s['rank']}.")
        lines.append(f"{medal} {esc(s['name'])} — <b>{s['tscore']:.1f}</b> ({s['grade']}) · {s['correct']}/{total}")
    if limit and len(subs) > limit:
        lines.append(f"\n... va yana {len(subs) - limit} ishtirokchi")
    dist = {}
    for s in subs:
        dist[s["grade"]] = dist.get(s["grade"], 0) + 1
    order = [g for _, g in GRADES] + ["—"]
    parts = [f"{g}: {dist[g]}" for g in order if g in dist]
    if parts:
        lines.append("\n📈 Darajalar: " + " · ".join(parts))
    return "\n".join(lines)


def hardest_text(test, subs) -> str:
    key = test["answers"]
    wrong = [0] * len(key)
    for s in subs:
        for q, (a, k) in enumerate(zip(s["answers"], key)):
            if a != k:
                wrong[q] += 1
    top = sorted(((w, q + 1) for q, w in enumerate(wrong) if w), reverse=True)[:5]
    if not top:
        return ""
    return "\n\n❗️ <b>Eng ko'p xato qilingan savollar:</b>\n" + "\n".join(
        f"{q}-savol — {w} ta ({w * 100 / len(subs):.0f}%)" for w, q in top)


async def send_long(bot: Bot, chat_id, text: str, **kw):
    """4096 belgidan uzun xabarni bo'lib yuboradi."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3900:
            await bot.send_message(chat_id, chunk, **kw)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await bot.send_message(chat_id, chunk, **kw)


# ---------------- Testni yakunlash ----------------
async def _compute_results(code: str) -> bool:
    async with _finalize_lock:
        changed = await db_exec("UPDATE tests SET status='finished' WHERE code=? AND status='open'", (code,))
        if not changed:
            return False
        test = await db_one("SELECT * FROM tests WHERE code=?", (code,))
        subs = await db_all("SELECT id, answers, correct FROM submissions WHERE test_code=?", (code,))
        if subs:
            key = test["answers"]
            matrix = [[1 if a == k else 0 for a, k in zip(s["answers"], key)] for s in subs]
            theta, _ = rasch(matrix)
            ts = t_scores(theta)
            order = sorted(range(len(subs)), key=lambda i: (-ts[i], -subs[i]["correct"]))
            rank = {i: pos for pos, i in enumerate(order, 1)}
            async with aiosqlite.connect(DB_PATH) as db:
                await db.executemany(
                    "UPDATE submissions SET tscore=?, grade=?, rank=? WHERE id=?",
                    [(ts[i], grade_of(ts[i]), rank[i], subs[i]["id"]) for i in range(len(subs))],
                )
                await db.commit()
        return True


async def ranked_subs(code: str):
    return await db_all(
        """SELECT s.*, COALESCE(u.full_name, '—') AS name
           FROM submissions s LEFT JOIN users u ON u.id = s.user_id
           WHERE s.test_code = ? ORDER BY s.rank ASC""",
        (code,),
    )


async def finalize_test(bot: Bot, code: str):
    if not await _compute_results(code):
        return
    test = await db_one("SELECT * FROM tests WHERE code=?", (code,))
    subs = await ranked_subs(code)
    n = len(subs)

    for s in subs:  # har bir o'quvchiga natija
        try:
            await send_long(bot, s["user_id"], final_result_text(test, s, n))
        except Exception:
            pass
        await asyncio.sleep(0.05)

    channel_note = ""
    if CHANNEL_ID and subs:
        try:
            await send_long(bot, CHANNEL_ID, ranking_text(test, subs, CHANNEL_TOP))
            channel_note = "\n📢 Natijalar kanalga joylandi."
        except Exception as e:
            channel_note = f"\n⚠️ Kanalga yuborilmadi (bot kanalga admin qilinganmi?): {esc(e)}"

    try:
        if subs:
            text = "✅ Test yakunlandi!\n\n" + ranking_text(test, subs) + hardest_text(test, subs) + channel_note
        else:
            text = f"✅ «{esc(test['title'])}» testi yakunlandi. Hech kim javob yubormadi."
        await send_long(bot, test["owner_id"], text)
    except Exception:
        pass


async def check_due(bot: Bot):
    rows = await db_all("SELECT code FROM tests WHERE status='open' AND end_at <= ?", (now(),))
    for r in rows:
        await finalize_test(bot, r["code"])


async def scheduler(bot: Bot):
    while True:
        try:
            await check_due(bot)
        except Exception:
            logging.exception("scheduler xatosi")
        await asyncio.sleep(30)


def run_bg(coro):
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# ---------------- Klaviaturalar va holatlar ----------------
teacher_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="➕ Yangi test"), KeyboardButton(text="📋 Testlarim")]],
    resize_keyboard=True,
)
student_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="✍️ Javob yuborish"), KeyboardButton(text="📊 Natijalarim")]],
    resize_keyboard=True,
)


class Reg(StatesGroup):
    name = State()


class NewTest(StatesGroup):
    title = State()
    code = State()
    answers = State()
    start = State()
    end = State()


class Submit(StatesGroup):
    code = State()
    answers = State()


# ---------------- Umumiy ----------------
@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    if is_teacher(uid):
        await m.answer(
            "Assalomu alaykum, ustoz! 👋\n\n"
            "➕ <b>Yangi test</b> — mock test yaratish (javoblar, vaqt)\n"
            "📋 <b>Testlarim</b> — natijalar, testni muddatidan oldin yakunlash",
            reply_markup=teacher_kb,
        )
        return
    user = await db_one("SELECT full_name FROM users WHERE id=?", (uid,))
    if user:
        await m.answer(f"Assalomu alaykum, {esc(user['full_name'])}! 👋\n"
                       "Javob yuborish uchun «✍️ Javob yuborish» tugmasini bosing.", reply_markup=student_kb)
    else:
        await state.set_state(Reg.name)
        await m.answer("Assalomu alaykum! 👋\nIsm va familiyangizni yozing (masalan: <i>Aliyev Vali</i>):")


@router.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Sizning Telegram ID: <code>{m.from_user.id}</code>")


@router.message(Command("bekor"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=teacher_kb if is_teacher(m.from_user.id) else student_kb)


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


# ---------------- USTOZ: test yaratish ----------------
@router.message(F.text == "➕ Yangi test")
@router.message(Command("yangi"))
async def new_test(m: Message, state: FSMContext):
    if not is_teacher(m.from_user.id):
        return
    await state.clear()
    await state.set_state(NewTest.title)
    await m.answer("1/5. Test nomini yozing (masalan: <i>Matematika MOCK #2</i>):\n\nBekor qilish: /bekor")


@router.message(NewTest.title, F.text)
async def nt_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip()[:100])
    await state.set_state(NewTest.code)
    await m.answer("2/5. Test kodini yozing (masalan: <code>mock002</code>)\n"
                   "yoki <b>avto</b> deb yozing, bot o'zi beradi.\n\n"
                   "Kodda faqat lotin harflari, raqamlar, - va _ bo'lsin.")


@router.message(NewTest.code, F.text)
async def nt_code(m: Message, state: FSMContext):
    t = m.text.strip().lower()
    if t == "avto":
        while True:
            code = str(random.randint(1000, 9999))
            if not await db_one("SELECT 1 FROM tests WHERE code=?", (code,)):
                break
    else:
        if not re.fullmatch(r"[a-z0-9_\-]{3,20}", t):
            await m.answer("⚠️ Kod 3–20 belgidan iborat bo'lsin (lotin harf, raqam, - _). Qayta yozing:")
            return
        if await db_one("SELECT 1 FROM tests WHERE code=?", (t,)):
            await m.answer("⚠️ Bu kod band. Boshqasini yozing:")
            return
        code = t
    await state.update_data(code=code)
    await state.set_state(NewTest.answers)
    await m.answer(
        f"Kod: <code>{code}</code>\n\n3/5. <b>To'g'ri javoblarni</b> yuboring:\n"
        "• ketma-ket: <code>abcdabcdab</code>\n"
        "• yoki raqam bilan: <code>1a 2b 3c 4d</code>"
    )


@router.message(NewTest.answers, F.text)
async def nt_answers(m: Message, state: FSMContext):
    key, err = parse_answers(m.text)
    if err:
        await m.answer(f"⚠️ {err}\nQaytadan yuboring:")
        return
    await state.update_data(answers=key)
    await state.set_state(NewTest.start)
    await m.answer(
        f"✅ {len(key)} ta savol qabul qilindi.\n\n"
        "4/5. Test <b>qachon boshlanadi</b>?\n"
        "• <code>hozir</code>\n• <code>10:00</code> (bugun)\n• <code>15.10 10:00</code>"
    )


@router.message(NewTest.start, F.text)
async def nt_start(m: Message, state: FSMContext):
    start = parse_when(m.text)
    if not start:
        await m.answer("⚠️ Vaqtni tushunmadim. Masalan: <code>hozir</code>, <code>10:00</code> yoki <code>15.10 10:00</code>")
        return
    await state.update_data(start=start.strftime(FMT))
    await state.set_state(NewTest.end)
    await m.answer(
        f"Boshlanish: <b>{show_dt(start.strftime(FMT))}</b>\n\n"
        "5/5. Test <b>qachon tugaydi</b>?\n"
        "• <code>12:30</code> yoki <code>15.10 12:30</code>\n"
        "• yoki davomiyligi daqiqada: <code>150</code>"
    )


@router.message(NewTest.end, F.text)
async def nt_end(m: Message, state: FSMContext):
    data = await state.get_data()
    start = datetime.strptime(data["start"], FMT)
    end = parse_when(m.text, start=start)
    if not end:
        await m.answer("⚠️ Vaqtni tushunmadim. Masalan: <code>12:30</code> yoki <code>150</code>")
        return
    if end <= start or end <= now_dt():
        await m.answer("⚠️ Tugash vaqti boshlanishdan keyin va kelajakda bo'lishi kerak. Qayta yozing:")
        return
    code, key = data["code"], data["answers"]
    if await db_one("SELECT 1 FROM tests WHERE code=?", (code,)):
        await state.clear()
        await m.answer("⚠️ Bu kod bilan test allaqachon yaratilgan. Qaytadan «➕ Yangi test» bosing.")
        return
    await db_exec(
        "INSERT INTO tests(code, title, answers, owner_id, start_at, end_at, created_at) VALUES(?,?,?,?,?,?,?)",
        (code, data["title"], key, m.from_user.id, data["start"], end.strftime(FMT), now()),
    )
    await state.clear()
    key_view = "  ".join(f"{i}{c.upper()}" for i, c in enumerate(key, 1))
    await m.answer(
        f"✅ <b>Test yaratildi!</b>\n\n"
        f"📝 {esc(data['title'])}\n"
        f"🔑 Kod: <code>{code}</code>\n"
        f"❓ Savollar: {len(key)}\n"
        f"🕙 {show_dt(data['start'])} — {show_dt(end.strftime(FMT))}\n\n"
        f"Javoblar: {key_view}\n\n"
        f"<b>O'quvchilar uchun e'lon:</b>\n"
        f"Javoblarni botga yuboring: <code>{code}*abcd...</code>\n"
        f"yoki «✍️ Javob yuborish» → kod: <code>{code}</code>\n\n"
        f"Vaqt tugagach bot Rasch modeli bo'yicha hisoblab, hammaga natijani yuboradi"
        + (" va kanalga joylaydi." if CHANNEL_ID else ".")
        + f"\n\nHozirgi holat: /natija_{code}\nMuddatidan oldin yakunlash: /yakun_{code}",
        reply_markup=teacher_kb,
    )


# ---------------- USTOZ: ro'yxat, natija, yakunlash ----------------
@router.message(F.text == "📋 Testlarim")
@router.message(Command("testlar"))
async def my_tests(m: Message, state: FSMContext):
    if not is_teacher(m.from_user.id):
        return
    await state.clear()
    rows = await db_all(
        """SELECT t.*, length(t.answers) AS n,
                  (SELECT COUNT(*) FROM submissions s WHERE s.test_code = t.code) AS c
           FROM tests t WHERE t.owner_id = ? ORDER BY t.created_at DESC LIMIT 30""",
        (m.from_user.id,),
    )
    if not rows:
        await m.answer("Hali test yo'q. «➕ Yangi test» tugmasini bosing.")
        return
    cur = now()
    lines = ["📋 <b>Testlaringiz:</b>\n"]
    for r in rows:
        if r["status"] == "finished":
            status = "🏁 yakunlangan"
        elif cur < r["start_at"]:
            status = "⏳ boshlanmagan"
        else:
            status = "🟢 davom etmoqda"
        lines.append(
            f"<b>{esc(r['title'])}</b> — <code>{esc(r['code'])}</code>\n"
            f"{status} · {r['n']} savol · {r['c']} ishtirokchi\n"
            f"🕙 {show_dt(r['start_at'])} — {show_dt(r['end_at'])}\n"
            f"/natija_{r['code']}" + (f"   /yakun_{r['code']}" if r["status"] == "open" else "") + "\n"
        )
    await send_long(m.bot, m.chat.id, "\n".join(lines))


async def get_own_test(m: Message, code: str):
    test = await db_one("SELECT * FROM tests WHERE code=?", (code.lower(),))
    if not test or test["owner_id"] != m.from_user.id:
        await m.answer("Bunday test topilmadi.")
        return None
    return test


@router.message(F.text.regexp(r"^/natija_([A-Za-z0-9_\-]+)"))
async def test_results(m: Message, bot: Bot):
    if not is_teacher(m.from_user.id):
        return
    await check_due(bot)
    code = re.match(r"^/natija_([A-Za-z0-9_\-]+)", m.text).group(1).lower()
    test = await get_own_test(m, code)
    if not test:
        return
    if test["status"] == "finished":
        subs = await ranked_subs(code)
        if not subs:
            await m.answer("Test yakunlangan, hech kim javob yubormagan.")
            return
        await send_long(bot, m.chat.id, ranking_text(test, subs) + hardest_text(test, subs))
        return
    subs = await db_all(
        """SELECT COALESCE(u.full_name, '—') AS name, s.correct, s.answers
           FROM submissions s LEFT JOIN users u ON u.id = s.user_id
           WHERE s.test_code = ? ORDER BY s.correct DESC, s.created_at ASC""",
        (code,),
    )
    total = len(test["answers"])
    lines = [
        f"⏳ <b>{esc(test['title'])}</b> (<code>{esc(code)}</code>) — test davom etmoqda",
        f"🕙 Tugaydi: {show_dt(test['end_at'])}",
        f"👥 Javob yuborganlar: {len(subs)}",
        "\n<i>Dastlabki natija (to'g'ri javoblar soni). Rasch ball test tugagach hisoblanadi.</i>\n",
    ]
    lines += [f"{i}. {esc(s['name'])} — {s['correct']}/{total}" for i, s in enumerate(subs, 1)]
    lines.append(f"\nMuddatidan oldin yakunlash: /yakun_{code}")
    await send_long(bot, m.chat.id, "\n".join(lines))


@router.message(F.text.regexp(r"^/yakun_([A-Za-z0-9_\-]+)"))
async def close_test(m: Message, bot: Bot):
    if not is_teacher(m.from_user.id):
        return
    code = re.match(r"^/yakun_([A-Za-z0-9_\-]+)", m.text).group(1).lower()
    test = await get_own_test(m, code)
    if not test:
        return
    if test["status"] == "finished":
        await m.answer("Bu test allaqachon yakunlangan.")
        return
    await m.answer("⏳ Natijalar hisoblanmoqda va yuborilmoqda...")
    await finalize_test(bot, code)


# ---------------- O'QUVCHI ----------------
async def ask_code(m: Message, state: FSMContext):
    await state.set_state(Submit.code)
    await m.answer("Test kodini yuboring (masalan: <code>mock002</code>):\n\nBekor qilish: /bekor")


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
    """Test mavjud va javob qabul qilinadigan holatda bo'lsa testni qaytaradi."""
    test = await db_one("SELECT * FROM tests WHERE code=?", (code,))
    if not test:
        await m.answer("❌ Bunday kodli test topilmadi. Kodni tekshirib qayta yuboring:")
        return None
    cur = now()
    if test["status"] == "finished":
        await m.answer("🏁 Bu test yakunlangan, javob qabul qilinmaydi.")
        return None
    if cur < test["start_at"]:
        await m.answer(f"⏳ Test hali boshlanmagan. Boshlanish vaqti: <b>{show_dt(test['start_at'])}</b>")
        return None
    if cur >= test["end_at"]:
        await m.answer("⌛️ Javob yuborish vaqti tugagan.")
        run_bg(finalize_test(m.bot, code))
        return None
    if await db_one("SELECT 1 FROM submissions WHERE test_code=? AND user_id=?", (code, m.from_user.id)):
        await m.answer("⚠️ Siz bu testga allaqachon javob yuborgansiz. Javob faqat bir marta qabul qilinadi.")
        return None
    return test


@router.message(Submit.code, F.text)
async def submit_code(m: Message, state: FSMContext):
    code = m.text.strip().lower()
    test = await check_test_available(m, code)
    if not test:
        if not await db_one("SELECT 1 FROM tests WHERE code=?", (code,)):
            return  # kod noto'g'ri - qayta kiritsin
        await state.clear()
        return
    await state.update_data(code=code)
    await state.set_state(Submit.answers)
    await m.answer(
        f"📝 <b>{esc(test['title'])}</b>\n❓ Savollar soni: <b>{len(test['answers'])}</b>\n"
        f"🕙 Tugaydi: {show_dt(test['end_at'])}\n\n"
        f"Javoblaringizni yuboring:\n<code>abcdabcd...</code>  yoki  <code>1a 2b 3c ...</code>"
    )


async def process_submission(m: Message, code: str, raw: str) -> bool:
    """True - jarayon tugadi, False - o'quvchi qayta yuborishi kerak."""
    test = await check_test_available(m, code)
    if not test:
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
    await m.answer(
        f"✅ <b>Javoblaringiz qabul qilindi!</b>\n\n"
        f"📝 {esc(test['title'])}\n"
        f"🔑 Kod: <code>{esc(code)}</code>\n"
        f"Javoblar: <code>{ans.upper()}</code>\n\n"
        f"📊 Natija (ball, daraja, ✅/❌) test tugagach — <b>{show_dt(test['end_at'])}</b> dan keyin yuboriladi.",
        reply_markup=student_kb,
    )
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
        """SELECT t.title, t.end_at, s.correct, s.total, s.tscore, s.grade, s.rank
           FROM submissions s JOIN tests t ON t.code = s.test_code
           WHERE s.user_id = ? ORDER BY s.created_at DESC LIMIT 20""",
        (m.from_user.id,),
    )
    if not rows:
        await m.answer("Siz hali test ishlamagansiz.")
        return
    lines = ["📊 <b>Natijalaringiz:</b>\n"]
    for r in rows:
        if r["tscore"] is None:
            lines.append(f"📝 {esc(r['title'])} — ⏳ natija {show_dt(r['end_at'])} dan keyin")
        else:
            lines.append(f"📝 {esc(r['title'])} — <b>{r['tscore']:.1f}</b> ball ({r['grade']}), "
                         f"{r['correct']}/{r['total']}, {r['rank']}-o'rin")
    await send_long(m.bot, m.chat.id, "\n".join(lines))


QUICK_RE = r"^\s*([A-Za-z0-9_\-]{3,20})\s*\*\s*(.+)$"


@router.message(StateFilter(None), F.text.regexp(QUICK_RE))
async def quick_submit(m: Message, state: FSMContext):
    match = re.match(QUICK_RE, m.text, re.S)
    if not await db_one("SELECT 1 FROM users WHERE id=?", (m.from_user.id,)):
        await state.set_state(Reg.name)
        await m.answer("Avval ism va familiyangizni yozing, keyin javobni qayta yuboring:")
        return
    await process_submission(m, match.group(1).lower(), match.group(2))


@router.message(StateFilter(None))
async def fallback(m: Message):
    if is_teacher(m.from_user.id):
        await m.answer("Quyidagi tugmalardan foydalaning 👇", reply_markup=teacher_kb)
    else:
        await m.answer("Javob yuborish uchun «✍️ Javob yuborish» tugmasini bosing\n"
                       "yoki shunday yuboring: <code>mock002*abcd...</code>", reply_markup=student_kb)


# ---------------- Ishga tushirish ----------------
async def on_startup(bot: Bot):
    await db_init()
    if WEBHOOK_URL:
        await bot.set_webhook(f"{WEBHOOK_URL}/webhook", secret_token=WEBHOOK_SECRET)
    else:
        await bot.delete_webhook()
    run_bg(scheduler(bot))


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN o'rnatilmagan")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)

    if WEBHOOK_URL:  # Render (web service)
        from aiohttp import web
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

        app = web.Application()
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path="/webhook")
        setup_application(app, dp, bot=bot)

        async def health(_):
            return web.Response(text="OK")

        app.router.add_get("/", health)
        web.run_app(app, host="0.0.0.0", port=PORT)
    else:  # kompyuter yoki VPS
        asyncio.run(dp.start_polling(bot))


if __name__ == "__main__":
    main()
