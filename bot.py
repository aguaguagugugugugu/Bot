import asyncio
import os
import logging
import requests
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramRetryAfter

import memory
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

# --- Настройка логирования ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("Не задан BOT_TOKEN в .env")

TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-1001234567890"))

# --- Telegram ---
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# --- LLM (OpenAI-совместимый) ---
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
LLM_API_KEY  = os.getenv("LLM_API_KEY", "ollama")
LLM_MODEL    = os.getenv("LLM_MODEL", "qwen3:8b")
LLM_TIMEOUT  = float(os.getenv("LLM_TIMEOUT", "120")) # 2 минуты

# --- Промпты ---
SYSTEM_PROMPT = os.getenv("SYSPROMPT", "")
INITIATIVE_PROMPT = os.getenv("INITPROMPT", "")

queue: asyncio.Queue = asyncio.Queue()

def _llm_request_sync(chat_text: str, system_prompt: str) -> str:
    """Синхронный запрос к LLM через requests."""
    url = f"{LLM_BASE_URL}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}",
    }
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": chat_text},
        ],
        "max_tokens": 2000,
        "temperature": 1,
    }

    r = requests.post(url, json=payload, headers=headers, timeout=LLM_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip()

async def ask_llm(chat_text: str, system_prompt: str = SYSTEM_PROMPT) -> str:
    """Обёртка: синхронный requests в отдельном потоке."""
    return await asyncio.to_thread(_llm_request_sync, chat_text, system_prompt)

async def sender_worker():
    """Воркер отправки сообщений с защитой от лимитов Telegram."""
    logging.info("sender_worker запущен")
    while True:
        chat_id, reply_to, text = await queue.get()
        try:
            await bot.send_message(
                chat_id, text,
                reply_to_message_id=reply_to,
                allow_sending_without_reply=True,
            )
            await asyncio.sleep(1.2)
        except TelegramRetryAfter as e:
            logging.warning("Flood control, ждём %s сек", e.retry_after)
            await asyncio.sleep(e.retry_after)
            await queue.put((chat_id, reply_to, text))
        except Exception as e:
            logging.error("Ошибка отправки: %s", e)
        finally:
            queue.task_done()

@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    memory.clear_history(message.chat.id)
    memory.save_history()
    await message.reply("🧹 Память чата очищена!")

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    hist = memory.get_history(message.chat.id)
    await message.reply(f"📊 Сообщений в памяти: {len(hist)}")

@dp.message(F.content_type == ContentType.TEXT)
async def on_message(message: Message):
    # 1. Игнорируем ботов и команды
    if message.from_user is None or message.from_user.is_bot:
        return
    if not message.text or message.text.startswith("/"):
        return
    message.text = message.text[:101]
    chat_id = message.chat.id
    user_name = message.from_user.first_name or "Участник"

    # 2. Добавляем сообщение в память
    memory.add_message(chat_id, user_name, message.text)
    memory.save_history()

    # 3. Формируем контекст
    chat_text = (
        "История чата:\n"
        + "\n".join(memory.get_history(chat_id))
        + f"\n\nОтветь на последнее сообщение от {user_name}."
    )

    # 4. Запрос к LLM
    try:
        answer = await ask_llm(chat_text)
    except requests.HTTPError as e:
        logging.error("LLM HTTP %s: %s", e.response.status_code, e.response.text[:300])
        return
    except requests.RequestException as e:
        logging.error("LLM network error: %s", e)
        return
    except (KeyError, ValueError) as e:
        logging.error("LLM parse error: %s", e)
        return

    if not answer or answer.upper() == "SKIP":
        return

    # 5. Отправляем ответ
    await queue.put((chat_id, message.message_id, answer))

async def send_initiative():
    """Функция, которая генерирует и отправляет сообщение сама."""
    logging.info("=== [ИНИЦИАТИВА] ЗАПУЩЕНА ===")
    cur = datetime.now().hour
    if cur > 19 or cur < 5:
        print("иди нахуй")
        return
    hist = memory.get_history(TARGET_CHAT_ID)
    context = "\n".join(hist[-10:]) if hist else "Чат пока пустой."
    chat_text = f"Последние сообщения в чате:\n{context}\n\nНапиши что-нибудь сам.Учти текущее время:{datetime.now().hour+3}"

    try:
        logging.info("=== [ИНИЦИАТИВА] Отправляю запрос в LLM... ===")
        answer = await ask_llm(chat_text, system_prompt=INITIATIVE_PROMPT)
        
        logging.info("=== [ИНИЦИАТИВА] LLM ответила: %r ===", answer)

        # Просто проверяем, что строка не пустая
        if not answer.strip():
            logging.warning("=== [ИНИЦИАТИВА] ОТМЕНА: LLM вернула пустую строку ===")
            return

        logging.info("=== [ИНИЦИАТИВА] Кладу в очередь для отправки в %s ===", TARGET_CHAT_ID)
        await queue.put((TARGET_CHAT_ID, None, answer))
        
        memory.add_message(TARGET_CHAT_ID, "Бот", answer)
        memory.save_history()
        logging.info("=== [ИНИЦИАТИВА] УСПЕХ! Сообщение в очереди ===")
        
    except Exception as e:
        logging.error("=== [ИНИЦИАТИВА] ОШИБКА: %s ===", e, exc_info=True)

async def main():
    memory.load_history()
    asyncio.create_task(sender_worker())
    # Инициализируем планировщик
    scheduler = AsyncIOScheduler()
    # Для теста: каждые 3 минуты, первый запуск через 1 минуту
    scheduler.add_job(
        send_initiative,
        trigger="interval",
        hours=1,
        jitter=0,
        next_run_time=datetime.now() + timedelta(minutes=1)
    )
    scheduler.start()

    me = await bot.get_me()
    logging.info("Запущен @%s | LLM: %s @ %s", me.username, LLM_MODEL, LLM_BASE_URL)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
