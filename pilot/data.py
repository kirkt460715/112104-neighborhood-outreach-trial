"""读取并校验试点交换表，提供分析所需的不变记录视图。"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

# CSV 中空字符串代表未填写；这里只在明确允许的字段上保留空值。
TABLE_KEYS = {
    "rounds": ("round",),
    "contrasts": ("round", "group", "exposed"),
    "communities": ("community",),
    "neighbors": ("round", "left", "right"),
    "restrictions": ("round", "community"),
    "assignments": ("round", "community"),
    "roster": ("round", "resident"),
    "applications": ("case", "kind"),
    "followup_coverage": ("round", "resident"),
    "execution_changes": ("round", "left", "right", "recorded"),
}


def _read_csv(directory: Path, name: str) -> list[dict[str, str]]:
    with (directory / f"{name}.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _as_date(value: str) -> date:
    return date.fromisoformat(value)


@dataclass(frozen=True)
class Round:
    name: str
    reference_date: date
    observation_days: int
    max_letters: int
    min_exposure_prob: float
    as_of: date
    priority_selected: int
    other_selected: int

    @property
    def window_start(self) -> date:
        return self.reference_date

    @property
    def window_end(self) -> date:
        """固定观察窗的最后一日（含）。"""
        return self.reference_date + timedelta(days=self.observation_days - 1)

    @property
    def complete(self) -> bool:
        """观察窗是否已经走完。未走完的轮次只报告进度。"""
        return self.as_of >= self.window_end


@dataclass(frozen=True)
class Community:
    name: str
    group: str
    station: str
    letters: int


@dataclass(frozen=True)
class Contrast:
    round: str
    group: str
    exposed: str
    control: str

    def label(self) -> str:
        return f"{self.group}/{self.exposed} vs {self.control}"


@dataclass(frozen=True)
class Assignment:
    round: str
    community: str
    assigned: int
    sent: int


@dataclass(frozen=True)
class RosterEntry:
    round: str
    resident: str
    community: str
    already_accepted: bool


@dataclass(frozen=True)
class Application:
    round: str
    case: str
    resident: str
    station: str
    day: date
    status: str
    kind: str


@dataclass(frozen=True)
class Removal:
    round: str
    left: str
    right: str
    kind: str
    recorded: date

    @property
    def edge(self) -> tuple[str, str]:
        return tuple(sorted((self.left, self.right)))  # type: ignore[return-value]


def _edge(left: str, right: str) -> tuple[str, str]:
    return tuple(sorted((left, right)))  # type: ignore[return-value]


class Tables:
    """一组经过唯一键与引用校验的交换表。"""

    def __init__(self, directory: Path | str = DATA_DIR):
        self.directory = Path(directory)
        self._raw = {name: _read_csv(self.directory, name) for name in TABLE_KEYS}
        self._validate_structure()

        self.rounds: dict[str, Round] = {}
        for row in self._raw["rounds"]:
            self.rounds[row["round"]] = Round(
                name=row["round"],
                reference_date=_as_date(row["reference_date"]),
                observation_days=int(row["observation_days"]),
                max_letters=int(row["max_letters"]),
                min_exposure_prob=float(row["min_exposure_prob"]),
                as_of=_as_date(row["as_of"]),
                priority_selected=int(row["priority_selected"]),
                other_selected=int(row["other_selected"]),
            )

        self.communities: dict[str, Community] = {
            row["community"]: Community(
                name=row["community"],
                group=row["group"],
                station=row["station"],
                letters=int(row["letters"]),
            )
            for row in self._raw["communities"]
        }
        self.groups: dict[str, list[str]] = defaultdict(list)
        for community in self.communities.values():
            self.groups[community.group].append(community.name)
        for members in self.groups.values():
            members.sort()

        self.contrasts: dict[str, list[Contrast]] = defaultdict(list)
        for row in self._raw["contrasts"]:
            self.contrasts[row["round"]].append(
                Contrast(row["round"], row["group"], row["exposed"], row["control"])
            )
        for members in self.contrasts.values():
            members.sort(key=lambda c: (c.group, c.exposed, c.control))

        self.neighbors: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for row in self._raw["neighbors"]:
            self.neighbors[row["round"]].add(_edge(row["left"], row["right"]))

        self.restrictions: dict[str, set[str]] = defaultdict(set)
        for row in self._raw["restrictions"]:
            self.restrictions[row["round"]].add(row["community"])

        self.assignments: dict[tuple[str, str], Assignment] = {}
        for row in self._raw["assignments"]:
            self.assignments[(row["round"], row["community"])] = Assignment(
                row["round"], row["community"], int(row["assigned"]), int(row["sent"])
            )

        self.roster: dict[str, list[RosterEntry]] = defaultdict(list)
        for row in self._raw["roster"]:
            self.roster[row["round"]].append(
                RosterEntry(
                    row["round"],
                    row["resident"],
                    row["community"],
                    row["already_accepted"] == "1",
                )
            )
        for entries in self.roster.values():
            entries.sort(key=lambda r: r.resident)
        self.roster_by_key: dict[tuple[str, str], RosterEntry] = {
            (entry.round, entry.resident): entry
            for entries in self.roster.values()
            for entry in entries
        }

        self.applications: list[Application] = [
            Application(
                row["round"],
                row["case"],
                row["resident"],
                row["station"],
                _as_date(row["day"]),
                row["status"],
                row["kind"],
            )
            for row in self._raw["applications"]
        ]

        self.coverage: dict[tuple[str, str], str] = {
            (row["round"], row["resident"]): row["coverage"]
            for row in self._raw["followup_coverage"]
        }

        self.execution_changes: list[Removal] = [
            Removal(
                row["round"],
                row["left"],
                row["right"],
                row["kind"],
                _as_date(row["recorded"]),
            )
            for row in self._raw["execution_changes"]
        ]

        self._validate_business_references()

    # -- 读取校验 ---------------------------------------------------------
    def _validate_structure(self) -> None:
        for name, fields in TABLE_KEYS.items():
            rows = self._raw[name]
            if not rows:
                raise ValueError(f"{name}.csv 没有记录")
            seen = set()
            for row in rows:
                if None in row or None in row.values():
                    raise ValueError(f"{name}.csv 存在列数不齐的记录")
                key = tuple(row[field] for field in fields)
                if not all(key):
                    raise ValueError(f"{name}.csv 主键字段为空: {key}")
                if key in seen:
                    raise ValueError(f"{name}.csv 主键重复: {key}")
                seen.add(key)

    def _validate_business_references(self) -> None:
        community_names = set(self.communities)
        round_names = set(self.rounds)
        resident_names = {(entry.round, entry.resident) for entries in self.roster.values() for entry in entries}

        for name, rows in self._raw.items():
            for row in rows:
                if "round" in row and row["round"] not in round_names:
                    raise ValueError(f"{name}.csv 引用了未登记轮次 {row['round']}")
        for row in self._raw["neighbors"]:
            if row["left"] not in community_names or row["right"] not in community_names:
                raise ValueError("neighbors.csv 引用了未登记社区")
        for row in self._raw["restrictions"]:
            if row["community"] not in community_names:
                raise ValueError("restrictions.csv 引用了未登记社区")
        for row in self._raw["assignments"]:
            if row["community"] not in community_names:
                raise ValueError("assignments.csv 引用了未登记社区")
        for row in self._raw["roster"]:
            if row["community"] not in community_names:
                raise ValueError("roster.csv 引用了未登记社区")
        for row in self._raw["applications"]:
            if (row["round"], row["resident"]) not in resident_names:
                raise ValueError("applications.csv 引用了未锁定居民")
            if row["station"] not in {c.station for c in self.communities.values()}:
                raise ValueError("applications.csv 引用了未登记站点")
        for row in self._raw["followup_coverage"]:
            if (row["round"], row["resident"]) not in resident_names:
                raise ValueError("followup_coverage.csv 引用了未锁定居民")

    # -- 便利视图 ---------------------------------------------------------
    @property
    def data_cutoff(self) -> date:
        """资料中已观察到的最后日期，用作默认的计划日。

        只取快照日、回单日和设计变更登记日；轮次的计划窗结束日只是设计
        参数，不能当作已经观察到的日期。
        """
        dates = [round_.as_of for round_ in self.rounds.values()]
        dates.extend(application.day for application in self.applications)
        dates.extend(change.recorded for change in self.execution_changes)
        return max(dates)

    def group_of(self, community: str) -> str:
        return self.communities[community].group

    def communities_in(self, group: str) -> list[str]:
        return list(self.groups[group])

    def registered_edges(self, round_name: str) -> set[tuple[str, str]]:
        """研究设计期登记的顾问共享关系（无向边）。"""
        return set(self.neighbors[round_name])

    def effective_edges(self, round_name: str, plan_date: date | None = None) -> set[tuple[str, str]]:
        """计划日仍可承诺覆盖的顾问共享关系。

        kind=removed 表示尚未发放的批次不能再按该边承诺覆盖；已经寄出的
        历史批次仍按登记时的原记录复盘，不因后来的借调被抹掉。
        """
        cutoff = plan_date or self.data_cutoff
        edges = self.registered_edges(round_name)
        for change in self.execution_changes:
            if change.kind == "removed" and change.recorded <= cutoff:
                edges.discard(change.edge)
        return edges

    def analysis_edges(self, round_name: str, plan_date: date | None = None) -> set[tuple[str, str]]:
        """历史轮次按原登记关系复盘；尚未发放的轮次按有效关系计划。"""
        if self.round_has_sent_batch(round_name):
            return self.registered_edges(round_name)
        return self.effective_edges(round_name, plan_date)

    def round_has_sent_batch(self, round_name: str) -> bool:
        return any(
            assignment.round == round_name and assignment.sent > 0
            for assignment in self.assignments.values()
        )

    def assigned_communities(self, round_name: str, basis: str = "assigned") -> frozenset[str]:
        """按分配或实际寄出划分的获邀社区集合。

        “实际到过哪个站”不参与分组；这里只用社区层面的抽签/寄出记录。
        """
        if basis not in {"assigned", "sent"}:
            raise ValueError("basis 只能是 assigned 或 sent")
        chosen = set()
        for assignment in self.assignments.values():
            if assignment.round != round_name:
                continue
            if getattr(assignment, basis) > 0:
                chosen.add(assignment.community)
        return frozenset(chosen)

    def adjacency(self, round_name: str, community: str, edges: set[tuple[str, str]]) -> set[str]:
        neighbours = set()
        for left, right in edges:
            if left == community:
                neighbours.add(right)
            elif right == community:
                neighbours.add(left)
        return neighbours

    def roster_for(self, round_name: str, include_prior: bool = False) -> list[RosterEntry]:
        entries = self.roster[round_name]
        if include_prior:
            return list(entries)
        return [entry for entry in entries if not entry.already_accepted]

    def letters_for(self, communities: Iterable[str]) -> int:
        return sum(self.communities[community].letters for community in communities)
