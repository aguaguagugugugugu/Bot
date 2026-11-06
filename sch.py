"""Раз в час ищет самый быстрый сервер+модель и обновляет .env."""

import logging
import signal
from threading import Event
from pathlib import Path

import ollama as sc

ENV_PATH = Path(".env")
INTERVAL = 3600

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("scheduler")

stop = Event()


def _handle(sig, frame):
    log.info("Останавливаюсь...")
    stop.set()


def one_cycle() -> bool:
    log.info("=== Новый цикл ===")
    ips = sc.fetch_ips_from_shodan_html()
    if not ips:
        log.warning("IP не найдены — пропуск")
        return False
    log.info("Кандидатов: %d", len(ips))

    servers = sc.scan(ips, stop=stop)
    if not servers:
        log.warning("Живых серверов нет")
        return False
    log.info("Живых: %d", len(servers))

    best = sc.pick_fastest(servers, timeout=90)
    if not best:
        log.warning("Ни одна модель не ответила")
        return False

    srv, model, latency = best
    log.info("Лучший: %s / %s (%.0f мс)", srv.ip, model, latency * 1000)

    sc.update_env(
        str(ENV_PATH),
        OLLAMA_BASE_URL=srv.base_url,
        OLLAMA_MODEL=model,
    )
    log.info("Обновил %s", ENV_PATH)
    return True


def main() -> None:
    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    while not stop.is_set():
        try:
            one_cycle()
        except Exception:
            log.exception("Ошибка в цикле")
        stop.wait(INTERVAL)


if __name__ == "__main__":
    main()
