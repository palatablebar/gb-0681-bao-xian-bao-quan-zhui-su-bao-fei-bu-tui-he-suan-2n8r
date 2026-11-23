from decimal import Decimal

import pytest

from app.errors import DomainError
from app.modules.m8_loan import ledger, service
from app.modules.m8_loan.engine import (
    allocate, adjudicate, loan_status, money, naive_principal_first,
    reconcile_cash_value, validate_account,
)
from app.modules.m8_loan.export_out import bundle, checksum, export_account

ACCOUNT = {
    'account_id': 'L-T1', 'policy_id': 'P-T1', 'currency': 'CNY',
    'cash_value_current': '11800.00', 'cash_value_retro': '11800.00',
    'loan_principal': '12000.00', 'loan_interest_due': '860.50',
    'unpaid_premium': '1380.00',
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'loan.db'))
    ledger.DB_PATH = str(tmp_path / 'loan.db')
    return ledger


@pytest.fixture
def opened(db):
    return service.open_account(dict(ACCOUNT))


def payment(pid, amount, when='2026-11-20T10:00:00+08:00'):
    return {'payment_id': pid, 'amount': amount, 'received_at': when}


# ---------- 唯一去向：固定瀑布 ----------

def test_waterfall_interest_before_principal():
    r = allocate('3000.00', '860.50', '12000.00', '1380.00')
    # 先全额结清欠息，剩余 2139.50 才压减本金，不碰保费、不退款
    assert r['applied']['interest'] == Decimal('860.50')
    assert r['applied']['principal'] == Decimal('2139.50')
    assert r['applied']['premium'] == Decimal('0.00')
    assert r['refund'] == Decimal('0.00')
    assert r['remaining_principal'] == Decimal('9860.50')


def test_waterfall_full_chain_with_refund():
    r = allocate('11420.50', '0.00', '9860.50', '1380.00')
    assert r['applied'] == {
        'interest': Decimal('0.00'), 'principal': Decimal('9860.50'),
        'premium': Decimal('1380.00'), 'refund': Decimal('180.00')}
    code, _ = loan_status(r['remaining_interest'], r['remaining_principal'],
                          r['remaining_premium'], r['refund'])
    assert code == 'CLOSED_REFUND'


def test_conservation_holds_for_every_allocation():
    r = allocate('3000.00', '860.50', '12000.00', '1380.00')
    assert sum(r['applied'].values(), Decimal('0.00')) == Decimal('3000.00')
    r2 = allocate('0.01', '0.00', '0.00', '0.00')
    assert r2['refund'] == Decimal('0.01')


def test_interest_first_and_principal_first_diverge():
    """裁决核心：两种做法在部分还款下产生不同的本金/欠息余额。"""
    legal = allocate('3000.00', '860.50', '12000.00', '1380.00')['applied']
    naive = naive_principal_first('3000.00', '860.50', '12000.00', '1380.00')
    assert legal['interest'] != naive['interest_paid']
    assert legal['principal'] != naive['principal_paid']
    # 先本后息会让欠息挂账 860.50，账户无法恢复；裁决禁止该路径
    assert naive['remaining_interest'] == Decimal('860.50')
    assert legal['interest'] == Decimal('860.50')


def test_status_enum():
    assert loan_status(Decimal('1'), Decimal('0'), Decimal('0'), Decimal('0'))[0] == 'LOAN_OPEN'
    assert loan_status(Decimal('0'), Decimal('0'), Decimal('5'), Decimal('0'))[0] == 'PREMIUM_DUE'
    assert loan_status(Decimal('0'), Decimal('0'), Decimal('0'), Decimal('9'))[0] == 'CLOSED_REFUND'
    assert loan_status(Decimal('0'), Decimal('0'), Decimal('0'), Decimal('0'))[0] == 'CLOSED_EVEN'


def test_negative_and_zero_payment_rejected():
    with pytest.raises(DomainError):
        allocate('0', '1', '1', '1')
    with pytest.raises(ValueError):
        money('-1.00', 'x')
    with pytest.raises(ValueError):
        money('abc', 'x')


