"""居民层办理结果：按受理编号去重、区分此前已受理与观察窗内办成、记录随访完整度。"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from .data import Tables

DONE = "done"
PENDING = "pending"
NONE = "none"
PRIOR = "prior"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class ResidentOutcome:
    round: str
    resident: str
    community: str
    already_accepted: bool
    application_in_window: bool
    case_status: str  # done / pending / none / prior / unknown
    observed: bool
    reason: str
    future_cases: int = 0

    @property
    def outcome(self) -> int | None:
        """观察窗内本轮办成=1，确认已受理未办成或查明无申请=0。

        未查齐的渠道返回 None，避免把“还没查到”当作“没有办理”。
        """
        if not self.observed:
            return None
        return 1 if self.case_status == DONE else 0


@dataclass(frozen=True)
class RoundOutcomes:
    round: str
    censor_date: date
    window_end: date
    complete: bool
    residents: dict[str, ResidentOutcome]

    def known(self, include_prior: bool = False):
        return [
            outcome
            for outcome in self.residents.values()
            if outcome.observed and (include_prior or not outcome.already_accepted)
        ]


def _case_statuses(tables: Tables) -> dict[tuple[str, str], str]:
    """同一受理编号的多张回单合并；任一来源显示完成即按完成计。"""
    by_case: dict[tuple[str, str], str] = {}
    for application in tables.applications:
        key = (application.round, application.case)
        if application.status == DONE or key not in by_case:
            by_case[key] = application.status
    return by_case


def build_outcomes(tables: Tables, round_name: str) -> RoundOutcomes:
    round_ = tables.rounds[round_name]
    censor_date = min(round_.as_of, round_.window_end)
    case_statuses = _case_statuses(tables)

    window_applications: dict[str, list] = defaultdict(list)
    future_cases: dict[str, int] = defaultdict(int)
    for application in tables.applications:
        if application.round != round_name:
            continue
        if not (round_.window_start <= application.day <= round_.window_end):
            continue
        if application.day > censor_date:
            future_cases[application.resident] += 1
            continue
        window_applications[application.resident].append(application)

    residents: dict[str, ResidentOutcome] = {}
    for entry in tables.roster[round_name]:
        coverage = tables.coverage.get((round_name, entry.resident), "awaiting")
        applications = window_applications.get(entry.resident, [])
        cases = {application.case for application in applications}
        statuses = {case_statuses[(round_name, case)] for case in cases}
        pending_future = future_cases.get(entry.resident, 0)

        if entry.already_accepted:
            residents[entry.resident] = ResidentOutcome(
                round_name,
                entry.resident,
                entry.community,
                True,
                bool(applications),
                PRIOR,
                False,
                "轮前已受理，作为分层/排除变量，不计入本轮办成",
                pending_future,
            )
            continue

        if DONE in statuses:
            resident = ResidentOutcome(
                round_name, entry.resident, entry.community, False, True,
                DONE, True, "观察窗内回单显示办成", pending_future,
            )
        elif statuses:
            resident = ResidentOutcome(
                round_name, entry.resident, entry.community, False, True,
                PENDING, True, "观察窗内已有受理，回单尚未办成", pending_future,
            )
        elif coverage == "confirmed_none":
            resident = ResidentOutcome(
                round_name, entry.resident, entry.community, False, False,
                NONE, True, "按期查完各渠道，确认没有申请", pending_future,
            )
        else:
            # registry_checked 只说明登记渠道已核对，未覆盖全部来源；没有
            # 受理记录时不能倒推为“确认没有办理”。awaiting 同理。
            reason = (
                "登记渠道已核对但未覆盖全部来源，不能按未办理计"
                if coverage == "registry_checked"
                else "仍有待回渠道（awaiting），不能按未办理计"
            )
            resident = ResidentOutcome(
                round_name, entry.resident, entry.community, False, False,
                UNKNOWN, False, reason, pending_future,
            )
        residents[entry.resident] = resident

    return RoundOutcomes(round_name, censor_date, round_.window_end, round_.complete, residents)


def outcome_kind(outcome: ResidentOutcome) -> str:
    """报告用分类：办成 / 未办成（已查齐）/ 未定（未查齐）/ 轮前已受理。"""
    if outcome.already_accepted:
        return PRIOR
    if outcome.outcome == 1:
        return "done"
    if outcome.outcome == 0:
        return "not_done"
    return UNKNOWN
