# Finance — утренний новостной скринер MOEX

Собирает новости за последние ~16 часов (вечер вчера + ночь + утро), привязывает к тикерам 1–2 эшелона MosExchange, первично классифицирует по типам событий. Дальше Claude Code превращает сводку в утренний брифинг с кандидатами на сильное движение к открытию.

**Важно.** Это инструмент для сбора и структурирования данных, а не источник инвестиционных рекомендаций. Никакая LLM не предсказывает завтрашние движения акций с честной вероятностью. Итоговый брифинг — материал для вашего решения, не решение за вас.

## Стек

- Python 3.12, SQLite, `httpx`, `feedparser`, `selectolax`, `aiomoex`
- LLM-слой — через Claude Code (используется ваша подписка, отдельного API-ключа не нужно)
- Отправка брифинга — Telegram-бот

## Источники

- **e-disclosure.ru** — обязательные раскрытия эмитентов. Работает только с российского IP (за пределами РФ — 403, скрипт логирует и продолжает).
- **MOEX ISS** — уведомления биржи и справочник инструментов.
- **RSS** — Интерфакс, РБК, Ведомости (finance), Коммерсантъ (business + finance), Прайм.

## Установка

```powershell
cd "C:\Users\maxop\Desktop\Масик\Projects\Finance"
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
# заполнить TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID (см. ниже)
```

## Ежедневный флоу

```
┌ 07:30 МСК ── Windows Task Scheduler запускает run_morning.bat
│              ├─ finance.collect  → пишет в data/finance.db
│              └─ finance.export   → output/daily_news_YYYY-MM-DD.md
│
├ 09:00 МСК ── Вы открываете Claude Code:
│              «прогони скринер»  (или /screener)
│              Claude читает daily_news.md + screener_prompt.md,
│              пишет output/daily_briefing_YYYY-MM-DD.md
│
└ ~09:30 МСК ─ Опционально:
               .venv\Scripts\python.exe -m finance.telegram_send output\daily_briefing_...md
               → брифинг падает в личку в Telegram
```

## Ручной прогон

```powershell
# собрать данные за последние 24 часа
.\.venv\Scripts\python.exe -X utf8 -m finance.collect --hours 24

# сгенерировать markdown-сводку
.\.venv\Scripts\python.exe -X utf8 -m finance.export --hours 24

# обновить только справочник тикеров
.\.venv\Scripts\python.exe -X utf8 -m finance.tickers

# отправить готовый брифинг в Telegram
.\.venv\Scripts\python.exe -m finance.telegram_send output\daily_briefing_2026-08-06.md
```

## Telegram-бот: настройка

1. В Telegram напишите [@BotFather](https://t.me/BotFather) команду `/newbot`, следуйте инструкциям
2. Полученный `HTTP API token` вставьте в `.env` в `TELEGRAM_BOT_TOKEN`
3. Найдите вашего бота в поиске Telegram и напишите ему любое сообщение (например, `/start`)
4. Получите свой `chat_id`:
   ```powershell
   .\.venv\Scripts\python.exe -m finance.telegram_send --whoami
   ```
5. Вставьте `chat_id` в `.env`
6. Проверка:
   ```powershell
   .\.venv\Scripts\python.exe -m finance.telegram_send README.md
   ```

## Windows Task Scheduler: автозапуск

1. Открыть `Task Scheduler` (Планировщик заданий) → `Create Basic Task`
2. Name: `Finance morning screener`
3. Trigger: `Daily`, время `07:30`, ежедневно
4. Action: `Start a program`
5. Program/script: полный путь к `run_morning.bat`
6. Start in: полный путь к папке проекта
7. Готово. Проверить: правой кнопкой по задаче → `Run`

Логи сбора: `output/collect.log`. Выход: `output/daily_news_YYYY-MM-DD.md`.

## Использование в Claude Code

Скажите: **«прогони скринер за сегодня»**. Claude прочитает свежий `daily_news_*.md` и файл `screener_prompt.md` с инструкцией по формату, соберёт брифинг в `output/daily_briefing_*.md`, покажет его в чате. Дальше на ваш выбор:

- отправить в Telegram командой выше
- углубить любую идею — запрос дополнительных данных через MOEX ISS уже подключён через скилл `moex-iss`

## Расширения (когда пригодится)

- **Telegram-каналы как источник**: подключить `telethon` или сборку через MTProto. Осторожно: много шума, нужен ручной whitelist каналов.
- **Товарный/валютный фон**: Brent, USD/RUB, азиатские индексы — добавить отдельный сборщик, чтобы включать в макро-резюме.
- **Ретроспектива**: собрать историю за квартал и посчитать, у каких типов событий фактически была реакция ≥5% на открытии → калибровка правил.
- **Sentiment**: локальная модель типа `blanchefort/rubert-base-cased-sentiment` для дополнительного тега.

## Структура проекта

```
Finance/
├── finance/
│   ├── config.py               # env, пути, зоны
│   ├── http.py                 # общий httpx-клиент
│   ├── storage.py              # SQLite: тикеры, news, runs
│   ├── tickers.py              # справочник MOEX 1-2 эшелона + синонимы
│   ├── matcher.py              # текст → SECID через синонимы
│   ├── rules.py                # первичные теги событий по регексам
│   ├── collectors/
│   │   ├── rss.py              # Интерфакс/РБК/Ведомости/Коммерсант/Прайм
│   │   ├── moex.py             # MOEX ISS site news
│   │   └── edisclosure.py      # e-disclosure.ru
│   ├── collect.py              # оркестратор
│   ├── export.py               # markdown-сводка
│   └── telegram_send.py        # отправка в Telegram
├── data/finance.db             # SQLite (создаётся автоматически)
├── output/                     # сводки и брифинги
├── screener_prompt.md          # инструкция для Claude Code по составлению брифинга
├── run_morning.bat             # запуск для Task Scheduler
├── requirements.txt
├── .env.example
└── README.md
```
