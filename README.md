# Ayla Grid Bot v0.6 — спрощений PAPER-проєкт

## Що змінилось
- Повністю прибрано `/api/keys/*` та форму введення ключів у вебпанелі.
- Мережа Bybit обирається в `.env`: `BYBIT_ENV=mainnet` або `BYBIT_ENV=testnet`.
- Ключі Bybit — **необов'язкові** для Paper: публічні котирування й свічки працюють без них.
- Ключі з `.env` **не використовуються** цим релізом (ніяких приватних API-викликів чи реальних ордерів).
- Оновлений компактний вебінтерфейс: KPI, свічки з рівнями та Paper-угодами, ордери, історія, сітка, backtest, Telegram у згорнутому блоці.
- Виправлено порядок визначення класів та маршрутів перед `uvicorn.run`.
- Підрахунок загальної кількості угод і комісій тепер враховує **всю** історію, а не лише останні 100.
- Збережено сумісність з `ayla_v04.db`.

## Оновлення на Raspberry Pi

Зроби резервну копію:
```bash
cd ~/ayla_grid_bot
sudo systemctl stop ayla-grid
cp main.py main_before_v06.py
cp index.html index_before_v06.html
cp ayla_v04.db ayla_v04_before_v06.db
```

Розпакуй архів на комп'ютері та скопіюй **main.py, index.html, requirements.txt** у `/home/pi/ayla_grid_bot/`. **Не замінюй `.env` і `ayla_v04.db`.**

Приклад `.env`:
```dotenv
BYBIT_ENV=mainnet
BYBIT_API_KEY=
BYBIT_API_SECRET=
HOST=0.0.0.0
PORT=8090
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

`BYBIT_API_KEY` та `BYBIT_API_SECRET` можна заповнити, але в цій версії вони **не потрібні та не використовуються**.

Після копіювання:
```bash
cd ~/ayla_grid_bot
chmod 600 .env
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m py_compile main.py
sudo systemctl start ayla-grid
sudo systemctl status ayla-grid --no-pager
curl -fsS http://127.0.0.1:8090/openapi.json | python3 -c "import json,sys; print('\n'.join(json.load(sys.stdin)['paths']))"
```

Для локальної перевірки віртуального середовища: `.venv/bin/python main.py`.

## Безпека та обмеження
- **PAPER ONLY**. Реальні ордери не підтримуються.
- Вебпанель не має авторизації, а Start/Pause/Reset доступні кожному, хто має доступ до порту. Не відкривай порт 8090 у публічний інтернет. Для доступу тільки через SSH постав `HOST=127.0.0.1` і використовуй `ssh -L 8090:127.0.0.1:8090 pi@raspberry4`.
- `.env` зберігається локально, не додавай його до Git і не надсилай нікому.
- Симуляція оптимістична: без прослизання, із модельною комісією, тому результати не гарантують прибутку.
- Зміна `BYBIT_ENV` змінює джерело котирувань, **але не скидає історію Paper**. Не порівнюй результати різних мереж як одну стратегію.
