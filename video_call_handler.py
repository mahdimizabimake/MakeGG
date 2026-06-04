import asyncio
import os
import subprocess
from pyrogram import Client
from pyrogram.types import Message
from py_tgcalls import PyTgCalls, idle
from py_tgcalls.types import Update, Call, VideoParameters, AudioParameters
from py_tgcalls.types.input_stream import InputStream, AudioStream, VideoStream

API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
DATABASE_URL = os.environ.get('DATABASE_URL', '')

# دیتابیس: ذخیره وضعیت ویدیو کال برای هر کاربر
import asyncpg
import json

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
            reply_msg_id INTEGER,
            reply_active BOOLEAN DEFAULT FALSE,
            reply_chat_id BIGINT,
            auto_video_enabled BOOLEAN DEFAULT FALSE,
            auto_video_path TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    await conn.close()

async def get_user_data(user_id):
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow('SELECT * FROM user_data WHERE user_id = $1', user_id)
    await conn.close()
    return row

async def enable_auto_video(user_id, video_path):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute('UPDATE user_data SET auto_video_enabled = TRUE, auto_video_path = $1 WHERE user_id = $2', video_path, user_id)
    await conn.close()

async def disable_auto_video(user_id):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute('UPDATE user_data SET auto_video_enabled = FALSE, auto_video_path = NULL WHERE user_id = $1', user_id)
    await conn.close()

# ... (توابع کمکی)

async def convert_to_square_ffmpeg(input_path, output_path, target_size=480):
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
    except:
        raise Exception("ffmpeg not found")
    cmd = [
        'ffmpeg', '-i', input_path,
        '-vf', f'crop=min(iw\\,ih):min(iw\\,ih),scale={target_size}:{target_size}',
        '-c:a', 'copy', '-y', output_path
    ]
    process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise Exception(f"ffmpeg error: {stderr.decode()}")
    return output_path

# راه‌اندازی PyTgCalls برای هر کاربر
call_clients = {}

async def setup_pytgcalls(user_id, session_string, api_id, api_hash):
    pyro_client = Client('pyro_user', api_id, api_hash, session_string=session_string)
    await pyro_client.start()
    call_client = PyTgCalls(pyro_client)
    await call_client.start()
    return call_client

async def answer_call(chat_id, call_client, video_path):
    try:
        await call_client.answer_call(chat_id)
        await call_client.play(
            chat_id,
            InputStream(
                AudioStream(video_path),
                VideoStream(video_path)
            )
        )
        # بررسی مدت زمان ویدیو
        result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', video_path], capture_output=True, text=True)
        duration = float(result.stdout.strip())
        await asyncio.sleep(duration)
        await call_client.leave_call(chat_id)
    except Exception as e:
        print(f"Error answering call for {chat_id}: {e}")

async def restore_auto_video_calls():
    async with await get_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute('SELECT user_id, api_id, api_hash, session_string, auto_video_path FROM user_data WHERE auto_video_enabled = TRUE AND session_string IS NOT NULL')
            active_users = await cur.fetchall()
            for user in active_users:
                user_id = user['user_id']
                api_id = user['api_id']
                api_hash = user['api_hash']
                session_str = user['session_string']
                video_path = user['auto_video_path']
                if video_path and os.path.exists(video_path):
                    call_client = await setup_pytgcalls(user_id, session_str, api_id, api_hash)
                    if call_client:
                        call_clients[user_id] = call_client
                        @call_client.on_call()
                        async def on_incoming_call(call: Call):
                            if call.chat_id == user_id:
                                current_data = await get_user_data(user_id)
                                vid_path = current_data.get('auto_video_path')
                                if vid_path and os.path.exists(vid_path):
                                    await answer_call(call.chat_id, call_client, vid_path)

async def main():
    await init_db()
    await restore_auto_video_calls()
    await idle()

if __name__ == '__main__':
    asyncio.run(main())
