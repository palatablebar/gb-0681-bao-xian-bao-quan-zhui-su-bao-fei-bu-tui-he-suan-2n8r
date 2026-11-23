import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from app.errors import DomainError
from app.modules.m8_loan import ledger
from app.modules.m8_loan.engine import adjudicate, loan_status, money, validate_account

SEED_PATH = Path(__file__).resolve().parents[3] / 'sample_data' / 'loans.json'
SEED_MARKER = 'loans-seed-v1'


def _now():
    return datetime.now(timezone.utc).isoformat()


def open_account(account):
    validate_account(account)
    ledger.save_account(account)
    ledger.append_event(account['account_id'], 'account_open', account['account_id'],
                        copy.deepcopy(account), _now())
    stored = ledger.get_account(account['account_id'])
    stored['cash_value_check'] = ledger.cash_value_gate(account['account_id'])
    return stored


def post_repayment(account_id, payment):
    """一笔还款的唯一入账路径。

    顺序：流水号校验 → 幂等检查 → 对平闸门 → 唯一去向裁决 → 资金守恒
    → 单事务（台账追加 + 条件换版）。同号重跑返回首次裁决，绝不重复入账。
    """
    if not isinstance(payment, dict):
        raise DomainError('PAYLOAD', '还款请求必须是对象')
    payment_id = str(payment.get('payment_id', '')).strip()
    if not payment_id:
        raise DomainError('PAYMENT_ID', '还款流水号缺失，无法保证幂等')
    if 'amount' not in payment:
        raise DomainError('MISSING', '还款金额缺失')
    payment = dict(payment, payment_id=payment_id,
                   received_at=payment.get('received_at') or _now())

    # 幂等：同一还款重跑返回首次裁决，不产生第二条台账、不再次换版。
    existing = ledger.get_event(account_id, 'payment', payment_id)
    if existing is not None:
        return existing, False

    # 闸门：现金价值与追溯版本未对平，不得入账。
    ledger.cash_value_gate(account_id)

    account = ledger.get_account(account_id)
    event = adjudicate(account, payment)
    ledger.assert_conservation(account, event)

    result = ledger.post_payment_tx(account, event, _now())
    event, inserted = result[0], result[1]
    if inserted:
        event['version_after'] = result[2]
    return event, inserted


def account_view(account_id):
    """复核页面与结案下载共用的同一份视图（单一事实来源）。"""
    account = ledger.get_account(account_id)
    events = ledger.list_events(account_id)
    payments = [e for e in events if e['_event_type'] == 'payment']
    endorsements = [e for e in events if e['_event_type'] == 'endorsement']
    check = ledger.reconcile_safe(account)
    status = _current_status(account, payments)
    closable = check['matched'] and status[0] in ('CLOSED_REFUND', 'CLOSED_EVEN')
    return {
        'account': account,
        'cash_value_check': check,
        'payments': payments,
        'endorsements': endorsements,
        'loan_status': status[0],
        'loan_status_label': status[1],
        'closable': closable,
    }


def _current_status(account, payments):
    if payments:
        last = payments[-1]
        return last['loan_status'], last['loan_status_label']
    return loan_status(money(account['loan_interest_due'], 'loan_interest_due'),
                       money(account['loan_principal'], 'loan_principal'),
                       money(account['unpaid_premium'], 'unpaid_premium'),
                       money('0', 'zero'))


def add_endorsement(account_id, endorsement):
    """批改记录只追加，永不覆盖原记录。"""
    required = ('endorsement_id', 'kind', 'effective')
    for field in required:
        if not str(endorsement.get(field, '')).strip():
            raise DomainError('MISSING', '批改记录缺少字段: ' + field)
    body = dict(endorsement, account_id=account_id, recorded_at=_now())
    saved, inserted = ledger.append_event(
        account_id, 'endorsement', endorsement['endorsement_id'], body, _now())
    if not inserted:
        raise DomainError('DUPLICATE_EVENT', '批改流水号已存在，禁止覆盖原记录', 409)
    return saved


def replay_view(account_id):
    snapshot, payments = ledger.replay(account_id)
    current = ledger.get_account(account_id)
    if snapshot is None:
        raise DomainError('REPLAY', '缺少建档台账，无法重放')
    for field in ('loan_interest_due', 'loan_principal', 'unpaid_premium'):
        if snapshot[field] != current[field]:
            raise DomainError('REPLAY', '台账重放与当前账户不一致，数据不可追溯')
    return {'replayed': snapshot, 'payment_count': len(payments), 'consistent': True}


def seed_if_empty():
    if ledger.seed_done(SEED_MARKER):
        return
    data = json.loads(SEED_PATH.read_text(encoding='utf-8'))
    for account in data['accounts']:
        if ledger.get_account_or_none(account['account_id']) is not None:
            continue
        try:
            open_account(copy.deepcopy(account))
        except DomainError as error:
            if error.code != 'CASH_UNRECONCILED':
                raise
            # 未对平账户也建档可见，但任何入账与结案都会被闸门拦截。
            ledger.save_account(copy.deepcopy(account))
            ledger.append_event(account['account_id'], 'account_open',
                                account['account_id'], copy.deepcopy(account), _now())
    for endorsement in data.get('endorsements', []):
        if ledger.get_event(endorsement['account_id'], 'endorsement',
                            endorsement['endorsement_id']) is None:
            add_endorsement(endorsement['account_id'], endorsement)
    for payment in data.get('payments', []):
        account_id = payment['account_id']
        payload = {k: v for k, v in payment.items() if k != 'account_id'}
        if ledger.get_event(account_id, 'payment', payload['payment_id']) is not None:
            continue
        try:
            post_repayment(account_id, payload)
        except DomainError as error:
            if error.code != 'CASH_UNRECONCILED':
                raise
    ledger.mark_seeded(SEED_MARKER)
