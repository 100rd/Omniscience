# Action Point 3: Confidence Score Calibration

## Overview
Successfully integrated a calibration pipeline for `confidence_score` calculations in `omniscience_retrieval/probabilistic_scoring.py` and updated related incident resolution endpoints to correctly handle time decay.

## Changes Made
1. **Added Feedback Endpoint**: 
   - Created `POST /incidents/{alert_id}/feedback` in `omniscience_server/rest/incidents.py` to collect user feedback (`predicted_confidence` and `true_label`) for continuous Isotonic Regression model tuning.
2. **Integrated Isotonic Calibration**: 
   - Applied `calibrate_isotonic()` to the calculated probability inside `calculate_probabilistic_incident_confidence()`.
3. **Fixed Temporal Decay issue**: 
   - Extended `pr_time` matched-window from 2h to 24h as per heuristic specifications.
   - Piped `as_of` context parameter directly through the MCP layers (`resolution.py:compute_confidence` and `incidents.py:mcp_resolve_incident`) so historical incidents (or static test fixtures) don't unfairly suffer extreme time-decay penalties.
4. **Validation**: 
   - Updated `test_incident_demo.py` to evaluate confidence by simulating runtime equivalent to the alert's creation time.
   - Dropped the rigid equality check (`== 0.9`) as requested by the #154 issue to support probabilistic variation while maintaining the functional floor (`>= 0.6`).
   - All tests now pass successfully (11 passed).

## Next Steps
The system is ready for real user feedback collection. This data can subsequently be plugged into the offline `CalibrationPipeline` (`omniscience_retrieval/incidents/calibration.py`) to periodically retrain the isotonic boundaries.
