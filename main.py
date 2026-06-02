import asyncio
import os
import psycopg
from psycopg.rows import dict_row
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
from aiohttp import web

# ========== متغیرهای محیطی ==========
BOT_TOKEN = os.environ.get('BOT_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
if not BOT_TOKEN or not DATABASE_URL:
    raise Exception("لطفاً BOT_TOKEN و DATABASE_URL را تنظیم کنید.")

# ========== مراحل مکالمه ==========
API_ID_STATE, API_HASH_STATE, PHONE_STATE, CODE_STATE, PASSWORD_STATE, TARGET_CHAT_STATE = range(6)

# ========== اتصال به PostgreSQL ==========
async def get_conn():
    return await psycopg.AsyncConnection.connect(DATABASE_URL)

async def init_db():
    async with await get_conn() as conn:
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
        await conn.commit()

async def get_user_data(user_id):
    async with await get_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute('SELECT api_id, api_hash, session_string, target_chat_id, target_chat_title FROM user_data WHERE user_id = %s', (user_id,))
            return await cur.fetchone()

async def save_user_data(user_id, api_id=None, api_hash=None, session_string=None, target_chat_id=None, target_chat_title=None):
    async with await get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute('SELECT 1 FROM user_data WHERE user_id = %s', (user_id,))
            exists = await cur.fetchone()
            if exists:
                await cur.execute('''
                    UPDATE user_data SET
                        api_id = COALESCE(%s, api_id),
                        api_hash = COALESCE(%s, api_hash),
                        session_string = COALESCE(%s, session_string),
                        target_chat_id = COALESCE(%s, target_chat_id),
                        target_chat_title = COALESCE(%s, target_chat_title)
                    WHERE user_id = %s
                ''', (api_id, api_hash, session_string, target_chat_id, target_chat_title, user_id))
            else:
                await cur.execute('''
                    INSERT INTO user_data (user_id, api_id, api_hash, session_string, target_chat_id, target_chat_title)
                    VALUES (%s, %s, %s, %s, %s, %s)
                ''', (user_id, api_id, api_hash, session_string, target_chat_id, target_chat_title))
            await conn.commit()

async def update_target_chat(user_id, chat_id, chat_title):
    async with await get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute('UPDATE user_data SET target_chat_id = %s, target_chat_title = %s WHERE user_id = %s', (chat_id, chat_title, user_id))
            await conn.commit()

# ========== توابع Telethon ==========
async def send_action_with_duration(client, chat, action_type, duration_sec):
    try:
        action = SendMessageRecordAudioAction() if action_type == 'voice' else SendMessageRecordVideoAction()
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < duration_sec:
            await client(SetTypingRequest(peer=chat, action=action))
            await asyncio.sleep(4.5)
        upload_action = SendMessageUploadAudioAction(progress=0) if action_type == 'voice' else SendMessageUploadVideoAction(progress=0)
        await client(SetTypingRequest(peer=chat, action=upload_action))
        await asyncio.sleep(1)
    except Exception as e:
        print(f"Action error: {e}")

async def send_as_voice_note(client, chat, file_path, duration):
    await send_action_with_duration(client, chat, 'voice', duration)
    await client.send_file(chat, file_path, voice_note=True)

async def send_as_video_note(client, chat, file_path, duration):
    await send_action_with_duration(client, chat, 'video', duration)
    await client.send_file(chat, file_path, video_note=True)

# ========== گرفتن ۱۰ چت آخر ==========
async def get_last_dialogs(user_id):
    data = await get_user_data(user_id)
    if not data or not data['session_string']:
        return None
    client = TelegramClient(StringSession(data['session_string']), data['api_id'], data['api_hash'])
    await client.connect()
    try:
        dialogs = await client.get_dialogs(limit=10)
        result = []
        for d in dialogs:
            title = d.title or d.name or (d.entity.first_name if d.entity else str(d.id))
            result.append((d.id, title))
        return result
    except Exception as e:
        print(f"Error getting dialogs: {e}")
        return []
    finally:
        await client.disconnect()

# ========== منوی اصلی ==========
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = await get_user_data(user_id)
    text = "🎛 **پنل مدیریت**\n\n"
    if data and data['session_string']:
        text += "✅ لاگین هستید\n"
        if data['target_chat_id']:
            text += f"🎯 چت هدف: `{data['target_chat_title'] or data['target_chat_id']}`\n"
        else:
            text += "❌ چت هدف تنظیم نشده\n"
    else:
        text += "❌ لاگین نیستید\n"
    buttons = [
        [InlineKeyboardButton("🔐 لاگین", callback_data="login")],
        [InlineKeyboardButton("🎯 تنظیم چت هدف", callback_data="set_target")],
        [InlineKeyboardButton("📋 وضعیت", callback_data="status")],
        [InlineKeyboardButton("🚪 خروج", callback_data="logout")]
    ]
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))

