import asyncio
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
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN', '')
ADMINS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip().isdigit()}
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '').strip()
OLLAMA_URL = os.getenv('OLLAMA_URL', 'http://localhost:11434').rstrip('/')
OLLAMA_MODEL = os.getenv('OLLAMA_MODEL', 'llama3.2:3b')

DB = Path(__file__).with_name('education.db')
router = Router()

STREAMS = {
    'science': 'Physical Science',
    'bio':     'Biological Science',
    'commerce':'Commerce',
    'arts':    'Arts',
    'tech':    'Technology',
}
SUBJECTS = {
    'science': ['Combined Mathematics', 'Physics', 'Chemistry'],
    'bio':     ['Biology', 'Chemistry', 'Physics'],
    'commerce':['Accounting', 'Business Studies', 'Economics'],
    'arts':    ['Sinhala', 'Logic', 'Geography'],
    'tech':    ['Engineering Technology', 'Science for Technology', 'ICT'],
}

MEDALS = ['🥇', '🥈', '🥉']


# ── Database ────────────────────────────────────────────────────────────────

def connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con


def init_db():
    with closing(connect()) as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS students(
            user_id INTEGER PRIMARY KEY,
            name    TEXT NOT NULL,
            stream  TEXT
        );
        CREATE TABLE IF NOT EXISTS papers(
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            stream  TEXT NOT NULL,
            subject TEXT NOT NULL,
            year    INTEGER NOT NULL,
            file_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS questions(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            stream      TEXT NOT NULL,
            subject     TEXT NOT NULL,
            question    TEXT NOT NULL,
            a TEXT NOT NULL, b TEXT NOT NULL, c TEXT NOT NULL, d TEXT NOT NULL,
            answer      TEXT NOT NULL CHECK(answer IN ('A','B','C','D')),
            explanation TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scores(
            user_id  INTEGER PRIMARY KEY,
            points   INTEGER NOT NULL DEFAULT 0,
            answered INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES students(user_id)
        );
        CREATE TABLE IF NOT EXISTS subject_scores(
            user_id  INTEGER NOT NULL,
            subject  TEXT NOT NULL,
            correct  INTEGER NOT NULL DEFAULT 0,
            answered INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id, subject),
            FOREIGN KEY(user_id) REFERENCES students(user_id)
        );
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


def record_answer(user_id: int, subject: str, correct: bool):
    """Update both global scores and per-subject scores."""
    execute(
        'UPDATE scores SET points=points+?, answered=answered+1 WHERE user_id=?',
        (int(correct), user_id),
    )
    execute(
        '''INSERT INTO subject_scores(user_id, subject, correct, answered)
           VALUES(?, ?, ?, 1)
           ON CONFLICT(user_id, subject) DO UPDATE SET
               correct  = correct  + excluded.correct,
               answered = answered + 1''',
        (user_id, subject, int(correct)),
    )


# ── Keyboards ───────────────────────────────────────────────────────────────

def keyboard(items, prefix):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(label), callback_data=f'{prefix}:{value}')]
        for label, value in items
    ])


def menu():
    buttons = [
        ('📚 Past Papers',    'papers'),
        ('📝 MCQ Quiz',       'quiz'),
        ('🔁 Practice Mode', 'practice'),
        ('📊 My Progress',   'progress'),
        ('🏆 Leaderboard',   'leaderboard'),
        ('🤖 AI Tutor',      'ai'),
        ('🔄 Change Stream', 'stream'),
    ]
    return keyboard(buttons, 'menu')


# ── FSM States ──────────────────────────────────────────────────────────────

class AddPaper(StatesGroup):
    stream   = State()
    subject  = State()
    year     = State()
    document = State()


class AddQuestion(StatesGroup):
    stream  = State()
    subject = State()
    content = State()


class AIChat(StatesGroup):
    question = State()


