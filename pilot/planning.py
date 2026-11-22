"""下一轮投放计划：在剩余条件下重建可交付名单，并给出补证所需人数与核验方式。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from .data import Contrast, Tables
from .design import (
    Allocation,
    DesignSupport,
    allocation_is_identifiable,
    allocation_material,
    build_support,
    community_cell,
    contrast_cells,
    draw_allocation,
    missing_conditions,
)

NEXT_ROUND_HINT = "下一轮"


@dataclass
class PowerNote:
    contrast: str
    baseline_rate: float | None
    mde_resident: float | None
    mde_community: float | None
    resident_n: int
    community_n: int
    design_effect: float | None
    note: str
    between_community_variance: float | None = None
    community_df: int | None = None


@dataclass
class DeliveryPlan:
    support: DesignSupport
    allocation: Allocation
    seed: str
    canonical: str
    material: dict[str, object]
    contrasts: list[dict[str, object]] = field(default_factory=list)
    power: list[PowerNote] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _two_proportion_mde(rate: float, n_exposed: int, n_control: int, alpha: float = 0.05, power: float = 0.8) -> float | None:
    if n_exposed <= 1 or n_control <= 1 or not (0.0 < rate < 1.0):
        return None
    z_alpha = 1.959963984540054
    z_beta = 0.8416212335729143
    return (z_alpha + z_beta) * math.sqrt(rate * (1 - rate) * (1 / n_exposed + 1 / n_control))


def _between_community_variance(
    tables: Tables, outcomes_by_round: dict
) -> float | None:
    """已查齐轮次中社区办成率的实际方差。

    这是社区层比较的可变性来源；若它小于二项抽样方差，说明社区之间没有
    额外异质性，社区层标准误不应按居民数夸大。
    """
    rates: list[float] = []
    for round_name, outcomes in outcomes_by_round.items():
        if not tables.rounds[round_name].complete:
            continue
        groups: dict[str, list[int]] = {}
        for entry in tables.roster[round_name]:
            if entry.already_accepted:
                continue
            outcome = outcomes.residents[entry.resident]
            if outcome.outcome is not None:
                groups.setdefault(entry.community, []).append(outcome.outcome)
        rates.extend(sum(values) / len(values) for values in groups.values() if values)
    if len(rates) < 2:
        return None
    mean = sum(rates) / len(rates)
    return sum((rate - mean) ** 2 for rate in rates) / (len(rates) - 1)


def _baseline_rate(
    tables: Tables, outcomes_by_round: dict, contrast: Contrast
) -> float | None:
    values = []
    for round_name, outcomes in outcomes_by_round.items():
        if not tables.rounds[round_name].complete:
            continue
        for entry in tables.roster[round_name]:
            if entry.already_accepted:
                continue
            outcome = outcomes.residents[entry.resident]
            if outcome.outcome is not None:
                values.append(outcome.outcome)
    return sum(values) / len(values) if values else None


def build_delivery_plan(
    tables: Tables,
    round_name: str,
    *,
    plan_date: date | None = None,
    outcomes_by_round: dict | None = None,
    support_kind: str = "identifiable",
    cluster_minimum: int = 2,
) -> DeliveryPlan:
    """在计划日重建下一轮可交付名单。

    名单先按组别人数、可投放总量与禁止干预范围筛为“可执行”；再从可执行
    名单中只保留使每个预先约定比较都同时具备暴露组和对照组的名单。单看
    名单可执行、却让某个比较在任一优先人群里失去对照的名单不会交付。

    抽取与随机化推断都在这批“可识别名单”上等概率进行；不另按“每档至少
    两个社区”再筛，否则会把部分社区的结构性暴露机会压到 0，既违反最低
    暴露概率要求，也会让事后推断的条件分布与设计不一致。每档实际有几个
    社区在报告中如实列出。
    """
    plan_date = plan_date or tables.data_cutoff
    edges = tables.effective_edges(round_name, plan_date)
    support = build_support(
        tables, round_name, edges, plan_date.isoformat(),
        support_kind=support_kind, cluster_minimum=cluster_minimum,
    )
    # 交付池就是该设计实际抽取的随机化参照集：使每个预先约定比较都同时
    # 具备暴露与对照的名单。若另按“每档至少两个社区”再筛，会把部分社区
    # 的暴露概率压到 0，破坏最低暴露机会要求，也会让事后推断的条件分布
    # 与设计不符。
    delivery_pool = support.delivery
    if not delivery_pool:
        raise ValueError(f"轮次 {round_name} 没有任何可交付的分配名单")
    support.delivery = list(delivery_pool)

    material = allocation_material(
        tables, round_name, edges, plan_date.isoformat(), support_kind
    )
    allocation, seed, canonical = draw_allocation(support.delivery, material)
    plan = DeliveryPlan(support, allocation, seed, canonical, material)

    for contrast in tables.contrasts[round_name]:
        exposed, control = contrast_cells(
            tables, round_name, allocation, contrast, edges
        )
        n_exposed = sum(
            1
            for entry in tables.roster[round_name]
            if entry.community in exposed and not entry.already_accepted
        )
        n_control = sum(
            1
            for entry in tables.roster[round_name]
            if entry.community in control and not entry.already_accepted
        )
        plan.contrasts.append(
            {
                "comparison": contrast.label(),
                "group": contrast.group,
                "exposed": contrast.exposed,
                "control": contrast.control,
                "exposed_communities": exposed,
                "control_communities": control,
                "exposed_communities_n": len(exposed),
                "control_communities_n": len(control),
                "eligible_exposed": n_exposed,
                "eligible_control": n_control,
                "identifiable": bool(exposed and control),
            }
        )

    outcomes_by_round = outcomes_by_round or {}
    between_variance = _between_community_variance(tables, outcomes_by_round)
    for contrast in tables.contrasts[round_name]:
        baseline = _baseline_rate(tables, outcomes_by_round, contrast)
        exposed, control = contrast_cells(tables, round_name, allocation, contrast, edges)
        n_exposed = sum(
            1
            for entry in tables.roster[round_name]
            if entry.community in exposed and not entry.already_accepted
        )
        n_control = sum(
            1
            for entry in tables.roster[round_name]
            if entry.community in control and not entry.already_accepted
        )
        mde_resident = (
            _two_proportion_mde(baseline, n_exposed, n_control)
            if baseline is not None
            else None
        )
        design_effect = None
        mde_community = None
        community_df = max(len(exposed) + len(control) - 2, 0)
        if between_variance is not None and exposed and control:
            # 社区层比较：以社区为观察单位，用实际社区间方差和 Welch 近似；
            # 不把居民数当独立样本。
            variance_exposed = between_variance / len(exposed)
            variance_control = between_variance / len(control)
            mde_community = (1.959963984540054 + 0.8416212335729143) * math.sqrt(
                variance_exposed + variance_control
            )
            if mde_resident and mde_resident > 0:
                design_effect = (mde_community / mde_resident) ** 2
        note = (
            "社区层可检出差按社区间实际方差估计；居民层按二项近似"
            if between_variance is not None
            else "仅给出居民层近似，社区层方差尚无可用的已查齐轮次"
        )
        plan.power.append(
            PowerNote(
                contrast=contrast.label(),
                baseline_rate=baseline,
                mde_resident=mde_resident,
                mde_community=mde_community,
                resident_n=n_exposed + n_control,
                community_n=len(exposed) + len(control),
                design_effect=design_effect,
                note=note,
                between_community_variance=between_variance,
                community_df=community_df,
            )
        )

    plan.notes.append(
        "名单按设计材料规范化后以 SHA-256 为种子抽取；同一材料重跑得到同一名单"
    )
    plan.notes.append(
        "执行时必须按此名单寄出；寄件失败、错寄或顾问临时借调只记录为执行偏离，"
        "不得据此重新划分试验组"
    )
    for contrast in tables.contrasts[round_name]:
        missing = missing_conditions(tables, round_name, allocation, contrast, edges)
        if missing:
            plan.notes.append(f"{contrast.label()} 仍不可识别：" + "；".join(missing))
    return plan


def prior_round_inventory(tables: Tables) -> list[dict[str, object]]:
    """已有历史发放记录的轮次；这些记录不得因后续重排被抹掉。"""
    rounds = []
    for round_name in sorted(tables.rounds):
        assignments = [
            assignment
            for assignment in tables.assignments.values()
            if assignment.round == round_name
        ]
        if assignments:
            rounds.append(
                {
                    "round": round_name,
                    "assigned": sorted(a.community for a in assignments if a.assigned > 0),
                    "sent": sorted(a.community for a in assignments if a.sent > 0),
                    "assigned_total": sum(a.assigned for a in assignments),
                    "sent_total": sum(a.sent for a in assignments),
                }
            )
    return rounds


def recheck_unsent(tables: Tables, round_name: str, plan_date: date) -> dict[str, object]:
    """检查尚未寄出的批次在借调后是否仍符合设计。

    已寄出的部分按原登记关系复盘，不因重排抹掉；未寄出的部分要用计划日
    仍有效的共享顾问关系重新核对。如果整批在有效关系下已不能同时提供
    各比较的对照，就不能把剩余部分当作原设计的继续执行。
    """
    registered = tables.registered_edges(round_name)
    effective = tables.effective_edges(round_name, plan_date)
    assignments = [
        assignment for assignment in tables.assignments.values() if assignment.round == round_name
    ]
    sent = sorted(a.community for a in assignments if a.sent > 0)
    unsent = sorted(a.community for a in assignments if a.assigned > 0 and a.sent == 0)
    batch = frozenset(sent) | frozenset(unsent)
    allocation = Allocation(round_name, batch, tables.letters_for(batch))

    changed = []
    if batch:
        for community in sorted(tables.communities):
            old = community_cell(tables, round_name, allocation, community, registered)
            new = community_cell(tables, round_name, allocation, community, effective)
            if old != new:
                changed.append(
                    {"community": community, "registered": old, "effective": new}
                )

    identifiable_effective = (
        allocation_is_identifiable(tables, round_name, allocation, effective)
        if batch
        else None
    )
    identifiable_registered = (
        allocation_is_identifiable(tables, round_name, allocation, registered)
        if batch
        else None
    )

    if not unsent:
        status = "no_unsent_batch"
    elif not identifiable_effective:
        status = "batch_no_longer_identifiable"
    elif changed:
        status = "batch_differs"
    else:
        status = "batch_matches"

    return {
        "round": round_name,
        "effective_edges": sorted("+".join(edge) for edge in effective),
        "removed_edges": sorted("+".join(edge) for edge in registered - effective),
        "sent_communities": sent,
        "unsent_communities": unsent,
        "cell_changes": changed,
        "identifiable_under_registered": identifiable_registered,
        "identifiable_under_effective": identifiable_effective,
        "status": status,
        "note": (
            "已寄出批次按原记录复盘，不因后续重新抽取抹掉；未寄出批次需按有效关系重新核对。"
            "若整批在有效关系下失去对照，剩余部分不能当作原设计的继续执行。"
        ),
    }