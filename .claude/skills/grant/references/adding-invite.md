# Добавить invite() в плагин

`grant.py` ищет функцию `invite` в том же модуле `services/svc_<slug>.py`, что и `deactivate`. Нет функции → в плане `missing-invite`, сервис пропускается.

## Контракт

```python
def invite(context, creds: dict, target_user: str, role: str = "full") -> str
```
Раннер сначала вызывает с `role`, при `TypeError` повторяет без него. `context` это Playwright BrowserContext под stealth-патчем, свой на сервис. `creds` те же, что у deactivate.

## Статусы

Успех и идемпотентность (задача закрывается): `invited`, `invited (<детали>)`, `already-invited`, `already-member`.
Отказ без действий (комментарий «Действий не выполнено», задача открыта): `skipped: <причина>`.
Перезапуск (без комментария): `needs-confirmation: …`, `needs-login: …`, `failed: …`.

## Правила

1. Идемпотентность обязательна: сначала проверить, есть ли пользователь, и вернуть `already-*` без открытия диалогов.
2. Роль неизвестна плагину → `needs-confirmation`, а не молчаливый дефолт, если от роли зависит лицензия (пример: Adobe).
3. Доменные правила (ремап, allow-list) живут в плагине, не в раннере. Комментарий для человека, если адрес поменяли, добавлять через раннер по образцу `FIGMA_REMAP_NOTE`.
4. Переиспользовать хелперы из `services/_common.py`: `get_page`, `session_attr`, `submit_totp`, `is_on_auth_page`.
5. Заголовок для Asana добавить в `GRANT_HEADLINES` в `grant.py` по каноническому env-ключу. Без него пишется «Доступ выдан автоматически».

## Проверка

```bash
python3 -c "import importlib; m=importlib.import_module('services.svc_<slug>'); print(hasattr(m,'invite'))"
python3 grant.py --task <gid> --dry-run
python3 grant.py --task <gid> --yes --no-complete
```
Первый боевой прогон headed и с `--no-complete`, закрыть задачу руками после проверки в сервисе.
