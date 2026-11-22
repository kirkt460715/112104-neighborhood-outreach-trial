"""生成按轮次的复盘报告、机器可读证据和下一轮发放清单。"""
from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict
from datetime import date
from pathlib import Path

from .analysis import (
    ESTIMATED,
    NOT_IDENTIFIABLE,
    PROGRESS_ONLY,
    ComparisonResult,
    cell_table,
    community_rates,
    compare,
    cross_station_summary,
    duplicate_case_summary,
    summarise_cell,
)
from .data import Tables
from .design import (
    CELL_LABELS,
    NEIGHBOR,
    NEITHER,
    OWN,
    Allocation,
    build_support,
    contrast_cells,
    excluded_allocations,
    missing_conditions,
)
from .outcomes import RoundOutcomes, build_outcomes, outcome_kind
from .planning import (
    DeliveryPlan,
    build_delivery_plan,
    prior_round_inventory,
    recheck_unsent,
)

STATUS_LABELS = {
    ESTIMATED: "可估计（观察窗已走完）",
    NOT_IDENTIFIABLE: "不可识别",
    PROGRESS_ONLY: "仅进度（观察窗未走完）",
}
CELL_ORDER = (OWN, NEIGHBOR, NEITHER)
CELL_ZH = {
    OWN: "自身获邀",
    NEIGHBOR: "邻接获邀",
    NEITHER: "自身及邻接均未获邀",
}


def comparison_label(contrast) -> str:
    return (
        f"{contrast.group}：{CELL_ZH.get(contrast.exposed, contrast.exposed)} "
        f"vs {CELL_ZH.get(contrast.control, contrast.control)}"
    )


def _round_status(tables: Tables, round_name: str) -> str:
    return ESTIMATED if tables.rounds[round_name].complete else PROGRESS_ONLY


