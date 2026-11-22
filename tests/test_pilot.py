"""核对复盘口径、分配计划与下一轮可交付名单的规则。"""
import json
import unittest
from collections import Counter
from datetime import date
from pathlib import Path

from pilot.analysis import compare
from pilot.data import Tables
from pilot.design import (
    Allocation,
    build_support,
    community_cell,
    contrast_cells,
    draw_allocation,
    allocation_material,
)
from pilot.outcomes import build_outcomes, outcome_kind
from pilot.planning import build_delivery_plan, recheck_unsent
from pilot.report import build_reports

ROOT = Path(__file__).resolve().parents[1]


class DesignTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = Tables(ROOT / "data")

    def test_allocations_respect_groups_budget_and_restrictions(self):
        for round_name in ("A", "B", "C", "D"):
            round_ = self.tables.rounds[round_name]
            allocations = build_support(
                self.tables,
                round_name,
                self.tables.analysis_edges(round_name),
                round_.reference_date.isoformat(),
            ).feasible
            self.assertTrue(allocations)
            for allocation in allocations:
                self.assertEqual(len(allocation.communities), 4)
                self.assertEqual(
                    sum(
                        1
                        for community in allocation.communities
                        if self.tables.group_of(community) == "少办成"
                    ),
                    round_.priority_selected,
                )
                self.assertLessEqual(allocation.letters, round_.max_letters)
                self.assertFalse(
                    allocation.communities & self.tables.restrictions[round_name]
                )

    def test_planned_exposure_not_observed_station(self):
        allocation = Allocation(
            "A", self.tables.assigned_communities("A"), self.tables.letters_for(
                self.tables.assigned_communities("A")
            )
        )
        edges = self.tables.analysis_edges("A")
        cells = {
            community: community_cell(self.tables, "A", allocation, community, edges)
            for community in self.tables.communities
        }
        # C02 的居民可能到过 S2 以外的站，但暴露档只由 C02 是否被抽中决定。
        self.assertEqual(cells["C02"], "own")
        self.assertEqual(cells["C06"], "neither")
        self.assertEqual(cells["C01"], "neighbor")

    def test_structural_opportunities_are_not_assumed(self):
        support = build_support(
            self.tables, "D", self.tables.effective_edges("D", date(2026, 11, 22))
            , "2026-11-22", support_kind="identifiable",
        )
        # C03 在 D 轮被禁止干预，自身获邀必须标为结构上无机会。
        self.assertFalse(support.structural[("C03", "own")])
        self.assertTrue(support.structural[("C03", "neighbor")])
        self.assertIn("禁止", support.opportunity_note("C03", "own"))
        self.assertTrue(support.minimum_opportunity_ok)

    def test_draw_is_deterministic_and_from_identifiable_pool(self):
        edges = self.tables.effective_edges("D")
        support = build_support(
            self.tables, "D", edges, "2026-11-22", support_kind="identifiable"
        )
        material = allocation_material(
            self.tables, "D", edges, "2026-11-22", "identifiable"
        )
        first, seed, _ = draw_allocation(support.identifiable, material)
        second, seed_again, _ = draw_allocation(
            list(reversed(support.identifiable)), material
        )
        self.assertEqual(first, second)
        self.assertEqual(seed, seed_again)
        self.assertIn(first, support.identifiable)

    def test_each_delivered_plan_keeps_every_contrast_identifiable(self):
        plan = build_delivery_plan(
            self.tables, "D", plan_date=date(2026, 11, 22),
            outcomes_by_round={"A": build_outcomes(self.tables, "A"),
                               "B": build_outcomes(self.tables, "B")},
        )
        for contrast in self.tables.contrasts["D"]:
            exposed, control = contrast_cells(
                self.tables, "D", plan.allocation, contrast, plan.support.edges
            )
            self.assertTrue(exposed, contrast.label())
            self.assertTrue(control, contrast.label())

    def test_execution_change_removes_edge_for_planning(self):
        self.assertIn(
            ("C03", "C09"), self.tables.registered_edges("D")
        )
        self.assertNotIn(
            ("C03", "C09"), self.tables.effective_edges("D", date(2026, 11, 22))
        )
        # 已寄出的历史轮次仍按原登记关系复盘。
        self.assertEqual(
            self.tables.analysis_edges("C"), self.tables.registered_edges("C")
        )


class OutcomeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = Tables(ROOT / "data")

    def test_awaiting_is_not_counted_as_not_done(self):
        outcomes = build_outcomes(self.tables, "A")
        awaiting = [
            outcome
            for outcome in outcomes.residents.values()
            if self.tables.coverage[("A", outcome.resident)] == "awaiting"
        ]
        self.assertTrue(awaiting)
        for outcome in awaiting:
            self.assertIsNone(outcome.outcome)
            self.assertEqual(outcome_kind(outcome), "unknown")

    def test_confirmed_none_counts_as_observed_zero(self):
        outcomes = build_outcomes(self.tables, "A")
        confirmed = [
            outcome
            for outcome in outcomes.residents.values()
            if self.tables.coverage[("A", outcome.resident)] == "confirmed_none"
        ]
        self.assertTrue(confirmed)
        for outcome in confirmed:
            self.assertEqual(outcome.outcome, 0)
            self.assertEqual(outcome_kind(outcome), "not_done")

    def test_prior_acceptance_is_separate_from_round_success(self):
        outcomes = build_outcomes(self.tables, "A")
        prior = [
            outcome for outcome in outcomes.residents.values() if outcome.already_accepted
        ]
        self.assertEqual(len(prior), 3)
        for outcome in prior:
            self.assertIsNone(outcome.outcome)
            self.assertEqual(outcome_kind(outcome), "prior")

    def test_incomplete_round_excludes_late_receipts(self):
        outcomes = build_outcomes(self.tables, "C")
        self.assertFalse(self.tables.rounds["C"].complete)
        self.assertEqual(outcomes.censor_date, date(2026, 9, 15))
        late = [
            outcome
            for outcome in outcomes.residents.values()
            if outcome.future_cases
        ]
        self.assertTrue(late)
        done = sum(1 for outcome in outcomes.residents.values() if outcome.outcome == 1)
        self.assertEqual(done, 19)

    def test_duplicate_cases_are_merged(self):
        outcomes = build_outcomes(self.tables, "A")
        # 同一受理编号只保留一条居民结果，不会因多张回单重复计办成。
        cases = Counter(
            application.case
            for application in self.tables.applications
            if application.round == "A"
        )
        self.assertEqual(sum(cases.values()), len(cases))
        self.assertEqual(len(outcomes.residents), 108)


class AnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = Tables(ROOT / "data")

    def _compare(self, round_name, contrast):
        outcomes = build_outcomes(self.tables, round_name)
        allocation = Allocation(
            round_name,
            self.tables.assigned_communities(round_name),
            self.tables.letters_for(self.tables.assigned_communities(round_name)),
        )
        support = build_support(
            self.tables,
            round_name,
            self.tables.analysis_edges(round_name),
            self.tables.rounds[round_name].reference_date.isoformat(),
        )
        return compare(
            self.tables,
            outcomes,
            support,
            round_name,
            allocation,
            contrast,
            self.tables.analysis_edges(round_name),
        )

    def test_round_a_own_contrast_reports_controls_and_counts(self):
        contrast = next(
            item
            for item in self.tables.contrasts["A"]
            if item.group == "少办成" and item.exposed == "own"
        )
        result = self._compare("A", contrast)
        self.assertTrue(result.identifiable)
        self.assertEqual(result.exposed.communities, ["C02", "C04"])
        self.assertEqual(result.control.communities, ["C06"])
        self.assertEqual(result.exposed.eligible, 18)
        self.assertEqual(result.control.eligible, 8)
        self.assertAlmostEqual(result.resident_rd, 0.1587301587301587)
        self.assertEqual(result.ri_total, 150)
        self.assertEqual(result.ri_defined, 135)
        self.assertAlmostEqual(result.ipw_rd, result.resident_rd)

    def test_round_c_is_progress_only(self):
        outcomes = build_outcomes(self.tables, "C")
        self.assertFalse(outcomes.complete)
        contrast = self.tables.contrasts["C"][0]
        allocation = Allocation(
            "C",
            self.tables.assigned_communities("C"),
            self.tables.letters_for(self.tables.assigned_communities("C")),
        )
        exposed, control = contrast_cells(
            self.tables, "C", allocation, contrast, self.tables.analysis_edges("C")
        )
        # C 轮的实际分配没有 neither 对照，任何效果估计都不能成立。
        self.assertEqual(control, [])
        support = build_support(
            self.tables, "C", self.tables.analysis_edges("C"), "2026-09-15",
            support_kind="identifiable",
        )
        self.assertIsNone(
            compare(
                self.tables, outcomes, support, "C", allocation, contrast,
                self.tables.analysis_edges("C"),
            ).resident_rd
        )

    def test_cross_station_success_stays_with_original_community(self):
        from pilot.analysis import cross_station_summary

        summary = cross_station_summary(self.tables, "A")
        self.assertGreater(summary["cross_station_done"], 0)
        # 跨站办妥不会改变居民在 roster 中的社区归属。
        home = {entry.resident: entry.community for entry in self.tables.roster["A"]}
        cross = [
            application
            for application in self.tables.applications
            if application.round == "A"
            and application.status == "done"
            and application.station
            != self.tables.communities[home[application.resident]].station
        ]
        self.assertEqual(len(cross), summary["cross_station_done"])


class ReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = Tables(ROOT / "data")

    def test_reports_cover_every_round(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            evidence = build_reports(self.tables, Path(directory))
            self.assertEqual(set(evidence["rounds"]), {"A", "B", "C", "D"})
            self.assertEqual(evidence["rounds"]["C"]["status"], "progress_only")
            self.assertEqual(evidence["rounds"]["D"]["status"], "planned")
            self.assertEqual(
                evidence["rounds"]["A"]["comparisons"][0]["status"], "estimated"
            )
            for name in ("summary.md", "evidence.json", "allocation_D.csv"):
                self.assertTrue((Path(directory) / name).exists(), name)
            payload = json.loads((Path(directory) / "evidence.json").read_text())
            self.assertEqual(payload["rounds"]["D"]["allocation"]["communities"],
                             evidence["rounds"]["D"]["allocation"]["communities"])

    def test_unsent_batch_is_rechecked_not_redrawn(self):
        result = recheck_unsent(self.tables, "C", date(2026, 11, 22))
        self.assertEqual(result["sent_communities"], ["C02", "C08", "C11"])
        self.assertEqual(result["unsent_communities"], ["C05"])
        self.assertIn("C05+C10", result["removed_edges"])
        self.assertFalse(result["identifiable_under_effective"])


if __name__ == "__main__":
    unittest.main()
