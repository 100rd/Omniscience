# Progress Report: Action Point 1 (P0)

## Goal
Внедрить anti-entropy reconciler, сравнивающий версии в Postgres-SoT, Neo4j и Qdrant, с backfill-механизмом для parked-entities в проекте Omniscience. Цель: закрыть риск R2 (projection skew). 

## Implementation Details
1. **Анализ**: Мы проанализировали текущий `ReconcileWorker`, механизмы `outbox_worker` и `outbox_consumer`. Обнаружили, что `Entity` в Postgres изначально не имел поля `version`, а `outbox_worker` синтетически генерировал версии при отправке в NATS, что делало невозможным сравнение версий между Postgres-SoT и downstream-сторами (Neo4j, Qdrant). А `outbox_consumer` просто сохранял ID сбойных сущностей в in-memory set `_parked_entities`, пропуская их при следующих попытках.

2. **Добавление версионирования в Postgres (SoT)**:
   - Создана миграция `0013_add_entity_version` для добавления колонки `version` в таблицы `entities` и `edges`.
   - Обновлены модели `Entity` и `Edge` в `omniscience_core/db/models.py`.
   - `OtlpIngester` обновлен для инкремента `version` при обновлениях сущностей и передачи `version` в payloads событий.
   - `outbox_worker.py` обновлен, чтобы больше не переопределять `version`, делая Postgres истинным Source of Truth для версий событий.

3. **Обновление Reconciler и Backfill-механизм**:
   - В `QdrantVectorStore` и `Neo4jGraphStore` добавлены методы `get_entity_versions(workspace_id) -> dict[uuid.UUID, int]`, которые возвращают маппинг ID сущности к ее версии.
   - В `ReconcileWorker` добавлен метод `_check_entity_drift`, который сверяет версии `Entity` в Postgres с версиями в Qdrant и Neo4j.
   - Если версия в downstream меньше, чем в PG, или сущность отсутствует (version = 0), Reconciler инициирует backfill путем вставки нового `OutboxEvent` с флагом `is_backfill=True`.

4. **Интеграция с Parked Entities**:
   - Обновлены `EntityUpsertEvent` и `EdgeUpsertEvent` с новым флагом `is_backfill: bool`.
   - `OutboxConsumerWorker` обновлен для обработки `is_backfill=True`: если флаг установлен, он удаляет сущность/грань из `_parked_entities` (`discard`), что позволяет успешно обработать застрявшие/сбойные сущности при повторной синхронизации из Postgres.

5. **Тестирование**:
   - Добавлен юнит-тест `test_check_entity_drift_creates_outbox_events` в `test_reconcile_worker.py`, который мокирует БД и downstream-сторы и проверяет, что генерируются корректные события `OutboxEvent` с флагом `is_backfill=True`.

Все цели Action Point 1 успешно выполнены, риск R2 (projection skew) закрыт.
