# Skills Base

Два имени в Asana, одна реализация:
- «Skills Base (Programmers)» → `services/svc_skills_base_programmers.py` (вся логика, `run()`)
- «Skills Base» → `services/svc_skills_base.py`, тонкая обёртка для орги `playrix`

**Орга `playrix` мертва:** «This Skills Base instance has been deleted» → `failed: skills-base-instance-deleted`. Живая только `playrixprogrammers`. Если задача на «Skills Base» без «(Programmers)», предупреди пользователя.

## Env

| Ключ | Значение |
|---|---|
| `SKILLS_BASE_PROGRAMMERS_URL` | `https://app.skills-base.com/o/playrixprogrammers` |
| `SKILLS_BASE_PROGRAMMERS_LOGIN` / `_PASSWORD` | админ |
| `SKILLS_BASE_URL` / `SKILLS_BASE_LOGIN` / `_PASSWORD` | то же для мёртвой орги |
| `SOLVECAPTCHA_API_KEY` | нужен: на логине Cloudflare Turnstile |

## Флоу

Login (Turnstile решается через solvecaptcha, `_maybe_solve_captcha`) → пропуск экрана «настройте MFA» (`_handle_mfa_setup`) → после входа сайт редиректит на хост **app-eu.skills-base.com**, лендинг это профиль админа, не список → плагин сам идёт на `https://app-eu.skills-base.com/people/` (Directories → People) → фильтр → строка → корзина → confirm «Delete».

## Особенности грида

- Legacy Bootstrap 2 + DataTables. Два поиска: `#global-search` (верхний бар, не трогать) и `#peopleSearch` (фильтр грида). Фильтр срабатывает на keyup, поэтому `.fill()` не работает, плагин печатает через `.type(delay)`.
- Пока грид грузится, он показывает «No matching records found». Плагин ждёт стабилизации `.dataTables_info` и только по `filtered from … 0 entries` отдаёт `user-not-found`.
- Оффбординг это **DELETE**, не deactivate: ссылка `a[title="Delete <Name>"][href^="/people/delete/id/"]` в крайней правой колонке, затем модалка с кнопкой «Delete». Результат маппится в `deactivated`.
- Разрушительный шаг реализован по подтверждённой разметке, но на реальном пользователе прогонялся мало. При первом боевом запуске держи браузер headed и смотри.

## Проверка без Asana

```bash
python3 _validate_skills_base.py <email>
```
Headed, только find: подсветит найденную строку.
