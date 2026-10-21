# Нямням · панель продвижения

Веб-панель для поиска небольших Telegram-каналов, где можно рассказать про бота Нямням (@ruslan_njamnjam_bot).

Что умеет:
- добавить каналы списком (`@имя`, `t.me/имя`, `https://t.me/s/имя`), дубликаты пропускаются;
- собрать с публичной страницы `t.me/s/ИМЯ` название, описание, подписчиков, последние 20 постов и контакты из описания;
- ИИ-анализ канала через любой OpenAI-совместимый API: тематика, аудитория, оценка 0–100, как зайти, угол подачи, риски; дневной лимит запросов;
- справочник админов: @username и ссылки t.me из описаний каналов (с ролью: реклама, сотрудничество, консультации…), привязка к каналам, ручное добавление, статус контакта (не писали, написали, ответил, договорились, отказ, не писать), заметки, история, CSV;
- воронка статусов (новый, подходит, не подходит, написали, ответили, договорились, отказ) с заметками и историей;
- выгрузка в CSV (открывается в Excel).

Отправки сообщений в Telegram нет.

Стек: Python 3.12, FastAPI, SQLite, шаблоны Jinja2, без CDN. Память около 60 МБ.

## Файлы

- `app.py` — веб-приложение, вход, страницы, фоновые задачи сбора и анализа
- `scraper.py` — разбор `t.me/s/ИМЯ`
- `ai.py` — промпт и запрос к ИИ
- `db.py` — SQLite
- `templates/`, `static/` — HTML и CSS
- `nyamnyam-promo.service` — systemd-юнит (MemoryMax=200M, DynamicUser)

## Локальный запуск

```
python -m venv venv
venv/bin/pip install -r requirements.txt   # Windows: venv\Scripts\pip
cp .env.example .env                       # заполнить PANEL_PASSWORD и SESSION_SECRET
venv/bin/python app.py                     # http://localhost:3003
```

## Сервер

Прод Timeweb 212.113.120.108, порт 3003, без домена.

- код: `/opt/nyamnyam-promo` (venv там же), настройки: `/opt/nyamnyam-promo/.env` (права 600, root)
- база: `/var/lib/nyamnyam-promo/promo.db`
- сервис: `systemctl status nyamnyam-promo`, логи: `journalctl -u nyamnyam-promo`

Обновление: скопировать файлы в `/opt/nyamnyam-promo` (кроме `.env`), при изменении зависимостей `venv/bin/pip install -r requirements.txt`, затем `systemctl restart nyamnyam-promo`.

## Деплой из GitHub

Код панели живёт в ветке `promo` репозитория https://github.com/rrangulov/Njamnjam (папка `promo/`). На проде скрипт `deploy.sh` забирает ветку, копирует файлы в `/opt/nyamnyam-promo` (кроме `.env`), при изменении `requirements.txt` ставит зависимости, делает копию базы и перезапускает сервис:

```
sudo bash /opt/nyamnyam-promo/deploy.sh
```
