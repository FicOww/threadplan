"""Dataset integrity and technical-score admission, no live model calls."""

import hashlib
import json
from pathlib import Path
import unittest

from threadplan import Intake, UpstreamContext
from scripts.evaluate_simulated_chats import score

ROOT=Path(__file__).resolve().parents[1]/"evals"


class Diversity50Checks(unittest.TestCase):
    def test_unique_aligned_valid_cases_and_preserved_original_components(self):
        manifest=json.loads((ROOT/"diversity50.json").read_text(encoding="utf-8"))
        parts={part:[json.loads(line) for line in (ROOT/manifest[part]).read_text(encoding="utf-8").splitlines()]
               for part in ("inputs","fixtures","expectations")}
        ids=[[row["case_id"] for row in rows] for rows in parts.values()]
        self.assertEqual(ids[0],ids[1]);self.assertEqual(ids[0],ids[2])
        self.assertEqual(len(set(ids[0])),50)
        for row in parts["inputs"]: Intake(row["input"])
        for row in parts["fixtures"]: UpstreamContext(**row["context"])
        for component in manifest["components"]:
            self.assertEqual(hashlib.sha256((ROOT/component["path"]).read_bytes()).hexdigest(),component["sha256"])
        technical=[row for row in parts["expectations"]
                   if row["expected"].get("evaluation_scope")=="technical_failure_contract"]
        self.assertEqual(len(technical),2)

    def test_technical_failures_need_real_failed_read_no_writes_and_exposed_result(self):
        label={"evaluation_scope":"technical_failure_contract","decisions":["FAILED"],
               "operations":[],"required_failed_read":True}
        good={"items":[{"decision":"FAILED"}],"operations":[],"reads":[{"read_status":"FAILED"}]}
        self.assertTrue(score(good,label)["technical_contract_passed"])
        for bad in ({},dict(good,operations=[{"kind":"CREATE"}]),dict(good,reads=[]),
                    dict(good,items=[{"decision":"NO_ACTION"}])):
            self.assertFalse(score(bad,label)["technical_contract_passed"])
        self.assertIn("not_a_semantic",score(good,label)["semantic_review"])


if __name__ == "__main__":
    unittest.main()
