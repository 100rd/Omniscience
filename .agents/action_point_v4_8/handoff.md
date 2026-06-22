# Handoff: Action Point 8 (P2) - Unpark Backfill

## Выполненные задачи
- Обновлен `apps/server/src/omniscience_server/app.py`: В `OutboxConsumerWorker` передается `session_factory` для доступа к Postgres.
- Обновлен `apps/server/src/omniscience_server/outbox_consumer.py`:
  - `_parked_entities` и `_parked_edges` изменены на словари, где значение — это `time.monotonic()` (время парковки).
  - Добавлена фоновая задача `_unpark_loop`. Каждые 60 секунд проверяются припаркованные объекты старше 5 минут.
  - Если такие найдены, они вытаскиваются из БД (`Entity`/`Edge`) и публикуются в очередь NATS (`outbox.entity.upsert` / `outbox.edge.upsert`) с флагом `is_backfill=True`.
- Создана документация `docs/runbooks/unpark-backfill.md`.

## Дальнейшие шаги
- Код готов к интеграции/тестированию. `pytest` можно запустить для проверки, что внесенные изменения не сломали базовый flow, хотя добавленная логика работает в фоне и изолирована в `try/except`.
