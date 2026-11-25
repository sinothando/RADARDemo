"""Academic-risk models: four-way comparison, time-based validation, prediction and explanations.

Validation rules (from the project's model documentation):
- Train on earlier academic years, test on the newest year.
- Students in the newest year are kept out of training even if they also appear earlier.
- Imputation and scaling are fitted on training data only (inside a Pipeline).
- F1 selects the model; recall, then ROC-AUC, break ties.
"""
from __future__ import annotations

import io
import math
import warnings

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.exceptions import ConvergenceWarning
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier

try:  # design-specified model; falls back to a sklearn model when not installed
    from xgboost import XGBClassifier
except Exception:  # pragma: no cover - depends on the machine
    XGBClassifier = None

try:  # design-specified explanations; occlusion fallback when not installed
    import shap
except Exception:  # pragma: no cover - depends on the machine
    shap = None

# Features follow design §3.2 and §4.2. examination_mark is deliberately excluded: it is
# recorded at the end of the year, so using it would leak the outcome into the predictors.
FEATURES = (
    "gpa",
    "assignment_average",
    "test_average",
    "attendance_percentage",
    "weekly_login_average",
    "assignment_submissions",
    "resource_downloads",
    "discussion_participation",
    "average_session_duration",
)
LABELS = {
    "gpa": "GPA",
    "assignment_average": "Assignment average",
    "test_average": "Test average",
    "attendance_percentage": "Attendance %",
    "weekly_login_average": "Weekly LMS logins",
    "assignment_submissions": "Assignments submitted",
    "resource_downloads": "Resource downloads",
    "discussion_participation": "Forum participation",
    "average_session_duration": "Average session (min)",
}
TERM_WEEKS = 12  # used to derive weekly_login_average from term login frequency
ALGORITHMS = ("Logistic Regression", "Decision Tree", "Random Forest", "XGBoost")
SEED = 42
RISK_MEDIUM_FROM = 0.30
RISK_HIGH_FROM = 0.60


class ModelError(ValueError):
    """Raised when the data cannot support training or prediction. The message is user-facing."""


def xgboost_implementation() -> str:
    return "xgboost" if XGBClassifier is not None else "sklearn HistGradientBoosting (xgboost not installed)"


def risk_level(probability: float) -> str:
    if probability < RISK_MEDIUM_FROM:
        return "Low"
    if probability < RISK_HIGH_FROM:
        return "Medium"
    return "High"


def build_matrix(feature_dicts: list[dict]) -> np.ndarray:
    matrix = []
    for values in feature_dicts:
        row = []
        for name in FEATURES:
            value = values.get(name)
            if value is None:
                row.append(np.nan)  # missing stays missing; the pipeline imputes it
                continue
            number = float(value)
            if math.isnan(number):
                row.append(np.nan)
                continue
            if math.isinf(number):
                raise ModelError(f"Feature {name} must be a finite number.")
            row.append(number)
        matrix.append(row)
    if not matrix:
        raise ModelError("No student rows were supplied.")
    return np.asarray(matrix, dtype=np.float64)


