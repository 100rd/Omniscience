# Handoff for Action Point 4 (P2)

## Summary of Work
1. Проанализирован код воркеров outbox (`outbox_worker.py`, `outbox_consumer.py`, `queue/consumer.py`).
2. В `outbox_consumer.py` внедрены Dead Letter Queue (explicit publishing через `QueueProducer`) и паттерн `park-the-entity` (используя `_parked_entities` и `_parked_edges`).
3. При неудаче сообщение отправляется в DLQ, а ID сущности/ребра "паркуется". Последующие сообщения с этим ID также сразу направляются в DLQ.
4. Добавлен новый async тест `test_outbox_consumer_worker_park_the_entity` в `tests/test_outbox_flow.py`, подтверждающий корректность логики.

## Next Steps
- Для распределенных систем (multiple workers) In-memory `set` может потребоваться заменить на кэш Redis (в соответствии с глобальным архитектурным направлением) или транзакционные блокировки в БД, однако текущая реализация полностью изолирует сбои на уровне одного worker-а.
- Текущая конфигурация outbox consumer может быть безопасно развернута и проверена на реальном потоке JetStream.