# ── /start ──────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    execute(
        'INSERT INTO students(user_id,name) VALUES(?,?) '
        'ON CONFLICT(user_id) DO UPDATE SET name=excluded.name',
        (message.from_user.id, message.from_user.full_name),
    )
    execute('INSERT OR IGNORE INTO scores(user_id) VALUES(?)', (message.from_user.id,))
    row = query('SELECT stream FROM students WHERE user_id=?', (message.from_user.id,))[0]
    if row['stream']:
        await message.answer(
            f'👋 Welcome back, <b>{message.from_user.first_name}</b>!\n'
            'Choose an option from the menu:',
            reply_markup=menu(),
        )
    else:
        await message.answer(
            '👋 Welcome to <b>StudyLK Bot</b>!\n'
            'ඔබේ A/L stream එක select කරන්න:',
            reply_markup=keyboard([(v, k) for k, v in STREAMS.items()], 'stream'),
        )


# ── Stream selection ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith('stream:'))
async def choose_stream(call: CallbackQuery, state: FSMContext):
    stream = call.data.split(':', 1)[1]
    if stream not in STREAMS:
        return await call.answer('Invalid stream', show_alert=True)
    execute('UPDATE students SET stream=? WHERE user_id=?', (stream, call.from_user.id))
    await state.clear()
    await call.message.answer(
        f'✅ Stream set: <b>{STREAMS[stream]}</b>\n\nChoose an option:',
        reply_markup=menu(),
    )
    await call.answer()


# ── Main Menu ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith('menu:'))
async def menu_click(call: CallbackQuery, state: FSMContext):
    action = call.data.split(':', 1)[1]
    await state.clear()
    uid = call.from_user.id

    if action == 'stream':
        await call.message.answer(
            'Stream change කරන්න:',
            reply_markup=keyboard([(v, k) for k, v in STREAMS.items()], 'stream'),
        )

    elif action == 'leaderboard':
        await show_leaderboard(call)

    elif action == 'progress':
        await show_progress(call)

    elif action == 'ai':
        await state.set_state(AIChat.question)
        await call.message.answer(
            '🤖 <b>AI Tutor</b>\n'
            'ඔබේ A/L ප්‍රශ්නය type කරන්න (Sinhala/English OK).\n'
            'AI ලබාදෙන answers verify කරන්න.\n\n'
            '/cancel to exit.',
        )

    elif action in ('papers', 'quiz', 'practice'):
        rows = query('SELECT stream FROM students WHERE user_id=?', (uid,))
        stream = rows[0]['stream'] if rows else None
        if not stream:
            await call.message.answer(
                'Stream select කරන්න:',
                reply_markup=keyboard([(v, k) for k, v in STREAMS.items()], 'stream'),
            )
        else:
            prefix_map = {'papers': 'papers', 'quiz': 'quiz', 'practice': 'practice'}
            label_map  = {
                'papers':   '📚 Which subject\'s papers?',
                'quiz':     '📝 Quiz — subject select කරන්න:',
                'practice': '🔁 Practice Mode — subject select කරන්න:',
            }
            await call.message.answer(
                label_map[action],
                reply_markup=keyboard([(s, s) for s in SUBJECTS[stream]], prefix_map[action]),
            )

    await call.answer()


# ── Leaderboard ──────────────────────────────────────────────────────────────

