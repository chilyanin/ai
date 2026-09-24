---
name: deactivate
description: Оффбординг через Asana-задачи «Удалить из <Сервис> - <email>». Запускает deactivate.py (Playwright/API) для Figma, Unity, Plastic SCM, Slack Redbark2, Skills Base, SyncSketch, Adobe, Autodesk, Maxon. Используй, когда просят обработать задачи «Удалить из» на дату, деактивировать/удалить пользователя из сервиса, проверить, есть ли пользователь в сервисе (find-only), или добавить новый сервис в деактиватор.
---

# Деактивация пользователей (deactivate.py)

Рабочая папка: `/Users/chilikin-a/Documents/claude`. Все команды запускать из неё.
Интерпретатор: системный `python3` (3.12, Playwright установлен). venv нет.

## Что делает раннер

1. Берёт из Asana открытые задачи текущего пользователя с due-date `--date`, чьё имя начинается с `Удалить из `.
2. Парсит `<Сервис>` и `<email>` из заголовка (разделитель: пробел-дефис-пробел). Email ищется сначала в заголовке, потом в notes.
3. Резолвит плагин `services/svc_<slug>.py` и креды из env (`<KEY>_URL/LOGIN/PASSWORD/...`, алиасы UNITY_*/ADOBE_*/SYNCSKETCH_*).
4. Проверяет в комментариях задачи, не выполнялась ли она уже (guard по заголовку комментария). Такие задачи помечаются `already-processed` и пропускаются без `--force`.
5. Для каждого сервиса открывает свой persistent-профиль `.browser_profiles/<slug>`, вызывает `deactivate(context, creds, target)`.
6. Делает скриншот в `screenshots/<date>/`, прикладывает его к задаче, пишет комментарий-заголовок и (по умолчанию) закрывает задачу.

## Порядок работы

1. **Всегда сначала dry-run.** Покажи пользователю план: сервисы, цели, статусы `ready` / `already-processed` / `missing-credentials`.
   ```bash
   python3 deactivate.py --date 2026-09-23 --dry-run
   ```
2. Если в плане есть `missing-credentials`, назови недостающие ключи (раннер печатает их) и не запускай этот сервис.
3. Для сервиса, в котором не уверен, сначала `--find-only`: плагин найдёт пользователя и остановится без разрушительных действий.
   ```bash
   python3 deactivate.py --date 2026-09-23 --service figma --find-only --yes
   ```
4. Боевой запуск. Браузер по умолчанию видимый (headed), это нужно для ручного фолбэка (капча, 2FA). `--headless` только если пользователь попросил.
   ```bash
   python3 deactivate.py --date 2026-09-23 --yes
   ```
5. Прочитай блок `=== summary ===` и отчитайся по каждой строке. Формат: `gid  service  target  outcome`.
6. Если в summary есть `asana-comment-error` с текстом `tasks:write`, значит комментарий и скриншот ушли, а задача не закрылась. Скажи пользователю закрыть задачу руками.

## Флаги

| Флаг | Смысл |
|---|---|
| `--date YYYY-MM-DD` | задачи с этим due-date (ровно один из `--date` / `--task`) |
| `--task <gid>` | одна задача по GID, без фильтра по исполнителю и дате |
| `--service NAME` | фильтр по сервису, подстрока без учёта регистра и пунктуации, повторяемый |
| `--target ID` | фильтр по email/идентификатору, повторяемый |
| `--dry-run` | только план |
| `--find-only` | найти пользователя и остановиться |
| `--yes` | без вопроса `proceed? [y/N]` (обязателен при запуске из Claude, stdin не TTY) |
| `--headless` | без окна браузера |
| `--no-complete` | не закрывать задачу в Asana |
| `--include-completed` | брать и закрытые задачи |
| `--force` | перезапустить задачи с уже существующим комментарием автоматизации. Только по явной просьбе пользователя |
| `--screenshot-dir` | куда складывать скриншоты (по умолчанию `screenshots`) |

## Статусы плагинов