def test_adjudicate_shape(opened):
    event = adjudicate(opened, payment('P1', '3000.00'))
    assert [w['step'] for w in event['waterfall']] == ['interest', 'principal', 'premium', 'refund']
    assert event['interest_paid'] == '860.50'
    assert event['cash_value_check']['matched'] is True


# ---------- 现金价值对平闸门 ----------

def test_unreconciled_account_rejects_open():
    bad = dict(ACCOUNT, account_id='L-BAD',
               cash_value_current='9000.00', cash_value_retro='8750.00')
    with pytest.raises(DomainError) as e:
        validate_account(bad)
    assert e.value.code == 'CASH_UNRECONCILED'


def test_unreconciled_gate_blocks_payment_and_export(db):
    bad = dict(ACCOUNT, account_id='L-BAD',
               cash_value_current='9000.00', cash_value_retro='8750.00')
    ledger.save_account(bad)
    ledger.append_event('L-BAD', 'account_open', 'L-BAD', bad, '2026-11-01T00:00:00+00:00')
    with pytest.raises(DomainError) as e:
        service.post_repayment('L-BAD', payment('PX', '100.00'))
    assert e.value.code == 'CASH_UNRECONCILED'
    with pytest.raises(DomainError) as e:
        export_account('L-BAD')
    assert e.value.code == 'CASH_UNRECONCILED'
    # 被拦截后账户余额与台账均无变化
    assert ledger.get_account('L-BAD')['loan_principal'] == '12000.00'
    assert [e['_event_type'] for e in ledger.list_events('L-BAD')] == ['account_open']


def test_reconcile_check_reads_both_versions():
    check = reconcile_cash_value(dict(ACCOUNT, cash_value_current='1.00',
                                      cash_value_retro='2.00', loan_principal='5.00',
                                      loan_interest_due='0.00'))
    assert check['matched'] is False and check['below_loan'] is True


# ---------- 幂等：同一还款重跑不重复入账 ----------

def test_same_payment_posted_once(opened):
    first, inserted1 = service.post_repayment('L-T1', payment('P1', '3000.00'))
    second, inserted2 = service.post_repayment('L-T1', payment('P1', '3000.00'))
    assert inserted1 is True and inserted2 is False
    # 台账裁决事实完全一致（version_after 仅在首次入账响应上附带）
    first.pop('version_after', None)
    assert first == second
    view = service.account_view('L-T1')
    assert len(view['payments']) == 1
    # 重跑不得再次换版
    assert view['account']['version'] == 2
    assert ledger.get_account('L-T1')['loan_principal'] == '9860.50'


def test_different_payments_accumulate(opened):
    service.post_repayment('L-T1', payment('P1', '3000.00'))
    service.post_repayment('L-T1', payment('P2', '11420.50'))
    view = service.account_view('L-T1')
    assert len(view['payments']) == 2 and view['account']['version'] == 3
    assert view['closable'] is True


def test_missing_payment_id_rejected(opened):
    with pytest.raises(DomainError) as e:
        service.post_repayment('L-T1', {'amount': '10.00'})
    assert e.value.code == 'PAYMENT_ID'


# ---------- 原子换版：失败保留旧版，台账不残留 ----------

def test_stale_switch_keeps_old_version(db, monkeypatch):
    service.open_account(dict(ACCOUNT))
    account = ledger.get_account('L-T1')
    event = adjudicate(account, payment('P1', '3000.00'))
    ledger.assert_conservation(account, event)
    # 外部先把版本推进，模拟并发换版冲突
    other = dict(account)
    other.pop('version', None)
    ledger.save_account(other, expected_version=1)
    assert ledger.get_account('L-T1')['version'] == 2
    with pytest.raises(DomainError) as e:
        ledger.post_payment_tx(account, event, '2026-11-20T10:00:00+00:00')
    assert e.value.code == 'STALE' and e.value.status == 409
    # 旧版保留：余额未变；失败事务回滚，台账无还款残留
    fresh = ledger.get_account('L-T1')
    assert fresh['version'] == 2 and fresh['loan_principal'] == '12000.00'
    assert [x['_event_type'] for x in ledger.list_events('L-T1')] == ['account_open']