async def show_leaderboard(call: CallbackQuery):
    uid = call.from_user.id
    rows = query('SELECT stream FROM students WHERE user_id=?', (uid,))
    stream = rows[0]['stream'] if rows else None

    # Global top 10
    global_rows = query(
        'SELECT s.name, sc.points, sc.answered '
        'FROM scores sc JOIN students s ON s.user_id=sc.user_id '
        'WHERE sc.answered > 0 '
        'ORDER BY sc.points DESC, sc.answered ASC LIMIT 10'
    )

    lines = ['🏆 <b>Global Leaderboard — Top 10</b>\n']
    for i, r in enumerate(global_rows, 1):
        medal = MEDALS[i-1] if i <= 3 else f'{i}.'
        pct   = round(r['points'] / r['answered'] * 100) if r['answered'] else 0
        lines.append(f'{medal} {r["name"]} — {r["points"]}/{r["answered"]} ({pct}%)')

    if not global_rows:
        lines.append('No results yet. Quiz play කරලා score ගන්න!')

    # Stream leaderboard (if student has a stream)
    if stream:
        stream_rows = query(
            'SELECT s.name, sc.points, sc.answered '
            'FROM scores sc JOIN students s ON s.user_id=sc.user_id '
            'WHERE s.stream=? AND sc.answered > 0 '
            'ORDER BY sc.points DESC, sc.answered ASC LIMIT 5',
            (stream,)
        )
        lines.append(f'\n🎓 <b>{STREAMS[stream]} Stream — Top 5</b>\n')
        for i, r in enumerate(stream_rows, 1):
            medal = MEDALS[i-1] if i <= 3 else f'{i}.'
            pct   = round(r['points'] / r['answered'] * 100) if r['answered'] else 0
            lines.append(f'{medal} {r["name"]} — {r["points"]}/{r["answered"]} ({pct}%)')
        if not stream_rows:
            lines.append('No stream results yet.')

    await call.message.answer('\n'.join(lines), reply_markup=menu())


# ── Subject-wise Progress ─────────────────────────────────────────────────────

async def show_progress(call: CallbackQuery):
    uid = call.from_user.id
    rows = query('SELECT stream FROM students WHERE user_id=?', (uid,))
    stream = rows[0]['stream'] if rows else None

    # Overall score
    sc = query('SELECT points, answered FROM scores WHERE user_id=?', (uid,))
    r  = sc[0] if sc else {'points': 0, 'answered': 0}
    overall_pct = round(r['points'] / r['answered'] * 100) if r['answered'] else 0

    lines = [
        f'📊 <b>My Progress — {call.from_user.first_name}</b>\n',
        f'📈 Overall: <b>{r["points"]}/{r["answered"]}</b> correct ({overall_pct}%)\n',
    ]

    if stream:
        lines.append(f'🎓 Stream: <b>{STREAMS[stream]}</b>\n')
        subj_rows = query(
            'SELECT subject, correct, answered FROM subject_scores WHERE user_id=? ORDER BY answered DESC',
            (uid,)
        )
        if subj_rows:
            lines.append('📚 <b>Subject Breakdown:</b>')
            for s in subj_rows:
                pct  = round(s['correct'] / s['answered'] * 100) if s['answered'] else 0
                bar  = progress_bar(pct)
                lines.append(f'  • <b>{s["subject"]}</b>: {s["correct"]}/{s["answered"]} ({pct}%)\n    {bar}')
        else:
            lines.append('Quiz/Practice play කළාට subject progress දිස්වෙයි.')

    await call.message.answer('\n'.join(lines), reply_markup=menu())


def progress_bar(pct: int, length: int = 10) -> str:
    filled = round(pct / 100 * length)
    return '█' * filled + '░' * (length - filled) + f' {pct}%'


# ── Past Papers ───────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith('papers:'))
async def papers(call: CallbackQuery):
    subject = call.data.split(':', 1)[1]
    rows    = query('SELECT stream FROM students WHERE user_id=?', (call.from_user.id,))
    stream  = rows[0]['stream'] if rows else None
    if not stream or subject not in SUBJECTS.get(stream, []):
        return await call.answer('Choose a valid subject', show_alert=True)
    ps = query(
        'SELECT id, year FROM papers WHERE stream=? AND subject=? ORDER BY year DESC',
        (stream, subject),
    )
    await call.message.answer(
        f'📚 <b>{subject}</b> — Past Papers:' if ps else '❌ No papers uploaded yet.',
        reply_markup=keyboard(
            [(f'📄 {p["year"]} — Paper #{p["id"]}', p['id']) for p in ps[:80]], 'paper'
        ) if ps else None,
    )
    await call.answer()


