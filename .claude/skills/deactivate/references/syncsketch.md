# SyncSketch

Плагин: `services/svc_syncsketch.py`. Чистый REST API, браузер не открывается, скриншота нет. Объявляет `REQUIRED_ENV = ("LOGIN", "TOKEN", "TEAM_ID")`, поэтому раннер требует ровно эти ключи.

Имена в Asana: «Syncsketch», «Syncsketch (RedBark)», «Syncsketch (<email>) при переходе в Playrix». Все слаги с префиксом `svc_syncsketch` резолвятся в этот плагин (`PREFIX_ALIASES`), креды всегда `SYNCSKETCH_*`.

## Env

| Ключ | Значение |
|---|---|
| `SYNCSKETCH_LOGIN` | email админа (общий `soft@playrix.com`) |
| `SYNCSKETCH_TOKEN` | API key со страницы Settings админа |
| `SYNCSKETCH_TEAM_ID` | id воркспейса, живой: `14550` («Playrix Account») |
| `SYNCSKETCH_HOST` | опционально, по умолчанию `https://www.syncsketch.com` |

## Механика

1. Авторизация `Authorization: apikey <login>:<key>`, при 401/403 фолбэк на `?api_key=&username=`.
2. `_verify_workspace` сверяет `SYNCSKETCH_TEAM_ID` с `/api/v1/account/` один раз за прогон. Неверный id опасен: connections отдают 404 для всех, и все выглядят уже удалёнными.
3. Поиск `GET /api/v1/simpleperson/?email__iexact=` (scoped на воркспейс).
4. Членство = **любое** connection в `GET /api/v2/user/<uid>/connections/account/<wsid>/`. Обычные пользователи держат только `type: project`, `type: account` только у админов. Не считай отсутствие account-записи за «уже удалён».
5. Удаление `POST /api/v2/remove-users/` с `which=account`, затем `which=project` для оставшихся connections. На практике account-вызов каскадит и второй этап не нужен, но плагин проверяет фактически.
6. После удаления connections отдают 404 `No Person matches the given query`, это читается как «ничего нет».

## Статусы

- `user-not-found` после успешного удаления в прошлом прогоне это норма: lookup scoped на воркспейс и удалённого не видит. Пользователь решил **не** маппить это в `already-deactivated`, чтобы задача не закрывалась на одном отсутствии.
- Заголовок комментария: «Аккаунт удалён».

## Транспортные задачи

Тип «Syncsketch (<email>) при переходе в Playrix»: адрес сервиса лежит в скобках в имени сервиса, парсер берёт его оттуда, а не из «Корпоративная почта» в notes. Это разные адреса.

## Проверки

```bash
python3 -m services.svc_syncsketch --accounts
```
Список воркспейсов админа (для `TEAM_ID`).
```bash
python3 _validate_syncsketch.py <email>
```
Find-only без Asana.
