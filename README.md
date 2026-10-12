# Mock imtihon boti (Rasch modeli)

## Ishlash tartibi
**Ustoz** «➕ Yangi test» tugmasini bosib 5 qadamni bajaradi:
1. Test nomi
2. Kod (`mock002` yoki `avto`)
3. To'g'ri javoblar (`abcd...` yoki `1a 2b 3c`)
4. Boshlanish vaqti (`hozir`, `10:00`, `15.10 10:00`)
5. Tugash vaqti (`12:30` yoki daqiqada `150`)

**O'quvchi** belgilangan vaqt ichida botga `mock002*abcd...` yuboradi yoki «✍️ Javob yuborish» tugmasini bosadi.
Bot faqat «qabul qilindi» deb javob beradi. Javob faqat bir marta qabul qilinadi.

**Vaqt tugagach** bot avtomatik ravishda:
- Rasch modeli bo'yicha ball (T-ball), foiz, daraja va o'rinni hisoblaydi
- har bir o'quvchiga natijasini ✅/❌ va to'g'ri javoblar bilan yuboradi
- kanalga reytingni joylaydi (`CHANNEL_ID` berilgan bo'lsa)
- ustozga umumiy hisobot va eng ko'p xato qilingan savollar ro'yxatini yuboradi

Ustoz buyruqlari: `/natija_mock002` (joriy holat yoki yakuniy natija), `/yakun_mock002` (muddatidan oldin yakunlash).

## Ball qanday hisoblanadi
- Rasch (1 parametrli IRT) modeli JML usulida baholanadi. Har bir o'quvchi uchun qobiliyat (θ), har bir savol uchun qiyinlik hisoblanadi.
- T-ball = 50 + 10 × Z. Ishtirokchilar o'rtachasi 50 ball bo'ladi.
- Daraja chegaralari: A+ ≥70, A ≥65, B+ ≥60, B ≥55, C+ ≥50, C ≥46. Ular `bot.py` dagi `GRADES` qatorida o'zgartiriladi.
- Ishtirokchilar soni kam bo'lsa (taxminan 20 tadan kam), ball ishonchli chiqmaydi.

## Sozlamalar (Environment)
| Nomi | Tavsif |
|---|---|
| `BOT_TOKEN` | @BotFather bergan token |
| `ADMIN_IDS` | Ustoz ID'lari, vergul bilan ajratiladi |
| `CHANNEL_ID` | `@kanal_nomi` yoki `-100...`. Bot kanalda admin bo'lishi shart |
| `WEBHOOK_URL` | Render manzili, masalan `https://xxx.onrender.com` |
| `PYTHON_VERSION` | `3.12.7` |
| `DB_PATH` | ixtiyoriy, Persistent Disk ulansa `/var/data/mock_bot.db` |

## Render bo'yicha eslatmalar
- Bepul tarifda servis 15 daqiqa ishlatilmasa uxlab qoladi. Shunda test vaqtida tugamaydi, faqat keyingi xabar kelganda yakunlanadi.
  Buning oldini olish uchun UptimeRobot (bepul) orqali `https://xxx.onrender.com/` manzilini har 5 daqiqada ping qiling.
- Bepul diskdagi baza har deployda o'chadi. Real ishlatish uchun Persistent Disk yoki VPS kerak.
