# EmpireBot

Десктопный экранный бот Empire: Four Kingdoms. Оболочка — **Tauri + Rust**, игровой движок — **Python** по скриншотам/OCR/кликам BlueStacks. Внутренний протокол игры не используется.

## Текущий этап

Живой сценарий реализован только для **замков разбойников** (Robber Baron Castles) в Великой империи. Остальные режимы подключены как модульные **заглушки** с официальными названиями, квотами и live-логами. Реализация атак по ним — позже.

Последняя живая проверка разбойников: успешная реальная отправка и ускоренный dry-run 24.78с без финальной отправки. Пакет из 5 атак остановился на 2/5 из-за военачальников/экрана подготовки.

## Архитектура

- `desktop/` — окно Tauri: квоты кампании, старт/пауза, live-лог каждого события движка.
- `e4kbot/runtime/` — JSON-воркер для Tauri (`python -u run.py worker`), планировщик квот, live.jsonl.
- `e4kbot/modes/` — каталог режимов. `robber_barons` = live, остальные = stub.
- `e4kbot/client.py` / `vision.py` — текущая экранная атака разбойников.

Кампания заполняет квоты **по очереди, но без ожидания возврата предыдущих войск**: 20 разбойников в пути, затем 5 драконов, затем 10 фортов ураганов — пока хватает свободных военачальников. Одновременно на экране всё равно один клик, потому что одно окно BlueStacks.

## Режимы и официальные имена

| id | RU | Official | Королевство | Статус |
|---|---|---|---|---|
| robber_barons | Замки разбойников | Robber Baron Castles | Великая империя | live |
| storm_forts | Форты островов ураганов | Storm Forts | Острова ураганов / Storm Islands | stub |
| barbarian_towers | Варварские башни | Barbarian Towers | Вечный ледник / Everwinter Glacier | stub |
| barbarian_fortresses | Варварские крепости | Barbarian Fortresses | Вечный ледник | stub |
| desert_towers | Башни пустыни | Desert Towers | Пылающие пески / Burning Sands | stub |
| desert_fortresses | Крепости пустыни | Desert Fortresses | Пылающие пески | stub |
| cultist_towers | Башни культистов | Cultist Towers | Огненные вершины / Fire Peaks | stub |
| dragons | Драконы | Dragons | Огненные вершины | stub |
| nomad_camps | Лагеря кочевников | Nomad Invasion / Nomad Camps | Великая империя | stub |
| samurai_camps | Лагеря самураев | Samurai Invasion / Samurai Camps | Великая империя | stub |
| bloodcrows | Стервятники | Bloodcrow Invasion | Великая империя | stub |
| alien_castles | Замки чужаков | Alien Invasion / Alien Castles | Великая империя | stub |

«Сёгун» в игре — это очки Shogun / Daimyo у ивента самураев, не отдельный тип лагеря. Стервятники в каталоге привязаны к Bloodcrow Invasion.

## Live-диагностика

Каждое событие пишется в:

- окно Tauri (фильтр по уровню и тексту);
- `data/live.jsonl`;
- `logs/bot_YYYY-MM-DD.log`.

Ключевые event: `cycle.next`, `mode.select`, `mode.stub`, `attack.cycle.start/end`, `attack.sent`, `worker.pause/start/stop`. Если бот ломается, в live-логе видно режим, экранный result и `last_error` без захода во внутренности игры.

## Запуск

```powershell
cd C:\Users\Dima\Desktop\EmpireBot
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
cd desktop
npm install
npm run dev
```

CLI без окна Tauri: `.\.venv\Scripts\python.exe run.py --no-panel`

`dry_run: true` по умолчанию. Финальная зелёная галочка нажимается только при `dry_run: false`.
