# Autodesk (manage.autodesk.com)

Плагин: `services/svc_autodesk.py`. **Удаление не реализовано.** Плагин логинится, при `--find-only` делает скриншот; в боевом режиме после `find_user_row` возвращает `needs-confirmation: autodesk-deactivate-flow-not-implemented`. Маркер в коде: `# TODO(autodesk-flow):`.

Не обещай пользователю, что Autodesk кого-то удалит. Правильный сценарий: запустить headed, дождаться логина и доделать удаление руками в открытом окне, либо реализовать флоу в плагине как отдельную задачу.

## Env

| Ключ | Значение |
|---|---|
| `AUTODESK_URL` | `https://manage.autodesk.com/` |
| `AUTODESK_LOGIN` / `AUTODESK_PASSWORD` | админ |
| `AUTODESK_2FA_SECRET` | base32 TOTP |

## Логин

signin.autodesk.com, поэтапно: email → Next → password → Sign in → TOTP. Старый `accounts.autodesk.com/Authentication/LogOn` редиректит сюда через `flowId`.

## hCaptcha

Страница входа под **невидимой hCaptcha** (sitekey `6670fa76-…`), срабатывает после сабмита email. На практике проходит молча: за 18+ попыток с чистыми профилями, headless и bot-UA видимый челлендж не появился ни разу.

**solvecaptcha.com не решает hCaptcha:** API отвечает `ERROR_METHOD_CALL` на `method=hcaptcha`. Библиотека `solvecaptcha-python` метод имеет, бэкенд не поддерживает. Решено оставить только ручной фолбэк: `_login()` ждёт до 180 с (`HUMAN_WAIT_MS`), чтобы оператор решил челлендж в видимом окне. Для автоматизации нужен другой провайдер (2captcha, CapSolver, Anti-Captcha), шаблон интеграции: `solve_cloudflare_turnstile` в `_common.py`.

## Если понадобится дописать флоу

После логина нужно: найти раздел управления пользователями в админке, подтвердить UI у оператора, реализовать remove/unassign по образцу `svc_unity.py` (поиск строки → меню → confirm). Добавить заголовок в `SERVICE_HEADLINES` уже есть: «Доступ к Autodesk отозван».
