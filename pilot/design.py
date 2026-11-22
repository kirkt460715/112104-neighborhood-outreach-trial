"""预先限定的社区分配方案：可行名单、可识别支持与暴露机会。"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from itertools import combinations
from typing import Iterable

from .data import Contrast, Tables

OWN = "own"
NEIGHBOR = "neighbor"
NEITHER = "neither"
CELLS = (OWN, NEIGHBOR, NEITHER)
CELL_LABELS = {OWN: "自身获邀", NEIGHBOR: "邻接获邀", NEITHER: "自身及邻接均未获邀"}


@dataclass(frozen=True)
class Allocation:
    """一次社区层面的投放名单。分配单位始终是社区，不是居民或站点。"""

    round: str
    communities: frozenset[str]
    letters: int

    @property
    def key(self) -> tuple[str, ...]:
        return tuple(sorted(self.communities))

    @property
    def label(self) -> str:
        return "+".join(self.key)


def community_cell(
    tables: Tables,
    round_name: str,
    allocation: Allocation,
    community: str,
    edges: set[tuple[str, str]],
) -> str:
    """按计划分配判定社区暴露档，不按居民后来到过哪个站判定。"""
    if community in allocation.communities:
        return OWN
    if tables.adjacency(round_name, community, edges) & set(allocation.communities):
        return NEIGHBOR
    return NEITHER


def contrast_cells(
    tables: Tables,
    round_name: str,
    allocation: Allocation,
    contrast: Contrast,
    edges: set[tuple[str, str]],
) -> tuple[list[str], list[str]]:
    members = tables.communities_in(contrast.group)
    exposed = sorted(c for c in members if community_cell(tables, round_name, allocation, c, edges) == contrast.exposed)
    control = sorted(c for c in members if community_cell(tables, round_name, allocation, c, edges) == contrast.control)
    return exposed, control


def missing_conditions(
    tables: Tables,
    round_name: str,
    allocation: Allocation,
    contrast: Contrast,
    edges: set[tuple[str, str]],
) -> list[str]:
    """说明一个预先约定的比较为什么在该分配下识别不了。"""
    members = tables.communities_in(contrast.group)
    assignment = {
        c: community_cell(tables, round_name, allocation, c, edges) for c in members
    }
    missing: list[str] = []
    if not any(cell == contrast.exposed for cell in assignment.values()):
        candidates = [c for c in members if assignment[c] == contrast.control]
        missing.append(
            f"缺少暴露组“{contrast.exposed}”：{contrast.group}组中没有社区处于该档"
            + (f"（其余对照候选：{', '.join(candidates)}）" if candidates else "")
        )
    if not any(cell == contrast.control for cell in assignment.values()):
        detail = "；".join(
            f"{c}={assignment[c]}" for c in members
        )
        missing.append(
            f"缺少对照组“{contrast.control}”：{contrast.group}组中没有社区同时满足该档"
            f"（逐社区：{detail}）"
        )
    return missing


def allocation_is_identifiable(
    tables: Tables,
    round_name: str,
    allocation: Allocation,
    edges: set[tuple[str, str]],
) -> bool:
    for contrast in tables.contrasts[round_name]:
        exposed, control = contrast_cells(tables, round_name, allocation, contrast, edges)
        if not exposed or not control:
            return False
    return True


def cluster_ready(
    tables: Tables,
    round_name: str,
    allocation: Allocation,
    edges: set[tuple[str, str]],
    minimum: int = 2,
) -> bool:
    """每个被比较的档至少有两个社区，才能观察到社区间变异。"""
    for contrast in tables.contrasts[round_name]:
        exposed, control = contrast_cells(tables, round_name, allocation, contrast, edges)
        if len(exposed) < minimum or len(control) < minimum:
            return False
    return True


def enumerate_allocations(
    tables: Tables,
    round_name: str,
    edges: set[tuple[str, str]] | None = None,
) -> list[Allocation]:
    """按轮次计划枚举所有满足组别人数、预算与禁止干预范围的名单。

    这是资料说明中“从满足预算和限制的所有名单中等概率抽取”的实际分配机制；
    edges 只影响暴露档解释，不影响名单本身是否可执行。
    """
    del edges  # 名单可执行性由组别、预算和禁止范围决定；保留参数以便调用一致。
    round_ = tables.rounds[round_name]
    groups = {name: tables.communities_in(name) for name in tables.groups}
    priority = [name for name in groups if name == "少办成"]
    other = [name for name in groups if name != "少办成"]
    if len(priority) != 1 or len(other) != 1:
        raise ValueError("计划要求恰好两个社区组：少办成 与 一般")
    priority_names = groups[priority[0]]
    other_names = groups[other[0]]
    restricted = tables.restrictions[round_name]

    allocations: list[Allocation] = []
    for chosen_priority in combinations(priority_names, round_.priority_selected):
        for chosen_other in combinations(other_names, round_.other_selected):
            chosen = frozenset(chosen_priority) | frozenset(chosen_other)
            if chosen & restricted:
                continue
            letters = tables.letters_for(chosen)
            if letters > round_.max_letters:
                continue
            allocations.append(Allocation(round_name, chosen, letters))
    allocations.sort(key=lambda allocation: allocation.key)
    return allocations


@dataclass
class DesignSupport:
    """某一轮在给定计划日下的可行、可识别与可交付分配。"""

    round_name: str
    plan_date: str
    edges: set[tuple[str, str]]
    feasible: list[Allocation]
    identifiable: list[Allocation]
    cluster_ready: list[Allocation]
    delivery: list[Allocation]
    support_kind: str = "feasible"
    probabilities: dict[tuple[str, str], float] = field(default_factory=dict)
    structural: dict[tuple[str, str], bool] = field(default_factory=dict)
    structural_reasons: dict[tuple[str, str], str] = field(default_factory=dict)
    min_exposure_prob: float = 0.0

    @property
    def allocations(self) -> list[Allocation]:
        if self.support_kind == "feasible":
            return self.feasible
        if self.support_kind == "identifiable":
            return self.identifiable
        raise ValueError(self.support_kind)

    def probability(self, community: str, cell: str) -> float:
        return self.probabilities.get((community, cell), 0.0)

    def exposure_checks(self) -> list[dict[str, object]]:
        """对结构上可能的暴露机会检查最低概率要求。

        结构上不可能的档（例如禁止干预社区的 own、只与受限社区相邻社区的
        neighbor）不按 0 概率判为设计失败，而是明确标注“无此机会”。
        """
        checks: list[dict[str, object]] = []
        communities = {community for community, _ in self.probabilities}
        for community in sorted(communities):
            for cell in (OWN, NEIGHBOR):
                key = (community, cell)
                if not self.structural.get(key, False):
                    continue
                probability = self.probability(community, cell)
                checks.append(
                    {
                        "community": community,
                        "cell": cell,
                        "probability": probability,
                        "threshold": self.min_exposure_prob,
                        "meets": probability + 1e-12 >= self.min_exposure_prob,
                    }
                )
        return checks

    @property
    def minimum_opportunity_ok(self) -> bool:
        return all(bool(check["meets"]) for check in self.exposure_checks())

    def opportunity_note(self, community: str, cell: str) -> str:
        if self.structural.get((community, cell), False):
            return ""
        return self.structural_reasons.get(
            (community, cell), "该档在此设计下没有可比较机会"
        )


def _structural_reason(
    tables: Tables,
    round_name: str,
    community: str,
    cell: str,
    edges: set[tuple[str, str]],
    feasible: list[Allocation],
) -> str:
    if cell == OWN:
        if community in tables.restrictions[round_name]:
            return "该轮禁止对该社区发额外邀请，结构上不可能自身获邀"
        return "在预算、组别与禁止范围下没有可行名单让该社区自身获邀"
    if cell == NEIGHBOR:
        neighbours = tables.adjacency(round_name, community, edges)
        if not neighbours:
            return "没有登记在案的共享顾问关系，结构上不可能由邻接社区获邀"
        allowed = [n for n in neighbours if n not in tables.restrictions[round_name]]
        if not allowed:
            return "全部邻接社区在该轮被禁止干预，结构上不可能邻接获邀"
        return "在预算、组别与禁止范围下没有可行名单让任一邻接社区获邀"
    return "在所有可行名单中该社区都至少处于自身或邻接获邀档"


def build_support(
    tables: Tables,
    round_name: str,
    edges: set[tuple[str, str]],
    plan_date: str,
    *,
    support_kind: str = "feasible",
    cluster_minimum: int = 2,
) -> DesignSupport:
    feasible = enumerate_allocations(tables, round_name, edges)
    identifiable = [
        allocation
        for allocation in feasible
        if allocation_is_identifiable(tables, round_name, allocation, edges)
    ]
    ready = [
        allocation
        for allocation in identifiable
        if cluster_ready(tables, round_name, allocation, edges, cluster_minimum)
    ]
    delivery = identifiable if support_kind == "identifiable" else feasible
    support = DesignSupport(
        round_name=round_name,
        plan_date=plan_date,
        edges=set(edges),
        feasible=feasible,
        identifiable=identifiable,
        cluster_ready=ready,
        delivery=delivery,
        support_kind=support_kind,
        min_exposure_prob=tables.rounds[round_name].min_exposure_prob,
    )

    support_allocations = support.allocations
    total = len(support_allocations)
    if total == 0:
        raise ValueError(f"轮次 {round_name} 没有可用的分配")
    for community in sorted(tables.communities):
        for cell in CELLS:
            count = sum(
                1
                for allocation in support_allocations
                if community_cell(tables, round_name, allocation, community, edges) == cell
            )
            support.probabilities[(community, cell)] = count / total
            possible = any(
                community_cell(tables, round_name, allocation, community, edges) == cell
                for allocation in feasible
            )
            support.structural[(community, cell)] = possible
            if not possible:
                support.structural_reasons[(community, cell)] = _structural_reason(
                    tables, round_name, community, cell, edges, feasible
                )
    return support


def draw_allocation(
    allocations: Iterable[Allocation],
    material: dict[str, object],
) -> tuple[Allocation, str, str]:
    """用设计材料的 SHA-256 作为公开种子，在可交付名单中等概率抽取。

    同一份设计材料与候选名单会得到同一份名单；名单顺序先规范化，避免
    文件顺序影响结果。
    """
    ordered = sorted(allocations, key=lambda allocation: allocation.key)
    if not ordered:
        raise ValueError("没有可抽取的分配")
    payload = dict(material)
    payload["candidates"] = [allocation.label for allocation in ordered]
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    seed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    rng = random.Random(int(seed[:16], 16))
    return rng.choice(ordered), seed, canonical


def allocation_material(
    tables: Tables,
    round_name: str,
    edges: set[tuple[str, str]],
    plan_date: str,
    support_kind: str,
) -> dict[str, object]:
    round_ = tables.rounds[round_name]
    return {
        "round": round_name,
        "plan_date": plan_date,
        "reference_date": round_.reference_date.isoformat(),
        "observation_days": round_.observation_days,
        "max_letters": round_.max_letters,
        "min_exposure_prob": round_.min_exposure_prob,
        "priority_selected": round_.priority_selected,
        "other_selected": round_.other_selected,
        "restrictions": sorted(tables.restrictions[round_name]),
        "effective_edges": sorted("|".join(edge) for edge in edges),
        "contrasts": [
            [contrast.group, contrast.exposed, contrast.control]
            for contrast in tables.contrasts[round_name]
        ],
        "candidate_rule": support_kind,
    }


def excluded_allocations(
    tables: Tables,
    round_name: str,
    edges: set[tuple[str, str]],
) -> list[dict[str, object]]:
    """可执行、但因至少一个预先约定比较失去对照而不交付的名单。"""
    excluded = []
    for allocation in enumerate_allocations(tables, round_name, edges):
        missing = []
        for contrast in tables.contrasts[round_name]:
            conditions = missing_conditions(
                tables, round_name, allocation, contrast, edges
            )
            if conditions:
                missing.append({"comparison": contrast.label(), "reasons": conditions})
        if missing:
            excluded.append(
                {
                    "communities": sorted(allocation.communities),
                    "letters": allocation.letters,
                    "missing": missing,
                }
            )
    return excluded