# ---------- مکالمه لاگین ----------
async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("api_id را وارد کنید:")
    return API_ID_STATE

async def login_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['api_id'] = int(update.message.text.strip())
        await update.message.reply_text("api_hash را وارد کنید:")
        return API_HASH_STATE
    except ValueError:
        await update.message.reply_text("api_id باید عدد باشد. دوباره:")
        return API_ID_STATE

async def login_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['api_hash'] = update.message.text.strip()
    await update.message.reply_text("شماره تلفن (با کد کشور):")
    return PHONE_STATE

async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    context.user_data['phone'] = phone
    client = TelegramClient(StringSession(), context.user_data['api_id'], context.user_data['api_hash'])
    await client.connect()
    try:
        await client.send_code_request(phone)
        context.user_data['temp_client'] = client
        await update.message.reply_text("کد تأیید +1 را وارد کنید (مثلاً 12345->12346):")
        return CODE_STATE
    except Exception as e:
        await update.message.reply_text(f"خطا: {e}")
        return ConversationHandler.END

async def login_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        real_code = int(update.message.text.strip()) - 1
        if real_code < 0: raise ValueError
    except ValueError:
        await update.message.reply_text("عدد معتبر وارد کنید:")
        return CODE_STATE
    client = context.user_data.get('temp_client')
    if not client:
        await update.message.reply_text("نشست منقضی")
        return ConversationHandler.END
    try:
        await client.sign_in(context.user_data['phone'], str(real_code))
        session_string = client.session.save()
        await save_user_data(update.effective_user.id,
            api_id=context.user_data['api_id'],
            api_hash=context.user_data['api_hash'],
            session_string=session_string)
        await client.disconnect()
        await update.message.reply_text("✅ لاگین موفق")
        await main_menu(update, context)
        return ConversationHandler.END
    except SessionPasswordNeededError:
        await update.message.reply_text("رمز دو مرحله‌ای را وارد کنید:")
        return PASSWORD_STATE
    except Exception as e:
        await update.message.reply_text(f"خطا: {e}")
        return ConversationHandler.END

async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pwd = update.message.text.strip()
    client = context.user_data.get('temp_client')
    if not client:
        await update.message.reply_text("نشست منقضی")
        return ConversationHandler.END
    try:
        await client.sign_in(password=pwd)
        session_string = client.session.save()
        await save_user_data(update.effective_user.id,
            api_id=context.user_data['api_id'],
            api_hash=context.user_data['api_hash'],
            session_string=session_string)
        await client.disconnect()
        await update.message.reply_text("✅ لاگین موفق")
        await main_menu(update, context)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"رمز اشتباه: {e}")
        return PASSWORD_STATE