@router.callback_query(F.data.startswith('paper:'))
async def send_paper(call: CallbackQuery):
    pid  = call.data.split(':', 1)[1]
    rows = query(
        'SELECT p.* FROM papers p JOIN students s ON s.stream=p.stream '
        'WHERE p.id=? AND s.user_id=?',
        (pid, call.from_user.id),
    )
    if not rows:
        return await call.answer('Paper not found', show_alert=True)
    p = rows[0]
    await call.message.answer_document(p['file_id'], caption=f'📄 {p["subject"]} — {p["year"]}')
    await call.answer()


# ── Quiz (10 questions, scored) ───────────────────────────────────────────────

@router.callback_query(F.data.startswith('quiz:'))
async def quiz(call: CallbackQuery, state: FSMContext):
    subject = call.data.split(':', 1)[1]
    rows    = query('SELECT stream FROM students WHERE user_id=?', (call.from_user.id,))
    stream  = rows[0]['stream'] if rows else None
    if not stream or subject not in SUBJECTS.get(stream, []):
        return await call.answer('Invalid subject', show_alert=True)
    qs = query(
        'SELECT * FROM questions WHERE stream=? AND subject=? ORDER BY RANDOM() LIMIT 10',
        (stream, subject),
    )
    if not qs:
        await call.message.answer('❌ No quiz questions uploaded yet for this subject.')
    else:
        await state.update_data(
            quiz_ids=[r['id'] for r in qs],
            index=0, correct=0,
            subject=subject,
            mode='quiz',
        )
        await send_question(call.message, state)
    await call.answer()


# ── Practice Mode (infinite, no score cap) ───────────────────────────────────

@router.callback_query(F.data.startswith('practice:'))
async def practice(call: CallbackQuery, state: FSMContext):
    subject = call.data.split(':', 1)[1]
    rows    = query('SELECT stream FROM students WHERE user_id=?', (call.from_user.id,))
    stream  = rows[0]['stream'] if rows else None
    if not stream or subject not in SUBJECTS.get(stream, []):
        return await call.answer('Invalid subject', show_alert=True)
    qs = query(
        'SELECT * FROM questions WHERE stream=? AND subject=? ORDER BY RANDOM() LIMIT 20',
        (stream, subject),
    )
    if not qs:
        await call.message.answer('❌ No questions yet for this subject.')
    else:
        await state.update_data(
            quiz_ids=[r['id'] for r in qs],
            index=0, correct=0,
            subject=subject,
            mode='practice',
            stream=stream,
        )
        await call.message.answer(
            f'🔁 <b>Practice Mode — {subject}</b>\n'
            'Score count කරයි. /cancel to stop anytime.',
        )
        await send_question(call.message, state)
    await call.answer()


# ── Shared question sender ────────────────────────────────────────────────────

async def send_question(message: Message, state: FSMContext):
    data        = await state.get_data()
    ids, index  = data['quiz_ids'], data['index']
    mode        = data.get('mode', 'quiz')

    if index >= len(ids):
        subject = data.get('subject', '')
        correct = data['correct']
        total   = len(ids)
        pct     = round(correct / total * 100) if total else 0
        bar     = progress_bar(pct)

        if mode == 'practice':
            # Offer to continue with more questions
            await message.answer(
                f'🔁 <b>Practice Round Done!</b>\n\n'
                f'📊 {subject}\n'
                f'✅ {correct}/{total} correct   {bar}\n\n'
                f'Continue practice කරන්නද?',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text='🔁 Continue Practice', callback_data=f'practice:{subject}')],
                    [InlineKeyboardButton(text='🏠 Main Menu',         callback_data='menu:score')],
                ]),
            )
        else:
            emoji = '🎉' if pct >= 70 else '📚'
            await message.answer(
                f'{emoji} <b>Quiz Complete!</b>\n\n'
                f'📊 {subject}\n'
                f'✅ {correct}/{total} correct   {bar}\n\n'
                '{"Well done! 🌟" if pct >= 70 else "Keep practicing! 💪"}',
                reply_markup=menu(),
            )
        await state.clear()
        return

    rows = query('SELECT * FROM questions WHERE id=?', (ids[index],))
    if not rows:
        await state.update_data(index=index + 1)
        return await send_question(message, state)

    q    = rows[0]
    mode_tag = '🔁 Practice' if mode == 'practice' else '📝 Quiz'
    text = (
        f'{mode_tag} | Q {index+1}/{len(ids)}\n\n'
        f'<b>{q["question"]}</b>\n\n'
        f'A. {q["a"]}\n'
        f'B. {q["b"]}\n'
        f'C. {q["c"]}\n'
        f'D. {q["d"]}'
    )
    await message.answer(text, reply_markup=keyboard([(x, x) for x in 'ABCD'], 'answer'))


