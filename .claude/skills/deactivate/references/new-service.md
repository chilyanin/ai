# Новый сервис / нет плагина

Если для сервиса из задачи нет `services/svc_<slug>.py`, раннер берёт `services/_generic.py`: открывает URL, печатает логин и ждёт человека. При запуске из Claude stdin не TTY, поэтому generic сразу отдаёт `failed: no-plugin` и оставляет скриншот лендинга.

## Слаг и env-ключ

Из имени сервиса в Asana: lowercase, всё не-буквенно-цифровое → `_`, обрезать `_` по краям.
- «Plastic SCM» → `svc_plastic_scm.py`, ключи `PLASTIC_SCM_*`
- «Slack (Workspace Redbark2)» → `svc_slack_workspace_redbark2.py`, `SLACK_WORKSPACE_REDBARK2_*`
- Кириллица сохраняется как есть.

Раннер читает и `SERVICE_<KEY>_<FIELD>`, и голый `<KEY>_<FIELD>`.

## Режимы кредов

| Режим | Ключи | Пример |
|---|---|---|
| Браузер | `URL` + `LOGIN` + `PASSWORD` (+ `2FA_SECRET`) | Figma, Unity |
| API-токен | `TOKEN` (+ что объявит `REQUIRED_ENV`) | SyncSketch |
| OAuth S2S | `CLIENT_ID` + `CLIENT_SECRET` + `ORG_ID` | Adobe |
| Сессия | `AUTH=session` + `URL`, вход руками через `prime_session.py` | ChatGPT |

Плагин может объявить `REQUIRED_ENV = ("LOGIN", "TOKEN", …)`, тогда раннер требует именно эти поля.

## Контракт плагина

```python
def deactivate(context, creds: dict, target_user: str) -> str
```
`context` это Playwright BrowserContext (persistent, свой на сервис). `creds` содержит `url/login/password/token/team_id/client_id/client_secret/org_id/auth/notes`. Возвращать один из статусов: `deactivated`, `already-deactivated`, `user-not-found`, `found` (при find-only), `skipped`, `needs-confirmation: <причина>`, `failed: <причина>`.

## Как писать

1. Скопировать `services/_template.py` в `services/svc_<slug>.py`.
2. Использовать хелперы из `services/_common.py`: `get_page(context, slug)` (страница для скриншота), `session_attr`/`set_session_attr` (не логиниться повторно между задачами), `submit_totp(page, "<KEY>_2FA_SECRET")`, `find_user_row(page, target)`, `is_find_only()`, `is_on_auth_page(page)`.
3. Обязательно уважать `is_find_only()`: вернуть `found` до любого разрушительного действия.
4. Перед разрушительным кликом перепроверять, что строка содержит `target_user` (см. `row-mismatch` в Figma).
5. Нативные `confirm()` принимать только через dialog-handler с проверкой текста (см. Plastic SCM).
6. Добавить заголовок для Asana в `SERVICE_HEADLINES` в `deactivate.py` **только** когда флоу реально удаляет. Без заголовка пишется «Деактивация выполнена автоматически».
7. Если имя в Asana бывает с хвостом (как Syncsketch), добавить префикс в `PREFIX_ALIASES` в `services/__init__.py` и алиас ключа в `_env_key_candidates` в `deactivate.py`.
8. Если сервис делает и invite, положить `invite(context, creds, target_user, role)` в тот же файл, см. скилл `grant`.

## Проверка

```bash
python3 -c "import ast; ast.parse(open('services/svc_<slug>.py').read()); print('ok')"
python3 deactivate.py --task <gid> --find-only --yes
```
Первый боевой прогон только headed, без `--headless`.
