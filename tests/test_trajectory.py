# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Regressions for post-hoc per-rollout scoring and the trajectory view over the real recursion."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from harness2.cli import judge as cli_judge
from harness2.cli import report as cli_report
from harness2.core import report, schedule
from harness2.demo import DemoAdapter, DemoProposer

K = 2
TASK = "ledger-1"
# label -> (n_passed, n_criteria); base2 is a judge failure and must stay ungraded.
TALLY = {"base1": (2, 4), "inv1": (1, 4), "dom1": (3, 4), "c1": (3, 4),
         "inv2": (2, 4), "dom2": (4, 4), "c2": (4, 4)}


def _score(run_dir, task_id):
    got = TALLY.get(Path(run_dir).name)
    return None if got is None else {"n_passed": got[0], "n_criteria": got[1]}


class TrajectoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.adapter = DemoAdapter(root)
        schedule.run_task(
            TASK, K, adapter=self.adapter,
            prop=DemoProposer(self.adapter.spec.channels["proposer"]),
            runs_root=self.adapter.spec.runs_root, modes=("seq",),
        )
        self.adapter.score = Mock(side_effect=_score)
        self.ns_dir = self.adapter.spec.runs_root / f"demo-oc-agg{K}"
        self.mode_ns = {"seq": f"demo-oc-seq{K}"}

    def judge(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return cli_judge.judge_rollouts(self.adapter, self.ns_dir, self.mode_ns)

    def test_every_rollout_scored_after_selection_and_resumed(self):
        self.assertEqual(self.judge(), 2)  # one ungraded row -> non-zero exit, as --bases does
        rows = json.loads((self.ns_dir / "rollouts_judged.json").read_text())
        self.assertEqual(len(rows), 4 * K)
        got = {(r["mode"], r["label"]): r["score"] for r in rows}
        self.assertEqual(got[("base", "base2")], None)
        self.assertEqual(got[("seq", "c2")], {"n_passed": 4, "n_criteria": 4})
        self.assertEqual(
            sorted(got), sorted([("base", "base1"), ("base", "base2")]
                                + [("seq", lbl) for lbl in ("inv1", "dom1", "c1",
                                                            "inv2", "dom2", "c2")]))
        self.assertEqual(self.adapter.score.call_count, 4 * K)

        # Resume: graded rows are kept; only the ungraded reference is retried.
        self.adapter.score.reset_mock()
        self.judge()
        self.assertEqual([Path(c.args[0]).name for c in self.adapter.score.call_args_list],
                         ["base2"])

    def test_trajectory_axis_excludes_ungraded_and_html_shows_rounds(self):
        self.judge()
        summary = json.loads((self.ns_dir / TASK / "summary.json").read_text())
        rows = json.loads((self.ns_dir / "rollouts_judged.json").read_text())
        steps = report.trajectory(summary, rows, "seq")
        self.assertEqual([s.label for s in steps],
                         ["base1", "base2", "inv1", "dom1", "c1", "inv2", "dom2", "c2"])
        self.assertEqual([s.round for s in steps], [0, 0, 1, 1, 1, 2, 2, 2])
        self.assertIsNone(steps[1].frac)
        # base2 is ungraded, so the reference mean is base1 alone — never averaged with a 0.
        self.assertEqual(report.spine(steps), [("base", 0.5), ("c1", 0.75), ("c2", 1.0)])
        self.assertEqual(sum(s.selected for s in steps), 1)

        with contextlib.redirect_stdout(io.StringIO()):
            page = cli_report.trajectory_html(
                self.adapter, self.ns_dir,
                {"seq": self.adapter.spec.runs_root / self.mode_ns["seq"]}, "demo",
            )
        self.assertIn("harness diff base → c1", page)
        self.assertIn("harness diff c1 → c2", page)
        self.assertIn("summarize.py", page)
        self.assertIn("ungraded", page)
        notes = next(s.notes for s in steps if s.label == "inv1")
        self.assertTrue(notes)
        self.assertIn(notes.split(":")[0], page)


if __name__ == "__main__":
    unittest.main()
