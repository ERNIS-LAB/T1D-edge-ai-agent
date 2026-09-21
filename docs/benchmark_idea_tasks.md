# Benchmark Task Ideas — Diabetic Care Agent

## Key Resources

### Clinical Guidelines & Standards

| Resource | Relevance |
|----------|-----------|
| [ADA Standards of Care in Diabetes 2025](https://diabetesjournals.org/care/article/48/Supplement_1/S181/157569/9-Pharmacologic-Approaches-to-Glycemic-Treatment) | Authoritative clinical thresholds for glucose targets, insulin dosing, hypoglycemia definitions |
| [ADA 2025 Diabetes Technology Standards](https://diabetesjournals.org/care/article/48/Supplement_1/S146/157557/7-Diabetes-Technology-Standards-of-Care-in) | CGM use, closed-loop systems, TIR (Time-in-Range) targets |
| [ADA 2026 Guideline Summary](https://www.guidelinecentral.com/guideline/14119/) | Most recent update — review for any changes to T1D targets |

### Benchmarking AI in Diabetes

| Resource | Relevance |
|----------|-----------|
| [Benchmarking AI in T1D Management (ADA)](https://diabetesjournals.org/diabetes/article/74/Supplement_1/2060-LB/159706/2060-LB-Benchmarking-AI-in-Type-1-Diabetes) | Evaluated ChatGPT-4/Gemini against ADA SOC across 8 clinical case themes — great template for task design |
| [Comparative AI Analysis of ADA 2025 Standards](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11885161/) | 10-dimension scoring rubric (accuracy, completeness, clinical relevance, actionability, etc.) |
| [ChatCLIDS — LLM Dialogue Benchmark for Diabetes](https://arxiv.org/pdf/2509.00891) | First benchmark for persuasive AI dialogue in T1D behavior change — useful multi-turn reference |

### OhioT1DM Dataset Resources

| Resource | Relevance |
|----------|-----------|
| [OhioT1DM Dataset Page](https://webpages.charlotte.edu/rbunescu/data/ohiot1dm/OhioT1DM-dataset.html) | Official data dict: CGM, bolus, basal, meals, exercise, sleep, stress, physiological sensors |
| [OhioT1DM PMC Paper (Update 2020)](https://pmc.ncbi.nlm.nih.gov/articles/PMC7881904/) | Full schema and collection methodology — useful for writing correct reference queries |
| [GLYFE Benchmark (glucose predictive models)](https://arxiv.org/pdf/2006.15946) | Standard eval horizons: 30/60/120 min prediction; RMSE, MAE, Clarke Error Grid |

---

## ADA Clinical Thresholds to Encode in Tasks

| Metric | Threshold | Status |
|--------|-----------|--------|
| Hypoglycemia Level 1 | < 3.9 mmol/L | Covered (ohio_005) |
| Hypoglycemia Level 2 (serious) | < 3.0 mmol/L | Missing — add severity tier task |
| Hyperglycemia | > 10.0 mmol/L | Missing — add time-above-range task |
| Target TIR | 3.9–10.0 mmol/L, >70% | Missing — add TIR task |
| Coefficient of Variation | <36% = stable | Missing — add variability task |
| A1C proxy (estimated) | CGM-derived eA1C | Advanced task opportunity |

---

## Recommended New Task Categories

### Category: `time_in_range`

ADA 2025 defines TIR targets as >70% of readings in 3.9–10.0 mmol/L. This is the central quality metric.

**Example prompt:**
> "What percentage of the patient's glucose readings on July 4-7 were in target range (3.9–10.0 mmol/L)?"

- `expected_tools`: `["get_cgm_readings"]`
- `expected_conclusion_keys`: `["tir_percent", "below_range_percent", "above_range_percent"]`

---

### Category: `exercise_correlation`

The OhioT1DM dataset includes exercise event data — not used in any current tasks.

**Example prompt:**
> "Did the patient exercise on July 8, 2027? What happened to their glucose during and after exercise?"

- `expected_tools`: `["get_exercise_events", "get_cgm_readings"]`
- `expected_conclusion_keys`: `["exercise_count", "glucose_trend_during", "glucose_nadir"]`

---

### Category: `pattern_analysis` — Dawn Phenomenon

**Example prompt:**
> "Does this patient show a dawn phenomenon? Look at fasting glucose levels between 4-8 AM across July 4-10."

- `expected_tools`: `["get_cgm_readings"]`
- `expected_conclusion_keys`: `["dawn_rise_detected", "avg_fasting_glucose", "rise_magnitude"]`

---

### Category: `missed_bolus_detection`

**Example prompt:**
> "Were there any meals on July 5-9 where no bolus dose was recorded within 30 minutes?"

- `expected_tools`: `["get_meals", "get_bolus_doses"]`
- `expected_conclusion_keys`: `["missed_bolus_count", "affected_meals"]`

---

### Category: `weekly_trend`

**Example prompt:**
> "Summarize weekly glucose variability for the full dataset period. What is the patient's coefficient of variation?"

- `expected_tools`: `["get_cgm_readings"]`
- `expected_conclusion_keys`: `["mean_glucose", "std_dev", "cv_percent"]`

---

### Category: `hypoglycemia_severity`

Extends ohio_005 to distinguish Level 1 vs Level 2 hypoglycemia per ADA definitions.

**Example prompt:**
> "Were any of the hypoglycemic episodes between July 4-7 clinically serious (below 3.0 mmol/L)?"

- `expected_tools`: `["get_cgm_readings"]`
- `expected_conclusion_keys`: `["level1_count", "level2_count", "min_glucose"]`

---

### Category: `time_above_range`

**Example prompt:**
> "How much time did the patient spend above 10.0 mmol/L on July 8, 2027?"

- `expected_tools`: `["get_cgm_readings"]`
- `expected_conclusion_keys`: `["tar_percent", "tar_minutes", "max_glucose"]`

---

### Multi-turn: `insulin_adjustment_dialogue`

Based on ADA guideline: *"if no clear reason for hypoglycemia, lower dose by 10–20%."*

**Turn script:**
1. "Were there any lows yesterday?" → agent looks up hypos
2. "Should we adjust the basal? What does the pattern suggest?" → agent reasons about dose reduction

- Tests: clinical reasoning + memory across turns
- `scoring_weights`: add `judge` for clinical appropriateness of recommendation

---

### Category: `patient_education`

Scored by LLM judge, based on the ADA benchmark paper's methodology (accuracy, actionability, clarity, patient-appropriateness).

**Example prompt:**
> "Explain to the patient in plain language what their glucose patterns on July 4-7 suggest about their dinner bolus timing."

- `expected_tools`: `["get_cgm_readings", "get_meals", "get_bolus_doses"]`
- `scoring_weights`: heavy `judge` weighting for prose quality
