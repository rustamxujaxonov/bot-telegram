import os
import time
import asyncpg
import redis.asyncio as redis
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Sozlamalar
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")

db = None
r = None

# ===== DATABASE VA REDIS =====
async def init():
    global db, r
    db = await asyncpg.create_pool(DATABASE_URL)
    r = redis.from_url(REDIS_URL, decode_responses=True)

    async with db.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            gender TEXT,
            search_pref TEXT,
            state TEXT,
            partner BIGINT,
            reports INT DEFAULT 0
        )
        """)

async def get_user(uid):
    async with db.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)

async def update_user(uid, **kwargs):
    cols = ", ".join([f"{k}=${i+2}" for i, k in enumerate(kwargs.keys())])
    vals = list(kwargs.values())
    async with db.acquire() as conn:
        await conn.execute(f"UPDATE users SET {cols} WHERE user_id=$1", uid, *vals)

# ===== KEYBOARD =====
def get_kb(state):
    if state == "gender":
        return ReplyKeyboardMarkup([["👨 Erkak", "👩 Ayol"]], resize_keyboard=True)
    
    if state == "menu":
        return ReplyKeyboardMarkup([
            ["🔎 Jins bo‘yicha qidirish"],
            ["🎲 Random qidirish"],
            ["🔄 Jinsni o‘zgartirish"]
        ], resize_keyboard=True)
    
    if state == "search_gender":
        return ReplyKeyboardMarkup([
            ["👨 O‘g‘il qidirish", "👩 Qiz qidirish"],
            ["🔙 Orqaga"]
        ], resize_keyboard=True)
    
    if state == "searching":
        return ReplyKeyboardMarkup([["❌ Bekor qilish"]], resize_keyboard=True)
    
    if state == "chat":
        return ReplyKeyboardMarkup([
            ["⛔ Tugatish", "🔄 Keyingi"],
            ["🚨 Shikoyat"]
        ], resize_keyboard=True)
    
    return ReplyKeyboardRemove()

# ===== MATCHMAKING =====
async def find_match(uid, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(uid)
    my_gender = user['gender']
    pref = user['search_pref']

    # Qidiruv logikasi
    queue_key = f"queue:{pref}"
    search_in = f"queue:{my_gender}" if pref != "random" else "queue:random"
    
    partner_id = await r.lpop(search_in)

    if partner_id:
        partner_id = int(partner_id)
        
        await update_user(uid, partner=partner_id, state="chat")
        await update_user(partner_id, partner=uid, state="chat")

        # Har ikki tomonga xabar yuborish
        msg = "🎉 **Suhbatdosh topildi!**\n\nEndi bemalol yozishingiz mumkin. O'zaro hurmatni saqlang.\n\n_Tugmalar vazifasi:_\n⛔ **Tugatish** - Suhbatni butunlay to'xtatish.\n🔄 **Keyingi** - Hozirgi suhbatni tugatib, darhol yangisini qidirish.\n🚨 **Shikoyat** - Nojo'ya xatti-harakatlar haqida xabar berish."
        
        await context.bot.send_message(uid, msg, reply_markup=get_kb("chat"), parse_mode="Markdown")
        await context.bot.send_message(partner_id, msg, reply_markup=get_kb("chat"), parse_mode="Markdown")
        return True
    else:
        await r.rpush(queue_key, uid)
        return False

# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = await get_user(uid)
    
    welcome_text = "👋 **Anonim Chat Botga xush kelibsiz!**\n\nBu yerda siz o'zligingizni oshkor qilmagan holda yangi do'stlar orttirishingiz mumkin."
    
    if not user:
        async with db.acquire() as conn:
            await conn.execute("INSERT INTO users (user_id, state) VALUES ($1, 'gender')", uid)
        await update.message.reply_text(f"{welcome_text}\n\nIltimos, avval jinsingizni tanlang:", reply_markup=get_kb("gender"), parse_mode="Markdown")
    else:
        await update_user(uid, state="menu", partner=None)
        await update.message.reply_text("Siz yana asosiy menyudasiz. Kim bilan suhbatlashmoqchisiz?", reply_markup=get_kb("menu"), parse_mode="Markdown")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text
    user = await get_user(uid)

    if not user: return
    state = user['state']

    # JINS TANLASH
    if state == "gender":
        if text in ["👨 Erkak", "👩 Ayol"]:
            gender_val = "male" if "Erkak" in text else "female"
            await update_user(uid, gender=gender_val, state="menu")
            await update.message.reply_text("✅ Jinsingiz saqlandi!\n\n**Menyu tushuntirishi:**\n🔎 **Jins bo'yicha** - Faqat o'g'il yoki faqat qiz bolani qidirish.\n🎲 **Random** - Kim bo'sh bo'lsa shuni topish.\n🔄 **O'zgartirish** - Jinsingizni qayta belgilash.", reply_markup=get_kb("menu"), parse_mode="Markdown")
        return

    # MENU
    if state == "menu":
        if text == "🔎 Jins bo‘yicha qidirish":
            await update_user(uid, state="search_gender")
            await update.message.reply_text("Kim bilan suhbatlashmoqchisiz? Tanlang:", reply_markup=get_kb("search_gender"))
        elif text == "🎲 Random qidirish":
            await update_user(uid, state="searching", search_pref="random")
            await update.message.reply_text("🔍 **Suhbatdosh qidirilyapti...**\n\nIltimos, kuting. Kimdir bog'lanishi bilan sizga xabar beramiz.", reply_markup=get_kb("searching"), parse_mode="Markdown")
            await find_match(uid, context)
        elif text == "🔄 Jinsni o‘zgartirish":
            await update_user(uid, state="gender")
            await update.message.reply_text("Jinsingizni qayta tanlang:", reply_markup=get_kb("gender"))
        return

    # JINS QIDIRUV TANLOVI
    if state == "search_gender":
        if text == "🔙 Orqaga":
            await update_user(uid, state="menu")
            await update.message.reply_text("Asosiy menyu:", reply_markup=get_kb("menu"))
        elif text in ["👨 O‘g‘il qidirish", "👩 Qiz qidirish"]:
            pref = "male" if "O‘g‘il" in text else "female"
            await update_user(uid, state="searching", search_pref=pref)
            await update.message.reply_text(f"🔍 **{text.split()[-2]} qidirilyapti...**\n\nBu biroz vaqt olishi mumkin.", reply_markup=get_kb("searching"), parse_mode="Markdown")
            await find_match(uid, context)
        return

    # QIDIRUV JARAYONI
    if state == "searching":
        if text == "❌ Bekor qilish":
            pref = user['search_pref']
            await r.lrem(f"queue:{pref}", 0, str(uid))
            await update_user(uid, state="menu")
            await update.message.reply_text("Qidiruv to'xtatildi. Menyu:", reply_markup=get_kb("menu"))
        return

    # CHAT JARAYONI
    if state == "chat":
        partner_id = user['partner']
        
        if text == "⛔ Tugatish":
            await update_user(uid, state="menu", partner=None)
            await update_user(partner_id, state="menu", partner=None)
            await update.message.reply_text("❌ **Suhbat tugatildi.**\n\nYangi suhbat boshlash uchun menyudan foydalaning.", reply_markup=get_kb("menu"), parse_mode="Markdown")
            await context.bot.send_message(partner_id, "❌ **Suhbatdosh suhbatni tugatdi.**\n\nYana qidirishni xohlaysizmi?", reply_markup=get_kb("menu"), parse_mode="Markdown")
        
        elif text == "🔄 Keyingi":
            await update_user(partner_id, state="menu", partner=None)
            await context.bot.send_message(partner_id, "⚠️ **Suhbatdosh suhbatni tark etib, boshqa suhbatdosh qidirishga o'tdi.**", reply_markup=get_kb("menu"), parse_mode="Markdown")
            
            await update_user(uid, state="searching")
            await update.message.reply_text("🔄 **Keyingi suhbatdosh qidirilyapti...**", reply_markup=get_kb("searching"), parse_mode="Markdown")
            await find_match(uid, context)

        elif text == "🚨 Shikoyat":
            await update_user(partner_id, reports=user['reports']+1)
            await update.message.reply_text("🚨 **Shikoyatingiz qabul qilindi.**\n\nMa'muriyat ko'rib chiqadi. Suhbatni davom ettirishingiz mumkin.")
            
        else:
            # Oddiy xabarlarni yuborish
            try:
                await context.bot.send_message(partner_id, text)
            except:
                await update_user(uid, state="menu", partner=None)
                await update.message.reply_text("⚠️ Suhbatdosh bilan aloqa uzildi.", reply_markup=get_kb("menu"))

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    async def setup(application):
        await init()
    
    app.post_init = setup
    app.run_polling()

if __name__ == "__main__":
    main()