def test_conservation_guard_rejects_broken_event(opened):
    account = ledger.get_account('L-T1')
    event = adjudicate(account, payment('P1', '3000.00'))
    event['principal_paid'] = '9999.99'  # 篡改破坏守恒
    with pytest.raises(DomainError) as e:
        ledger.assert_conservation(account, event)
    assert e.value.code == 'CONSERVATION'
    # 防护在换版前拦截，账户与台账不变
    assert ledger.get_account('L-T1')['version'] == 1


# ---------- 追加式台账：原记录与批改不可覆盖，可重放 ----------

def test_ledger_is_append_only_and_replayable(opened):
    service.post_repayment('L-T1', payment('P1', '3000.00'))
    service.post_repayment('L-T1', payment('P2', '11420.50'))
    service.add_endorsement('L-T1', {'endorsement_id': 'E1', 'kind': 'retro_reduce',
                                     'effective': '2026-10-01'})
    snapshot, payments = ledger.replay('L-T1')
    current = ledger.get_account('L-T1')
    assert snapshot['loan_principal'] == current['loan_principal'] == '0.00'
    assert snapshot['loan_interest_due'] == '0.00'
    assert snapshot['unpaid_premium'] == '0.00'
    assert len(payments) == 2
    assert service.replay_view('L-T1')['consistent'] is True
    # 台账只增：事件序号连续，且批改重复提交被拒绝而非覆盖
    seqs = [e['_seq'] for e in ledger.list_events('L-T1')]
    assert seqs == list(range(1, len(seqs) + 1))
    with pytest.raises(DomainError) as e:
        service.add_endorsement('L-T1', {'endorsement_id': 'E1', 'kind': 'other',
                                         'effective': '2026-10-02'})
    assert e.value.code == 'DUPLICATE_EVENT'
    original = ledger.get_event('L-T1', 'endorsement', 'E1')
    assert original['kind'] == 'retro_reduce'


def test_replay_detects_tampered_account(opened):
    service.post_repayment('L-T1', payment('P1', '3000.00'))
    tampered = ledger.get_account('L-T1')
    tampered['loan_principal'] = '0.01'
    ledger.save_account(tampered, expected_version=tampered['version'])
    with pytest.raises(DomainError) as e:
        service.replay_view('L-T1')
    assert e.value.code == 'REPLAY'


# ---------- 复核页与结案下载同一份数字 ----------

def test_export_matches_review_view(opened):
    service.post_repayment('L-T1', payment('P1', '3000.00'))
    service.post_repayment('L-T1', payment('P2', '11420.50'))
    view = service.account_view('L-T1')
    _, csv_body, digest = export_account('L-T1')
    assert digest == checksum(view) and len(digest) == 64
    for event in view['payments']:
        for field in ('interest_paid', 'principal_paid', 'premium_offset', 'refund'):
            assert event[field] in csv_body
    # 第一笔部分还款标不可结案，最后一笔标可结案
    assert '不可结案' in csv_body and '可结案' in csv_body


def test_export_blocked_while_loan_open(opened):
    service.post_repayment('L-T1', payment('P1', '3000.00'))
    with pytest.raises(DomainError) as e:
        export_account('L-T1')
    assert e.value.code == 'NOT_CLOSABLE'


def test_bundle_is_single_source(opened):
    service.post_repayment('L-T1', payment('P1', '3000.00'))
    service.post_repayment('L-T1', payment('P2', '11420.50'))
    view = service.account_view('L-T1')
    csv_body = bundle(view)
    # 同一份台账事件驱动：视图与 CSV 的退还金额完全一致
    assert view['payments'][-1]['refund'] == '180.00'
    assert '180.00' in csv_body
