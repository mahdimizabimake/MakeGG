import asyncio
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SetTypingRequest
from telethon.tl.types import (
    SendMessageRecordAudioAction, SendMessageUploadAudioAction,
    SendMessageRecordVideoAction, SendMessageUploadVideoAction
)
from telethon.errors import FloodWaitError, SessionPasswordNeededError
import asyncpg
from aiohttp import web

# ========== متغیرهای محیطی ==========
BOT_TOKEN = os.environ.get('BOT_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
if not BOT_TOKEN or not DATABASE_URL:
    raise Exception("لطفاً BOT_TOKEN و DATABASE_URL را تنظیم کنید.")

# ========== مراحل مکالمه ==========
API_ID_STATE, API_HASH_STATE, PHONE_STATE, CODE_STATE, PASSWORD_STATE, TARGET_CHAT_STATE = range(6)

# ========== دیتابیس ==========
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS user_data (
            user_id BIGINT PRIMARY KEY,
            api_id INTEGER,
            api_hash TEXT,
            session_string TEXT,
            target_chat_id BIGINT,
            target_chat_title TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    await conn.close()

async def get_user_data(user_id):
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow('SELECT api_id, api_hash, session_string, target_chat_id, target_chat_title FROM user_data WHERE user_id = $1', user_id)
    await conn.close()
    return row

async def save_user_data(user_id, api_id=None, api_hash=None, session_string=None, target_chat_id=None, target_chat_title=None):
    conn = await asyncpg.connect(DATABASE_URL)
    existing = await conn.fetchrow('SELECT * FROM user_data WHERE user_id = $1', user_id)
    if existing:
        await conn.execute('''
            UPDATE user_data SET
                api_id = COALESCE($2, api_id),
                api_hash = COALESCE($3, api_hash),
                session_string = COALESCE($4, session_string),
                target_chat_id = COALESCE($5, target_chat_id),
                target_chat_title = COALESCE($6, target_chat_title)
            WHERE user_id = $1
        ''', user_id, api_id, api_hash, session_string, target_chat_id, target_chat_title)
    else:
        await conn.execute('''
            INSERT INTO user_data (user_id, api_id, api_hash, session_string, target_chat_id, target_chat_title)
            VALUES ($1, $2, $3, $4, $5, $6)
        ''', user_id, api_id, api_hash, session_string, target_chat_id, target_chat_title)
    await conn.close()

async def update_target_chat(user_id, chat_id, chat_title):
    await save_user_data(user_id, target_chat_id=chat_id, target_chat_title=chat_title)

# ========== توابع کمکی Telethon برای هر کاربر ==========
async def send_action_with_duration(client, chat, action_type, duration_sec):
    try:
        if action_type == 'voice':
            action = SendMessageRecordAudioAction()
        else:
            action = SendMessageRecordVideoAction()
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < duration_sec:
            await client(SetTypingRequest(peer=chat, action=action))
            await asyncio.sleep(4.5)
        upload_action = SendMessageUploadAudioAction(progress=0) if action_type == 'voice' else SendMessageUploadVideoAction(progress=0)
        await client(SetTypingRequest(peer=chat, action=upload_action))
        await asyncio.sleep(1)
    except Exception as e:
        print(f"خطا در اکشن: {e}")

async def send_as_voice_note(client, chat, file_path, duration):
    await send_action_with_duration(client, chat, 'voice', duration)
    await client.send_file(chat, file_path, voice_note=True)

async def send_as_video_note(client, chat, file_path, duration):
    await send_action_with_duration(client, chat, 'video', duration)
    await client.send_file(chat, file_path, video_note=True)

# ========== منوی اصلی ==========
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = await get_user_data(user_id)
    text = "🎛 **پنل مدیریت یوزربات شخصی شما**\n\n"
    if data and data[2]:  # session_string وجود دارد
        text += "✅ وضعیت: **لاگین هستید**\n"
        if data[3]:  # target_chat_id
            text += f"🎯 چت هدف: `{data[4] or data[3]}`\n"
        else:
            text += "❌ چت هدف تنظیم نشده.\n"
    else:
        text += "❌ شما لاگین نیستید. ابتدا لاگین کنید.\n"
    buttons = [
        [InlineKeyboardButton("🔐 لاگین (با api_id و api_hash)", callback_data="login")],
        [InlineKeyboardButton("🎯 تنظیم چت هدف", callback_data="set_target")],
        [InlineKeyboardButton("📋 وضعیت", callback_data="status")],
        [InlineKeyboardButton("🚪 خروج از اکانت", callback_data="logout")]
    ]
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))

