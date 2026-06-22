# Отчет по Action Point 9 (P3)

## Цель
Добавить alert-правила для метрик lag и parked-entity, интегрировать с SRE runbooks в проекте Omniscience.

## Выполненные задачи

1. **Экспорт метрик (Prometheus)**
   В `omniscience_core/telemetry/metrics.py` добавлены новые метрики для outbox consumer:
   - `omniscience_outbox_parked_total` (Gauge)
   - `omniscience_outbox_event_lag_seconds` (Histogram)

   Также обнаружено, что в коде `outbox_consumer.py` уже были определены `Gauge` метрики для припаркованных сущностей и ребер:
   - `omniscience_outbox_parked_entities_total`
   - `omniscience_outbox_parked_entities_oldest_age_seconds`
   - `omniscience_outbox_parked_edges_total`
   - `omniscience_outbox_parked_edges_oldest_age_seconds`

   В `QueueConsumer` и `Message` добавлена поддержка передачи времени публикации из метаданных NATS, что в будущем упростит точный расчет histogram метрики lag-а событий.

2. **Конфигурации алертов**
   Создан файл с правилами Prometheus: `monitoring/prometheus/alerts/outbox.yaml`.
   В нем добавлены 4 алерта (Warning, P2/P3):
   - `OutboxParkedEntitiesCount` (срабатывает, если есть припаркованные сущности более 5 минут)
   - `OutboxParkedEdgesCount` (срабатывает, если есть припаркованные ребра более 5 минут)
   - `OutboxParkedEntityLagHigh` (срабатывает, если сущность находится в припаркованном состоянии более 1 часа - индикатор поломки anti-entropy reconciler или невозможности обработать событие даже через backfill)
   - `OutboxParkedEdgeLagHigh` (срабатывает, если ребро находится в припаркованном состоянии более 1 часа)

3. **SRE Runbooks**
   Создан runbook `docs/runbooks/outbox.md`, описывающий:
   - Причины срабатывания для каждого из 4 алертов.
   - Диагностические команды (просмотр логов kubernetes и nats stream).
   - Инструкции по эскалации и разрешению инцидентов (в том числе ручной запуск reconciler backfill).
   Все алерты в `outbox.yaml` ссылаются на соответствующие якорные ссылки в `runbook_url`.

## Итог
Пайплайн выполнен: алерты настроены, метрики доступны, SRE runbook написан и корректно привязан.
