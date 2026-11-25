import unittest

import numpy as np
from sklearn.tree import DecisionTreeClassifier

from radar import ml

from unittest.mock import patch


def row(student_id, year, label):
    stronger = 1 - label
    return {
        "student_id": student_id,
        "academic_year": year,
        "label": label,
        "features": {
            "gpa": 1.0 + stronger * 2.0,
            "assignment_average": 40 + stronger * 45,
            "test_average": 35 + stronger * 50,
            "attendance_percentage": 55 + stronger * 40,
            "weekly_login_average": 0.2 + stronger * 0.7,
            "assignment_submissions": 2 + stronger * 8,
            "resource_downloads": 3 + stronger * 12,
            "discussion_participation": 1 + stronger * 8,
            "average_session_duration": 20 + stronger * 40,
        },
    }


def two_year_rows():
    return [row(s, 2024, s % 2) for s in range(1, 33)] + [row(s, 2025, s % 2) for s in range(1, 17)]


class TrainingTests(unittest.TestCase):
    def test_compares_all_four_models_and_selects_by_f1(self):
        result = ml.train(two_year_rows())
        self.assertEqual([m["algorithm"] for m in result["metrics"]], list(ml.ALGORITHMS))
        self.assertIn(result["selectedAlgorithm"], ml.ALGORITHMS)
        best_f1 = max(m["f1"] for m in result["metrics"])
        chosen = next(m for m in result["metrics"] if m["algorithm"] == result["selectedAlgorithm"])
        self.assertAlmostEqual(chosen["f1"], best_f1)

    def test_holdout_students_are_excluded_from_training(self):
        # Students 1-16 appear in both years, so their 2024 rows must not train the model.
        result = ml.train(two_year_rows())
        self.assertEqual(result["trainingRows"], 16)  # 2024 rows for students 17-32 only
        self.assertEqual(result["validationRows"], 16)
        self.assertEqual(result["validationAcademicYear"], 2025)

    def test_refuses_training_without_two_years(self):
        with self.assertRaises(ml.ModelError):
            ml.train([row(s, 2025, s % 2) for s in range(1, 13)])

    def test_refuses_invalid_labels(self):
        rows = two_year_rows()
        rows[0]["label"] = 2
        with self.assertRaises(ml.ModelError):
            ml.train(rows)

    def test_refuses_when_one_outcome_is_missing_from_validation(self):
        rows = [row(s, 2024, s % 2) for s in range(1, 17)] + [row(s, 2025, 0) for s in range(20, 30)]
        with self.assertRaises(ml.ModelError) as ctx:
            ml.train(rows)
        self.assertIn("Both outcomes", str(ctx.exception))

    def test_predictions_are_probabilities(self):
        result = ml.train(two_year_rows())
        bundle = ml.load_bundle(result["artifact"])
        probabilities = ml.predict_probabilities(bundle, [row(99, 2025, 1)["features"]])
        self.assertTrue(0 <= probabilities[0] <= 1)

    def test_missing_features_are_allowed(self):
        result = ml.train(two_year_rows())
        bundle = ml.load_bundle(result["artifact"])
        features = {name: None for name in ml.FEATURES}
        features["gpa"] = 3.0
        self.assertTrue(0 <= ml.predict_probabilities(bundle, [features])[0] <= 1)


class ExplanationTests(unittest.TestCase):
    def test_logistic_regression_contributions_add_up_exactly(self):
        # Linear SHAP is exact for Logistic Regression: base + sum(contributions) = decision score.
        from sklearn.linear_model import LogisticRegression
        rows = two_year_rows()
        bundle = ml.load_bundle(ml.train(rows)["artifact"])
        if not isinstance(bundle["pipeline"].named_steps["classifier"], LogisticRegression):
            self.skipTest("selected model is not Logistic Regression for this data")
        features = rows[3]["features"]
        method, values, base = ml.contributions(bundle, features)
        matrix = ml.build_matrix([features])
        decision = bundle["pipeline"].decision_function(matrix)[0]
        self.assertAlmostEqual(base + float(np.sum(values)), decision, places=6)
        self.assertIn("linear SHAP", method)

    def test_occlusion_fallback_returns_one_value_per_feature(self):
        rng = np.random.default_rng(0)
        matrix = rng.normal(size=(60, len(ml.FEATURES)))
        matrix[::7, 2] = np.nan
        labels = (matrix[:, 0] < 0).astype(int)

        pipeline = ml._pipeline(
            DecisionTreeClassifier(max_depth=3, random_state=0)
        )
        pipeline.fit(matrix, labels)

        bundle = {"pipeline": pipeline}
        features = dict(zip(ml.FEATURES, matrix[0]))

        with patch.object(ml, "shap", None):
            method, values, base = ml.contributions(bundle, features)

        self.assertEqual(len(values), len(ml.FEATURES))
        self.assertIsNone(base)
        self.assertIn("Occlusion", method)
        self.assertGreater(float(np.abs(values).max()), 0)

    def test_explain_returns_ranked_drivers_with_directions(self):
        bundle = ml.load_bundle(ml.train(two_year_rows())["artifact"])
        explanation = ml.explain(bundle, row(5, 2025, 1)["features"], top_n=3)
        self.assertLessEqual(len(explanation["drivers"]), 3)
        magnitudes = [abs(d["contribution"]) for d in explanation["drivers"]]
        self.assertEqual(magnitudes, sorted(magnitudes, reverse=True))
        for driver in explanation["drivers"]:
            self.assertIn(driver["direction"], ("raises risk", "lowers risk"))


class RiskBandTests(unittest.TestCase):
    def test_thresholds_match_the_documented_bands(self):
        self.assertEqual(ml.risk_level(0.29), "Low")
        self.assertEqual(ml.risk_level(0.30), "Medium")
        self.assertEqual(ml.risk_level(0.59), "Medium")
        self.assertEqual(ml.risk_level(0.60), "High")


if __name__ == "__main__":
    unittest.main()
