# uicheck: ночная проверка интерфейсов и самопочинка селекторов

Раннер: `uicheck.py`. Пакет: `uicheck/`. Конфиг селекторов: `uicheck/overrides.json`.
Задача планировщика на Windows: `AsanaUICheck`, каждый день в 03:00 (`install-uicheck-task.bat` → `uicheck-run.bat` → `uicheck.log`, отчёт `uicheck\reports\latest.md`).

## Зачем

Плагины `services/svc_*.py` держатся на CSS-селекторах админок. Когда сервис меняет интерфейс, `deactivate.py`/`grant.py` падают с `failed: …` в момент, когда задача уже горит. `uicheck.py` каждую ночь проходит по браузерным сервисам, у которых есть задача в Asana на дату прогона, на «канарейке» из этой задачи, проверяет каждый селектор и, если он перестал совпадать, подбирает замену на живой странице, верифицирует её Playwright-ом и записывает в `uicheck/overrides.json`. Плагины читают этот файл при старте, так что следующий боевой прогон использует уже починенный селектор. Код плагинов ночью не меняется.

## Канарейка: кто ищется в сервисе

Решение оператора от 2026-10-14: канарейка берётся из задач Asana с due-date = дата прогона (`--date`, по умолчанию сегодня), а не из админской учётки.

| Задача на дату | Канарейка | Что проверяется |
|---|---|---|
| `Удалить из <Сервис> - <email>` | этот email (он ещё в сервисе, удаление в 20:00) | полный путь: список → поиск → строка → меню → пункт Remove; плюс invite-путь, где есть |
| только `Доступ к <Сервис> - <ФИ>` | email из тела задачи (в сервисе его ещё нет) | login → список → поиск → invite-диалог; шаги строки помечаются `skipped` |
| задач нет | — | сервис **не проверяется**, в отчёте «Not run: no-tasks» |

Сервис сопоставляется с задачей по каноническому env-ключу (`Unity (Pro)` → `UNITY`, `ChatGPT Team` → `CHATGPT`). Если задач на сервис несколько, приоритет у `Удалить из`. Если канарейка в списке не найдена (уже удалена, другой адрес), шаги строки получают `skipped` с пометкой user-not-found, это не ошибка интерфейса. `--canary EMAIL` задаёт канарейку вручную (как участника) и отключает обращение к Asana: это для отладки.

Следствие: в ночь без задач проверка не идёт, дрифт обнаружится в ночь перед первой задачей на этот сервис, т.е. до 20:00-прогона того же дня.

## Что проверяется (глубина)

login (если форма показана) → список участников → поиск → строка канарейки → кнопка меню строки → **наличие** пункта Remove / Deactivate / Remove from team / trash → `Escape`. Для Figma и ChatGPT дополнительно: кнопка Invite → диалог → поле email → кнопка Send (ничего не вводится, `Escape`).

Никогда: клик по подтверждению, открытие диалога подтверждения удаления, ввод email в invite. Нативные `confirm()` отклоняются глобальным хендлером. Это решение оператора от 2026-10-13, глубина «до пункта меню».

Сервисы: Figma, Unity (один probe на все алиасы), Plastic SCM, Slack Redbark2, Skills Base (оба org, общие селекторы `skills_base`), Maxon, Autodesk (только login + список), ChatGPT (invite). Adobe и SyncSketch — API, не проверяются.

## Как устроены селекторы

В плагине каждый CSS-селектор объявлен через `sel(slug, key, default)` из `services/_selectors.py`:
```python
SEARCH = sel("slack_workspace_redbark2", "search", '[data-qa="workspace-members__table-header-search_input"]')
```
`sel()` регистрирует дефолт и возвращает значение из `uicheck/overrides.json`, если оно там есть. Регексы `get_by_role(name=re…)` не подменяются, они и так текстовые.

Ключи строк (`row`) хранят селектор **контейнера** (`tr, [role="row"]`), `with_text()` добавляет `:has-text(email)`. Ключи с `{target}` (`plastic_scm.delete_button`) — шаблоны, подставляет `fill_target()`.

Полный список ключей: `python3 uicheck.py --list` (звёздочка = действует override).

## Файл overrides.json

