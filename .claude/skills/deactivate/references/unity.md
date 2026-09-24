# Unity Cloud (и алиасы)

Плагин: `services/svc_unity.py`. Алиасы для точных имён из Asana:
- `svc_unity_pro.py` → «Unity (Pro)»
- `svc_unity_enterprise.py` → «Unity (Enterprise)»
- `svc_unity_pro_unity_teams_advanced.py` → «Unity (Pro + Unity Teams Advanced)»

Все алиасы вызывают `svc_unity._deactivate` с собственным `page_slug`, чтобы скриншоты не путались. Креды у всех одни: любой ключ `UNITY_*` схлопывается в канонический `UNITY`.

## Env

| Ключ | Значение |
|---|---|
| `UNITY_URL` | URL страницы Members организации на cloud.unity.com |
| `UNITY_LOGIN` / `UNITY_PASSWORD` | Unity ID админа |
| `UNITY_2FA_SECRET` | base32 TOTP или `otpauth://` URL |

## Флоу

Members URL → Unity ID login (email → password) → TOTP (страница «Security check», инпут находится по placeholder/aria-label «Authentication code») → cookie-баннер → поиск пользователя → «⋯» в строке → «Remove from organization» → confirm.

## Особенности

- Тот же Unity ID используется для входа в Plastic SCM, см. `plastic-scm.md`. Профили браузера разные, сессии не шарятся.
- Заголовок комментария: «Доступ к Unity отозван» для всех алиасов.
- Если задача называется просто «Unity», грузится `svc_unity` напрямую.

## Диагностика

- Застрял на TOTP: проверь `UNITY_2FA_SECRET`, сгенерируй код `python3 totp.py` и сравни с приложением.
- `user-not-found`: в Unity членство по email Unity ID, а не по корпоративной почте. Пользователь мог зарегистрироваться на личный адрес.

## Новый UI (2026-09)

- В шапке появился глобальный поиск (⌘K, placeholder «Search...»), который матчится теми же селекторами, что и фильтр участников. `_pick_search_input` в `services/_common.py` пропускает инпуты внутри nav/header/dialog и берёт фильтр таблицы; если оверлей глобального поиска всё же открылся, `_close_search_overlay` закрывает его по Escape.
- Симптом регресса: скриншот с неотфильтрованной таблицей (пагинация «1-50 of N») и открытым оверлеем «Search Dashboard or Documentation».