def _pipeline(classifier) -> Pipeline:
    return Pipeline([
        ("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
        ("scaler", StandardScaler()),
        ("classifier", classifier),
    ])


def make_models(positive: int, negative: int) -> dict[str, Pipeline]:
    weight = negative / max(positive, 1)
    if XGBClassifier is not None:
        xgb = XGBClassifier(
            eval_metric="logloss", learning_rate=0.05, max_depth=3, n_estimators=200, n_jobs=1,
            random_state=SEED, scale_pos_weight=weight, subsample=0.85, colsample_bytree=0.85, verbosity=0,
        )
    else:
        xgb = HistGradientBoostingClassifier(
            learning_rate=0.05, max_depth=3, max_iter=200, class_weight="balanced", random_state=SEED,
        )
    return {
        "Logistic Regression": _pipeline(LogisticRegression(class_weight="balanced", max_iter=2000, random_state=SEED)),
        "Decision Tree": _pipeline(DecisionTreeClassifier(
            class_weight="balanced", max_depth=5, min_samples_leaf=2, random_state=SEED)),
        "Random Forest": _pipeline(RandomForestClassifier(
            class_weight="balanced_subsample", max_depth=8, min_samples_leaf=2, n_estimators=250,
            n_jobs=1, random_state=SEED)),
        "XGBoost": _pipeline(xgb),
    }


def _metrics(labels: np.ndarray, probabilities: np.ndarray) -> dict:
    predicted = (probabilities >= 0.5).astype(np.int8)
    return {
        "accuracy": float(accuracy_score(labels, predicted)),
        "balancedAccuracy": float(balanced_accuracy_score(labels, predicted)),
        "precision": float(precision_score(labels, predicted, zero_division=0)),
        "recall": float(recall_score(labels, predicted, zero_division=0)),
        "f1": float(f1_score(labels, predicted, zero_division=0)),
        "rocAuc": float(roc_auc_score(labels, probabilities)),
    }


def train(rows: list[dict]) -> dict:
    """rows: [{"student_id", "academic_year", "label" (0/1), "features": {...}}]"""
    if len(rows) < 8:
        raise ModelError("At least eight labelled annual records are required.")
    for row in rows:
        if row["label"] not in (0, 1):
            raise ModelError("Academic outcome labels must be 0 or 1.")
    years = sorted({row["academic_year"] for row in rows})
    if len(years) < 2:
        raise ModelError("At least two academic years are needed for a time-based holdout.")

    holdout_year = years[-1]
    holdout = [row for row in rows if row["academic_year"] == holdout_year]
    holdout_students = {row["student_id"] for row in holdout}
    training = [
        row for row in rows
        if row["academic_year"] < holdout_year and row["student_id"] not in holdout_students
    ]
    y_train = np.asarray([row["label"] for row in training], dtype=np.int8)
    y_test = np.asarray([row["label"] for row in holdout], dtype=np.int8)
    if set(y_train.tolist()) != {0, 1} or set(y_test.tolist()) != {0, 1}:
        raise ModelError(
            "Both outcomes (successful and unsuccessful) must appear in the earlier training years "
            "and in the newest validation year."
        )

    x_train = build_matrix([row["features"] for row in training])
    x_test = build_matrix([row["features"] for row in holdout])
    counts = np.bincount(y_train, minlength=2)
    results = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        for name, model in make_models(int(counts[1]), int(counts[0])).items():
            model.fit(x_train, y_train)
            probability = model.predict_proba(x_test)[:, 1]
            implementation = xgboost_implementation() if name == "XGBoost" else "scikit-learn"
            results.append({"algorithm": name, "implementation": implementation, **_metrics(y_test, probability)})

    selected = max(
        results,
        key=lambda r: (r["f1"], r["recall"], r["rocAuc"], -ALGORITHMS.index(r["algorithm"])),
    )
    all_x = build_matrix([row["features"] for row in rows])
    all_y = np.asarray([row["label"] for row in rows], dtype=np.int8)
    final_counts = np.bincount(all_y, minlength=2)
    final = make_models(int(final_counts[1]), int(final_counts[0]))[selected["algorithm"]]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        final.fit(all_x, all_y)

    buffer = io.BytesIO()
    joblib.dump({"pipeline": final, "algorithm": selected["algorithm"],
                 "implementation": selected["implementation"], "features": list(FEATURES)},
                buffer, compress=3)
    warnings_out = [
        "Check that predictors were recorded before the official annual outcome was decided.",
    ]
    if len(holdout) < 10:
        warnings_out.append("The validation year has fewer than 10 students, so the metrics are unstable.")
    if XGBClassifier is None:
        warnings_out.append("XGBoost was not installed; the sklearn HistGradientBoosting fallback was used.")
    return {
        "selectedAlgorithm": selected["algorithm"],
        "selectionMetric": "f1",
        "metrics": results,
        "trainingRows": len(training),
        "validationRows": len(holdout),
        "validationAcademicYear": holdout_year,
        "trainingAcademicYears": years[:-1],
        "validationMethod": (
            "Train on earlier academic years and test on the newest year. Students in the newest year "
            "are excluded from training. Imputation and scaling are fitted on training rows only. "
            "Selected by F1, then recall and ROC-AUC."
        ),
        "warnings": warnings_out,
        "artifact": buffer.getvalue(),
    }


def load_bundle(artifact: bytes) -> dict:
    return joblib.load(io.BytesIO(artifact))


def predict_probabilities(bundle: dict, feature_dicts: list[dict]) -> np.ndarray:
    return bundle["pipeline"].predict_proba(build_matrix(feature_dicts))[:, 1]


def _is_tree(classifier) -> bool:
    return isinstance(classifier, (DecisionTreeClassifier, RandomForestClassifier)) or (
        XGBClassifier is not None and isinstance(classifier, XGBClassifier)
    )


def contributions(bundle: dict, features: dict):
    """Return (method, per-feature contributions towards risk, base value or None).

    Positive contribution = pushes the predicted failure probability (or log-odds) up.
    """
    pipeline = bundle["pipeline"]
    imputer = pipeline.named_steps["imputer"]
    scaler = pipeline.named_steps["scaler"]
    classifier = pipeline.named_steps["classifier"]
    matrix = build_matrix([features])

    if isinstance(classifier, LogisticRegression):
        scaled = scaler.transform(imputer.transform(matrix))[0]
        values = classifier.coef_[0] * scaled
        base = float(classifier.intercept_[0])
        method = "Exact linear SHAP for Logistic Regression (coefficient × standardised value, log-odds)"
        return method, values, base

    if shap is not None and _is_tree(classifier):
        scaled = scaler.transform(imputer.transform(matrix))
        raw = shap.TreeExplainer(classifier).shap_values(scaled)
        if isinstance(raw, list):
            raw = raw[1]
        raw = np.asarray(raw)
        if raw.ndim == 3:
            raw = raw[:, :, 1]
        return "SHAP TreeExplainer (class: academic failure)", raw[0], None

    # Fallback: occlusion. Replace one feature with its training median and measure the change.
    base_probability = float(pipeline.predict_proba(matrix)[0, 1])
    values = np.zeros(len(FEATURES))
    medians = imputer.statistics_
    for index in range(len(FEATURES)):
        occluded = matrix.copy()
        occluded[0, index] = medians[index] if np.isfinite(medians[index]) else 0.0
        values[index] = base_probability - float(pipeline.predict_proba(occluded)[0, 1])
    return "Occlusion estimate (each feature replaced by its training median; used when SHAP is unavailable)", values, None


def explain(bundle: dict, features: dict, top_n: int = 5) -> dict:
    method, values, _base = contributions(bundle, features)
    named = {name: float(value) for name, value in zip(FEATURES, values)}
    order = sorted(FEATURES, key=lambda name: abs(named[name]), reverse=True)[:top_n]
    drivers = [
        {
            "feature": LABELS[name],
            "value": features.get(name),
            "contribution": round(named[name], 4),
            "direction": "raises risk" if named[name] > 0 else "lowers risk",
        }
        for name in order
        if abs(named[name]) > 1e-9
    ]
    return {"method": method, "drivers": drivers}