```json
"figma": {
  "search": {
    "selector": "input[data-testid='people-filter']",
    "default":  "input[type=\"search\"], input[placeholder*=\"Search\" i], ...",
    "source":   "llm:google:gemini-2.5-flash",
    "since":    "2026-10-14",
    "verified": "2026-10-14",
    "replaced": "input[type=\"search\"], ..."
  }
}
```
- `source: code` — значение равно дефолту из кода.
- `source: heuristic | llm:<provider:model> | agent:<…>` — ночная починка.
- Если override перестал совпадать, а дефолт снова работает, раннер сам возвращает `source: code` (с пометкой `note`).
- Файл коммитится. `python3 uicheck.py --init-overrides` дописывает новые ключи и сохраняет живые overrides.

## Починка (repair)

1. С живой страницы собираются кандидаты: интерактивные элементы + сгенерированный селектор (`data-qa` > `data-testid` > `id` > `name` > `aria-label` > классы > `tag:has-text`).
2. Выбор кандидата: LLM через browser-use (`UICHECK_LLM`) или эвристика по `hints` шага, если ключа нет / LLM упал.
3. Playwright проверяет предложение: ровно одно видимое совпадение (для строк — содержит канарейку). Только после этого пишется override.
4. Навигационный шаг (`figma.people_url`): browser-use `Agent` подключается к тому же Chromium по CDP, доходит до страницы без изменений, итоговый URL сохраняется.

`UICHECK_LLM`: `auto` (по умолчанию) берёт победителя из `uicheck/state/llm_choice.json`; если файла нет и есть оба ключа (`OPENAI_API_KEY`, `GOOGLE_API_KEY`), один раз запускает замер (`--bench-llm`) и сохраняет более быстрого из ответивших верно. `openai[:model]`, `google[:model]`, `anthropic[:model]` — явно. `heuristic` — без LLM. `--no-repair` — только отчёт.

## Отчёт

`uicheck/reports/<дата_время>.md` (+ `.json`, `latest.md`), скриншоты упавших шагов рядом. Статусы шага:

| статус | смысл | что делать |
|---|---|---|
| `ok` | селектор совпал | ничего; `ui-diff +N/-M` в detail = разметка вокруг изменилась, селекторы пока живы |
| `repaired` | записан новый override или снят старый | посмотреть `selector` в отчёте; при желании перенести в код |
| `warn` | CSS-фолбэк устарел, но role/text-локатор плагина работает | не срочно |
| `failed` | не совпало и починить не удалось | скриншот, пройти руками, поправить плагин |
| `error` | probe упал исключением | смотреть `uicheck.log` |
| `skipped` | шаг не выполняется ночью (confirm, activate) или сессия кэширована (форма логина не показана) | ничего |

Exit code: 0 — ок/repaired/warn, 1 — есть `failed`/`error`, 2 — конфиг.

## Команды

```bash
python3 uicheck.py --list                       # сервисы и ключи
python3 uicheck.py --dry-run                    # задачи на сегодня -> сервисы, канарейки, путь
python3 uicheck.py --dry-run --date 2026-10-20  # то же на другую дату
python3 uicheck.py --service figma              # один сервис, headed
python3 uicheck.py --yes --headless             # как ночью
python3 uicheck.py --init-overrides             # пересобрать overrides.json
python3 uicheck.py --bench-llm                  # замер OpenAI vs Google
python3 -m uicheck.selftest                     # офлайн-тест на фикстурах (работает на Mac)
python3 -m uicheck.selftest --llm --env-file .env_test   # то же с реальной LLM
```
На Mac боевой прогон невозможен (нет Vault), доступны `--list`, `--init-overrides`, selftest и `--bench-llm --env-file .env_test`.

## Откат и перенос в код

- Откатить одну починку: в `overrides.json` поставить `selector` = `default` и `source: code` (или удалить запись и запустить `--init-overrides`).
- Запечь в код: скопировать `selector` в дефолт `sel(...)` плагина, затем `--init-overrides`.
- Канарейка: см. раздел выше; `--canary EMAIL` только для ручной отладки.

## Ограничения

- Login-селекторы проверяются только когда сервис показал форму (живая сессия их пропускает со статусом `skipped`).
- Autodesk: hCaptcha не решается, в headless упавший логин вернёт `failed: login`, без зависания (таймауты шагов).
- Slack: `confirm_button` и `activate_item` не проверяются (нужен диалог / деактивированный пользователь).
- Профили Chromium общие с `deactivate.py`/`grant.py`: не запускать uicheck одновременно с ними (20:00 vs 03:00).
