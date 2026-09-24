# ChatGPT Team invite

Плагин: `services/svc_chatgpt.py`, только `invite()`, деактивации нет.

## Env

| Ключ | Значение |
|---|---|
| `SERVICE_CHATGPT_URL` | `https://chatgpt.com/admin/members` |
| `SERVICE_CHATGPT_AUTH` | `session` |

Пароль в плагин не передаётся. Вход только через сохранённую сессию persistent-профиля `.browser_profiles/chatgpt`. В Vault есть `SERVICE_CHATGPT_LOGIN/PASSWORD`, но они нужны оператору для ручного входа, не плагину.

## Доменный guard

Приглашаются **только** адреса `@fluytstudio.net`. Любой другой домен → `skipped: not-fluytstudio-domain`, в Asana уходит «Действий не выполнено», задача не закрывается. Это правило воркспейса, а не баг: воркспейс ChatGPT Team скоуплен на подрядчиков fluyt. Не переписывай адрес на `.ff@playrix.com`, здесь ремапа нет намеренно.

## Флоу

`/admin/members` → ждём admin shell → кнопка Invite → email → Send.

## Протухшая сессия

Статус `needs-login: …`. Открыть окно и войти руками, cookies сохранятся:
```bash
python3 prime_session.py chatgpt --url https://chatgpt.com/admin/members
```
Окно ждёт 15 минут (`--wait` меняет). Затем перезапустить grant.py.

## Заголовок в Asana

«Приглашение в ChatGPT Team отправлено». Ключи `CHATGPT` и `CHATGPT_TEAM` оба замаплены.
