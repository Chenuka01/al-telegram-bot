import asyncio
import json
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
ADMINS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip().isdigit()} | {7546815103}
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '').strip()
OLLAMA_URL = os.getenv('OLLAMA_URL', 'http://localhost:11434').rstrip('/')
OLLAMA_MODEL = os.getenv('OLLAMA_MODEL', 'llama3.2:3b')

DB = Path(__file__).with_name('education.db')
router = Router()

STREAMS = {
    'commerce': 'Commerce',
    'tech':     'Technology',
}
SUBJECTS = {
    'commerce': ['Accounting'],
    'tech':     ['ICT'],
}

ALL_SUBJECTS = ['ICT', 'Accounting']

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
        CREATE TABLE IF NOT EXISTS notes(
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            stream  TEXT NOT NULL,
            subject TEXT NOT NULL,
            title   TEXT NOT NULL,
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

        # Seed starter questions if database is newly initialized
        count = con.execute('SELECT COUNT(*) FROM questions').fetchone()[0]
        if count == 0:
            starters = [
                ('tech', 'ICT', 'What is the binary representation of decimal number 13?', '1101', '1011', '1110', '1001', 'A', '13 = 8 + 4 + 1 = 1101_2'),
                ('tech', 'ICT', 'Which of the following is NOT an operating system?', 'Linux', 'Windows', 'Oracle', 'macOS', 'C', 'Oracle is a Database Management System (DBMS), not an OS.'),
                ('tech', 'ICT', 'What is the primary key in a relational database?', 'A key that can be null', 'A unique identifier for each record in a table', 'A foreign key from another table', 'An encrypted password', 'B', 'A primary key uniquely identifies each record and cannot be NULL.'),
                ('tech', 'ICT', 'Which protocol is used to securely transfer web pages?', 'HTTP', 'FTP', 'HTTPS', 'SMTP', 'C', 'HTTPS encrypts web traffic using TLS/SSL.'),
                ('tech', 'ICT', 'In Python, which keyword is used to define a function?', 'func', 'def', 'function', 'define', 'B', 'Python uses the def keyword to define functions.'),
                ('commerce', 'Accounting', 'What is the basic Accounting Equation?', 'Assets = Liabilities + Equity', 'Assets = Liabilities - Equity', 'Liabilities = Assets + Equity', 'Equity = Assets + Liabilities', 'A', 'Assets = Liabilities + Owner\'s Equity (වත්කම් = වගකීම් + හිමිකම)'),
                ('commerce', 'Accounting', 'Which concept states that revenue is recognized when earned, regardless of cash receipt?', 'Cash Concept', 'Accrual Concept', 'Prudence Concept', 'Going Concern', 'B', 'Accrual Concept (උපචිත සංකල්පය) recognizes income when earned.'),
                ('commerce', 'Accounting', 'Depreciation of machinery is classified as which type of expense?', 'Direct Expense', 'Non-cash Expense', 'Finance Expense', 'Capital Expenditure', 'B', 'Depreciation (ක්ෂයවීම) is a non-cash operating expense.'),
                ('commerce', 'Accounting', 'Goodwill of a business is categorized under:', 'Current Assets', 'Intangible Non-current Assets', 'Current Liabilities', 'Owner\'s Equity', 'B', 'Goodwill (කීර්තිනාමය) is an intangible non-current asset.'),
                ('commerce', 'Accounting', 'Trial Balance is prepared to check the:', 'Net Profit of the year', 'Arithmetical accuracy of ledger accounts', 'Financial position of the business', 'Cash balance only', 'B', 'Trial Balance (ශේෂ පිරික්සුම) verifies mathematical accuracy of double-entry postings.')
            ]
            con.executemany('''
                INSERT INTO questions(stream, subject, question, a, b, c, d, answer, explanation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', starters)
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


def menu(is_adm: bool = False):
    buttons = [
        ('📚 Past Papers',    'papers'),
        ('📖 Short Notes',    'notes'),
        ('📝 MCQ Quiz',       'quiz'),
        ('🔁 Practice Mode', 'practice'),
        ('🤖 AI Tutor',      'ai'),
        ('📊 My Progress',   'progress'),
        ('🏆 Leaderboard',   'leaderboard'),
        ('🔄 Change Stream', 'stream'),
    ]
    if is_adm:
        buttons.append(('⚙️ Admin Panel', 'admin_panel'))
    return keyboard(buttons, 'menu')


def admin_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='➕ Add Question (Manual)', callback_data='adm:add_q_stream')],
        [InlineKeyboardButton(text='✨ AI Generate Questions (Gemini)', callback_data='adm:ai_q_stream')],
        [InlineKeyboardButton(text='📄 Upload Past Paper (PDF)', callback_data='adm:add_paper_stream')],
        [InlineKeyboardButton(text='📖 Upload Short Note (PDF)', callback_data='adm:add_note_stream')],
        [InlineKeyboardButton(text='📢 Broadcast to All Students', callback_data='adm:broadcast')],
        [InlineKeyboardButton(text='📊 View Bot Stats', callback_data='adm:stats')],
        [InlineKeyboardButton(text='🏠 Main Menu', callback_data='menu:main')],
    ])


# ── FSM States ──────────────────────────────────────────────────────────────

class AddPaper(StatesGroup):
    stream   = State()
    subject  = State()
    year     = State()
    document = State()


class AddNote(StatesGroup):
    stream   = State()
    subject  = State()
    title    = State()
    document = State()


class ManualQuestion(StatesGroup):
    stream      = State()
    subject     = State()
    question    = State()
    opt_a       = State()
    opt_b       = State()
    opt_c       = State()
    opt_d       = State()
    answer      = State()
    explanation = State()


class AIGenerateQuestions(StatesGroup):
    stream  = State()
    subject = State()
    prompt  = State()


class BroadcastMessage(StatesGroup):
    message = State()


class AIChat(StatesGroup):
    question = State()


# ── /start & Main Menu ──────────────────────────────────────────────────────

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    execute(
        'INSERT INTO students(user_id,name) VALUES(?,?) '
        'ON CONFLICT(user_id) DO UPDATE SET name=excluded.name',
        (uid, message.from_user.full_name),
    )
    execute('INSERT OR IGNORE INTO scores(user_id) VALUES(?)', (uid,))
    row = query('SELECT stream FROM students WHERE user_id=?', (uid,))[0]

    if row['stream']:
        await message.answer(
            f'👋 Welcome back, <b>{message.from_user.first_name}</b>!\n'
            'Choose an option from the menu:',
            reply_markup=menu(is_adm=(uid in ADMINS)),
        )
    else:
        await message.answer(
            '👋 Welcome to <b>StudyLK Bot</b>!\n'
            'ඔබේ විෂය ධාරාව තෝරන්න:',
            reply_markup=keyboard([('💻 Technology (ICT)', 'tech'), ('📊 Commerce (Accounting)', 'commerce')], 'stream'),
        )


@router.callback_query(F.data.startswith('stream:'))
async def choose_stream(call: CallbackQuery, state: FSMContext):
    stream = call.data.split(':', 1)[1]
    if stream not in STREAMS:
        return await call.answer('Invalid stream', show_alert=True)
    execute('UPDATE students SET stream=? WHERE user_id=?', (stream, call.from_user.id))
    await state.clear()
    await call.message.answer(
        f'✅ Stream set: <b>{STREAMS[stream]}</b>\n\nChoose an option:',
        reply_markup=menu(is_adm=(call.from_user.id in ADMINS)),
    )
    await call.answer()


@router.callback_query(F.data.startswith('menu:'))
async def menu_click(call: CallbackQuery, state: FSMContext):
    action = call.data.split(':', 1)[1]
    await state.clear()
    uid = call.from_user.id

    if action == 'main':
        await call.message.answer('🏠 Main Menu:', reply_markup=menu(is_adm=(uid in ADMINS)))

    elif action == 'admin_panel':
        if uid not in ADMINS:
            return await call.answer('Admin only!', show_alert=True)
        await call.message.answer('⚙️ <b>Admin Control Panel</b>\nManage quizzes, papers, notes & broadcasts:', reply_markup=admin_menu_keyboard())

    elif action == 'stream':
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
            '🤖 <b>AI Tutor (Gemini AI)</b>\n'
            'ICT හෝ Accounting සම්බන්ධ ඕනෑම A/L ගැටලුවක් type කර එවන්න:\n'
            '(/cancel to exit)',
        )

    elif action in ('papers', 'notes', 'quiz', 'practice'):
        label_map = {
            'papers':   '📚 Which subject\'s papers?',
            'notes':    '📖 Which subject\'s notes?',
            'quiz':     '📝 Quiz — subject select කරන්න:',
            'practice': '🔁 Practice Mode — subject select කරන්න:',
        }
        await call.message.answer(
            label_map[action],
            reply_markup=keyboard([(s, s) for s in ALL_SUBJECTS], action),
        )

    await call.answer()


# ── Leaderboard & Progress ──────────────────────────────────────────────────

async def show_leaderboard(call: CallbackQuery):
    uid = call.from_user.id
    rows = query('SELECT stream FROM students WHERE user_id=?', (uid,))
    stream = rows[0]['stream'] if rows else None

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

    await call.message.answer('\n'.join(lines), reply_markup=menu(is_adm=(uid in ADMINS)))


async def show_progress(call: CallbackQuery):
    uid = call.from_user.id
    rows = query('SELECT stream FROM students WHERE user_id=?', (uid,))
    stream = rows[0]['stream'] if rows else None

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
                pct = round(s['correct'] / s['answered'] * 100) if s['answered'] else 0
                bar = progress_bar(pct)
                lines.append(f'  • <b>{s["subject"]}</b>: {s["correct"]}/{s["answered"]} ({pct}%)\n    {bar}')
        else:
            lines.append('Quiz/Practice play කළ පසු විෂය ප්‍රගතිය මෙහි පෙන්වනු ඇත.')

    await call.message.answer('\n'.join(lines), reply_markup=menu(is_adm=(uid in ADMINS)))


def progress_bar(pct: int, length: int = 10) -> str:
    filled = round(pct / 100 * length)
    return '█' * filled + '░' * (length - filled) + f' {pct}%'


# ── Past Papers & Notes (Student View) ──────────────────────────────────────

@router.callback_query(F.data.startswith('papers:'))
async def papers(call: CallbackQuery):
    subject = call.data.split(':', 1)[1]
    if subject not in ALL_SUBJECTS:
        return await call.answer('Choose a valid subject', show_alert=True)
    ps = query(
        'SELECT id, year FROM papers WHERE subject=? ORDER BY year DESC',
        (subject,),
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
    rows = query('SELECT p.* FROM papers p WHERE p.id=?', (pid,))
    if not rows:
        return await call.answer('Paper not found', show_alert=True)
    p = rows[0]
    await call.message.answer_document(p['file_id'], caption=f'📄 {p["subject"]} — {p["year"]}')
    await call.answer()


@router.callback_query(F.data.startswith('notes:'))
async def notes(call: CallbackQuery):
    subject = call.data.split(':', 1)[1]
    if subject not in ALL_SUBJECTS:
        return await call.answer('Choose a valid subject', show_alert=True)
    ns = query(
        'SELECT id, title FROM notes WHERE subject=? ORDER BY id DESC',
        (subject,),
    )
    await call.message.answer(
        f'📖 <b>{subject}</b> — Short Notes:' if ns else '❌ No notes uploaded yet.',
        reply_markup=keyboard(
            [(f'📖 {n["title"]}', n['id']) for n in ns[:80]], 'note'
        ) if ns else None,
    )
    await call.answer()


@router.callback_query(F.data.startswith('note:'))
async def send_note(call: CallbackQuery):
    nid  = call.data.split(':', 1)[1]
    rows = query('SELECT n.* FROM notes n WHERE n.id=?', (nid,))
    if not rows:
        return await call.answer('Note not found', show_alert=True)
    n = rows[0]
    await call.message.answer_document(n['file_id'], caption=f'📖 {n["subject"]} — {n["title"]}')
    await call.answer()


# ── Quiz & Practice Execution ───────────────────────────────────────────────

@router.callback_query(F.data.startswith('quiz:'))
async def quiz(call: CallbackQuery, state: FSMContext):
    subject = call.data.split(':', 1)[1]
    if subject not in ALL_SUBJECTS:
        return await call.answer('Invalid subject', show_alert=True)
    qs = query(
        'SELECT * FROM questions WHERE subject=? ORDER BY RANDOM() LIMIT 10',
        (subject,),
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


@router.callback_query(F.data.startswith('practice:'))
async def practice(call: CallbackQuery, state: FSMContext):
    subject = call.data.split(':', 1)[1]
    if subject not in ALL_SUBJECTS:
        return await call.answer('Invalid subject', show_alert=True)
    qs = query(
        'SELECT * FROM questions WHERE subject=? ORDER BY RANDOM() LIMIT 20',
        (subject,),
    )
    if not qs:
        await call.message.answer('❌ No questions yet for this subject.')
    else:
        await state.update_data(
            quiz_ids=[r['id'] for r in qs],
            index=0, correct=0,
            subject=subject,
            mode='practice',
        )
        await call.message.answer(
            f'🔁 <b>Practice Mode — {subject}</b>\n'
            'Score count කරයි. /cancel to stop anytime.',
        )
        await send_question(call.message, state)
    await call.answer()


async def send_question(message: Message, state: FSMContext):
    data       = await state.get_data()
    ids, index = data['quiz_ids'], data['index']
    mode       = data.get('mode', 'quiz')

    if index >= len(ids):
        subject = data.get('subject', '')
        correct = data['correct']
        total   = len(ids)
        pct     = round(correct / total * 100) if total else 0
        bar     = progress_bar(pct)

        if mode == 'practice':
            await message.answer(
                f'🔁 <b>Practice Round Done!</b>\n\n'
                f'📊 {subject}\n'
                f'✅ {correct}/{total} correct   {bar}\n\n'
                f'Continue practice කරන්නද?',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text='🔁 Continue Practice', callback_data=f'practice:{subject}')],
                    [InlineKeyboardButton(text='🏠 Main Menu',         callback_data='menu:main')],
                ]),
            )
        else:
            emoji = '🎉' if pct >= 70 else '📚'
            await message.answer(
                f'{emoji} <b>Quiz Complete!</b>\n\n'
                f'📊 {subject}\n'
                f'✅ {correct}/{total} correct   {bar}\n\n'
                f'{"Well done! 🌟" if pct >= 70 else "Keep practicing! 💪"}',
                reply_markup=menu(),
            )
        await state.clear()
        return

    rows = query('SELECT * FROM questions WHERE id=?', (ids[index],))
    if not rows:
        await state.update_data(index=index + 1)
        return await send_question(message, state)

    q = rows[0]
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

    await call.message.edit_reply_markup(reply_markup=None)
    record_answer(call.from_user.id, subject, correct)

    result_icon = '✅ Correct!' if correct else f'❌ Incorrect. Answer: <b>{q["answer"]}</b>'
    await call.message.answer(
        f'{result_icon}\n\n'
        f'💡 <i>{q["explanation"]}</i>'
    )

    await state.update_data(index=index + 1, correct=data['correct'] + int(correct))
    await send_question(call.message, state)
    await call.answer()


# ── ADMIN PANEL & MANAGEMENT ────────────────────────────────────────────────

@router.message(Command('admin'))
async def admin_cmd(message: Message, state: FSMContext):
    if message.from_user.id not in ADMINS:
        return await message.answer('Admin only.')
    await state.clear()
    await message.answer('⚙️ <b>Admin Control Panel</b>\nManage quizzes, papers, notes & broadcasts:', reply_markup=admin_menu_keyboard())


@router.callback_query(F.data == 'adm:stats')
async def adm_stats(call: CallbackQuery):
    if call.from_user.id not in ADMINS: return
    counts = [query(f'SELECT COUNT(*) AS n FROM {t}')[0]['n'] for t in ('students', 'papers', 'notes', 'questions')]
    await call.message.answer(
        f'📊 <b>Bot Database Stats</b>\n\n'
        f'👨‍🎓 Registered Students: <b>{counts[0]}</b>\n'
        f'📄 Past Papers: <b>{counts[1]}</b>\n'
        f'📖 Short Notes: <b>{counts[2]}</b>\n'
        f'❓ Quiz Questions: <b>{counts[3]}</b>',
        reply_markup=admin_menu_keyboard()
    )
    await call.answer()


# 1. Manual Question Addition Flow (Step-by-Step with buttons)
@router.callback_query(F.data == 'adm:add_q_stream')
async def adm_q_stream(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMINS: return
    await state.clear()
    await call.message.answer(
        '1️⃣ විෂය (Subject) තෝරන්න:',
        reply_markup=keyboard([(s, s) for s in ALL_SUBJECTS], 'adm_qsub')
    )
    await call.answer()


@router.callback_query(F.data.startswith('adm_qsub:'))
async def adm_q_enter_question(call: CallbackQuery, state: FSMContext):
    subject = call.data.split(':', 1)[1]
    stream = 'tech' if subject == 'ICT' else 'commerce'
    await state.update_data(subject=subject, stream=stream)
    await state.set_state(ManualQuestion.question)
    await call.message.answer(f'2️⃣ <b>{subject}</b> සඳහා MCQ ප්‍රශ්නය (Question text) type කර එවන්න:\n(/cancel to stop)')
    await call.answer()


@router.message(ManualQuestion.question)
async def mq_q(message: Message, state: FSMContext):
    text = (message.text or '').strip()
    if not text: return await message.answer('කරුණාකර ප්‍රශ්නය ඇතුළත් කරන්න.')
    await state.update_data(question=text)
    await state.set_state(ManualQuestion.opt_a)
    await message.answer('4️⃣ <b>Option A</b> ඇතුළත් කරන්න:')


@router.message(ManualQuestion.opt_a)
async def mq_a(message: Message, state: FSMContext):
    await state.update_data(a=message.text.strip())
    await state.set_state(ManualQuestion.opt_b)
    await message.answer('5️⃣ <b>Option B</b> ඇතුළත් කරන්න:')


@router.message(ManualQuestion.opt_b)
async def mq_b(message: Message, state: FSMContext):
    await state.update_data(b=message.text.strip())
    await state.set_state(ManualQuestion.opt_c)
    await message.answer('6️⃣ <b>Option C</b> ඇතුළත් කරන්න:')


@router.message(ManualQuestion.opt_c)
async def mq_c(message: Message, state: FSMContext):
    await state.update_data(c=message.text.strip())
    await state.set_state(ManualQuestion.opt_d)
    await message.answer('7️⃣ <b>Option D</b> ඇතුළත් කරන්න:')


@router.message(ManualQuestion.opt_d)
async def mq_d(message: Message, state: FSMContext):
    await state.update_data(d=message.text.strip())
    await state.set_state(ManualQuestion.answer)
    await message.answer(
        '8️⃣ නිවැරදි පිළිතුර (Correct Answer) තෝරන්න:',
        reply_markup=keyboard([(x, x) for x in 'ABCD'], 'mq_ans')
    )


@router.callback_query(F.data.startswith('mq_ans:'), ManualQuestion.answer)
async def mq_ans(call: CallbackQuery, state: FSMContext):
    ans = call.data.split(':', 1)[1]
    await state.update_data(answer=ans)
    await state.set_state(ManualQuestion.explanation)
    await call.message.answer('9️⃣ පිළිතුර සඳහා පැහැදිලි කිරීම (Explanation in Sinhala/English) ඇතුළත් කරන්න:')
    await call.answer()


@router.message(ManualQuestion.explanation)
async def mq_exp(message: Message, state: FSMContext):
    exp = (message.text or '').strip()
    data = await state.get_data()
    execute(
        'INSERT INTO questions(stream, subject, question, a, b, c, d, answer, explanation) '
        'VALUES(?,?,?,?,?,?,?,?,?)',
        (data['stream'], data['subject'], data['question'], data['a'], data['b'], data['c'], data['d'], data['answer'], exp)
    )
    await state.clear()
    await message.answer(
        f'✅ <b>ප්‍රශ්නය සාර්ථකව Database එකට Add විය!</b>\n\n'
        f'📚 {data["subject"]}\n'
        f'❓ {data["question"]}\n'
        f'✔️ Answer: {data["answer"]}',
        reply_markup=admin_menu_keyboard()
    )


# 2. AI Question Generator using Gemini API
@router.callback_query(F.data == 'adm:ai_q_stream')
async def adm_ai_stream(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMINS: return
    if not GEMINI_API_KEY:
        return await call.answer('GEMINI_API_KEY is not configured!', show_alert=True)
    await state.clear()
    await call.message.answer(
        '✨ <b>Gemini AI Quiz Generator</b>\nවිෂය (Subject) තෝරන්න:',
        reply_markup=keyboard([(s, s) for s in ALL_SUBJECTS], 'ai_qsub')
    )
    await call.answer()


@router.callback_query(F.data.startswith('ai_qsub:'))
async def adm_ai_prompt(call: CallbackQuery, state: FSMContext):
    subject = call.data.split(':', 1)[1]
    stream = 'tech' if subject == 'ICT' else 'commerce'
    await state.update_data(subject=subject, stream=stream)
    await state.set_state(AIGenerateQuestions.prompt)
    await call.message.answer(
        f'🤖 <b>{subject}</b> සඳහා ප්‍රශ්න ජනනය කිරීමට මාතෘකාව (Topic or Lesson) එවන්න:\n'
        '(උදා: "Python Programming", "Database SQL", "Accounting Ratios", "Financial Statements")'
    )
    await call.answer()


@router.message(AIGenerateQuestions.prompt)
async def generate_ai_mcq(message: Message, state: FSMContext):
    topic = message.text.strip()
    data = await state.get_data()
    stream = data['stream']
    subject = data['subject']

    await message.answer('⏳ Gemini AI මඟින් A/L standard MCQs 3ක් සාදමින් පවතී...')

    prompt = (
        f"Generate exactly 3 multiple choice questions for Sri Lankan GCE A/L {subject} on topic: '{topic}'.\n"
        "Return ONLY a valid JSON array of objects with keys: "
        "'question', 'a', 'b', 'c', 'd', 'answer' (one of A,B,C,D), 'explanation' (in Sinhala with clear theory).\n"
        "Example format:\n"
        '[{"question":"...","a":"...","b":"...","c":"...","d":"...","answer":"A","explanation":"..."}]'
    )

    try:
        url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}'
        payload = {'contents': [{'parts': [{'text': prompt}]}]}
        async with httpx.AsyncClient(timeout=45) as client:
            res = await client.post(url, json=payload)
            text = res.json()['candidates'][0]['content']['parts'][0]['text']

        clean_json = text.strip().replace('```json', '').replace('```', '').strip()
        items = json.loads(clean_json)

        count = 0
        for item in items:
            execute(
                'INSERT INTO questions(stream, subject, question, a, b, c, d, answer, explanation) '
                'VALUES(?,?,?,?,?,?,?,?,?)',
                (stream, subject, item['question'], item['a'], item['b'], item['c'], item['d'], item['answer'].upper(), item['explanation'])
            )
            count += 1

        await state.clear()
        await message.answer(
            f'🎉 <b>Gemini AI මඟින් සාදන ලද ප්‍රශ්න {count}ක් සාර්ථකව Save විය!</b>\n'
            f'සිසුන්ට දැන් {subject} Quiz හෝ Practice වලදී මේවා භාවිතා කළ හැක.',
            reply_markup=admin_menu_keyboard()
        )
    except Exception as e:
        logging.error('AI Generator Error: %s', e)
        await message.answer(f'❌ ප්‍රශ්න සැකසීමේදී දෝෂයක් ඇති විය: {e}\nකරුණාකර නැවත උත්සාහ කරන්න.')


# 3. Add Past Paper Flow
@router.callback_query(F.data == 'adm:add_paper_stream')
async def adm_paper_stream(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMINS: return
    await state.clear()
    await call.message.answer('විෂය තෝරන්න:', reply_markup=keyboard([(s, s) for s in ALL_SUBJECTS], 'adm_ppsub'))
    await call.answer()


@router.callback_query(F.data.startswith('adm_ppsub:'))
async def adm_paper_year(call: CallbackQuery, state: FSMContext):
    subject = call.data.split(':', 1)[1]
    stream = 'tech' if subject == 'ICT' else 'commerce'
    await state.update_data(subject=subject, stream=stream)
    await state.set_state(AddPaper.year)
    await call.message.answer(f'<b>{subject}</b> past paper එකට අදාළ වර්ෂය (Year, e.g. 2024) type කරන්න:')
    await call.answer()


@router.message(AddPaper.year)
async def adm_paper_year_in(message: Message, state: FSMContext):
    val = (message.text or '').strip()
    if not val.isdigit() or not 2000 <= int(val) <= 2100:
        return await message.answer('Enter valid year (2000–2100).')
    await state.update_data(year=int(val))
    await state.set_state(AddPaper.document)
    await message.answer('📎 Past Paper PDF ගොනුව (Document ලෙස) Telegram වෙත send කරන්න:')


@router.message(AddPaper.document, F.document)
async def adm_paper_doc(message: Message, state: FSMContext):
    if message.from_user.id not in ADMINS: return
    doc = message.document
    if doc.mime_type != 'application/pdf':
        return await message.answer('කරුණාකර PDF ගොනුවක් පමණක් එවන්න.')
    data = await state.get_data()
    execute(
        'INSERT INTO papers(stream, subject, year, file_id) VALUES(?,?,?,?)',
        (data['stream'], data['subject'], data['year'], doc.file_id)
    )
    await state.clear()
    await message.answer(f'✅ Past Paper Saved: <b>{data["subject"]} - {data["year"]}</b>', reply_markup=admin_menu_keyboard())


# 4. Add Short Note Flow
@router.callback_query(F.data == 'adm:add_note_stream')
async def adm_note_stream(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMINS: return
    await state.clear()
    await call.message.answer('විෂය තෝරන්න:', reply_markup=keyboard([(s, s) for s in ALL_SUBJECTS], 'adm_ntsub'))
    await call.answer()


@router.callback_query(F.data.startswith('adm_ntsub:'))
async def adm_note_title(call: CallbackQuery, state: FSMContext):
    subject = call.data.split(':', 1)[1]
    stream = 'tech' if subject == 'ICT' else 'commerce'
    await state.update_data(subject=subject, stream=stream)
    await state.set_state(AddNote.title)
    await call.message.answer(f'<b>{subject}</b> Short Note එකේ මාතෘකාව (Title, e.g. "Theory Summary") type කරන්න:')
    await call.answer()


@router.message(AddNote.title)
async def adm_note_title_in(message: Message, state: FSMContext):
    title = message.text.strip()
    await state.update_data(title=title)
    await state.set_state(AddNote.document)
    await message.answer('📎 Short Note PDF ගොනුව send කරන්න:')


@router.message(AddNote.document, F.document)
async def adm_note_doc(message: Message, state: FSMContext):
    if message.from_user.id not in ADMINS: return
    doc = message.document
    if doc.mime_type != 'application/pdf':
        return await message.answer('කරුණාකර PDF ගොනුවක් පමණක් එවන්න.')
    data = await state.get_data()
    execute(
        'INSERT INTO notes(stream, subject, title, file_id) VALUES(?,?,?,?)',
        (data['stream'], data['subject'], data['title'], doc.file_id)
    )
    await state.clear()
    await message.answer(f'✅ Short Note Saved: <b>{data["subject"]} - {data["title"]}</b>', reply_markup=admin_menu_keyboard())


# 5. Broadcast Announcement to All Students
@router.callback_query(F.data == 'adm:broadcast')
async def adm_broadcast(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in ADMINS: return
    await state.set_state(BroadcastMessage.message)
    await call.message.answer('📢 සියලුම සිසුන් වෙත යැවිය යුතු පණිවිඩය (Text/Announcement) type කරන්න:\n(/cancel to stop)')
    await call.answer()


@router.message(BroadcastMessage.message)
async def adm_broadcast_send(message: Message, state: FSMContext, bot: Bot):
    text = message.text
    students = query('SELECT user_id FROM students')
    sent = 0
    await message.answer(f'⏳ Students {len(students)} දෙනෙකු වෙත පණිවිඩය යවමින් පවතී...')
    for s in students:
        try:
            await bot.send_message(s['user_id'], f'📢 <b>StudyLK Announcement:</b>\n\n{text}')
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await state.clear()
    await message.answer(f'✅ සිසුන් {sent}/{len(students)} දෙනෙකුට පණිවිඩය සාර්ථකව ලැබිණි.', reply_markup=admin_menu_keyboard())


# ── Common /cancel & Fallback ────────────────────────────────────────────────

@router.message(Command('cancel'))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('❌ Action cancelled.', reply_markup=menu(is_adm=(message.from_user.id in ADMINS)))


@router.message(Command('myid'))
async def myid(message: Message):
    await message.answer(f'Your Telegram ID: <code>{message.from_user.id}</code>')


# ── AI Tutor (Gemini AI) ──────────────────────────────────────────────────────

@router.message(AIChat.question)
async def ai_question(message: Message, state: FSMContext):
    prompt = (message.text or '').strip()
    if not prompt or len(prompt) > 1200:
        return await message.answer('Send a text question up to 1200 characters.')
    await message.answer('🤖 Generating explanation...')
    system = (
        'You are a helpful Sri Lankan GCE A/L study tutor specializing in ICT and Accounting. '
        'Explain clearly in simple Sinhala mixed with English technical terms where appropriate. '
        'Show detailed step-by-step methods for calculations or code. '
        'Be accurate according to Sri Lankan A/L syllabus.'
    )

    result = None

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

    if result:
        for i in range(0, len(result), 3500):
            await message.answer(result[i:i + 3500])
    else:
        await message.answer('⚠️ AI service temporarily unavailable. Use /cancel to go back.')


@router.message()
async def fallback(message: Message):
    await message.answer(
        'Use /start for the menu.\n'
        '/myid — your Telegram ID\n'
        '/cancel — exit current action'
    )


# ── Startup & Engine ──────────────────────────────────────────────────────────

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
        logging.info('WEBHOOK_URL not set → polling mode.')
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