# ========== مکالمه لاگین (گرفتن api_id, api_hash, شماره, کد+1, رمز) ==========
async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "🔐 **مرحله 1 از 5: api_id**\n\n"
        "لطفاً `api_id` اپلیکیشن خود را وارد کنید.\n"
        "(از my.telegram.org دریافت کنید)\n\n"
        "برای لغو: /cancel"
    )
    return API_ID_STATE

async def login_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        api_id = int(update.message.text.strip())
        context.user_data['api_id'] = api_id
        await update.message.reply_text(
            "🔐 **مرحله 2 از 5: api_hash**\n\n"
            "لطفاً `api_hash` خود را وارد کنید.\n"
            "(رشته طولانی)"
        )
        return API_HASH_STATE
    except ValueError:
        await update.message.reply_text("❌ api_id باید یک عدد صحیح باشد. دوباره وارد کنید:")
        return API_ID_STATE

async def login_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_hash = update.message.text.strip()
    context.user_data['api_hash'] = api_hash
    await update.message.reply_text(
        "📞 **مرحله 3 از 5: شماره تلفن**\n\n"
        "لطفاً شماره تلفن خود را به همراه کد کشور وارد کنید.\n"
        "مثال: `+989123456789`"
    )
    return PHONE_STATE

async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    context.user_data['phone'] = phone
    api_id = context.user_data['api_id']
    api_hash = context.user_data['api_hash']
    # ساخت کلاینت موقت برای ارسال درخواست کد
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        await client.send_code_request(phone)
        context.user_data['temp_client'] = client
        await update.message.reply_text(
            "✅ کد تأیید به تلگرام شما ارسال شد.\n\n"
            "⚠️ **نکته امنیتی**: لطفاً کد دریافتی را **به اضافه ۱** برای من بفرستید.\n"
            "مثال: اگر کد شما `12345` است، عدد `12346` را ارسال کنید.\n\n"
            "من خودکار ۱ واحد از آن کم کرده و کد اصلی را استفاده می‌کنم.\n"
            "کد (+1) را وارد کنید:"
        )
        return CODE_STATE
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در ارسال کد: {str(e)}\nدوباره تلاش کنید /start")
        return ConversationHandler.END

async def login_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_code_plus_one = int(update.message.text.strip())
        real_code = user_code_plus_one - 1
        if real_code < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ کد باید یک عدد باشد. لطفاً کد +1 را وارد کنید:")
        return CODE_STATE
    client = context.user_data.get('temp_client')
    phone = context.user_data.get('phone')
    if not client:
        await update.message.reply_text("❌ نشست منقضی شده. دوباره /start کنید.")
        return ConversationHandler.END
    try:
        await client.sign_in(phone, str(real_code))
        session_string = client.session.save()
        user_id = update.effective_user.id
        api_id = context.user_data['api_id']
        api_hash = context.user_data['api_hash']
        await save_user_data(user_id, api_id=api_id, api_hash=api_hash, session_string=session_string)
        await client.disconnect()
        await update.message.reply_text("✅ لاگین موفقیت‌آمیز بود. اکنون می‌توانید چت هدف را تنظیم کنید.\nاز منوی اصلی استفاده کنید.")
        await main_menu(update, context)
        return ConversationHandler.END
    except SessionPasswordNeededError:
        await update.message.reply_text("🔐 **مرحله 5 از 5: رمز دو مرحله‌ای**\n\nحساب شما رمز两步 تأیید دارد. لطفاً رمز خود را وارد کنید:")
        return PASSWORD_STATE
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {str(e)}")
        return ConversationHandler.END

async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pwd = update.message.text.strip()
    client = context.user_data.get('temp_client')
    if not client:
        await update.message.reply_text("❌ نشست منقضی شده.")
        return ConversationHandler.END
    try:
        await client.sign_in(password=pwd)
        session_string = client.session.save()
        user_id = update.effective_user.id
        api_id = context.user_data['api_id']
        api_hash = context.user_data['api_hash']
        await save_user_data(user_id, api_id=api_id, api_hash=api_hash, session_string=session_string)
        await client.disconnect()
        await update.message.reply_text("✅ لاگین موفقیت‌آمیز بود.")
        await main_menu(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ رمز اشتباه است: {str(e)}")
        return PASSWORD_STATE

# ========== تنظیم چت هدف ==========
async def set_target_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "🎯 **تنظیم چت هدف**\n\n"
        "لطفاً **یوزرنیم** (مثل @username) یا **آیدی عددی** چت مورد نظر را وارد کنید.\n"
        "برای لغو: /cancel"
    )
    return TARGET_CHAT_STATE