def _fmt(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def _fmt_prob(value: float) -> str:
    return f"{value:.3f}"


def _markdown_table(headers: list[str], rows: list[list[object]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(lines)


def _allocation_for_round(tables: Tables, round_name: str) -> Allocation:
    communities = tables.assigned_communities(round_name, "assigned")
    return Allocation(round_name, communities, tables.letters_for(communities))


def _comparison_payload(result: ComparisonResult) -> dict[str, object]:
    return {
        "comparison": result.label,
        "label": comparison_label(result.contrast),
        "group": result.contrast.group,
        "exposed": result.contrast.exposed,
        "control": result.contrast.control,
        "status": result.status,
        "missing": result.missing,
        "exposed_communities": result.exposed.communities,
        "control_communities": result.control.communities,
        "exposed_eligible": result.exposed.eligible,
        "control_eligible": result.control.eligible,
        "exposed_known": result.exposed.known,
        "control_known": result.control.known,
        "exposed_done": result.exposed.done,
        "control_done": result.control.done,
        "exposed_awaiting": result.exposed.awaiting,
        "control_awaiting": result.control.awaiting,
        "exposed_rate": result.exposed.rate,
        "control_rate": result.control.rate,
        "resident_rd": result.resident_rd,
        "community_rd": result.community_rd,
        "ipw_rd": result.ipw_rd,
        "ri_p_resident": result.ri_p_resident,
        "ri_p_community": result.ri_p_community,
        "ri_p_ipw": result.ri_p_ipw,
        "ri_defined": result.ri_defined,
        "ri_total": result.ri_total,
        "bounds": result.bounds,
        "note": result.note,
    }


def review_payload(
    tables: Tables,
    round_name: str,
    outcomes: RoundOutcomes,
    support,
    allocation: Allocation,
    results: list[ComparisonResult],
    icc: float | None,
) -> dict[str, object]:
    round_ = tables.rounds[round_name]
    status = _round_status(tables, round_name)
    deviations = []
    for assignment in sorted(
        (a for a in tables.assignments.values() if a.round == round_name),
        key=lambda a: a.community,
    ):
        if assignment.assigned != assignment.sent:
            deviations.append(
                {
                    "community": assignment.community,
                    "assigned": assignment.assigned,
                    "sent": assignment.sent,
                    "planned_letters": tables.communities[assignment.community].letters,
                    "note": "已抽取但未寄出/寄件失败，属执行偏离",
                }
            )
    opportunities = []
    for community in sorted(tables.communities):
        row = {"community": community, "group": tables.group_of(community)}
        for cell in (OWN, NEIGHBOR):
            row[f"p_{cell}"] = support.probability(community, cell)
            row[f"possible_{cell}"] = support.structural.get((community, cell), False)
            row[f"note_{cell}"] = support.opportunity_note(community, cell)
        opportunities.append(row)
    return {
        "round": round_name,
        "status": status,
        "window_start": round_.window_start.isoformat(),
        "window_end": round_.window_end.isoformat(),
        "observation_days": round_.observation_days,
        "as_of": round_.as_of.isoformat(),
        "censor_date": outcomes.censor_date.isoformat(),
        "complete": round_.complete,
        "max_letters": round_.max_letters,
        "min_exposure_prob": round_.min_exposure_prob,
        "planned_allocation": {
            "communities": sorted(allocation.communities),
            "letters": allocation.letters,
        },
        "support": {
            "kind": support.support_kind,
            "plan_date": support.plan_date,
            "edge_basis": (
                "registered" if tables.round_has_sent_batch(round_name) else "effective"
            ),
            "effective_edges": sorted("+".join(edge) for edge in support.edges),
            "feasible_n": len(support.feasible),
            "identifiable_n": len(support.identifiable),
            "cluster_ready_n": len(support.cluster_ready),
            "delivery_n": len(support.delivery),
            "minimum_opportunity_ok": support.minimum_opportunity_ok,
            "min_exposure_prob": round_.min_exposure_prob,
        },
        "opportunities": opportunities,
        "cells": cell_table(tables, outcomes, round_name, allocation, support.edges),
        "comparisons": [_comparison_payload(result) for result in results],
        "execution": {
            "deviations": deviations,
            "cross_station": cross_station_summary(tables, round_name),
            "duplicates": duplicate_case_summary(tables, round_name),
            "prior_accepted": sum(1 for entry in tables.roster[round_name] if entry.already_accepted),
            "followup": {
                kind: sum(
                    1 for outcome in outcomes.residents.values() if outcome_kind(outcome) == kind
                )
                for kind in ("done", "not_done", "unknown", "prior")
            },
        },
        "community_correlation": icc,
    }


def _render_opportunities(round_name: str, payload: dict[str, object]) -> str:
    del round_name
    rows = []
    for row in payload["opportunities"]:
        notes = []
        for cell in (OWN, NEIGHBOR):
            if not row[f"possible_{cell}"]:
                notes.append(f"{CELL_LABELS[cell]}：{row[f'note_{cell}']}")
        rows.append(
            [
                row["community"],
                row["group"],
                _fmt_prob(row["p_own"]),
                "是" if row["possible_own"] else "否",
                _fmt_prob(row["p_neighbor"]),
                "是" if row["possible_neighbor"] else "否",
                "；".join(notes) if notes else "—",
            ]
        )
    table = _markdown_table(
        ["社区", "组", "P(自身获邀)", "结构可能", "P(邻接获邀)", "结构可能", "无此机会的说明"],
        rows,
    )
    threshold = payload["support"]["min_exposure_prob"]
    ok = payload["support"]["minimum_opportunity_ok"]
    note = (
        f"设计最低暴露概率为 {threshold:.2f}；结构上可能的档均达到要求。"
        if ok
        else f"设计最低暴露概率为 {threshold:.2f}；存在结构上可能却低于要求的档，见上表。"
    )
    basis = "可交付" if payload["support"]["kind"] == "identifiable" else "可行"
    return (
        f"{note} 概率是在该轮全部{basis}名单上等权计算的，"
        "用于说明各社区获得可比较机会的差异。\n\n" + table
    )


def _render_comparisons(payload: dict[str, object]) -> str:
    blocks = []
    for comparison in payload["comparisons"]:
        lines = [f"### {comparison['label']}"]
        status_text = STATUS_LABELS[comparison["status"]]
        if comparison["missing"] and comparison["status"] == PROGRESS_ONLY:
            status_text += "；当前分配还缺少必要对照，窗走完也不能估计这项比较"
        lines.append(f"状态：**{status_text}**")
        if comparison["missing"]:
            lines.append("")
            lines.append("缺少的条件：")
            for item in comparison["missing"]:
                lines.append(f"- {item}")
            if comparison["status"] == NOT_IDENTIFIABLE:
                lines.append("")
                lines.append(
                    "这些条件在当前分配下无法从资料中补足；"
                    "只能通过重新设计分配或另立观察来提供。"
                )
                blocks.append("\n".join(lines))
                continue
        rows = [
            [
                "暴露组",
                ", ".join(comparison["exposed_communities"]) or "—",
                comparison["exposed_eligible"],
                comparison["exposed_known"],
                comparison["exposed_done"],
                _fmt(comparison["exposed_rate"]),
                comparison["exposed_awaiting"],
            ],
            [
                "对照组",
                ", ".join(comparison["control_communities"]) or "—",
                comparison["control_eligible"],
                comparison["control_known"],
                comparison["control_done"],
                _fmt(comparison["control_rate"]),
                comparison["control_awaiting"],
            ],
        ]
        lines.append("")
        lines.append(
            _markdown_table(
                ["档", "社区", "可观察人数", "已查齐", "办成", "办成率", "未查齐"],
                rows,
            )
        )
        lines.append("")
        if comparison["status"] == PROGRESS_ONLY:
            lines.append(
                "观察窗未走完，以上只是当前进度；不计算风险差、p 值或补查边界。"
            )
            blocks.append("\n".join(lines))
            continue
        lines.append(
            f"居民层风险差 {_fmt(comparison['resident_rd'])}；"
            f"社区层风险差 {_fmt(comparison['community_rd'])}；"
            f"按设计概率加权的社区层风险差 {_fmt(comparison['ipw_rd'])}。"
        )
        lines.append(
            f"随机化推断（在全部可行社区分配上重排）p：居民层 "
            f"{_fmt(comparison['ri_p_resident'])}，社区层 "
            f"{_fmt(comparison['ri_p_community'])}，加权社区层 "
            f"{_fmt(comparison['ri_p_ipw'])}；参与重排的名单 "
            f"{comparison['ri_defined']}/{comparison['ri_total']}。"
        )
        if comparison["bounds"]:
            low, high = comparison["bounds"]
            lines.append(
                f"仅就未查齐渠道作极端补查时，风险差的边界为 [{_fmt(low)}, {_fmt(high)}]。"
            )
        if comparison["note"]:
            lines.append(comparison["note"] + "。")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _render_cells(payload: dict[str, object]) -> str:
    rows = []
    for cell in payload["cells"]:
        rows.append(
            [
                cell["group"],
                CELL_LABELS[cell["cell"]],
                ", ".join(cell["communities"]) or "—",
                cell["communities_n"],
                cell["eligible"],
                cell["prior"],
                cell["known"],
                cell["done"],
                cell["pending"],
                cell["confirmed_none"],
                cell["awaiting"],
                cell["future"],
                _fmt(cell["rate"]),
            ]
        )
    return _markdown_table(
        [
            "组", "档", "社区", "社区数", "可观察人数", "轮前已受理",
            "已查齐", "办成", "已受理未办成", "查明无申请", "未查齐", "窗后回单", "办成率",
        ],
        rows,
    )


def _render_execution(payload: dict[str, object]) -> str:
    execution = payload["execution"]
    lines = []
    lines.append(
        f"轮前已受理：{execution['prior_accepted']} 人；这些人作为此前状态单列，"
        "不计入本轮办成分子或分母。"
    )
    if execution["deviations"]:
        lines.append("")
        lines.append("执行偏离（只用于解释执行，不用于重新分组）：")
        for item in execution["deviations"]:
            detail = (
                f"（该社区计划 {item['planned_letters']} 封）"
                if item.get("planned_letters")
                else ""
            )
            lines.append(
                f"- {item['community']}：抽取 {item['assigned']}、寄出 {item['sent']}{detail}"
            )
        lines.append(
            "寄件失败、错寄或顾问临时借调都只记录为执行偏离；"
            "社区仍按原抽取名单分组，不能因实际寄出情况改写谁是对照。"
        )
    else:
        lines.append("")
        lines.append("本轮的社区分配与寄出记录一致。")
    cross = execution["cross_station"]
    lines.append(
        f"跨站办妥 {cross['cross_station_done']} 件（全部办妥回单 {cross['all_done_receipts']} 件）；"
        "这些居民仍按轮前锁定的原社区归属统计。"
    )
    duplicate = execution["duplicates"]
    lines.append(
        f"受理编号 {duplicate['cases']} 个，其中重复回单编号 {duplicate['duplicate_cases']} 个；"
        "同一编号只计一次。"
    )
    followup = execution["followup"]
    lines.append(
        f"随访分类：办成 {followup['done']}，已查齐未办成 {followup['not_done']}，"
        f"未查齐 {followup['unknown']}，轮前已受理 {followup['prior']}。"
    )
    if followup["unknown"]:
        lines.append(
            "未查齐（awaiting）只表示还有渠道未回，不能当作未办理；"
            "报告中的已查齐人数与办成率均以已查齐者为分母。"
        )
    return "\n".join(lines)


def _render_review(
    tables: Tables,
    payload: dict[str, object],
    results: list[ComparisonResult],
) -> str:
    round_name = payload["round"]
    lines = [f"# 轮次 {round_name} 复盘", ""]
    status = payload["status"]
    if status == PROGRESS_ONLY:
        lines.append(
            f"> 观察窗 {payload['window_start']} 至 {payload['window_end']} 尚未走完"
            f"（资料快照 {payload['as_of']}）。本轮只报告进度，不提供效果估计。"
        )
    else:
        lines.append(
            f"> 观察窗 {payload['window_start']} 至 {payload['window_end']} 已走完"
            f"（资料快照 {payload['as_of']}）。以下比较按预先约定的社区分配分档。"
        )
    lines.extend(["", "## 设计与分配", ""])
    allocation = payload["planned_allocation"]
    lines.append(
        f"本轮抽取 {len(allocation['communities'])} 个社区："
        f"{', '.join(allocation['communities'])}；计划寄送 {allocation['letters']} 封"
        f"（上限 {payload['max_letters']} 封）；实际寄出情况见“执行与口径”。"
    )
    support = payload["support"]
    basis = "原登记" if support["kind"] == "feasible" else "有效"
    lines.append(
        f"在轮次计划下（{basis}共享顾问关系 {len(support['effective_edges'])} 条），"
        f"满足组别人数、预算和禁止范围的名单 {support['feasible_n']} 个，"
        f"其中每个预先约定比较都同时具备暴露与对照的名单 {support['identifiable_n']} 个，"
        f"每个被比较档至少两个社区的名单 {support['cluster_ready_n']} 个。"
    )
    lines.append(
        "分配单位是社区；居民后来到过哪个站不参与分组。"
    )
    if support["kind"] == "feasible" and support["identifiable_n"] < support["feasible_n"]:
        lines.append(
            f"注意：有 {support['feasible_n'] - support['identifiable_n']} 个可执行名单会让至少一项"
            "预先约定比较失去对照，这些名单不用于交付；本轮的暴露概率仍按全部可执行"
            "名单计算，因为当时的抽取机制就是在这个名单池上进行的。"
        )
    lines.extend(["", "## 暴露机会", "", _render_opportunities(round_name, payload)])
    lines.extend(["", "## 各档观察人数与办成情况", ""])
    if status == PROGRESS_ONLY:
        lines.append(
            "下表仅为当前进度；“未查齐”包含仍在等待的回单渠道，窗后回单未计入。"
        )
        lines.append("")
    lines.append(_render_cells(payload))
    lines.extend(["", "## 预先约定的比较", ""])
    if status == PROGRESS_ONLY:
        lines.append(
            "观察窗未走完前不计算风险差或 p 值，以免把进度当成结论；"
            "下列比较同时标出当前分配是否具备必要对照。"
        )
        lines.append("")
    lines.append(_render_comparisons(payload))
    lines.extend(["", "## 执行与口径", "", _render_execution(payload)])
    lines.extend(["", "## 解释边界", ""])
    icc = payload["community_correlation"]
    if icc is not None:
        lines.append(
            f"用已查齐轮次估计的社区内相关约为 {icc:.3f}。"
            "随机化推断在全部可行社区分配上重排，保留了预先限定的分配机制与各社区"
            "不同的暴露机会；居民层统计量以居民为观察单位，社区层统计量以社区为观察"
            "单位，两者并列以避免把同社区内的相关性当成独立信息。"
        )
    else:
        lines.append(
            "尚无可用的已查齐轮次估计社区内相关；社区层统计量以社区为观察单位。"
        )
    unidentifiable = [r for r in results if not r.identifiable]
    if unidentifiable:
        lines.append(
            "本轮不可识别的比较见上文“缺少的条件”；这里不再重复逐社区明细。"
        )
    if status == PROGRESS_ONLY:
        lines.append(
            "本轮不产生可用于下一轮确认性检验的效果结论；下一轮须使用新的独立证据。"
        )
    else:
        lines.append(
            "这里的风险差是观察性对照，不是把居民随机分组的结果；"
            "可识别性来自社区层面的预先分配机制，解释时仍受名单内社区组合与执行偏离限制。"
        )
    lines.append(
        "窗内已受理但回单未办成记为未办成；尚待回渠道者不进入办成率分母，"
        "也不按 0 处理。"
    )
    return "\n".join(lines) + "\n"


def _render_plan(tables: Tables, plan: DeliveryPlan, payload: dict[str, object]) -> str:
    allocation = plan.allocation
    lines = [f"# 轮次 {allocation.round} 下一轮投放计划", ""]
    lines.append("> 这一轮尚未寄出，本文件是可执行计划，不是效果结论。")
    lines.extend(["", "## 可交付名单", ""])
    lines.append(
        f"按计划日 {plan.support.plan_date} 的可投放量、组别数量、禁止干预范围和"
        f"有效共享顾问关系，可执行名单 {len(plan.support.feasible)} 个；"
        f"其中 {len(plan.support.identifiable)} 个使三项预先约定比较都同时具备暴露与对照。"
    )
    lines.append(
        f"从可交付名单中按设计材料抽取：**{', '.join(sorted(allocation.communities))}**，"
        f"计划寄送 {allocation.letters} 封（上限 {tables.rounds[allocation.round].max_letters} 封）。"
    )
    lines.append(f"抽取种子（SHA-256）：`{plan.seed}`。")
    lines.append(
        "同一份设计材料与候选名单重跑会得到同一名单；若需换名单，必须登记为设计变更，"
        "不能事后从结果反推。"
    )
    lines.extend(["", "## 对应比较与观察人数", ""])
    rows = []
    for item in plan.contrasts:
        rows.append(
            [
                _power_label(f"{item['group']}/{item['exposed']} vs {item['control']}"),
                ", ".join(item["exposed_communities"]) or "—",
                item["eligible_exposed"],
                ", ".join(item["control_communities"]) or "—",
                item["eligible_control"],
                "是" if item["identifiable"] else "否",
            ]
        )
    lines.append(
        _markdown_table(
            ["比较", "暴露社区", "可观察暴露人数", "对照社区", "可观察对照人数", "可识别"],
            rows,
        )
    )
    lines.append("")
    lines.append("这些比较与上面的名单来自同一次有效分配。")
    lines.extend(["", "## 暴露机会", ""])
    lines.append(_render_opportunities(allocation.round, payload))
    lines.extend(["", "## 补证所需证据量", ""])
    if plan.power:
        rows = []
        for note in plan.power:
            rows.append(
                [
                    _power_label(note.contrast),
                    _fmt(note.baseline_rate),
                    note.resident_n,
                    note.community_n,
                    _fmt(note.mde_resident),
                    _fmt(note.mde_community),
                    note.community_df if note.community_df is not None else "—",
                ]
            )
        lines.append(
            _markdown_table(
                ["比较", "已查齐轮次基线办成率", "居民数", "社区数", "可检出差（居民层）",
                 "可检出差（社区层）", "社区层自由度"],
                rows,
            )
        )
        lines.append("")
        lines.append(
            "按 5% 双侧显著性和 80% 功效的近似计算。居民层可检出差按二项近似；"
            "社区层可检出差以社区为观察单位，用已查齐轮次的社区间方差估计。"
            "社区间方差本身只由已查齐轮次的 24 个社区率估计，而下表每项比较只有 "
            f"{plan.power[0].community_df} 个社区层自由度，这个估计并不稳定；"
            "规划时应以居民层数字为保守口径，社区层数字只作参考。"
            "两者都远大于现实中可能的效果时，这一轮只能作为补证的一部分，不能单独给出确定结论。"
        )
        lines.append("")
        lines.append("若希望检出更小的差，所需每档可观察人数（居民层近似）为：")
        rows = []
        for delta in (0.20, 0.15, 0.10):
            row = [f"{delta:.2f}"]
            for note in plan.power:
                if note.baseline_rate is None:
                    row.append("—")
                else:
                    row.append(_required_n(note.baseline_rate, delta))
            rows.append(row)
        lines.append("")
        lines.append(
            _markdown_table(
                ["目标差"] + [_power_label(note.contrast) for note in plan.power], rows
            )
        )
        lines.append("")
        lines.append(
            "实际可用寄件量只决定社区名单，不改变每档可观察居民数；"
            "若所需人数超过名单能覆盖的人数，应考虑增加轮次或扩大社区名单，"
            "而不是在同一轮内事后挑选人群。"
        )
    lines.extend(["", "## 执行规则", ""])
    for note in plan.notes:
        lines.append(f"- {note}")
    excluded = payload.get("excluded_allocations") or []
    if excluded:
        lines.extend(["", "## 未交付名单及原因", ""])
        lines.append(
            f"另有 {len(excluded)} 个名单单看可执行，但会让需要判断的暴露在某个优先人群里"
            "失去必要对照。它们不作为任何比较的执行计划；下面列出前若干个及缺失条件。"
        )
        lines.append("")
        rows = []
        for item in excluded[:10]:
            reasons = []
            for missing in item["missing"]:
                for reason in missing["reasons"]:
                    short = reason.split("（逐社区")[0]
                    if short not in reasons:
                        reasons.append(short)
            affected = "、".join(
                _power_label(
                    f"{missing['comparison'].split('/')[0]}/{missing['comparison'].split('/')[1].split(' ')[0]}"
                    f" vs {missing['comparison'].split(' ')[-1]}"
                )
                for missing in item["missing"]
            )
            rows.append(
                [
                    ", ".join(item["communities"]),
                    item["letters"],
                    affected,
                    "；".join(reasons),
                ]
            )
        lines.append(
            _markdown_table(["社区名单", "寄送量", "受影响的比较", "失去对照的条件"], rows)
        )
        if len(excluded) > 10:
            lines.append("")
            lines.append(
                f"其余 {len(excluded) - 10} 个未交付名单的结构相同，"
                "完整清单保存在 `evidence.json` 的 `excluded_allocations`。"
            )
    lines.extend(["", "## 剩余条件下的问题", ""])
    lines.append(
        "这份名单能回答的三项比较见上表；不能回答的是更小的效应、未纳入比较的"
        "“一般/邻接”档，以及任何需要把不同轮次人群合并后才能构造的对照。"
    )
    lines.append(
        "上一轮的结果只能用于规划本轮；本轮结束后须用本轮独立观察复核，"
        "不得把上一轮改过人群后的结果当作本轮预先约定的结论。"
    )
    return "\n".join(lines) + "\n"


def _power_label(contrast_label: str) -> str:
    """把 “少办成/own vs neither” 转成中文比较名。"""
    lookup = {
        "少办成/own vs neither": "少办成：自身获邀 vs 自身及邻接均未获邀",
        "少办成/neighbor vs neither": "少办成：邻接获邀 vs 自身及邻接均未获邀",
        "一般/own vs neither": "一般：自身获邀 vs 自身及邻接均未获邀",
        "一般/neighbor vs neither": "一般：邻接获邀 vs 自身及邻接均未获邀",
    }
    return lookup.get(contrast_label, contrast_label)


def _required_n(rate: float, delta: float) -> str:
    if not (0 < rate < 1) or delta <= 0:
        return "—"
    z_alpha = 1.959963984540054
    z_beta = 0.8416212335729143
    per_arm = 2 * (z_alpha + z_beta) ** 2 * rate * (1 - rate) / (delta**2)
    return f"{math.ceil(per_arm)}"


def _write_allocation_csv(path: Path, plan: DeliveryPlan, tables: Tables) -> None:
    members = plan.allocation.communities
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["round", "community", "group", "letters", "exposed_cell", "note"]
        )
        for community in sorted(tables.communities):
            if community not in members:
                continue
            cell = "own"
            writer.writerow(
                [
                    plan.allocation.round,
                    community,
                    tables.group_of(community),
                    tables.communities[community].letters,
                    cell,
                    "按设计名单寄出；寄件失败或错寄只记录执行偏离",
                ]
            )


def build_reports(
    tables: Tables,
    output_dir: Path,
    *,
    plan_date: date | None = None,
    rounds: list[str] | None = None,
) -> dict[str, object]:
    output_dir = Path(output_dir)
    rounds_dir = output_dir / "rounds"
    rounds_dir.mkdir(parents=True, exist_ok=True)
    plan_date = plan_date or tables.data_cutoff

    selected = set(rounds) if rounds else None
    evidence: dict[str, object] = {
        "data_cutoff": tables.data_cutoff.isoformat(),
        "plan_date": plan_date.isoformat(),
        "rounds": {},
        "prior_rounds": prior_round_inventory(tables),
    }
    outcomes_by_round = {}

    # 已寄出的轮次：按原记录复盘。
    for round_name in sorted(tables.rounds):
        if selected is not None and round_name not in selected:
            continue
        if not tables.assigned_communities(round_name):
            continue
        outcomes = build_outcomes(tables, round_name)
        outcomes_by_round[round_name] = outcomes
        edges = tables.analysis_edges(round_name)
        # 历史轮次的暴露概率应还原到该轮设计时的可行名单上，而不是用
        # 后续资料截止日重新解释；已寄出的轮次一律按 feasible 支持集。
        support = build_support(
            tables,
            round_name,
            edges,
            tables.rounds[round_name].reference_date.isoformat(),
            support_kind="feasible",
        )
        allocation = _allocation_for_round(tables, round_name)
        complete = tables.rounds[round_name].complete
        results: list[ComparisonResult] = []
        for contrast in tables.contrasts[round_name]:
            if not complete:
                exposed, control = contrast_cells(
                    tables, round_name, allocation, contrast, edges
                )
                missing = missing_conditions(
                    tables, round_name, allocation, contrast, edges
                )
                result = ComparisonResult(
                    contrast=contrast,
                    exposed=summarise_cell(
                        tables, outcomes, round_name, exposed, contrast.exposed
                    ),
                    control=summarise_cell(
                        tables, outcomes, round_name, control, contrast.control
                    ),
                    community_exposed=community_rates(
                        tables, outcomes, round_name, exposed
                    ),
                    community_control=community_rates(
                        tables, outcomes, round_name, control
                    ),
                    status=PROGRESS_ONLY,
                    identifiable=not missing,
                    missing=missing,
                    note="观察窗未走完，只报告进度",
                )
            else:
                result = compare(
                    tables, outcomes, support, round_name, allocation, contrast, edges
                )
            results.append(result)
        icc = _round_icc(tables, outcomes)
        payload = review_payload(
            tables, round_name, outcomes, support, allocation, results, icc
        )
        (rounds_dir / f"{round_name}.md").write_text(
            _render_review(tables, payload, results), encoding="utf-8"
        )
        evidence["rounds"][round_name] = payload

    # 已有历史分配但未走完的轮次仍需按原记录保留（上面的循环已覆盖）；
    # 计划名单只对资料中还没有历史分配的轮次生成。
    unscheduled = [
        name
        for name in sorted(tables.rounds)
        if (selected is None or name in selected)
        and not tables.assigned_communities(name)
    ]
    plan_payloads = {}
    for round_name in unscheduled:
        plan = build_delivery_plan(
            tables,
            round_name,
            plan_date=plan_date,
            outcomes_by_round=outcomes_by_round,
            support_kind="identifiable",
        )
        edges = plan.support.edges
        support = plan.support
        allocation = plan.allocation
        rows = []
        for community in sorted(tables.communities):
            row = {"community": community, "group": tables.group_of(community)}
            for cell in (OWN, NEIGHBOR):
                row[f"p_{cell}"] = support.probability(community, cell)
                row[f"possible_{cell}"] = support.structural.get((community, cell), False)
                row[f"note_{cell}"] = support.opportunity_note(community, cell)
            rows.append(row)
        payload = {
            "round": round_name,
            "status": "planned",
            "plan_date": plan_date.isoformat(),
            "allocation": {
                "communities": sorted(allocation.communities),
                "letters": allocation.letters,
                "seed": plan.seed,
                "rule": "SHA-256(规范化设计材料) 在可识别且使每个比较都具备对照的名单中等概率抽取",
            },
            "support": {
                "kind": support.support_kind,
                "feasible_n": len(support.feasible),
                "identifiable_n": len(support.identifiable),
                "cluster_ready_n": len(support.cluster_ready),
                "delivery_n": len(support.delivery),
                "effective_edges": sorted("+".join(edge) for edge in edges),
                "minimum_opportunity_ok": support.minimum_opportunity_ok,
                "min_exposure_prob": support.min_exposure_prob,
            },
            "opportunities": rows,
            "comparisons": plan.contrasts,
            "power": [asdict(note) for note in plan.power],
            "notes": plan.notes,
            "excluded_allocations": excluded_allocations(tables, round_name, edges),
            "unsent_rechecks": _unsent_rechecks(tables, round_name, plan_date),
        }
        plan_payloads[round_name] = payload
        evidence["rounds"][round_name] = payload
        (rounds_dir / f"{round_name}.md").write_text(
            _render_plan(tables, plan, payload), encoding="utf-8"
        )
        _write_allocation_csv(output_dir / f"allocation_{round_name}.csv", plan, tables)

    evidence["plan"] = plan_payloads
    _write_summary(tables, output_dir, evidence, outcomes_by_round)
    (output_dir / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return evidence


def _unsent_rechecks(tables: Tables, plan_round: str, plan_date: date):
    """在重新计划之前，核对所有已寄出轮次中尚未寄完的批次。"""
    rechecks = []
    for round_name in sorted(tables.rounds):
        if round_name == plan_round:
            continue
        assignments = [
            assignment
            for assignment in tables.assignments.values()
            if assignment.round == round_name
        ]
        if any(assignment.assigned > assignment.sent for assignment in assignments):
            rechecks.append(recheck_unsent(tables, round_name, plan_date))
    return rechecks


def _round_icc(tables: Tables, outcomes: RoundOutcomes) -> float | None:
    if not tables.rounds[outcomes.round].complete:
        return None
    groups: dict[str, list[int]] = {}
    for entry in tables.roster[outcomes.round]:
        if entry.already_accepted:
            continue
        outcome = outcomes.residents[entry.resident]
        if outcome.outcome is None:
            continue
        groups.setdefault(entry.community, []).append(outcome.outcome)
    values = list(groups.values())
    n_total = sum(len(g) for g in values)
    if len(values) < 2 or n_total <= len(values):
        return None
    grand = sum(sum(g) for g in values) / n_total
    ss_between = sum(len(g) * (sum(g) / len(g) - grand) ** 2 for g in values)
    ss_within = sum(sum((y - sum(g) / len(g)) ** 2 for y in g) for g in values)
    ms_between = ss_between / (len(values) - 1)
    ms_within = ss_within / (n_total - len(values))
    n0 = (n_total - sum(len(g) ** 2 for g in values) / n_total) / (len(values) - 1)
    denominator = ms_between + (n0 - 1) * ms_within
    if denominator <= 0:
        return None
    return max(0.0, (ms_between - ms_within) / denominator)


def _write_summary(
    tables: Tables,
    output_dir: Path,
    evidence: dict[str, object],
    outcomes_by_round: dict,
) -> None:
    lines = ["# 试点复盘与下一轮计划索引", ""]
    lines.append(
        f"资料截止 {evidence['data_cutoff']}；计划日 {evidence['plan_date']}。"
        "所有分组以轮前锁定的社区分配为准，不按居民后来到过的站点分组。"
    )
    lines.extend(["", "## 轮次状态与比较", ""])
    rows = []
    for round_name, payload in evidence["rounds"].items():
        if payload["status"] == "planned":
            rows.append(
                [
                    round_name,
                    "计划（未寄出）",
                    ", ".join(payload["allocation"]["communities"]),
                    "见轮次报告",
                    "—",
                ]
            )
            continue
        status = STATUS_LABELS[payload["status"]]
        comparisons = payload["comparisons"]
        if payload["status"] == PROGRESS_ONLY:
            summary = "；".join(
                f"{item['label']}：仅进度"
                + ("，且缺少对照" if item["missing"] else "")
                for item in comparisons
            )
        else:
            summary = "；".join(
                f"{item['label']}："
                + ("不可识别" if item["status"] == NOT_IDENTIFIABLE else _fmt(item["resident_rd"]))
                for item in comparisons
            )
        followup = payload["execution"]["followup"]
        rows.append(
            [
                round_name,
                status,
                ", ".join(payload["planned_allocation"]["communities"]),
                summary,
                f"已查齐 {followup['done'] + followup['not_done']}，未查齐 {followup['unknown']}",
            ]
        )
    lines.append(
        _markdown_table(["轮次", "状态", "获邀社区", "比较结果（风险差）", "随访"], rows)
    )
    lines.extend(["", "## 已寄出批次", ""])
    for item in evidence["prior_rounds"]:
        lines.append(
            f"- 轮次 {item['round']}：分配 {', '.join(item['assigned']) or '—'}；"
            f"寄出 {', '.join(item['sent']) or '—'}"
            + (
                f"（分配 {item['assigned_total']} 封，寄出 {item['sent_total']} 封）"
                if item["assigned_total"] != item["sent_total"]
                else ""
            )
        )
        lines.append(
            "  已寄出批次按原记录复盘；后续借调或重新抽取不改变这些社区的历史分组。"
        )
    rechecks = []
    for payload in evidence["plan"].values():
        rechecks.extend(payload.get("unsent_rechecks") or [])
    if rechecks:
        lines.extend(["", "## 未寄出批次复核", ""])
        for recheck in rechecks:
            lines.append(
                f"轮次 {recheck['round']} 在计划日的有效共享顾问关系 "
                f"{len(recheck['effective_edges'])} 条；"
                f"已取消覆盖 {', '.join(recheck['removed_edges']) or '—'}。"
            )
            lines.append(
                f"已寄出：{', '.join(recheck['sent_communities']) or '—'}；"
                f"未寄出：{', '.join(recheck['unsent_communities']) or '—'}。"
            )
            if recheck["cell_changes"]:
                lines.append("借调使以下社区的暴露档发生变化：")
                for item in recheck["cell_changes"]:
                    lines.append(
                        f"- {item['community']}：原登记 {CELL_LABELS[item['registered']]} → "
                        f"有效关系 {CELL_LABELS[item['effective']]}"
                    )
            if recheck["unsent_communities"]:
                if not recheck["identifiable_under_effective"]:
                    if recheck["identifiable_under_registered"]:
                        lines.append(
                            "借调后整批名单已不能同时提供各比较的对照，未寄出的部分不能当作"
                            "原设计的继续执行；已寄出部分仍按原抽取名单复盘。"
                        )
                    else:
                        lines.append(
                            "这批名单在原登记关系下本来就没有完整对照，有效关系下同样不可识别；"
                            "未寄出的部分不能用来补齐任何比较，已寄出部分仍按原记录复盘。"
                        )
                elif recheck["cell_changes"]:
                    lines.append(
                        "借调改变了部分社区的暴露档；未寄出批次需按上表重新核对后再寄。"
                    )
            lines.append(
                "若后续仍要覆盖其中社区，需按新的有效关系另立登记，不能按剩余社区重新拼一个组。"
            )
            lines.append("")
    lines.extend(["", "## 下一轮", ""])
    for round_name, payload in evidence["plan"].items():
        lines.append(
            f"- 轮次 {round_name}：从 {payload['support']['feasible_n']} 个可执行名单中，"
            f"保留 {payload['support']['identifiable_n']} 个使全部预先约定比较具备对照的名单，"
            f"抽取 {', '.join(payload['allocation']['communities'])}"
            f"（{payload['allocation']['letters']} 封）。"
        )
        comparisons = payload.get("comparisons") or []
        singles = [
            item
            for item in comparisons
            if item.get("exposed_communities_n", 0) < 2
            or item.get("control_communities_n", 0) < 2
        ]
        if singles:
            labels = []
            for item in singles:
                key = f"{item['group']}/{item['exposed']} vs {item['control']}"
                labels.append(
                    f"{_power_label(key)}（暴露 {item['exposed_communities_n']} 个社区、"
                    f"对照 {item['control_communities_n']} 个社区）"
                )
            weak = "；".join(labels)
            lines.append(
                f"  按名单可识别但社区数偏少的比较：{weak}；"
                "这些比较的社区层变异只能谨慎解释。"
            )
        if payload["power"]:
            worst = max(
                (item["mde_resident"] for item in payload["power"] if item["mde_resident"] is not None),
                default=None,
            )
            if worst is not None:
                lines.append(
                    f"  在现有可观察人数下，居民层可检出差约 {worst:.2f} 起；"
                    "更小的效果需增加轮次或扩大名单。"
                )
    lines.extend(["", "## 使用说明", ""])
    plan_files = ", ".join(
        f"`allocation_{round_name}.csv`" for round_name in evidence["plan"]
    ) or "（本轮没有需要新发放的名单）"
    lines.append(
        "轮次报告分别列出可识别比较、对照社区与观察人数、未查齐人数和解释边界；"
        f"`evidence.json` 保存同一批数字，{plan_files} 是下一轮发放清单。"
    )
    lines.append(
        "上一轮结果只用于规划；下一轮的确认性结论必须来自下一轮新的独立观察，"
        "不能在旧结果里改完人群后当作预先约定结论。"
    )
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
