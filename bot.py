import asyncio
import hashlib
import hmac
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN', '')
ADMINS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip().isdigit()}
OLLAMA_URL = os.getenv('OLLAMA_URL', 'http://localhost:11434').rstrip('/')
OLLAMA_MODEL = os.getenv('OLLAMA_MODEL', 'llama3.2:3b')

# Webhook settings (set these on Render as environment variables)
# WEBHOOK_URL  = https://<your-app>.onrender.com
# WEBHOOK_PATH = /webhook  (default)
# WEBHOOK_SECRET = any random string for security
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '').rstrip('/')
WEBHOOK_PATH = os.getenv('WEBHOOK_PATH', '/webhook')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '')

# Render assigns a PORT env var; default 8080 for local testing
PORT = int(os.getenv('PORT', '8080'))

DB = Path(__file__).with_name('education.db')
router = Router()

STREAMS = {'science': 'Physical Science', 'bio': 'Biological Science', 'commerce': 'Commerce', 'arts': 'Arts', 'tech': 'Technology'}
SUBJECTS = {
    'science': ['Combined Mathematics', 'Physics', 'Chemistry'],
    'bio': ['Biology', 'Chemistry', 'Physics'],
    'commerce': ['Accounting', 'Business Studies', 'Economics'],
    'arts': ['Sinhala', 'Logic', 'Geography'],
    'tech': ['Engineering Technology', 'Science for Technology', 'ICT'],
}


def connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con


