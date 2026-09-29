import importlib.util
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_baseline", ROOT / "Scripts" / "eval" / "run_baseline.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class EvalMetricTests(unittest.TestCase):
    def test_unlabeled_results_do_not_claim_acceptance(self):
        cases = [{"id": "c1", "profileId": 1, "photoSha256": "sha", "truth": None}]
        results = [{
            "id": "c1", "profileId": 1, "ok": True, "usable": True,
            "inferredView": "TopCover", "verdicts": [
                {"markKey": "m1", "state": "Matched"}
            ], "counts": {"Matched": 1}, "ms": 10,
        }]
        metrics = MODULE.calc(results, cases, {})
        self.assertIsNone(metrics["viewAccuracy"])
        self.assertIsNone(metrics["falseGreenRate"])
        self.assertFalse(metrics["acceptanceReady"])

    def test_truth_is_joined_by_case_not_global_mark_key(self):
        cases = [
            {"id": "c1", "profileId": 1, "photoSha256": "a", "truth": {"view": "TopCover"}},
            {"id": "c2", "profileId": 1, "photoSha256": "b", "truth": {"view": "Side"}},
        ]
        results = [
            {"id": "c1", "profileId": 1, "ok": True, "usable": True, "inferredView": "TopCover",
             "verdicts": [{"markKey": "same", "state": "Matched"}], "counts": {}, "ms": 10},
            {"id": "c2", "profileId": 1, "ok": True, "usable": True, "inferredView": "Side",
             "verdicts": [{"markKey": "same", "state": "Missing"}], "counts": {}, "ms": 10},
        ]
        human = {
            (1, "a", "same"): "HumanMissing",
            (1, "b", "same"): "HumanPresent",
        }
        metrics = MODULE.calc(results, cases, human)
        self.assertEqual(metrics["viewAccuracy"], 100.0)
        self.assertEqual(metrics["falseGreenRate"], 100.0)
        self.assertEqual(metrics["falseRedRate"], 100.0)
        self.assertTrue(metrics["acceptanceReady"])


if __name__ == "__main__":
    unittest.main()
