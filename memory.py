import json
import os
import logging
from collections import defaultdict
from typing import Deque, Dict, List

HISTORY_FILE = "history.json"
MAX_MESSAGES = 50  # сколько последних сообщений хранить для каждого чата

# В памяти держим словарь: {chat_id: [сообщение1, сообщение2, ...]}
_history: Dict[int, List[str]] = defaultdict(list)

def load_history():
    """Загружает историю из файла при старте бота."""
    global _history
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Преобразуем ключи обратно в int (в JSON они всегда строки)
                _history = defaultdict(list, {int(k): v for k, v in data.items()})
            logging.info(f"История загружена из {HISTORY_FILE}. Чатов: {len(_history)}")
        except Exception as e:
            logging.error(f"Ошибка загрузки истории: {e}")

def save_history():
    """Синхронно сохраняет историю в файл."""
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(_history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Ошибка сохранения истории: {e}")

def add_message(chat_id: int, user_name: str, text: str):
    """Добавляет сообщение и обрезает историю до MAX_MESSAGES."""
    _history[chat_id].append(f"{user_name}: {text}")
    # Оставляем только последние MAX_MESSAGES
    if len(_history[chat_id]) > MAX_MESSAGES:
        _history[chat_id] = _history[chat_id][-MAX_MESSAGES:]

def get_history(chat_id: int) -> List[str]:
    """Возвращает историю для конкретного чата."""
    return _history[chat_id]

def clear_history(chat_id: int):
    """Очищает историю чата (для команды /reset)."""
    _history[chat_id].clear()
