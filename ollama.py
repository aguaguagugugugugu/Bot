"""Модуль для поиска открытых Ollama-серверов через HTML Shodan."""

from __future__ import annotations

import ipaddress
import os
import re
import time
import logging
import urllib.parse
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Event
from typing import Optional

import requests
from requests.exceptions import RequestException

log = logging.getLogger(__name__)

DEFAULT_PORT = 11434
DEFAULT_TIMEOUT = 5
DEFAULT_WORKERS = 20

IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


@dataclass
class Server:
    ip: str
    models: list[str] = field(default_factory=list)
    latency: Optional[float] = None

    @property
    def base_url(self) -> str:
        return f"http://{self.ip}:{DEFAULT_PORT}"


# ---------- Shodan HTML ----------

def _is_public_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def fetch_ips_from_shodan_html(
    query: str = 'port:11434 product:"Ollama" -tag:honeypot',
    timeout: float = 10,
    retries: int = 2,
) -> list[str]:
    """
    Парсит HTML-страницу shodan.io/search/facet и вытаскивает IP.
    Возвращает публичные IP без дубликатов.
    Внимание: без авторизации возвращается ограниченный набор (~100 IP).
    """
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.shodan.io/search/facet?query={encoded}&facet=ip"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }

    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 429:
                log.warning("Shodan 429 (rate limit), ждём...")
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            ips = set(IP_RE.findall(r.text))
            return [ip for ip in ips if _is_public_ip(ip)]
        except RequestException as e:
            last_err = e
            log.warning("Shodan HTML попытка %d: %s", attempt + 1, e)
            time.sleep(2)

    log.error("Не удалось получить IP: %s", last_err)
    return []


# ---------- Проверка сервера ----------

def _check_server(ip: str, stop: Event, timeout: float) -> Optional[Server]:
    if stop.is_set():
        return None
    try:
        t0 = time.perf_counter()
        r = requests.get(f"http://{ip}:{DEFAULT_PORT}/api/tags", timeout=timeout)
        latency = time.perf_counter() - t0
        r.raise_for_status()
        data = r.json()
    except (RequestException, ValueError):
        return None

    models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    if not models:
        return None
    return Server(ip=ip, models=models, latency=latency)


def scan(
    ips: list[str],
    timeout: float = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
    stop: Optional[Event] = None,
) -> list[Server]:
    stop = stop or Event()
    servers: list[Server] = []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_check_server, ip, stop, timeout): ip for ip in ips}
        for fut in as_completed(futures):
            if stop.is_set():
                break
            srv = fut.result()
            if srv:
                servers.append(srv)
                log.info("Живой: %s (%d моделей, %.0f мс)",
                         srv.ip, len(srv.models), srv.latency * 1000)
    return servers


# ---------- Тест инференса ----------

def test_model_latency(
    server: Server,
    model: str,
    prompt: str = "ping",
    timeout: float = 30,
) -> Optional[float]:
    url = f"{server.base_url}/api/generate"
    payload = {"model": model, "prompt": prompt, "stream": False,
               "options": {"num_predict": 1}}
    try:
        t0 = time.perf_counter()
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return time.perf_counter() - t0
    except RequestException as e:
        log.debug("Инференс %s/%s упал: %s", server.ip, model, e)
        return None


def pick_fastest(
    servers: list[Server],
    timeout: float = 30,
    workers: int = 10,
) -> Optional[tuple[Server, str, float]]:
    tasks = [(s, m) for s in servers for m in s.models]
    if not tasks:
        return None

    best: Optional[tuple[Server, str, float]] = None
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(test_model_latency, s, m, "ping", timeout): (s, m)
            for s, m in tasks
        }
        for fut in as_completed(futures):
            srv, model = futures[fut]
            lat = fut.result()
            if lat is None:
                continue
            log.info("Инференс %s/%s: %.0f мс", srv.ip, model, lat * 1000)
            if best is None or lat < best[2]:
                best = (srv, model, lat)
    return best


# ---------- .env ----------

def update_env(path: str, **values: str) -> None:
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()

    keys = set(values)
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            out.append(line)
            continue
        k = s.split("=", 1)[0].strip()
        if k in keys:
            out.append(f"{k}={values[k]}")
            keys.discard(k)
        else:
            out.append(line)

    for k in keys:
        out.append(f"{k}={values[k]}")

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)
