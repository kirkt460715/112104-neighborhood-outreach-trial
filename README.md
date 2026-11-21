# 跨站上门服务代办邀请的试点设计和复盘

养老顾问代办邀请试点的研究计划、社区关系、原登记和办理回单。 所有人员、服务机构和业务凭证均为合成资料。

完整观察窗从轮次的观察起始日起，按计划日数计；申请状态按受理编号归并，现有办理资格不受研究分配影响。邻接关系记录共享顾问所覆盖的两个社区，是无向边。每个社区由所属组定义资格约束，邀请量以对该社区发出的全部额外邀请计。

## 资料查阅

需要 Python 3.11.2，查看资料与运行资料检查只用标准库。CSV 使用 UTF-8，首行为字段名。空字段表示该项未填写或不适用，具体含义按各表约定。

```sh
python3 catalog.py
python3 -m unittest discover -s tests -v
```

例如，`python3 catalog.py --table rounds` 可读取该表原始记录。资料检查核对结构、唯一键及部分业务引用。

## 交换表

- `rounds.csv`。试点计划。窗口期内的合规比较类型由 contrasts 给出。最低概率是暴露机会的设计要求。每轮按两组声明的选中数量，从满足预算和限制的所有名单中等概率抽取，priority_selected 是少办成一组的选中数量。 主键由 `round` 确定。
- `contrasts.csv`。预先约定的比较。own 是自身社区获邀，neighbor 是未获邀而至少一个邻接社区获邀，neither 是自身及邻接均未获邀。 主键由 `round`、`group`、`exposed` 确定。
- `communities.csv`。各轮沿用的社区组别与每个获邀社区的计划寄送量。 主键由 `community` 确定。
- `neighbors.csv`。登记在研究设计期的顾问共享关系。 主键由 `round`、`left`、`right` 确定。
- `restrictions.csv`。不允许发邀请的社区。未列出的社区无此限制。分组至少要能覆盖对照计划要求的机会，不能超轮次寄送总量。 主键由 `round`、`community` 确定。
- `assignments.csv`。实际历史抽取及发放记录。sent 比 assigned 少的原因为执行偏离，留空的轮次没有历史分配。 主键由 `round`、`community` 确定。
- `roster.csv`。轮前锁定的分析人群及此前已受理标记。 主键由 `round`、`resident` 确定。
- `applications.csv`。不同站的办理回单。case 相同表示同一已确认申请，kind 是回单来源类型。 主键由 `case`、`kind` 确定。
- `followup_coverage.csv`。已核对的办理来源覆盖。confirmed_none 是查明没有申请，registry_checked 覆盖登记渠道，awaiting 仍有待回渠道。 主键由 `round`、`resident` 确定。
- `execution_changes.csv`。设计登记后发生的共享顾问借调。kind=removed 表示尚未发放时后续轮次不能再按该边承诺覆盖。 主键由 `round`、`left`、`right`、`recorded` 确定。
