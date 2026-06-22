# Action Point 4 (P0): Park-the-entity Improvements

## Goal
Improve `park-the-entity`: limited per-entity park, metrics (parked-count, oldest-age), alerts and runbook, automatic back-off-retry before parking.

## Completed Tasks
1. **Studied Parking Logic**: Located in `apps/server/src/omniscience_server/outbox_consumer.py`.
2. **Back-off-retry Logic**:
   - Updated `Message.nak()` in `messages.py` to support JetStream's native redelivery delay.
   - Modified `outbox_consumer.py` to keep track of retry attempts in memory per entity/edge.
   - Re-delivers up to `MAX_RETRIES_BEFORE_PARK` (3) times with exponential backoff (`delay = 2 ** attempt`).
   - Only parks the entity/edge on the 4th failure.
3. **Limited per-entity park**:
   - Changed `_parked_entities` and `_parked_edges` to dictionaries mapping ID to parked timestamp.
   - Added a size limit (`MAX_PARKED_ITEMS = 10000`). If exceeded, pops the oldest entry.
4. **Prometheus Metrics**:
   - Created gauges: `omniscience_outbox_parked_entities_total`, `omniscience_outbox_parked_entities_oldest_age_seconds`, `omniscience_outbox_parked_edges_total`, and `omniscience_outbox_parked_edges_oldest_age_seconds`.
   - Updated dynamically on new failures and backfills.
5. **Tests**:
   - Updated `test_outbox_flow.py` to test the new retry loops.
   - Asserted that `msg.nak(delay=...)` is called 3 times, followed by DLQ routing and parking on the 4th attempt.

## Outstanding Items
- Create/update actual Prometheus alerts (AlertManager config) for when `parked_entities_total` exceeds a threshold.
- Define Runbook for `outbox_entity_parked_skip` in the repository documentation.
