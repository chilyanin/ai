# Adobe invite (UMAPI)

Плагин: `services/svc_adobe.py`, функция `invite(context, creds, target_user, role)`. Браузер не открывается, скриншота нет. Авторизация та же OAuth S2S (`ADOBE_CLIENT_ID/CLIENT_SECRET/ORG_ID`), см. `.claude/skills/deactivate/references/adobe.md`.

## Главное про лицензии

Членство в орге само по себе **не даёт лицензию**. Лицензии едут на членстве в product profile. Поэтому роль из задачи обязана маппиться на группы; если маппинга нет, плагин отдаёт `needs-confirmation` **до** любой записи в оргу.

## Маппинг роли на группы (.env)

| Ключ | Значение |
|---|---|
| `ADOBE_INVITE_GROUPS_<ROLE>` | группы для конкретной роли. `<ROLE>` = роль из задачи, uppercase, `\W+` → `_`: «after effects» → `ADOBE_INVITE_GROUPS_AFTER_EFFECTS` |
| `ADOBE_INVITE_GROUPS` | дефолт для ролей без своего ключа |
| `ADOBE_IDENTITY_TYPE` | `adobeID` (по умолчанию) / `enterpriseID` / `federatedID`. Enterprise/federated требуют заявленный домен в directory |
| `ADOBE_COUNTRY` | ISO-2, по умолчанию US, обязателен для federatedID |

Текущие ключи в `.env`: PHOTOSHOP, ILLUSTRATOR, ANIMATE, AFTER_EFFECTS, CREATIVE_CLOUD_PRO, ACROBAT_PRO. Реальные имена профилей орги: «<Product> Configuration» для одиночных продуктов, «Creative Cloud All Apps Configuration» для Creative Cloud Pro. Если Adobe добавит продукты, актуальный список профилей берётся из `GET /v2/usermanagement/groups/{orgId}/{page}`.

## Откуда роль

Онбординг-шаблон: строка `Роль: Photoshop`. Support-вариант: `Путь к объекту: Adobe -> After Effects`, берётся весь последний сегмент после `->` (фикс 2026-08-20, раньше обрезалось до «after»).

## Флоу

1. GET пользователя.
2. Нет в орге → `addAdobeID` (или тип из `ADOBE_IDENTITY_TYPE`) + `add {group: [...]}` → `invited (added groups: …)`.
3. Есть в орге → добавляются только недостающие группы. Все уже есть → `already-member`.

## Заголовок в Asana

«Лицензия <Product> выдана», product это роль в Title Case («photoshop» → «Photoshop»).

## Диагностика

- `needs-confirmation: role … not mapped`: добавить `ADOBE_INVITE_GROUPS_<ROLE>` в `.env` с точным именем профиля и перезапустить.
- `failed` при create для enterprise/federated: домен пользователя не заявлен в directory орги.
