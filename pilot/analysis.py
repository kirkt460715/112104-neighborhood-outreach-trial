"""轮次复盘：按预先限定的分配做暴露分档、随机化推断与进度报告。"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from .data import Contrast, Tables
from .design import (
    Allocation,
    DesignSupport,
    community_cell,
    contrast_cells,
    missing_conditions,
)
from .outcomes import DONE, NONE, PENDING, RoundOutcomes

ESTIMATED = "estimated"
NOT_IDENTIFIABLE = "not_identifiable"
PROGRESS_ONLY = "progress_only"


@dataclass
class CellSummary:
    cell: str
    communities: list[str]
    eligible: int
    prior: int
    known: int
    done: int
    pending: int
    confirmed_none: int
    awaiting: int
    future: int

    @property
    def rate(self) -> float | None:
        return self.done / self.known if self.known else None


@dataclass
class CommunityRate:
    community: str
    eligible: int
    known: int
    done: int

    @property
    def rate(self) -> float | None:
        return self.done / self.known if self.known else None


@dataclass
class ComparisonResult:
    contrast: Contrast
    exposed: CellSummary
    control: CellSummary
    community_exposed: list[CommunityRate]
    community_control: list[CommunityRate]
    status: str
    identifiable: bool = True
    missing: list[str] = field(default_factory=list)
    resident_rd: float | None = None
    community_rd: float | None = None
    ipw_rd: float | None = None
    ri_p_resident: float | None = None
    ri_p_community: float | None = None
    ri_p_ipw: float | None = None
    ri_defined: int = 0
    ri_total: int = 0
    bounds: tuple[float, float] | None = None
    note: str = ""

    @property
    def label(self) -> str:
        return self.contrast.label()


def summarise_cell(
    tables: Tables,
    outcomes: RoundOutcomes,
    round_name: str,
    communities: Iterable[str],
    cell: str,
) -> CellSummary:
    members = set(communities)
    entries = [entry for entry in tables.roster[round_name] if entry.community in members]
    known = done = pending = confirmed_none = awaiting = future = 0
    prior = 0
    for entry in entries:
        if entry.already_accepted:
            prior += 1
            continue
        outcome = outcomes.residents[entry.resident]
        future += outcome.future_cases
        if outcome.observed and outcome.outcome is not None:
            known += 1
            if outcome.case_status == DONE:
                done += 1
            elif outcome.case_status == PENDING:
                pending += 1
            elif outcome.case_status == NONE:
                confirmed_none += 1
        else:
            awaiting += 1
    return CellSummary(
        cell=cell,
        communities=sorted(members),
        eligible=known + awaiting,
        prior=prior,
        known=known,
        done=done,
        pending=pending,
        confirmed_none=confirmed_none,
        awaiting=awaiting,
        future=future,
    )


def community_rates(
    tables: Tables,
    outcomes: RoundOutcomes,
    round_name: str,
    communities: Iterable[str],
) -> list[CommunityRate]:
    result = []
    for community in sorted(communities):
        entries = [
            entry
            for entry in tables.roster[round_name]
            if entry.community == community and not entry.already_accepted
        ]
        known = done = 0
        for entry in entries:
            outcome = outcomes.residents[entry.resident]
            if outcome.observed and outcome.outcome is not None:
                known += 1
                done += outcome.outcome
        result.append(CommunityRate(community, len(entries), known, done))
    return result


def resident_statistic(
    tables: Tables,
    outcomes: RoundOutcomes,
    round_name: str,
    allocation: Allocation,
    contrast: Contrast,
    edges: set[tuple[str, str]],
):
    exposed, control = contrast_cells(tables, round_name, allocation, contrast, edges)
    if not exposed or not control:
        return None
    exposed_entries = [
        entry
        for entry in tables.roster[round_name]
        if entry.community in exposed and not entry.already_accepted
    ]
    control_entries = [
        entry
        for entry in tables.roster[round_name]
        if entry.community in control and not entry.already_accepted
    ]
    exposed_known = [
        outcomes.residents[entry.resident].outcome
        for entry in exposed_entries
        if outcomes.residents[entry.resident].outcome is not None
    ]
    control_known = [
        outcomes.residents[entry.resident].outcome
        for entry in control_entries
        if outcomes.residents[entry.resident].outcome is not None
    ]
    if not exposed_known or not control_known:
        return None
    return sum(exposed_known) / len(exposed_known) - sum(control_known) / len(control_known)


def community_statistic(
    tables: Tables,
    outcomes: RoundOutcomes,
    round_name: str,
    allocation: Allocation,
    contrast: Contrast,
    edges: set[tuple[str, str]],
):
    exposed, control = contrast_cells(tables, round_name, allocation, contrast, edges)
    if not exposed or not control:
        return None

    def cell_rates(communities):
        rates = []
        for community in communities:
            entries = [
                entry
                for entry in tables.roster[round_name]
                if entry.community == community and not entry.already_accepted
            ]
            known = [
                outcomes.residents[entry.resident].outcome
                for entry in entries
                if outcomes.residents[entry.resident].outcome is not None
            ]
            if known:
                rates.append(sum(known) / len(known))
        return rates

    exposed_rates = cell_rates(exposed)
    control_rates = cell_rates(control)
    if not exposed_rates or not control_rates:
        return None
    return sum(exposed_rates) / len(exposed_rates) - sum(control_rates) / len(control_rates)


def _cell_probabilities(
    tables: Tables,
    round_name: str,
    contrast: Contrast,
    edges: set[tuple[str, str]],
    support: DesignSupport,
) -> dict[tuple[str, str], float]:
    """各社区落入该比较相关档的设计概率（来自该轮支持名单）。"""
    result = {}
    for cell in (contrast.exposed, contrast.control):
        for community in tables.communities_in(contrast.group):
            result[(community, cell)] = support.probability(community, cell)
    return result


def ipw_statistic(
    tables: Tables,
    outcomes: RoundOutcomes,
    round_name: str,
    allocation: Allocation,
    contrast: Contrast,
    edges: set[tuple[str, str]],
    support: DesignSupport,
):
    """用设计抽取概率倒数加权的社区层风险差。

    直接把“分配概率不同”的社区等权相加，会把容易获邀的社区与难以获邀
    的社区混在一起；这里按社区落入该档的设计概率加权，使比较对应到
    “若全体社区都有机会时”的暴露组与对照组。
    """
    probabilities = _cell_probabilities(tables, round_name, contrast, edges, support)
    exposed, control = contrast_cells(tables, round_name, allocation, contrast, edges)

    def weighted(communities, cell):
        total_weight = 0.0
        total_value = 0.0
        for community in communities:
            probability = probabilities.get((community, cell), 0.0)
            if probability <= 1e-12:
                continue
            entries = [
                entry
                for entry in tables.roster[round_name]
                if entry.community == community and not entry.already_accepted
            ]
            known = [
                outcomes.residents[entry.resident].outcome
                for entry in entries
                if outcomes.residents[entry.resident].outcome is not None
            ]
            if not known:
                continue
            weight = (len(known) / probability) if probability else 0.0
            total_weight += weight
            total_value += weight * (sum(known) / len(known))
        if total_weight <= 0:
            return None
        return total_value / total_weight

    exposed_value = weighted(exposed, contrast.exposed)
    control_value = weighted(control, contrast.control)
    if exposed_value is None or control_value is None:
        return None
    return exposed_value - control_value


def _ri_p_value(observed: float | None, values: list[float]) -> tuple[float | None, int]:
    if observed is None or not values:
        return None, 0
    tolerance = 1e-12
    extreme = sum(1 for value in values if abs(value) >= abs(observed) - tolerance)
    return extreme / len(values), len(values)


def _bounds(
    exposed: CellSummary, control: CellSummary
) -> tuple[float, float] | None:
    if not exposed.known or not control.known:
        return None
    expose_low = exposed.done / (exposed.known + exposed.awaiting) if exposed.known + exposed.awaiting else None
    expose_high = (
        (exposed.done + exposed.awaiting) / (exposed.known + exposed.awaiting)
        if exposed.known + exposed.awaiting
        else None
    )
    control_low = control.done / (control.known + control.awaiting) if control.known + control.awaiting else None
    control_high = (
        (control.done + control.awaiting) / (control.known + control.awaiting)
        if control.known + control.awaiting
        else None
    )
    if None in (expose_low, expose_high, control_low, control_high):
        return None
    return (
        max(-1.0, expose_low - control_high),
        min(1.0, expose_high - control_low),
    )


def compare(
    tables: Tables,
    outcomes: RoundOutcomes,
    support: DesignSupport,
    round_name: str,
    allocation: Allocation,
    contrast: Contrast,
    edges: set[tuple[str, str]],
    *,
    randomisation_allocations: list[Allocation] | None = None,
) -> ComparisonResult:
    exposed_communities, control_communities = contrast_cells(
        tables, round_name, allocation, contrast, edges
    )
    exposed = summarise_cell(tables, outcomes, round_name, exposed_communities, contrast.exposed)
    control = summarise_cell(tables, outcomes, round_name, control_communities, contrast.control)
    missing = missing_conditions(tables, round_name, allocation, contrast, edges)
    result = ComparisonResult(
        contrast=contrast,
        exposed=exposed,
        control=control,
        community_exposed=community_rates(tables, outcomes, round_name, exposed_communities),
        community_control=community_rates(tables, outcomes, round_name, control_communities),
        status=NOT_IDENTIFIABLE if missing else ESTIMATED,
        missing=missing,
    )
    if missing:
        return result

    result.resident_rd = (
        exposed.rate - control.rate
        if exposed.rate is not None and control.rate is not None
        else None
    )
    exposed_rates = [rate.rate for rate in result.community_exposed if rate.rate is not None]
    control_rates = [rate.rate for rate in result.community_control if rate.rate is not None]
    result.community_rd = (
        sum(exposed_rates) / len(exposed_rates) - sum(control_rates) / len(control_rates)
        if exposed_rates and control_rates
        else None
    )
    result.ipw_rd = ipw_statistic(
        tables, outcomes, round_name, allocation, contrast, edges, support
    )
    result.bounds = _bounds(exposed, control)

    pool = randomisation_allocations if randomisation_allocations is not None else support.feasible
    resident_values = []
    community_values = []
    ipw_values = []
    for candidate in pool:
        resident_value = resident_statistic(
            tables, outcomes, round_name, candidate, contrast, edges
        )
        if resident_value is not None:
            resident_values.append(resident_value)
        community_value = community_statistic(
            tables, outcomes, round_name, candidate, contrast, edges
        )
        if community_value is not None:
            community_values.append(community_value)
        ipw_value = ipw_statistic(
            tables, outcomes, round_name, candidate, contrast, edges, support
        )
        if ipw_value is not None:
            ipw_values.append(ipw_value)
    result.ri_total = len(pool)
    result.ri_defined = len(resident_values)
    result.ri_p_resident, _ = _ri_p_value(result.resident_rd, resident_values)
    result.ri_p_community, _ = _ri_p_value(result.community_rd, community_values)
    result.ri_p_ipw, _ = _ri_p_value(result.ipw_rd, ipw_values)
    if len(exposed.communities) < 2 or len(control.communities) < 2:
        result.note = "至少一档只有 1 个社区，社区层变异无法与单社区抽样误差分开"
    return result


def cell_table(
    tables: Tables,
    outcomes: RoundOutcomes,
    round_name: str,
    allocation: Allocation,
    edges: set[tuple[str, str]],
) -> list[dict[str, object]]:
    rows = []
    for group in sorted(tables.groups):
        for cell in ("own", "neighbor", "neither"):
            communities = [
                community
                for community in tables.communities_in(group)
                if community_cell(tables, round_name, allocation, community, edges) == cell
            ]
            if not communities:
                continue
            summary = summarise_cell(tables, outcomes, round_name, communities, cell)
            rows.append(
                {
                    "group": group,
                    "cell": cell,
                    "communities": communities,
                    "communities_n": len(communities),
                    "eligible": summary.eligible,
                    "prior": summary.prior,
                    "known": summary.known,
                    "done": summary.done,
                    "pending": summary.pending,
                    "confirmed_none": summary.confirmed_none,
                    "awaiting": summary.awaiting,
                    "future": summary.future,
                    "rate": summary.rate,
                }
            )
    return rows


def cross_station_summary(tables: Tables, round_name: str) -> dict[str, int]:
    """跨站办妥仍按锁定时的原社区归属统计。"""
    home = {entry.resident: entry.community for entry in tables.roster[round_name]}
    cross = sum(
        1
        for application in tables.applications
        if application.round == round_name
        and application.status == DONE
        and application.resident in home
        and application.station != tables.communities[home[application.resident]].station
    )
    total_done = sum(
        1
        for application in tables.applications
        if application.round == round_name and application.status == DONE
    )
    return {"cross_station_done": cross, "all_done_receipts": total_done}


def duplicate_case_summary(tables: Tables, round_name: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for application in tables.applications:
        if application.round == round_name:
            counts[application.case] += 1
    duplicated = {case: count for case, count in counts.items() if count > 1}
    return {
        "cases": len(counts),
        "duplicate_cases": len(duplicated),
        "extra_receipts": sum(count - 1 for count in duplicated.values()),
    }
