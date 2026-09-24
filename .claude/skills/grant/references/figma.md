# Figma invite

Плагин: `services/svc_figma.py`, функция `invite(context, creds, target_user, role)`. Креды и логин те же, что у деактивации (`FIGMA_URL/LOGIN/PASSWORD/2FA_SECRET`), см. `.claude/skills/deactivate/references/figma.md`.

## Флоу

login → TOTP → Admin → People → кнопка «Invite users» → диалог → email → Seat type → «Send invite».

## Ремап партнёрских адресов

Allow-list орги Figma не включает партнёрские студии, поэтому `<local>@fluytstudio.net` переписывается в `<local>.ff@playrix.com` внутри `_remap_partner_email`. Без ремапа кнопка «Send invite» остаётся неактивной даже с заполненным email и seat. Подтверждено 2026-05-19.

Когда ремап сработал, раннер добавляет в комментарий Asana инструкцию (`FIGMA_REMAP_NOTE`): вход по логину `<local>.ff@playrix.com` и паролю после принятия инвайта на почте, не через кнопку Google и не через аккаунт fluyt. Не убирай эту заметку, без неё человек ищет не тот логин.

Новые партнёрские домены добавляются в `_remap_partner_email` в плагине, не в раннер.

## Seat type

- По умолчанию «Full». Роль из задачи (`Роль:` или лист `Путь к объекту`) маппится в View / Full / Dev через `_resolve_role`.
- Пикер это обычный `<button>` под лейблом «Seat type», ищется по xpath-близости к лейблу, не по `aria-haspopup`. Опции рендерятся в портале вне диалога без семантической роли, поиск по тексту.

## Идемпотентность

Если цель уже в списке People, диалог не открывается → `already-invited`, задача закрывается с «Пользователь уже имеет доступ к Figma».

## Заголовок в Asana

«Инвайт отправлен».
