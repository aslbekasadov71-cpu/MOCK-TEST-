# Test tekshiruvchi Telegram bot

## Qanday ishlaydi
**Ustoz:**
1. «➕ Yangi test» → test nomi → to'g'ri javoblar (`abcdabcd` yoki `1a 2b 3c`)
2. Bot 4 xonali **test kodi** beradi, uni o'quvchilarga aytasiz
3. «📋 Testlarim» → `/natija_1234` bilan reyting, o'rtacha ball va eng ko'p xato qilingan savollarni ko'rasiz
4. `/yopish_1234` buyrug'i testni yopadi va har bir o'quvchiga to'g'ri javoblarni yuboradi

**O'quvchi:**
1. /start → ism-familiyasini yozadi (bir marta)
2. «✍️ Javob yuborish» → kod → javoblar. Yoki bitta xabarda: `1234*abcdabcd`
3. Har bir savol bo'yicha ✅ yoki ❌ va umumiy ball chiqadi

Qo'shimcha imkoniyatlar:
- Har o'quvchi bitta testga faqat bir marta javob yubora oladi.
- Javoblar soni noto'g'ri bo'lsa, bot qayta yuborishni so'raydi.
- Kirill harflar (А, В, С, Д) ham qabul qilinadi.
- To'g'ri javob test yopilguncha ko'rsatilmaydi, shuning uchun o'quvchilar bir-biriga javob tarqata olmaydi.

## O'rnatish
1. @BotFather → `/newbot` → **token** oling
2. Ustozning Telegram ID'sini toping: botni ishga tushirib `/id` yozing (yoki @userinfobot orqali)
3. Muhit o'zgaruvchilari:
   - `BOT_TOKEN` — token
   - `ADMIN_IDS` — ustoz ID'lari, vergul bilan: `12345678,87654321`
   - `WEBHOOK_URL` — faqat Render uchun: `https://sizning-bot.onrender.com`
   - `DB_PATH` — ixtiyoriy, baza fayli yo'li

### Kompyuterda / VPS'da
```
pip install -r requirements.txt
BOT_TOKEN=... ADMIN_IDS=... python bot.py
```

### Render'da
- New → **Web Service** → GitHub repo
- Build: `pip install -r requirements.txt`
- Start: `python bot.py`
- Environment: `BOT_TOKEN`, `ADMIN_IDS`, `WEBHOOK_URL`

⚠️ **Muhim:** Render'ning bepul tarifida disk vaqtinchalik. Har restart yoki deployda SQLite bazasi o'chib ketadi.
Real mijoz uchun quyidagilardan birini tanlang:
- Render **Persistent Disk** ulang va `DB_PATH=/var/data/test_bot.db` qiling (pullik)
- Arzon VPS'da polling rejimida ishlating (`WEBHOOK_URL` qo'ymasdan)