# ── Answer handler ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith('answer:'))
async def answer(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if 'quiz_ids' not in data:
        return await call.answer('Quiz expired. Start again.', show_alert=True)

    index = data['index']
    rows  = query('SELECT * FROM questions WHERE id=?', (data['quiz_ids'][index],))
    if not rows:
        return await call.answer('Question unavailable', show_alert=True)

    q       = rows[0]
    chosen  = call.data.split(':', 1)[1]
    if chosen not in 'ABCD':
        return await call.answer('Invalid answer', show_alert=True)

    correct = chosen == q['answer']
    subject = data.get('subject', q['subject'])

    # Prevent double-scoring on repeated taps
    await call.message.edit_reply_markup(reply_markup=None)

    # Record in both global + subject tables
    record_answer(call.from_user.id, subject, correct)

    result_icon = '✅ Correct!' if correct else f'❌ Incorrect. Answer: <b>{q["answer"]}</b>'
    await call.message.answer(
        f'{result_icon}\n\n'
        f'💡 <i>{q["explanation"]}</i>'
    )

    await state.update_data(index=index + 1, correct=data['correct'] + int(correct))
    await send_question(call.message, state)
    await call.answer()


# ── /cancel ───────────────────────────────────────────────────────────────────

@router.message(Command('cancel'))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('❌ Cancelled.', reply_markup=menu())


# ── Admin helpers ─────────────────────────────────────────────────────────────

def is_admin(message: Message):
    return message.from_user.id in ADMINS


@router.message(Command('myid'))
async def myid(message: Message):
    await message.answer(f'Your Telegram ID: <code>{message.from_user.id}</code>')


@router.message(Command('stats'))
async def stats(message: Message):
    if not is_admin(message): return await message.answer('Admin only.')
    counts = [query(f'SELECT COUNT(*) AS n FROM {t}')[0]['n'] for t in ('students', 'papers', 'questions')]
    top    = query(
        'SELECT s.name, sc.points, sc.answered FROM scores sc '
        'JOIN students s ON s.user_id=sc.user_id '
        'ORDER BY sc.points DESC LIMIT 3'
    )
    top_text = '\n'.join(
        f'{MEDALS[i] if i<3 else i+1}. {r["name"]} — {r["points"]} pts'
        for i, r in enumerate(top)
    ) or 'No quiz data.'
    await message.answer(
        f'📊 <b>Bot Stats</b>\n\n'
        f'👨‍🎓 Students: {counts[0]}\n'
        f'📄 Papers:   {counts[1]}\n'
        f'❓ Questions:{counts[2]}\n\n'
        f'🏆 <b>Top Players:</b>\n{top_text}'
    )


# ── Admin: Add Paper ──────────────────────────────────────────────────────────

@router.message(Command('addpaper'))
async def addpaper(message: Message, state: FSMContext):
    if not is_admin(message): return await message.answer('Admin only.')
    await state.clear()
    await state.set_state(AddPaper.stream)
    await message.answer('Stream key:\n<code>science / bio / commerce / arts / tech</code>')


@router.message(AddPaper.stream)
async def paper_stream(message: Message, state: FSMContext):
    key = (message.text or '').strip().lower()
    if key not in STREAMS: return await message.answer('Invalid stream. Try again.')
    await state.update_data(stream=key)
    await state.set_state(AddPaper.subject)
    await message.answer('Subject name:\n' + '\n'.join(f'• {s}' for s in SUBJECTS[key]))


@router.message(AddPaper.subject)
async def paper_subject(message: Message, state: FSMContext):
    data    = await state.get_data()
    subject = (message.text or '').strip()
    if subject not in SUBJECTS[data['stream']]: return await message.answer('Use exact subject name.')
    await state.update_data(subject=subject)
    await state.set_state(AddPaper.year)
    await message.answer('Paper year (e.g. <code>2024</code>):')


@router.message(AddPaper.year)
async def paper_year(message: Message, state: FSMContext):
    val = (message.text or '').strip()
    if not val.isdigit() or not 2000 <= int(val) <= 2100:
        return await message.answer('Enter valid year (2000–2100).')
    await state.update_data(year=int(val))
    await state.set_state(AddPaper.document)
    await message.answer('📎 Send the PDF as a Telegram document.')


@router.message(AddPaper.document, F.document)
async def paper_document(message: Message, state: FSMContext):
    if not is_admin(message): return
    doc = message.document
    if doc.mime_type != 'application/pdf' or doc.file_size > 20 * 1024 * 1024:
        return await message.answer('Send a PDF up to 20 MB.')
    data = await state.get_data()
    execute(
        'INSERT INTO papers(stream,subject,year,file_id) VALUES(?,?,?,?)',
        (data['stream'], data['subject'], data['year'], doc.file_id),
    )
    await state.clear()
    await message.answer(
        f'✅ Paper saved!\n'
        f'📚 {data["subject"]} — {data["year"]}',
        reply_markup=menu(),
    )


# ── Admin: Add Question ───────────────────────────────────────────────────────

@router.message(Command('addquestion'))
async def addquestion(message: Message, state: FSMContext):
    if not is_admin(message): return await message.answer('Admin only.')
    await state.clear()
    await state.set_state(AddQuestion.stream)
    await message.answer('Stream key:\n<code>science / bio / commerce / arts / tech</code>')


@router.message(AddQuestion.stream)
async def question_stream(message: Message, state: FSMContext):
    key = (message.text or '').strip().lower()
    if key not in STREAMS: return await message.answer('Invalid stream.')
    await state.update_data(stream=key)
    await state.set_state(AddQuestion.subject)
    await message.answer('Subject:\n' + '\n'.join(f'• {s}' for s in SUBJECTS[key]))


@router.message(AddQuestion.subject)
async def question_subject(message: Message, state: FSMContext):
    data    = await state.get_data()
    subject = (message.text or '').strip()
    if subject not in SUBJECTS[data['stream']]: return await message.answer('Invalid subject.')
    await state.update_data(subject=subject)
    await state.set_state(AddQuestion.content)
    await message.answer(
        '📝 Send exactly <b>7 lines</b>:\n\n'
        '<code>Question text\n'
        'Option A\n'
        'Option B\n'
        'Option C\n'
        'Option D\n'
        'Correct letter (A/B/C/D)\n'
        'Explanation (Sinhala/English)</code>'
    )


@router.message(AddQuestion.content)
async def question_content(message: Message, state: FSMContext):
    if not is_admin(message): return
    lines = [x.strip() for x in (message.text or '').splitlines()]
    if len(lines) != 7 or not all(lines) or lines[5].upper() not in 'ABCD' or len(lines[5]) != 1:
        return await message.answer('Invalid format. Send exactly 7 non-empty lines; line 6 = A/B/C/D.')
    data = await state.get_data()
    execute(
        'INSERT INTO questions(stream,subject,question,a,b,c,d,answer,explanation) VALUES(?,?,?,?,?,?,?,?,?)',
        (data['stream'], data['subject'], *lines[:5], lines[5].upper(), lines[6]),
    )
    await state.clear()
    await message.answer('✅ Question saved!', reply_markup=menu())


# ── AI Tutor ──────────────────────────────────────────────────────────────────

@router.message(AIChat.question)
async def ai_question(message: Message, state: FSMContext):
    prompt = (message.text or '').strip()
    if not prompt or len(prompt) > 1200:
        return await message.answer('Send a text question up to 1200 characters.')
    await message.answer('🤖 Generating explanation...')
    system = (
        'You are a helpful Sri Lankan GCE A/L study tutor. '
        'Explain clearly in simple Sinhala mixed with English technical terms where appropriate. '
        'Show detailed step-by-step methods for calculations or derivations. '
        'Be accurate according to Sri Lankan A/L syllabus.'
    )

    result = None

    # 1. Try Gemini API if key is configured
    if GEMINI_API_KEY:
        try:
            url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}'
            payload = {
                'system_instruction': {'parts': [{'text': system}]},
                'contents': [{'parts': [{'text': prompt}]}]
            }
            async with httpx.AsyncClient(timeout=40) as client:
                res = await client.post(url, json=payload)
                if res.status_code == 200:
                    data = res.json()
                    candidates = data.get('candidates', [])
                    if candidates and 'content' in candidates[0]:
                        parts = candidates[0]['content'].get('parts', [])
                        if parts:
                            result = parts[0].get('text', '')
        except Exception as e:
            logging.error('Gemini error: %s', e)

    # 2. Fallback to local Ollama if Gemini failed or key not set
    if not result:
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                response = await client.post(
                    f'{OLLAMA_URL}/api/chat',
                    json={'model': OLLAMA_MODEL, 'stream': False,
                          'messages': [{'role': 'system', 'content': system},
                                       {'role': 'user',   'content': prompt}]},
                )
                if response.status_code == 200:
                    result = response.json()['message']['content']
        except Exception:
            pass

    if result:
        for i in range(0, len(result), 3500):
            await message.answer(result[i:i + 3500])
    else:
        await message.answer('⚠️ AI service temporarily unavailable. Use /cancel to go back.')


