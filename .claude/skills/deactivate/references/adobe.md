# Adobe (Admin Console)

Плагин: `services/svc_adobe.py`. Чистый User Management API (UMAPI), браузер не открывается, скриншота нет. Есть и `deactivate()`, и `invite()`.

Имя в Asana: «Adobe Creative Cloud (RedBark)». Алиас `svc_adobe_creative_cloud_redbark.py` реэкспортирует обе функции. Любой ключ `ADOBE*` схлопывается в канонический `ADOBE`.

## Env

| Ключ | Значение |
|---|---|
| `ADOBE_CLIENT_ID` / `ADOBE_CLIENT_SECRET` / `ADOBE_ORG_ID` | OAuth **Server-to-Server** из Adobe Developer Console, проект с добавленным User Management API. JWT/Service Account умер 2025-06-30 |
| `ADOBE_DELETE_ACCOUNT` | `true` = удалить аккаунт из directory (разрушительно). По умолчанию false: только removeFromOrg |
| `ADOBE_SCOPES`, `ADOBE_IMS_TOKEN_URL` | переопределения, обычно не нужны |

## Механика deactivate

1. IMS `client_credentials` → `POST https://ims-na1.adobelogin.com/ims/token/v3`, scope `openid,AdobeID,user_management_sdk`.
2. `GET` пользователя. Это обязательно: `removeFromOrg` отвечает success даже для несуществующего, без GET не отличить `user-not-found`.
3. `POST /v2/usermanagement/action/{orgId}` с `removeFromOrg`, `deleteAccount:false`. Эквивалент удаления из меню Users в Admin Console: снимает все product profiles и user groups, возвращает лицензии.

Заголовок комментария: «Лицензия Adobe Creative Cloud отозвана».

## Диагностика

- `failed` на токене: проверь, что креды OAuth S2S, а не JWT, и что в проекте подключён UMAPI.
- `needs-confirmation` при deactivate почти не бывает. При invite означает, что роль не замаплена на product profile, см. скилл `grant`.
