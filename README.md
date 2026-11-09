# A/L Telegram Education Bot

## Windows quick start
1. Install Python 3.11+ from python.org. Enable **Add Python to PATH**.
2. Open https://t.me/BotFather, send `/newbot`, and copy the token.
3. Open a terminal in this project folder.
4. `py -m venv .venv`
5. `.venv\Scripts\activate`
6. `pip install -r requirements.txt`
7. Copy `.env.example` to `.env`, and set `BOT_TOKEN`.
8. `python bot.py`; message your bot `/myid`.
9. Stop the process, set `ADMIN_IDS` to that numeric ID in `.env`, and restart.
10. Send `/addpaper` or `/addquestion` as admin. Use `/stats` to check counts.

## AI (optional)
Install Ollama from https://ollama.com, run `ollama pull llama3.2:3b`, ensure Ollama is running, then choose AI Tutor in the bot. Local AI may require substantial RAM and may make mistakes, especially in Sinhala and A/L calculations.

## Example question (seven lines after /addquestion and stream/subject selection)
What is 2 + 3?
4
5
6
7
B
2 සහ 3 එකතු කළ විට 5 ලැබේ.

## Operational notes
- SQLite stores all scores, students, questions and PDF file IDs in `education.db`; back up this file regularly.
- This MVP uses Telegram numeric user IDs and names; notify users and avoid collecting unnecessary personal data.
- In-memory FSM means ongoing quizzes are reset if the bot restarts. Persistent scores remain.
- The bot supports one quiz session per student and cumulative 1 point per correct answer. It does not yet have timed exams, per-paper scores, bulk import, or a web admin dashboard.
- Admin access is controlled by `ADMIN_IDS`; never share `.env` or the bot token.
- Upload only papers that you have permission to distribute.
- The bot is online only while the Python process is running.
