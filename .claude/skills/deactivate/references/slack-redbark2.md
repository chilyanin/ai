# Slack (Workspace Redbark2)

Плагин: `services/svc_slack_workspace_redbark2.py`. Браузерный: у обычного (не Enterprise Grid) воркспейса нет API для деактивации, `admin.users.*` и SCIM недоступны. Старый xoxp-токен отозван и не используется.

## Env

| Ключ | Значение |
|---|---|
| `SLACK_WORKSPACE_REDBARK2_URL` | `https://redbark2.slack.com/admin` |
| `SLACK_WORKSPACE_REDBARK2_LOGIN` / `_PASSWORD` | `services@fluytstudio.net` |
| `SLACK_WORKSPACE_REDBARK2_2FA_SECRET` | base32 TOTP |
| `SLACK_WORKSPACE_REDBARK2_LOGIN_URL` | no-SSO форма, по умолчанию `https://redbark2.slack.com/?no_sso=1&redir=%2Fadmin` |

## Флоу

Если в профиле есть живая сессия, логин пропускается. Иначе: no-SSO форма → отклонить OneTrust cookie-баннер (`#onetrust-reject-all-handler`) → `#email` / `#password` → `#signin_btn` → TOTP → `/admin` → таблица Members → фильтр по email → «Actions» в строке → «Deactivate account» → confirm.

## Особенности

- OTP у Slack это шесть однобуквенных инпутов, только у первого `autocomplete="one-time-password"`. Общий `submit_totp` в `_common.py` распознаёт `maxlength=1` и печатает код через `keyboard.type`. Slack сам сабмитит на последней цифре.
- Статус `already-deactivated` возвращается, если в строке уже стоит метка Deactivated. Задача при этом закрывается.
- Заголовок комментария: «Аккаунт деактивирован в админке».

## Диагностика

- `failed: login` при валидных кредах: Slack мог включить проверку «new device» по email. Запусти headed и пройди руками, сессия сохранится в профиле.
- Ручной прайминг сессии без кредов:
  ```bash
  python3 prime_session.py slack --url https://redbark2.slack.com/admin
  ```
