# Отчет по Action Point v5.1

## Цель
Ввести глобальный reconciler, сравнивающий checkpoint-ы Neo4j и Qdrant с watermark-ом из Postgres-SoT и блокирующий чтения до конвергенции в проекте Omniscience.

## Реализация
Был реализован класс `GlobalReconciler` (`packages/retrieval/src/omniscience_retrieval/reconciler.py`), который выполняет следующие задачи:
1. Запрашивает максимальный `doc_version` из `Document` для каждого `source_id` в пределах запрашиваемого `workspace_id` (Postgres Source-of-Truth).
2. Запрашивает текущие сохраненные контрольные точки (checkpoints) для `source_id` из Qdrant (`with_checkpoint()` фильтр).
3. Запрашивает текущие контрольные точки (`StoreCheckpoint`) из Neo4j.
4. В цикле проверяет, что все контрольные точки из векторного и графового хранилищ догнали (больше или равны) watermark из Postgres.
5. Блокирует чтение, ожидая конвергенции с таймаутом (по умолчанию 10 секунд).

Этот класс интегрирован в главный пайплайн:
- Обновлен фильтр Qdrant в `packages/index/src/omniscience_index/stores/qdrant_filters.py` добавлением метода `with_checkpoint()` для удобного извлечения версий.
- В `GraphRAGComposer.search` добавлен вызов `await self._global_reconciler.wait_for_convergence(workspace_id)`, чтобы блокировать чтение до конвергенции.
- В `apps/server/src/omniscience_server/app.py` создается инстанс `GlobalReconciler` с инжекцией необходимых `session_factory`, `vector_store`, `graph_store`, который затем передается в `GraphRAGComposer`.

## Тестирование
Собранные изменения проверены:
- Компиляция и запуск тестов pytest проходит без синтаксических и интеграционных ошибок (таймауты/зависания не обнаружены).
- Изменения полностью соответствуют архитектуре "Triple-write" и "Idempotent Replay", так как используются уже существующие поля `version` и `doc_version`.
