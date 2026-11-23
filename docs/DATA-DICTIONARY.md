# 保费补退定义

每份保单有且仅有一条issue基线。change按effective、责任、version选版；同生效日同版本矛盾拒绝。cancel必须引用同保单的change，取消整份变更，不按录入顺序覆盖。区间为[start,end)。年度保费按所在自然年365或366日拆分，跨年强制切段。先求每段精确保费，再最大余额法分配到分，使段合计等于保单总额。paid为已收金额，总应收减已收为补退额，正数补缴、负数退费。

## 通用采信约定

CSV按UTF-8 BOM、UTF-8、GB18030回退，保留逻辑记录结束行号；引号内换行由CSV解析。重复表头单独计数，列数不齐隔离。编码与ID按字符串保形。缺失哨兵为 `-`、`N/A`、`NULL`、`未知` 与空串；不得作为零。数值先NFKC归一、去千分位，再用Decimal。日期采用ISO；时间必须带偏移并转UTC。空文件、全空行、仅表头分别返回EMPTY_FILE、BLANK_FILE、HEADER_ONLY。

重复键为(id,version)，完全相同行去重；同键内容冲突则整组隔离，不取最后一行。行数对账：输入=采纳+隔离+空行+重复表头+完全重复。运行幂等键由输入字节、参数与规则版本计算。

# 贷款还款去向裁决（m8_loan）

一笔还款只有唯一去向，按《民法典》第五百六十一条固定瀑布冲抵，禁止"先本后息"：

1. **结清欠息**：还款先全额冲抵 `loan_interest_due`；
2. **压减贷款本金**：剩余冲减 `loan_principal`；
3. **抵作欠交保费**：再剩余抵作 `unpaid_premium`；
4. **退还客户**：仍有剩余才形成 `refund`。

部分还款下"先息后本"与"先本后息"产生不同的本金/欠息余额（见 `engine.naive_principal_first` 对照与 `tests/test_loan.py::test_interest_first_and_principal_first_diverge`），系统只采信固定瀑布结果。资金守恒：每笔还款 `received = interest_paid + principal_paid + premium_offset + refund`，且新余额 = 旧余额 - 冲抵额，守恒不过拒绝换版。

## 发布闸门与状态

- 现金价值对平：`cash_value_current` 必须等于 `cash_value_retro`。未对平（`CASH_UNRECONCILED`）时拒绝入账、拒绝结案下载，页面只读展示未对平原因。
- 贷款状态：`LOAN_OPEN`（本息任一未清）/ `PREMIUM_DUE`（贷款清、保费欠）/ `CLOSED_REFUND`（结清应退）/ `CLOSED_EVEN`（结清无退）。仅后两者且对平通过才可结案下载，否则 `NOT_CLOSABLE`。
- 结案 CSV 与复核页面共用 `service.account_view` 同一份台账事件，字段：账户、保单、还款流水、到账、冲抵欠息、压减本金、抵作保费、退还客户、剩余欠息、剩余本金、欠交保费、贷款状态、对平、结案状态。

## 幂等、换版与可追溯

- 还款以 `payment_id` 为幂等键：同号重跑返回首次裁决（`reposted=true`），不新增台账、不再次换版。
- 台账 `loan_ledger` 只追加（INSERT），无 UPDATE/DELETE；账户 `loan_accounts` 条件更新（`WHERE version=?`），版本冲突抛 `STALE` 409，台账追加与账户换版在同一 SQLite 事务内，失败整体回滚、旧版保留。
- 批改记录 `endorsement` 同号追加抛 `DUPLICATE_EVENT`，原记录永不覆盖。
- `replay` 从建档事件与逐笔还款重放余额，与账户当前值不一致抛 `REPLAY`，保证债权与保障可追溯。
