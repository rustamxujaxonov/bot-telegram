import os
import logging
import asyncpg
import redis.asyncio as redis
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- SOZLAMALAR ---
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")

# --- TO'LOV SOZLAMALARI ---
ADMIN_ID = 1442214910  # <--- BU YERGA O'ZINGIZNING ID-INGIZNI YOZING! (masalan: 51234567)
CARD_NUMBER = "5614682115991368" # <--- KARTANGIZNI YOZING
CARD_HOLDER = "Rustamxon Xujaxonov"

BAD_WORDS = ["janxknwkxnkwnxkwnxknwxknwx"]

db = None
r = None

# ===== DATABASE VA REDIS =====
async def init():
    global db, r
    try:
        db = await asyncpg.create_pool(DATABASE_URL)
        r = redis.from_url(REDIS_URL, decode_responses=True)

        async with db.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                gender TEXT,
                search_pref TEXT DEFAULT 'random',
                state TEXT DEFAULT 'gender',
                partner BIGINT,
                reports INT DEFAULT 0,
                is_banned BOOLEAN DEFAULT FALSE,
                is_premium BOOLEAN DEFAULT FALSE
            )
            """)
            # Ustunlarni tekshirish
            for col in [
                ("is_premium", "BOOLEAN DEFAULT FALSE"),
                ("is_banned", "BOOLEAN DEFAULT FALSE")
            ]:
                try:
                    await conn.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col[0]} {col[1]}")
                except: pass
        logger.info("✅ Baza tayyor!")
    except Exception as e:
        logger.error(f"❌ Xatolik: {e}")

async def get_user(uid):
    async with db.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)

async def update_user(uid, **kwargs):
    if not kwargs: return
    cols = ", ".join([f"{k}=${i+2}" for i, k in enumerate(kwargs.keys())])
    vals = list(kwargs.values())
    async with db.acquire() as conn:
        await conn.execute(f"UPDATE users SET {cols} WHERE user_id=$1", uid, *vals)

# ===== KEYBOARD =====
def get_kb(state, is_premium=False):
    if state == "gender":
        return ReplyKeyboardMarkup([["👨 Erkak", "👩 Ayol"]], resize_keyboard=True)
    if state == "menu":
        vip_text = "🔎 Jins bo‘yicha qidirish" if is_premium else "🔎 Jins bo‘yicha (⭐ VIP)"
        return ReplyKeyboardMarkup([
            ["🎲 Random qidirish"],
            [vip_text],
            ["🔄 Jinsni o‘zgartirish", "💎 VIP sotib olish"]
        ], resize_keyboard=True)
    if state == "search_gender":
        return ReplyKeyboardMarkup([["👨 O‘g‘il qidirish", "👩 Qiz qidirish"], ["🔙 Orqaga"]], resize_keyboard=True)
    if state == "searching":
        return ReplyKeyboardMarkup([["❌ Bekor qilish"]], resize_keyboard=True)
    if state == "chat":
        return ReplyKeyboardMarkup([["⛔ Tugatish", "🔄 Keyingi"], ["🚨 Shikoyat"]], resize_keyboard=True)
    return ReplyKeyboardRemove()

# ===== MATCHMAKING =====
async def find_match(uid, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(uid)
    if not user: return False
    
    my_gender = user['gender']
    pref = user['search_pref']

    search_key = f"queue:{pref}" 
    my_queue_key = f"queue:{my_gender}" if pref != "random" else "queue:random"
    
    partner_id = await r.lpop(search_key)

    if partner_id:
        partner_id = int(partner_id)
        if partner_id == uid:
            await r.rpush(my_queue_key, uid)
            return False
            
        p_data = await get_user(partner_id)
        if not p_data or p_data['state'] != "searching":
            return await find_match(uid, context)

        await update_user(uid, partner=partner_id, state="chat")
        await update_user(partner_id, partner=uid, state="chat")

        msg = "🎉 **Suhbatdosh topildi!**\n\nMarhamat, suhbatni boshlang."
        await context.bot.send_message(uid, msg, reply_markup=get_kb("chat"), parse_mode="Markdown")
        await context.bot.send_message(partner_id, msg, reply_markup=get_kb("chat"), parse_mode="Markdown")
        return True
    else:
        queue_to_join = f"queue:{my_gender}" if pref != "random" else "queue:random"
        await r.rpush(queue_to_join, uid)
        return False

# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = await get_user(uid)
    
    if not user:
        async with db.acquire() as conn:
            await conn.execute("INSERT INTO users (user_id, state) VALUES ($1, 'gender') ON CONFLICT DO NOTHING", uid)
        await update.message.reply_text("👋 **Xush kelibsiz!** Jinsingizni tanlang:", reply_markup=get_kb("gender"), parse_mode="Markdown")
    else:
        if user['is_banned']: return await update.message.reply_text("🚫 Bloklangansiz!")
        await update_user(uid, state="menu", partner=None)
        await update.message.reply_text("Asosiy menyu:", reply_markup=get_kb("menu", user['is_premium']))

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text
    user = await get_user(uid)

    if not user or user['is_banned']: return

    # To'lov chekini qabul qilish (waiting_check holatida rasm kelsa)
    if user['state'] == "waiting_check" and update.message.photo:
        photo_id = update.message.photo[-1].file_id
        await update_user(uid, state="menu")
        await update.message.reply_text("✅ Chek qabul qilindi. Admin tasdiqlashini kuting.", reply_markup=get_kb("menu", user['is_premium']))
        
        # Adminga yuborish
        kb = [[InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"accept_{uid}"),
               InlineKeyboardButton("❌ Rad etish", callback_data=f"reject_{uid}")]]
        await context.bot.send_photo(ADMIN_ID, photo_id, caption=f"💰 To'lov!\nUser: {uid}\nUsername: @{update.effective_user.username}", reply_markup=InlineKeyboardMarkup(kb))
        return

    if not text: return

    # STATE: GENDER
    if user['state'] == "gender":
        if text in ["👨 Erkak", "👩 Ayol"]:
            gender_val = "male" if "Erkak" in text else "female"
            await update_user(uid, gender=gender_val, state="menu")
            await update.message.reply_text("Tayyor! Kimni qidiramiz?", reply_markup=get_kb("menu", False))
        return

    # STATE: MENU
    elif user['state'] == "menu":
        if text == "🎲 Random qidirish":
            await update_user(uid, state="searching", search_pref="random")
            await update.message.reply_text("🔍 Qidirilmoqda...", reply_markup=get_kb("searching"))
            await find_match(uid, context)
            
        elif "Jins bo‘yicha" in text:
            if not user['is_premium']:
                await update.message.reply_text("⭐ Bu funksiya faqat VIP a'zolar uchun! Sotib olish uchun '💎 VIP sotib olish' tugmasini bosing.")
            else:
                await update_user(uid, state="search_gender")
                await update.message.reply_text("Kimni qidiramiz?", reply_markup=get_kb("search_gender"))

        elif text == "💎 VIP sotib olish":
            await update_user(uid, state="waiting_check")
            await update.message.reply_text(
                f"💎 **VIP status (10,000 UZS)**\n\n"
                f"Imkoniyat: Jins bo'yicha cheksiz qidirish.\n\n"
                f"💳 Karta: `{CARD_NUMBER}`\n👤 Ega: {CARD_HOLDER}\n\n"
                f"To'lovni amalga oshirib, **chekni (rasm)** shu yerga yuboring!",
                parse_mode="Markdown", reply_markup=ReplyKeyboardMarkup([["🔙 Bekor qilish"]], resize_keyboard=True)
            )
            
        elif text == "🔄 Jinsni o‘zgartirish":
            await update_user(uid, state="gender")
            await update.message.reply_text("Jinsingizni tanlang:", reply_markup=get_kb("gender"))

    # STATE: SEARCH_GENDER
    elif user['state'] == "search_gender":
        if text == "🔙 Orqaga":
            await update_user(uid, state="menu")
            await update.message.reply_text("Menyu:", reply_markup=get_kb("menu", user['is_premium']))
        elif text in ["👨 O‘g‘il qidirish", "👩 Qiz qidirish"]:
            pref = "male" if "O‘g‘il" in text else "female"
            await update_user(uid, state="searching", search_pref=pref)
            await update.message.reply_text("🔍 Qidirilmoqda...", reply_markup=get_kb("searching"))
            await find_match(uid, context)

    # STATE: SEARCHING
    elif user['state'] == "searching":
        if text == "❌ Bekor qilish":
            pref = user['search_pref']
            q_key = f"queue:{user['gender']}" if pref != "random" else "queue:random"
            await r.lrem(q_key, 0, str(uid))
            await update_user(uid, state="menu")
            await update.message.reply_text("Bekor qilindi.", reply_markup=get_kb("menu", user['is_premium']))

    # STATE: CHAT
    elif user['state'] == "chat":
        p_id = user['partner']
        if not p_id: return
        
        if text == "⛔ Tugatish":
            await update_user(uid, state="menu", partner=None)
            await update_user(p_id, state="menu", partner=None)
            await update.message.reply_text("❌ Suhbat tugadi.", reply_markup=get_kb("menu", user['is_premium']))
            await context.bot.send_message(p_id, "❌ Suhbatdosh suhbatni tugatdi.", reply_markup=get_kb("menu", (await get_user(p_id))['is_premium']))
        
        elif text == "🔄 Keyingi":
            await update_user(p_id, state="menu", partner=None)
            await context.bot.send_message(p_id, "⚠️ Suhbatdosh boshqa chatga o'tdi.", reply_markup=get_kb("menu", (await get_user(p_id))['is_premium']))
            await update_user(uid, state="searching")
            await update.message.reply_text("🔄 Keyingi qidiruv...", reply_markup=get_kb("searching"))
            await find_match(uid, context)

        elif text == "🚨 Shikoyat":
            await update_user(p_id, reports=user['reports']+1)
            await update_user(uid, state="menu", partner=None)
            await update_user(p_id, state="menu", partner=None)
            await update.message.reply_text("🚨 Shikoyat qabul qilindi.", reply_markup=get_kb("menu", user['is_premium']))
        
        else:
            try: await context.bot.send_message(p_id, text)
            except: pass

# ===== CALLBACK HANDLER (ADMIN UCHUN) =====
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    action, target_id = data.split("_")
    target_id = int(target_id)

    if action == "accept":
        await update_user(target_id, is_premium=True)
        await context.bot.send_message(target_id, "🎉 Tabriklaymiz! To'lovingiz tasdiqlandi. Endi siz VIP a'zosiz!")
        await query.edit_message_caption("✅ Tasdiqlandi!")
    else:
        await context.bot.send_message(target_id, "❌ Uzr, to'lovingiz tasdiqlanmadi.")
        await query.edit_message_caption("❌ Rad etildi.")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))
    
    async def post_init(application):
        await init()
        await application.bot.delete_webhook(drop_pending_updates=True)
        print("🚀 Bot ishga tushdi!")

    app.post_init = post_init
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()