# ── Fallback ──────────────────────────────────────────────────────────────────

@router.message()
async def fallback(message: Message):
    await message.answer(
        'Use /start for the menu.\n'
        '/myid — your Telegram ID\n'
        '/cancel — exit current action'
    )


# ── Startup ───────────────────────────────────────────────────────────────────

async def main():
    if not TOKEN or TOKEN == 'PASTE_BOTFATHER_TOKEN_HERE':
        raise RuntimeError('Set BOT_TOKEN in environment / .env')

    init_db()
    logging.basicConfig(level=logging.INFO)

    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp  = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    webhook_url = os.getenv('WEBHOOK_URL', '').rstrip('/')

    if webhook_url:
        # ── WEBHOOK MODE (Render / any platform with public HTTPS URL) ────────
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
        from aiohttp import web

        webhook_path   = os.getenv('WEBHOOK_PATH', '/webhook')
        webhook_secret = os.getenv('WEBHOOK_SECRET', '') or None
        port           = int(os.getenv('PORT', '8080'))

        async def on_startup(bot: Bot):
            await bot.set_webhook(
                url=f'{webhook_url}{webhook_path}',
                secret_token=webhook_secret,
                drop_pending_updates=True,
            )
            logging.info('Webhook set → %s%s', webhook_url, webhook_path)

        async def on_shutdown(bot: Bot):
            await bot.delete_webhook()
            logging.info('Webhook removed.')

        dp.startup.register(on_startup)
        dp.shutdown.register(on_shutdown)

        app = web.Application()
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=webhook_secret).register(app, path=webhook_path)
        setup_application(app, dp, bot=bot)

        async def health(request):
            return web.Response(text='OK')
        app.router.add_get('/', health)

        web.run_app(app, host='0.0.0.0', port=port)

    else:
        # ── POLLING MODE (Railway / local PC) ─────────────────────────────────
        logging.info('WEBHOOK_URL not set → polling mode.')
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
