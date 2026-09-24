# Plastic SCM (cloud dashboard)

Плагин: `services/svc_plastic_scm.py`. Проверен вживую 2026-06-03.

## Env

| Ключ | Значение |
|---|---|
| `PLASTIC_SCM_URL` | `https://www.plasticscm.com/dashboard/cloud/<org>/users-and-groups` |
| `PLASTIC_SCM_LOGIN` / `PLASTIC_SCM_PASSWORD` | Unity ID |
| `PLASTIC_SCM_2FA_SECRET` | base32 TOTP или `otpauth://` |

## Флоу

Login page → кнопка «Sign in with Unity» → Unity ID OAuth (email → password → TOTP) → редирект на users-and-groups.

## Механика удаления

Страница server-rendered Bootstrap, кебаб-меню нет. Каждая строка `div.row.user-row` с красной кнопкой `button.btn-delete[data-email="<email>"]`.

1. Клик по кнопке вызывает **нативный** `window.confirm("Are you sure you want to remove user "<email>"?")`. Плагин регистрирует одноразовый dialog-handler до клика и принимает confirm только если в тексте есть «remove user» и email цели. Любой другой диалог отклоняется.
2. При accept уходит `DELETE /api/cloud/organizations/<org>/users/<email>`.
3. Успех: строка удаляется из DOM → `deactivated`.
4. Ошибка: в строке появляется `.alert-danger` → `failed: delete-rejected (<текст>)`.
5. Строка осталась, ошибки нет → `needs-confirmation: row-still-present`. Открой скриншот и проверь руками.

## Диагностика

- `failed: not-on-plastic-scm`: после логина не вернулись на домен plasticscm.com. Обычно Unity показала «trust this device» с нестандартной кнопкой.
- `failed: delete-button-not-found`: пользователь найден в поиске, но у строки нет кнопки. Возможно, это владелец организации, его удалить нельзя.
