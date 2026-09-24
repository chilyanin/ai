# Figma

Плагин: `services/svc_figma.py` (~1000 строк, самый зрелый). Есть и `deactivate()`, и `invite()`.

## Env

| Ключ | Значение |
|---|---|
| `FIGMA_URL` | login URL, обычно `https://www.figma.com/login` |
| `FIGMA_LOGIN` / `FIGMA_PASSWORD` | админ |
| `FIGMA_2FA_SECRET` | base32 TOTP |

## Флоу

login → 2FA (TOTP) → Admin → People → строка пользователя → меню «⋯» → Remove → чекбокс «I understand» → подтверждение.

## Особенности

- Перед кликом по Remove плагин повторно проверяет, что в строке есть `target_user`. Иначе `failed: row-mismatch`. Это защита от клика по чужой строке при перерисовке таблицы.
- Диалог Remove требует ввести имя/чекбокс, плагин это делает (`_check_understand_box`, `_wait_remove_enabled`).
- In-app оверлеи (промо, туры) закрываются `_dismiss_in_app_overlay`.
- Ссылка Admin в сайдбаре появляется быстро, таймаут 5 с. Если `failed` на этапе Admin, скорее всего аккаунт не админ.
- Заголовок комментария в Asana: «Пользователь удалён из организации Playrix».

## Партнёрские адреса

Адреса `<local>@fluytstudio.net` в оргу Figma не входят, при invite они переписываются в `<local>.ff@playrix.com`. При deactivate ремапа нет: удаляется ровно тот адрес, что в задаче. Если пользователь «не найден», проверь, не лежит ли он под `.ff@playrix.com`.

## Диагностика

- `failed: login`: смотри скриншот, чаще всего Figma показала капчу или новое «trust device». Запусти headed и пройди руками.
- `user-not-found`: проверь ремап выше и то, что искали по email, а не по имени.
