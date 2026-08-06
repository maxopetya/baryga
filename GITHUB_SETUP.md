# Настройка GitHub Actions для автоматических брифингов

## Что делает автоматика

- **Каждый будний день в ~09:30 МСК** — workflow `morning.yml` собирает свежие новости, гоняет модель, формирует брифинг и отправляет в Telegram
- **Каждое воскресенье в 20:00 МСК** — workflow `weekly_recalibrate.yml` обновляет справочник тикеров, пересчитывает бэты и overnight-регрессию на новых 180 днях

## Шаги (первый раз)

### 1. Создать приватный репозиторий

На github.com → New repository → **Private** → без README (у нас уже есть).

### 2. Инициализировать git локально и запушить

Из папки проекта:

```powershell
cd "C:\Users\maxop\Desktop\Масик\Projects\Finance"
git init
git add .
git commit -m "initial: project skeleton + calibration + regression"
git branch -M main
git remote add origin git@github.com:<ВАШ-USERNAME>/<НАЗВАНИЕ-РЕПО>.git
git push -u origin main
```

Если по SSH не пускает — используйте HTTPS-URL: `https://github.com/<ВАШ-USERNAME>/<НАЗВАНИЕ-РЕПО>.git`. Понадобится Personal Access Token как пароль.

### 3. Добавить секреты

Репозиторий → Settings → Secrets and variables → Actions → New repository secret.

Добавить **два** секрета:

| Название | Значение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Ваш bot token от BotFather (тот, что сейчас в `.env`) |
| `TELEGRAM_CHAT_ID`   | Ваш chat_id (сейчас `5257677770` в `.env`) |

Ни в коем случае не пушить `.env` в репо — он в `.gitignore`.

### 4. Проверить, что workflow виден

Репозиторий → вкладка Actions → должны появиться два workflow:
- `Morning briefing`
- `Weekly recalibration`

### 5. Первый ручной запуск

Actions → `Morning briefing` → **Run workflow** → main → Run.

Через 2-3 минуты должен прийти брифинг в Telegram и в репозитории появится коммит от `GitHub Actions Bot`.

## Ротация токена

Токен, который был засвечен в этом чате, **обязательно ротируйте** до пуша в GitHub:

1. Telegram → @BotFather → /mybots → ваш бот → API Token → Revoke current token
2. Новый токен → GitHub Secrets → отредактировать `TELEGRAM_BOT_TOKEN` (уже не через `.env`)
3. Локально в `.env` тоже обновить, если хотите продолжать локально тестировать

## Как понять, что что-то сломалось

- Утренний брифинг **не пришёл в Telegram** → зайти в Actions → Morning briefing → посмотреть последний run → красная звёздочка = ошибка, кликнуть чтобы прочитать логи
- Артефакт `briefing-*.md` в run'е доступен 14 дней — можно скачать и посмотреть, что было сгенерировано
- Cron может задерживаться на 5-15 минут в часы пик — это норма GitHub Actions

## Что где хранится

| Файл | Куда попадает |
|---|---|
| Код (`finance/*.py`) | В репо, версионируется |
| `data/finance.db` | В репо, обновляется workflow'ом ежедневно |
| `output/daily_briefing_*.md` | Только в артефакте run'а, локально в вашей папке |
| `.env` | **Никогда** в репо |
| Токены | Только в GitHub Secrets |

## Что не сделано, потому что переехали в CI

- **Task Scheduler на локале** — можно удалить, если больше не хотите дублирования:
  ```powershell
  Unregister-ScheduledTask -TaskName 'FinanceMorningScreener' -Confirm:$false
  ```
- Локальный `run_morning.bat` можно оставить как ручной триггер.

## Возможные проблемы и их решения

**«e-disclosure возвращает 403»** — GitHub Actions runners в США, e-disclosure гео-блокирует. Ничего не сделать без прокси-сервера в РФ. Скрипт корректно продолжает работу без этого источника.

**«Yahoo Finance иногда отваливается»** — попробуйте повторный запуск (Actions → Re-run failed jobs).

**«БД разрастается»** — если через полгода `finance.db` станет > 100 МБ, включим политику: чистить news старше 90 дней автоматически в weekly workflow.
