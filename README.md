# 🌐 ok-ru-friends-pathfinder

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Domain: OSINT / Graph Theory](https://img.shields.io/badge/OSINT-Graph%20Analysis-orange.svg)](#)
[![Storage: SQLite WAL](https://img.shields.io/badge/Cache-SQLite%20WAL-brightgreen.svg)](#)

Модульный Open-Source инструмент для анализа социальных графов и поиска кратчайших цепочек связей в социальной сети **Одноклассники (OK.ru)** на базе **asyncio + Двунаправленного BFS**, SQLite WAL-кэширования, пула сессий/прокси и мультиформатного экспорта для OSINT.

> **English Summary:** Modular Python 3.9+ OSINT & Graph Analysis tool implementing async Bidirectional BFS, Anti-Hub degree pruning, surname/geo/education heuristics, SQLite WAL persistent caching with TTL, proxy/account pool rotation, K-shortest paths, and GEXF/CSV/JSON/HTML exports.

---

## ⚡ Архитектура и ключевые возможности

- **Модульная архитектура (`ok_traverser/`)**: чёткое разделение слоёв (`config`, `models`, `heuristics`, `storage`, `auth`, `crawler`, `engine`, `exporters`, `cli`).
- **Async Двунаправленный BFS + K-Shortest Paths (`--paths K`)**: снижает сложность с $O(b^d)$ до $O(b^{d/2})$ и умеет находить до $K$ независимых кратчайших цепочек.
- **SQLite WAL-кэш (`ok_graph_cache.db`) с TTL**: $O(1)$ запись каждого узла вместо перезаписи гигантских JSON. При первом запуске **автоматически мигрирует** данные из старого `ok_friends_cache.json`.
- **Anti-Hub фильтрация**: отсечение коммерческих страниц и сетевых хабов ($>N$ друзей) на 1-й странице пагинации и быстрая 1-страничная проверка мостов (без скачивания всех 20 страниц).
- **Расширенные эвристики (Фамилии + Гео/ВУЗ/Школа)**: учёт девичьих фамилий в скобках `(...)`, совпадения городов/школ/ВУЗов и органического круга общения.
- **Пул аккаунтов и ротация прокси (`sessions.json`) + Token Bucket**: поддержка нескольких сессий и HTTP/SOCKS5 прокси с переключением при локальном rate limit.
- **Защита от сбоев и честный антифрод**: явный детект `429`, капчи, удалённых и закрытых аккаунтов; защита кэша от частичной пагинации; атомарные чекпоинты с сохранением активной очереди.
- **Deep OSINT Fallback (`--deep-osint`)**: опциональный сбор публичных комментаторов/реакций с открытой стены, если список друзей закрыт настройками приватности.
- **Экспорт (`html,json,csv,gexf`)**: XSS-безопасный HTML, JSON, табличный CSV и XML-граф **GEXF 1.2** для анализа в **Gephi / Maltego / NetworkX**.

---

## 📁 Структура проекта

```text
├── ok_pathfinder.py          # Точка входа (CLI / Интерактивный Dashboard)
├── login_helper.py           # Авторизация через Playwright
├── start.bat                 # Быстрый запуск в Windows
├── ok_traverser/
│   ├── config.py             # Конфигурация (.env), константы, единый Mobile UA
│   ├── models.py             # Dataclass-модели результатов и слотов сессий
│   ├── heuristics.py         # Нормализация URL, Anti-Hub, эвристики (фамилии + гео/учёба)
│   ├── storage.py            # SQLiteGraphCache (WAL + TTL) и атомарный CheckpointManager
│   ├── auth.py               # OKBrowserSession, SessionPool (proxy/accounts), TokenBucket
│   ├── crawler.py            # OKCrawler (HTTP-пул, статусы 429/капча/удалён, Deep OSINT)
│   ├── engine.py             # Async GraphSearchEngine (Двунаправленный BFS, K-пути)
│   ├── exporters.py          # Экспорт в HTML (XSS-safe), JSON, CSV, GEXF
│   └── cli.py                # Эргономичный CLI (subcommands + интерактивные пресеты)
├── tests/                    # Unit/Integration тесты на базе мок HTML-фикстур
├── .env.example              # Шаблон переменных окружения
├── sessions.example.json     # Шаблон пула аккаунтов и прокси
├── requirements.txt          # Зависимости проекта
├── .gitignore                # Исключение секретов, куки, БД кэша и отчётов
└── LICENSE                   # Лицензия MIT
```

---

## 🚀 Установка и запуск

```bash
git clone https://github.com/blecan/ok-ru-friends-pathfinder.git
cd ok-ru-friends-pathfinder

python -m venv venv
# Windows: venv\Scripts\activate
# Linux/macOS: source venv/bin/activate

pip install -r requirements.txt
playwright install chromium

cp .env.example .env
```

---

## 📖 Использование

### Интерактивный Dashboard
```bash
python ok_pathfinder.py
```
Доступны быстрые пресеты (Быстрый, Глубокий OSINT, Ручной), статистика SQLite-кэша, статус пула сессий и мгновенное возобновление с чекпоинта (`[R]`).

### Команды CLI (для автоматизации и пайплайнов)
```bash
# 1. Авторизация через браузер
python ok_pathfinder.py login

# 2. Поиск пути + экспорт в HTML, JSON и GEXF (для Gephi)
python ok_pathfinder.py search -s 1234567890 -t 9876543210 --paths 3 --export html,json,gexf --non-interactive

# 3. Поиск с проверкой публичных реакций на закрытых профилях
python ok_pathfinder.py search -s 1234567890 -t 9876543210 --deep-osint

# 4. Возобновление прерванного поиска
python ok_pathfinder.py resume --export html,json

# 5. Статистика SQLite кэша и пула сессий
python ok_pathfinder.py stats

# 6. Очистка кэша и чекпоинтов
python ok_pathfinder.py cache --clear
```

---

## 🧪 Запуск тестов

```bash
python -m unittest discover -s tests -v
# или с pytest:
pytest -v
```

---

## ⚖️ Дисклеймер (Legal & Ethical Disclaimer)

Инструмент создан исключительно в образовательных и исследовательских целях для демонстрации графовых алгоритмов и OSINT-методологии. Программа работает только с публично доступными данными согласно настройкам приватности пользователей. Пользователь самостоятельно несёт ответственность за соблюдение правил платформы и применимого законодательства. Распространяется по лицензии [MIT](LICENSE).