def init_db():
    with closing(connect()) as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS students(user_id INTEGER PRIMARY KEY, name TEXT NOT NULL, stream TEXT);
        CREATE TABLE IF NOT EXISTS papers(id INTEGER PRIMARY KEY AUTOINCREMENT, stream TEXT NOT NULL, subject TEXT NOT NULL, year INTEGER NOT NULL, file_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS questions(id INTEGER PRIMARY KEY AUTOINCREMENT, stream TEXT NOT NULL, subject TEXT NOT NULL, question TEXT NOT NULL, a TEXT NOT NULL, b TEXT NOT NULL, c TEXT NOT NULL, d TEXT NOT NULL, answer TEXT NOT NULL CHECK(answer IN ('A','B','C','D')), explanation TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS scores(user_id INTEGER PRIMARY KEY, points INTEGER NOT NULL DEFAULT 0, answered INTEGER NOT NULL DEFAULT 0, FOREIGN KEY(user_id) REFERENCES students(user_id));
        ''')
        con.commit()


def query(sql, params=()):
    with closing(connect()) as con:
        return con.execute(sql, params).fetchall()


def execute(sql, params=()):
    with closing(connect()) as con:
        cur = con.execute(sql, params)
        con.commit()
        return cur.lastrowid


def keyboard(items, prefix):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(label), callback_data=f'{prefix}:{value}')]
        for label, value in items
    ])


def menu():
    return keyboard([('📚 Past Papers', 'papers'), ('📝 MCQ Quiz', 'quiz'), ('🤖 AI Tutor', 'ai'), ('🏆 Leaderboard', 'leaderboard'), ('📊 My Score', 'score'), ('🔄 Change Stream', 'stream')], 'menu')


class AddPaper(StatesGroup):
    stream = State()
    subject = State()
    year = State()
    document = State()


class AddQuestion(StatesGroup):
    stream = State()
    subject = State()
    content = State()


class AIChat(StatesGroup):
    question = State()


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    execute('INSERT INTO students(user_id,name) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name', (message.from_user.id, message.from_user.full_name))
    execute('INSERT OR IGNORE INTO scores(user_id) VALUES(?)', (message.from_user.id,))
    row = query('SELECT stream FROM students WHERE user_id=?', (message.from_user.id,))[0]
    if row['stream']:
        await message.answer('Welcome! Choose an option:', reply_markup=menu())
    else:
        await message.answer('ඔබේ A/L stream එක තෝරන්න:', reply_markup=keyboard([(v,k) for k,v in STREAMS.items()], 'stream'))


@router.callback_query(F.data.startswith('stream:'))
async def choose_stream(call: CallbackQuery, state: FSMContext):
    stream = call.data.split(':', 1)[1]
    if stream not in STREAMS:
        return await call.answer('Invalid stream', show_alert=True)
    execute('UPDATE students SET stream=? WHERE user_id=?', (stream, call.from_user.id))
    await state.clear()
    await call.message.answer(f'Stream: {STREAMS[stream]}', reply_markup=menu())
    await call.answer()


@router.callback_query(F.data.startswith('menu:'))
async def menu_click(call: CallbackQuery, state: FSMContext):
    action = call.data.split(':', 1)[1]
    await state.clear()
    if action == 'stream':
        await call.message.answer('Choose stream:', reply_markup=keyboard([(v,k) for k,v in STREAMS.items()], 'stream'))
    elif action == 'leaderboard':
        rows = query('SELECT s.name, sc.points, sc.answered FROM scores sc JOIN students s ON s.user_id=sc.user_id ORDER BY sc.points DESC, sc.answered ASC LIMIT 10')
        text = '\n'.join(f'{i}. {r["name"]}: {r["points"]} points ({r["answered"]} answers)' for i,r in enumerate(rows,1))
        await call.message.answer('🏆 Leaderboard\n' + (text or 'No results yet'))
    elif action == 'score':
        rows = query('SELECT points, answered FROM scores WHERE user_id=?', (call.from_user.id,))
        r = rows[0] if rows else {'points':0,'answered':0}
        await call.message.answer(f'Your total: {r["points"]} / {r["answered"]} correct answers')
    elif action == 'ai':
        await state.set_state(AIChat.question)
        await call.message.answer('ඔබේ A/L ප්‍රශ්නය එවන්න. AI ලබාදෙන පිළිතුරු verify කරන්න. /cancel to exit.')
    elif action in ('papers', 'quiz'):
        rows = query('SELECT stream FROM students WHERE user_id=?', (call.from_user.id,))
        stream = rows[0]['stream'] if rows else None
        if not stream:
            await call.message.answer('First choose stream:', reply_markup=keyboard([(v,k) for k,v in STREAMS.items()], 'stream'))
        else:
            await call.message.answer('Choose subject:', reply_markup=keyboard([(s,s) for s in SUBJECTS[stream]], action))
    await call.answer()


@router.callback_query(F.data.startswith('papers:'))
async def papers(call: CallbackQuery):
    subject = call.data.split(':',1)[1]
    rows = query('SELECT stream FROM students WHERE user_id=?', (call.from_user.id,))
    stream = rows[0]['stream'] if rows else None
    if not stream or subject not in SUBJECTS.get(stream, []):
        return await call.answer('Choose a valid subject', show_alert=True)
    papers = query('SELECT id, year FROM papers WHERE stream=? AND subject=? ORDER BY year DESC', (stream,subject))
    await call.message.answer('Select paper:' if papers else 'No papers uploaded yet.', reply_markup=keyboard([(f'{p["year"]} paper #{p["id"]}', p['id']) for p in papers[:80]], 'paper') if papers else None)
    await call.answer()


@router.callback_query(F.data.startswith('paper:'))
async def send_paper(call: CallbackQuery):
    pid = call.data.split(':',1)[1]
    rows = query('SELECT p.* FROM papers p JOIN students s ON s.stream=p.stream WHERE p.id=? AND s.user_id=?', (pid, call.from_user.id))
    if not rows:
        return await call.answer('Paper not found', show_alert=True)
    p = rows[0]
    await call.message.answer_document(p['file_id'], caption=f'{p["subject"]} – {p["year"]}')
    await call.answer()


@router.callback_query(F.data.startswith('quiz:'))
async def quiz(call: CallbackQuery, state: FSMContext):
    subject = call.data.split(':',1)[1]
    rows = query('SELECT stream FROM students WHERE user_id=?', (call.from_user.id,))
    stream = rows[0]['stream'] if rows else None
    if not stream or subject not in SUBJECTS.get(stream, []):
        return await call.answer('Invalid subject', show_alert=True)
    qs = query('SELECT * FROM questions WHERE stream=? AND subject=? ORDER BY RANDOM() LIMIT 10', (stream,subject))
    if not qs:
        await call.message.answer('No quiz questions uploaded yet.')
    else:
        await state.update_data(quiz_ids=[r['id'] for r in qs], index=0, correct=0)
        await send_question(call.message, state)
    await call.answer()


async def send_question(message: Message, state: FSMContext):
    data = await state.get_data()
    ids, index = data['quiz_ids'], data['index']
    if index >= len(ids):
        await message.answer(f'🎉 Quiz complete! Score: {data["correct"]}/{len(ids)}', reply_markup=menu())
        await state.clear()
        return
    rows = query('SELECT * FROM questions WHERE id=?', (ids[index],))
    if not rows:
        await state.update_data(index=index+1)
        return await send_question(message, state)
    q = rows[0]
    text = f'Question {index+1}/{len(ids)}\n\n{q["question"]}\n\nA. {q["a"]}\nB. {q["b"]}\nC. {q["c"]}\nD. {q["d"]}'
    await message.answer(text, reply_markup=keyboard([(x,x) for x in 'ABCD'], 'answer'))


@router.callback_query(F.data.startswith('answer:'))
async def answer(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if 'quiz_ids' not in data:
        return await call.answer('Quiz expired. Start again.', show_alert=True)
    index = data['index']
    rows = query('SELECT * FROM questions WHERE id=?', (data['quiz_ids'][index],))
    if not rows:
        return await call.answer('Question unavailable', show_alert=True)
    q = rows[0]
    chosen = call.data.split(':',1)[1]
    if chosen not in 'ABCD':
        return await call.answer('Invalid answer', show_alert=True)
    correct = chosen == q['answer']
    # Remove old buttons before advancing; repeated taps cannot award points twice.
    await call.message.edit_reply_markup(reply_markup=None)
    execute('UPDATE scores SET points=points+?, answered=answered+1 WHERE user_id=?', (int(correct),call.from_user.id))
    await state.update_data(index=index+1, correct=data['correct']+int(correct))
    await call.message.answer(f'{"✅ Correct" if correct else "❌ Incorrect"}. Answer: {q["answer"]}\n{q["explanation"]}')
    await send_question(call.message, state)
    await call.answer()


@router.message(Command('cancel'))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('Cancelled.', reply_markup=menu())


def is_admin(message: Message):
    return message.from_user.id in ADMINS


@router.message(Command('myid'))
async def myid(message: Message):
    await message.answer(f'Your Telegram numeric ID: {message.from_user.id}')


@router.message(Command('addpaper'))
async def addpaper(message: Message, state: FSMContext):
    if not is_admin(message): return await message.answer('Admin only.')
    await state.clear()
    await state.set_state(AddPaper.stream)
    await message.answer('Stream key: science / bio / commerce / arts / tech')


@router.message(AddPaper.stream)
async def paper_stream(message: Message, state: FSMContext):
    key = (message.text or '').strip().lower()
    if key not in STREAMS: return await message.answer('Invalid stream. Try again.')
    await state.update_data(stream=key)
    await state.set_state(AddPaper.subject)
    await message.answer('Exact subject name: ' + ', '.join(SUBJECTS[key]))


@router.message(AddPaper.subject)
async def paper_subject(message: Message, state: FSMContext):
    data = await state.get_data()
    subject = (message.text or '').strip()
    if subject not in SUBJECTS[data['stream']]: return await message.answer('Use an exact subject name.')
    await state.update_data(subject=subject)
    await state.set_state(AddPaper.year)
    await message.answer('Paper year (e.g. 2024):')


@router.message(AddPaper.year)
async def paper_year(message: Message, state: FSMContext):
    val = (message.text or '').strip()
    if not val.isdigit() or not 2000 <= int(val) <= 2100: return await message.answer('Enter valid year.')
    await state.update_data(year=int(val))
    await state.set_state(AddPaper.document)
    await message.answer('Send the PDF as a Telegram document.')


@router.message(AddPaper.document, F.document)
async def paper_document(message: Message, state: FSMContext):
    if not is_admin(message): return
    doc = message.document
    if doc.mime_type != 'application/pdf' or doc.file_size > 20*1024*1024:
        return await message.answer('Send a PDF up to 20 MB (conservative bot limit).')
    data = await state.get_data()
    execute('INSERT INTO papers(stream,subject,year,file_id) VALUES(?,?,?,?)', (data['stream'],data['subject'],data['year'],doc.file_id))
    await state.clear()
    await message.answer('✅ Paper saved.', reply_markup=menu())


@router.message(Command('addquestion'))
async def addquestion(message: Message, state: FSMContext):
    if not is_admin(message): return await message.answer('Admin only.')
    await state.clear()
    await state.set_state(AddQuestion.stream)
    await message.answer('Stream key: science / bio / commerce / arts / tech')


@router.message(AddQuestion.stream)
async def question_stream(message: Message, state: FSMContext):
    key = (message.text or '').strip().lower()
    if key not in STREAMS: return await message.answer('Invalid stream.')
    await state.update_data(stream=key)
    await state.set_state(AddQuestion.subject)
    await message.answer('Exact subject: ' + ', '.join(SUBJECTS[key]))


@router.message(AddQuestion.subject)
async def question_subject(message: Message, state: FSMContext):
    data = await state.get_data()
    subject = (message.text or '').strip()
    if subject not in SUBJECTS[data['stream']]: return await message.answer('Invalid subject.')
    await state.update_data(subject=subject)
    await state.set_state(AddQuestion.content)
    await message.answer('Send exactly 7 lines:\nQuestion\nOption A\nOption B\nOption C\nOption D\nCorrect letter (A/B/C/D)\nSinhala explanation')


@router.message(AddQuestion.content)
async def question_content(message: Message, state: FSMContext):
    if not is_admin(message): return
    lines = [x.strip() for x in (message.text or '').splitlines()]
    if len(lines) != 7 or not all(lines) or lines[5].upper() not in 'ABCD' or len(lines[5]) != 1:
        return await message.answer('Invalid format. Send exactly 7 non-empty lines; line 6 must be A/B/C/D.')
    data = await state.get_data()
    execute('INSERT INTO questions(stream,subject,question,a,b,c,d,answer,explanation) VALUES(?,?,?,?,?,?,?,?,?)', (data['stream'],data['subject'],*lines[:5],lines[5].upper(),lines[6]))
    await state.clear()
    await message.answer('✅ Question saved.', reply_markup=menu())


@router.message(Command('stats'))
async def stats(message: Message):
    if not is_admin(message): return await message.answer('Admin only.')
    counts = [query(f'SELECT COUNT(*) AS n FROM {table}')[0]['n'] for table in ('students','papers','questions')]
    await message.answer(f'Students: {counts[0]}\nPapers: {counts[1]}\nQuestions: {counts[2]}')


@router.message(AIChat.question)
async def ai_question(message: Message, state: FSMContext):
    prompt = (message.text or '').strip()
    if not prompt or len(prompt) > 1200: return await message.answer('Send a text question up to 1200 characters.')
    await message.answer('🤖 Generating explanation...')
    system = ('You are a helpful Sri Lankan GCE A/L study tutor. Explain in simple Sinhala with English technical terms. '
              'Show steps for calculations. Say when uncertain; do not invent paper references or official answers. '
              'Do not ask students to share private information.')
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(f'{OLLAMA_URL}/api/chat', json={'model':OLLAMA_MODEL,'stream':False,'messages':[{'role':'system','content':system},{'role':'user','content':prompt}]})
            response.raise_for_status()
            result = response.json()['message']['content']
        for i in range(0,len(result),3500):
            await message.answer(result[i:i+3500])
    except (httpx.HTTPError, KeyError, ValueError):
        await message.answer('AI unavailable. The AI Tutor feature requires a running Ollama instance. /cancel to go back.')


@router.message()
async def fallback(message: Message):
    await message.answer('Use /start for the menu, /myid for your ID, /cancel to exit.')


async def on_startup(bot: Bot) -> None:
    """Register the webhook with Telegram on server startup."""
    if not WEBHOOK_URL:
        logging.warning('WEBHOOK_URL not set – bot will not receive updates on this deployment.')
        return
    full_url = f'{WEBHOOK_URL}{WEBHOOK_PATH}'
    await bot.set_webhook(
        url=full_url,
        secret_token=WEBHOOK_SECRET or None,
        drop_pending_updates=True,
    )
    logging.info('Webhook set to %s', full_url)


async def on_shutdown(bot: Bot) -> None:
    """Remove the webhook cleanly when the server shuts down."""
    await bot.delete_webhook()
    logging.info('Webhook deleted.')


def main():
    if not TOKEN or TOKEN == 'PASTE_BOTFATHER_TOKEN_HERE':
        raise RuntimeError('Set BOT_TOKEN in environment / .env')

    init_db()
    logging.basicConfig(level=logging.INFO)

    bot = Bot(
        token=TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Register startup / shutdown hooks on the dispatcher
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Build the aiohttp web application
    app = web.Application()
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET or None,
    ).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    # Health-check endpoint so Render marks the service as healthy
    async def health(request):
        return web.Response(text='OK')

    app.router.add_get('/', health)

    # Start the aiohttp server – Render injects $PORT automatically
    web.run_app(app, host='0.0.0.0', port=PORT)


if __name__ == '__main__':
    main()