# ---------- تنظیم چت هدف ----------
async def set_target_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🔄 در حال دریافت لیست چت‌های اخیر...")
    dialogs = await get_last_dialogs(query.from_user.id)
    if dialogs is None:
        await query.edit_message_text("❌ ابتدا لاگین کنید.")
        return ConversationHandler.END
    if not dialogs:
        await query.edit_message_text("⚠️ هیچ چتی پیدا نشد. دوباره لاگین کنید.")
        return ConversationHandler.END
    buttons = []
    for chat_id, title in dialogs:
        short_title = title[:30] + "..." if len(title) > 30 else title
        buttons.append([InlineKeyboardButton(f"📌 {short_title}", callback_data=f"select_chat|{chat_id}|{title[:50]}")])
    buttons.append([InlineKeyboardButton("✏️ ورود دستی", callback_data="manual_input")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back_main")])
    await query.edit_message_text("🎯 انتخاب چت هدف:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode='Markdown')
    return TARGET_CHAT_STATE

async def set_target_chat_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    if data == "manual_input":
        await query.edit_message_text("یوزرنیم یا آیدی عددی چت را وارد کنید:\n(برای لغو /cancel)")
        return TARGET_CHAT_STATE
    elif data == "back_main":
        await main_menu(update, context)
        return ConversationHandler.END
    elif data.startswith("select_chat|"):
        parts = data.split("|")
        if len(parts) >= 2:
            try:
                chat_id = int(parts[1])
                chat_title = parts[2] if len(parts) > 2 else str(chat_id)
                await update_target_chat(user_id, chat_id, chat_title)
                await query.edit_message_text(f"✅ چت هدف تنظیم شد: `{chat_title}`")
                await asyncio.sleep(1)
                await main_menu(update, context)
                return ConversationHandler.END
            except ValueError:
                await query.edit_message_text("❌ خطا در شناسایی چت.")
                return ConversationHandler.END
    await query.edit_message_text("❌ دستور نامعتبر.")
    return ConversationHandler.END

async def set_target_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_input = update.message.text.strip()
    data = await get_user_data(user_id)
    if not data or not data['session_string']:
        await update.message.reply_text("❌ ابتدا لاگین کنید.")
        return ConversationHandler.END
    client = TelegramClient(StringSession(data['session_string']), data['api_id'], data['api_hash'])
    await client.connect()
    try:
        if chat_input.lstrip('-').isdigit():
            entity = await client.get_entity(int(chat_input))
        else:
            entity = await client.get_entity(chat_input)
        chat_id = entity.id
        chat_title = getattr(entity, 'title', None) or entity.first_name or str(entity.id)
        await update_target_chat(user_id, chat_id, chat_title)
        await update.message.reply_text(f"✅ چت هدف تنظیم شد: `{chat_title}`")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}")
    finally:
        await client.disconnect()
    return ConversationHandler.END

# ---------- وضعیت و خروج ----------
async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = await get_user_data(query.from_user.id)
    if not data or not data['session_string']:
        text = "❌ لاگین نیستید"
    else:
        text = f"✅ لاگین هستید\n"
        if data['target_chat_id']:
            text += f"🎯 چت هدف: `{data['target_chat_title'] or data['target_chat_id']}`"
        else:
            text += "❌ چت هدف تنظیم نشده"
    await query.edit_message_text(text, parse_mode='Markdown')

async def logout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await save_user_data(query.from_user.id, session_string="")
    await query.edit_message_text("✅ از اکانت خارج شدید.")

# ========== هندلر فایل ==========
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text("🔄 در حال پردازش...")
    try:
        data = await get_user_data(user_id)
        if not data or not data['session_string']:
            await status_msg.edit_text("❌ لاگین نیستید. از منو لاگین کنید.")
            return
        if not data['target_chat_id']:
            await status_msg.edit_text("❌ چت هدف تنظیم نشده.")
            return
        # تشخیص نوع فایل
        msg = update.message
        is_audio = bool(msg.audio or msg.voice)
        is_video = bool(msg.video or msg.video_note)
        if not is_audio and not is_video:
            await status_msg.edit_text("❌ فقط فایل صوتی یا ویدیویی پشتیبانی می‌شود.")
            return
        
        duration = 3
        if is_audio:
            duration = getattr(msg.audio or msg.voice, 'duration', 3)
        else:
            duration = getattr(msg.video or msg.video_note, 'duration', 3)
        if not duration:
            duration = 3
        
        await status_msg.edit_text("📥 در حال دانلود فایل...")
        file_path = await msg.effective_attachment.get_file().download_to_drive()
        
        client = TelegramClient(StringSession(data['session_string']), data['api_id'], data['api_hash'])
        await client.connect()
        target = await client.get_entity(data['target_chat_id'])
        await status_msg.edit_text(f"🎬 {'در حال ارسال ویس' if is_audio else 'در حال ارسال ویدیو'}...")
        if is_audio:
            await send_as_voice_note(client, target, file_path, duration)
        else:
            await send_as_video_note(client, target, file_path, duration)
        await status_msg.edit_text(f"✅ {'ویس' if is_audio else 'ویدیو'} نوت ارسال شد (مدت {duration} ثانیه)")
        await client.disconnect()
        os.remove(file_path)
    except FloodWaitError as e:
        await status_msg.edit_text(f"⏳ محدودیت: {e.seconds} ثانیه صبر کنید")
    except Exception as e:
        await status_msg.edit_text(f"❌ خطا: {str(e)}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("لغو شد.")
    return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await main_menu(update, context)

# ========== وب سرور ==========
async def health_check(request):
    return web.Response(text="OK")

async def run_web():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', 8080)))
    await site.start()
    await asyncio.Event().wait()

async def main():
    await init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    # مکالمه لاگین
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(login_start, pattern='^login$')],
        states={
            API_ID_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_api_id)],
            API_HASH_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_api_hash)],
            PHONE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)],
            CODE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_code)],
            PASSWORD_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    # تنظیم چت هدف
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(set_target_start, pattern='^set_target$')],
        states={
            TARGET_CHAT_STATE: [
                CallbackQueryHandler(set_target_chat_button, pattern='^(select_chat\\||manual_input|back_main)'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_target_manual)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    app.add_handler(CallbackQueryHandler(status_callback, pattern='^status$'))
    app.add_handler(CallbackQueryHandler(logout_callback, pattern='^logout$'))
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VIDEO | filters.VOICE | filters.VIDEO_NOTE, handle_file))
    await app.initialize()
    await app.start()
    asyncio.create_task(app.updater.start_polling())
    await run_web()

if __name__ == '__main__':
    asyncio.run(main())
