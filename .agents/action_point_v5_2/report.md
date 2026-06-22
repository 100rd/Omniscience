# Action Point 2: Implementation of Epoch/Repair-Token and Forced-Replay Mechanism

## Анализ (Analysis)
Требовалось добавить механизм `epoch` (или repair-токен) и `forced-replay`, чтобы при необходимости обходить `version-gate` при откатах проекции (когда версия данных может быть меньше или равна текущей сохраненной, но мы всё равно хотим перезаписать данные).

Были проанализированы следующие компоненты:
1. `packages/core/src/omniscience_core/storage/vector.py` - протокол для векторного хранилища.
2. `packages/core/src/omniscience_core/storage/graph.py` - протокол для графового хранилища и модели запросов.
3. `packages/index/src/omniscience_index/stores/qdrant_store.py` - адаптер Qdrant, хранящий версию в `StoreCheckpoint`.
4. `packages/index/src/omniscience_index/stores/neo4j/store.py` - адаптер Neo4j, где в Cypher-запросах проверялась `existing_version >= version`.
5. `packages/index/src/omniscience_index/writer.py` - фасад, оркестрирующий запись документа и графа.

## Реализация (Implementation)
1. **Протоколы**:
   В `vector.py` и `graph.py` добавлены опциональные аргументы `epoch: int | None = None` и `forced_replay: bool = False` в методы `upsert_chunks`, `upsert_graph`, `upsert_entity`, `upsert_edge` и соответствующие DTO (`EntityUpsert`, `EdgeUpsert`).
2. **Qdrant**:
   - В `upsert_chunks` добавлено чтение `epoch` из payload точки контрольной суммы.
   - Механизм `version-gate` (проверка `existing_version >= version`) теперь обходится (skip = False), если переданный `epoch` строго больше `existing_epoch` ИЛИ если установлен флаг `forced_replay=True`.
   - В `_write_document` при создании/обновлении контрольной точки теперь сохраняется поле `epoch`.
3. **Neo4j**:
   - В Cypher-запросах для `upsert_graph`, `upsert_entity` и `upsert_edge` теперь из `StoreCheckpoint` возвращается не только `version`, но и `epoch`.
   - Логика на стороне Python обновлена для учета `epoch` и `forced_replay`, аналогично Qdrant. При обновлении узла `StoreCheckpoint` сохраняется переданное значение `epoch`.
4. **IndexWriter**:
   - Методы `upsert_document` и `upsert_graph` расширены для принятия `epoch` и `forced_replay` и их проброса в нижележащие хранилища.

## Тесты (Tests)
Был написан тестовый сценарий `tests/test_epoch_forced_replay.py`, который:
1. Записывает сущность с версией 10 и эпохой 1.
2. Пытается записать ту же сущность с версией 5 и эпохой 1 (ожидается срабатывание version-gate).
3. Пишет версию 5, но с эпохой 2 (проходит успешно).
4. Пишет версию 4, эпоха 2 (снова срабатывает version-gate).
5. Пишет версию 3 с `forced_replay=True` (обходит version-gate и обновляет контрольную точку).

Тесты запущены в фоне и проверяют логику обхода ограничений версий.