| Статус | Что значит | Что делать |
|---|---|---|
| `deactivated` | пользователь удалён, задача закрыта (если есть права) | отчитаться |
| `already-deactivated` | уже не было доступа, задача закрыта | отчитаться |
| `user-not-found` | в сервисе не найден, комментарий написан, задача НЕ закрывается | пользователь решает сам |
| `found` | только при `--find-only` | можно запускать боевой прогон |
| `needs-confirmation: …` | плагин дошёл до конца, но не уверен в результате | открыть скриншот, посмотреть референс сервиса |
| `failed: login` | не залогинился | референс сервиса, раздел «Логин» |
| `failed: no-plugin` | плагина нет | `references/new-service.md` |
| `missing-credentials` / `missing-url` | нет ключей в env | назвать ключи из вывода раннера |
| `already-processed` | guard по комментарию | пропустить или `--force` |

## Референсы по сервисам

Открывай референс, когда сервис падает, возвращает `needs-confirmation`, или пользователь спрашивает про его особенности.

| Имя сервиса в Asana | Файл | Режим |
|---|---|---|
| Figma | [references/figma.md](references/figma.md) | браузер + TOTP |
| Unity, Unity (Pro), Unity (Enterprise), Unity (Pro + Unity Teams Advanced) | [references/unity.md](references/unity.md) | браузер + TOTP |
| Plastic SCM | [references/plastic-scm.md](references/plastic-scm.md) | браузер, вход через Unity ID |
| Slack (Workspace Redbark2) | [references/slack-redbark2.md](references/slack-redbark2.md) | браузер + TOTP |
| Skills Base, Skills Base (Programmers) | [references/skills-base.md](references/skills-base.md) | браузер + Turnstile через solvecaptcha |
| Syncsketch, Syncsketch (RedBark), Syncsketch (…) при переходе в Playrix | [references/syncsketch.md](references/syncsketch.md) | REST API |
| Adobe, Adobe Creative Cloud (RedBark) | [references/adobe.md](references/adobe.md) | REST API (UMAPI, OAuth S2S) |
| Autodesk | [references/autodesk.md](references/autodesk.md) | браузер, **удаление не реализовано** |
| Maxon | [references/maxon.md](references/maxon.md) | браузер, **удаление не реализовано** |
| любой другой | [references/new-service.md](references/new-service.md) | generic: открывает URL и ждёт человека |

## Окружение

Боевые прогоны идут на **Windows-хосте** (Vault, `.venv\Scripts\python.exe`, планировщик `AsanaDeactivateFindOnly` каждый день в 20:00 в `--find-only --headless`, лог `deactivate-find.log`; дашборд `monitor.py` на порту 5111 с кнопками Offboarding/Onboarding). На этом Mac `VAULT_ADDR` в `.env` пустой, поэтому любой запуск падает с `secrets error: VAULT_ADDR is not set`. Не переключай `SECRETS_BACKEND` на env и не трогай `.env.backup-pre-vault` ради запуска: на Mac допустимы только проверки синтаксиса, `--help` и правки кода.

Профили браузера между ОС не переносятся, на новой машине сессии праймятся заново через `prime_session.py`.

## Секреты

Креды живут в Vault (`SECRETS_BACKEND` в `.env`), в самом `.env` только URL и несекретные настройки. Проверка, что все нужные ключи на месте:
```bash
python3 secrets_provider.py --check
```
Список обязательных ключей: `vault-required-keys.txt`. Никогда не печатай значения секретов в чат.

## Сессии браузера

Каждый сервис держит свой профиль в `.browser_profiles/<slug>`. Если плагин пишет `needs-login` или сессия протухла у session-auth сервиса, открой окно для ручного входа:
```bash
python3 prime_session.py <slug> --url <admin-url>
```

## Правила безопасности

- Не запускай боевой прогон без dry-run в этой же сессии.
- Не используй `--force` и `--include-completed` без явной просьбы.
- Autodesk и Maxon останавливаются после логина: не обещай пользователю, что они кого-то удалят.
- Многие задачи «Удалить из Syncsketch» висят на пуле Routing ISS, а не на пользователе. Раннер их не видит, пока их не переназначат. Для одиночной задачи используй `--task <gid>`.
- Не редактируй плагины «на лету» ради одного запуска. Если нужен фикс, скажи об этом и внеси его как обычное изменение кода.