async def set_target_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_input = update.message.text.strip()
    data = await get_user_data(user_id)
    if not data or not data[2]:  # session_string وجود ندارد
        await update.message.reply_text("❌ شما لاگین نیستید. ابتدا لاگین کنید /start")
        return ConversationHandler.END
    session_string = data[2]
    api_id = data[0]
    api_hash = data[1]
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    try:
        if chat_input.lstrip('-').isdigit():
            entity = await client.get_entity(int(chat_input))
        else:
            entity = await client.get_entity(chat_input)
        chat_id = entity.id
        chat_title = getattr(entity, 'title', None) or entity.first_name or str(entity.id)
        await update_target_chat(user_id, chat_id, chat_title)
        await update.message.reply_text(f"✅ چت هدف تنظیم شد: `{chat_title}` (ID: {chat_id})")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در یافتن چت: {str(e)}")
    finally:
        await client.disconnect()
    return ConversationHandler.END

# ========== دکمه وضعیت ==========
async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = await get_user_data(user_id)
    if not data or not data[2]:
        text = "❌ شما لاگین نیستید. از دکمه لاگین استفاده کنید."
    else:
        text = f"✅ لاگین هستید.\n"
        if data[3]:
            text += f"🎯 چت هدف: `{data[4] or data[3]}` (ID: {data[3]})"
        else:
            text += "❌ چت هدف تنظیم نشده."
    await query.edit_message_text(text, parse_mode='Markdown')

# ========== خروج از اکانت ==========
async def logout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    await save_user_data(user_id, session_string="")
    await query.edit_message_text("✅ از اکانت خود خارج شدید. برای ورود مجدد از دکمه لاگین استفاده کنید.")

# ========== هندلر فایل‌ها ==========
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = await get_user_data(user_id)
    if not data or not data[2] or not data[3]:
        await update.message.reply_text("❌ ابتدا لاگین کنید و چت هدف را تنظیم نمایید. /start")
        return
    session_string = data[2]
    api_id = data[0]
    api_hash = data[1]
    target_chat_id = data[3]
    # دانلود فایل
    file = await update.message.effective_attachment
    if not file:
        await update.message.reply_text("لطفاً یک فایل صوتی یا ویدیویی بفرستید.")
        return
    duration = getattr(file, 'duration', 3) or 3
    file_path = await file.get_file().download_to_drive()
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    try:
        target_entity = await client.get_entity(target_chat_id)
        mime_type = getattr(file, 'mime_type', '')
        if mime_type.startswith('audio/') or file.__class__.__name__.find('Audio') != -1:
            await send_as_voice_note(client, target_entity, file_path, duration)
            await update.message.reply_text(f"🎙️ ویس نوت ارسال شد (مدت {duration} ثانیه)")
        elif mime_type.startswith('video/') or file.__class__.__name__.find('Video') != -1:
            await send_as_video_note(client, target_entity, file_path, duration)
            await update.message.reply_text(f"📹 ویدیو نوت ارسال شد (مدت {duration} ثانیه)")
        else:
            await update.message.reply_text("❌ فقط فایل‌های صوتی یا ویدیویی پشتیبانی می‌شوند.")
    except FloodWaitError as e:
        await update.message.reply_text(f"⏳ محدودیت تلگرام: {e.seconds} ثانیه صبر کنید")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در ارسال: {str(e)}")
    finally:
        await client.disconnect()
        if os.path.exists(file_path):
            os.remove(file_path)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("عملیات لغو شد.")
    return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await main_menu(update, context)

# ========== وب سرور برای Render ==========
async def health_check(request):
    return web.Response(text="OK")

async def run_web():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', 8080)))
    await site.start()
    print(f"✅ وب سرور روی پورت {os.environ.get('PORT', 8080)} اجرا شد")
    await asyncio.Event().wait()

async def main():
    await init_db()
    application = Application.builder().token(BOT_TOKEN).build()
    # مکالمه لاگین
    login_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(login_start, pattern='^login$')],
        states={
            API_ID_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_api_id)],
            API_HASH_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_api_hash)],
            PHONE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)],
            CODE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_code)],
            PASSWORD_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    application.add_handler(login_conv)
    # مکالمه تنظیم چت هدف
    target_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_target_start, pattern='^set_target$')],
        states={TARGET_CHAT_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_target_chat)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    application.add_handler(target_conv)
    # دکمه‌های دیگر
    application.add_handler(CallbackQueryHandler(status_callback, pattern='^status$'))
    application.add_handler(CallbackQueryHandler(logout_callback, pattern='^logout$'))
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.AUDIO | filters.VIDEO | filters.VOICE | filters.VIDEO_NOTE, handle_file))
    # اجرا
    await application.initialize()
    await application.start()
    asyncio.create_task(application.updater.start_polling())
    await run_web()

if __name__ == '__main__':
    asyncio.run(main())
