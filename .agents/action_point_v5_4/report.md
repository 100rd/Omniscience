# Action Point 4 (P1): Confidence Score Calibration Improvements

## Analysis: Beta-Calibration vs. Isotonic Regression

As requested, we evaluated **Beta-Calibration** as a potential alternative to **Isotonic Regression** for calibrating confidence scores in the Omniscience project.

### Isotonic Regression
- **Pros**: Non-parametric, perfectly maps rank-ordered predictions to probabilities, optimal when sufficient data is available.
- **Cons**: Prone to severe overfitting on small datasets. Tends to produce step-wise, jagged mappings with zero-variance bins, causing overconfidence and biased metrics on unseen data.

### Beta-Calibration
- **Pros**: Parametric approach (based on Beta distribution). Provides smooth mappings, which often yield better empirical log-loss and Brier scores on small to medium datasets compared to Isotonic Regression. Naturally handles extreme predictions near 0 and 1 without harsh truncation.
- **Cons**: Assumes the scores are distributed as Beta class-conditionally. If the true underlying distribution is highly irregular or multimodal, it may underfit compared to a non-parametric method.

### Conclusion for Omniscience
For now, we retain Isotonic Regression but mitigate its primary flaw (overfitting/bias in metrics) by adding **Out-of-Fold (OOF) evaluation** and a **minimum sample fallback**. Beta-calibration is a strong candidate for a future v0.3 model if the number of incident labels remains strictly low and we desire a continuous, smooth mapping rather than step functions.

## Implementation Details

We completed the full pipeline:

1. **Out-of-Fold (OOF) Isotonic Evaluation**:
   - `get_out_of_fold_predictions`: Added K-Fold cross-validation (default `k_folds=5`) to produce unbiased calibrated predictions.
   - The reported Brier and ECE scores in `CalibrationPipeline.run()` now use these OOF predictions to prevent artificially inflated calibration metrics caused by evaluating on the training set.

2. **Bootstrap Confidence Intervals (Bootstrap-CI)**:
   - `bootstrap_metrics`: Implemented random resampling with replacement (`num_bootstraps=200`) to compute 95% Confidence Intervals for both Brier Score and Expected Calibration Error (ECE).
   - This provides administrators robust bounds (`brier_ci`, `ece_ci`) reflecting metric variance.

3. **Uncalibrated Fallback (Fixed Prior)**:
   - Introduced `min_samples` (default `50`) to `CalibrationPipeline`.
   - If the number of collected labels is below this threshold, the pipeline automatically switches to `"mode": "uncalibrated"`.
   - The fallback forces `isotonic_thresholds = [0.0, 1.0]` and `isotonic_values = [0.0, 1.0]`, essentially leaving original predictions unchanged (Identity mapping) to avoid fitting a degenerate model on tiny sample sizes.

4. **Tests**:
   - Updated `tests/test_calibration_pipeline.py`.
   - Added unit tests for `get_out_of_fold_predictions` and `bootstrap_metrics`.
   - Added integration tests covering both the `uncalibrated` fallback and `calibrated` full pipeline execution. All tests pass successfully.
