import json
import os
import sqlite3
from pathlib import Path

from app.errors import DomainError
from app.modules.m8_loan.engine import money, reconcile_cash_value

DB_PATH = os.environ.get('LOAN_DB_PATH', os.environ.get('DB_PATH', 'data/records.db'))


def connection():
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(
        '''
        CREATE TABLE IF NOT EXISTS loan_accounts (
            account_id TEXT PRIMARY KEY,
            body       TEXT NOT NULL,
            version    INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS loan_ledger (
            seq        INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_key  TEXT NOT NULL,
            body       TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            UNIQUE(account_id, event_type, event_key)
        );
        CREATE TABLE IF NOT EXISTS loan_seed_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        '''
    )
    return db


# ---------- 账户快照：换版失败保留旧版，不覆盖旧结论 ----------

def _insert_account(db, account):
    body = json.dumps(account, ensure_ascii=False, sort_keys=True)
    try:
        db.execute('INSERT INTO loan_accounts(account_id,body,version) VALUES (?,?,1)',
                   (account['account_id'], body))
    except sqlite3.IntegrityError:
        raise DomainError('ACCOUNT_EXISTS', '贷款账户已存在')
    return 1


def _conditional_update(db, account, expected_version):
    """条件更新：版本不匹配抛 409，调用方事务整体回滚，旧版原样保留。"""
    body = json.dumps(account, ensure_ascii=False, sort_keys=True)
    cur = db.execute(
        'UPDATE loan_accounts SET body=?, version=version+1 '
        'WHERE account_id=? AND version=?',
        (body, account['account_id'], expected_version))
    if cur.rowcount != 1:
        row = db.execute('SELECT version FROM loan_accounts WHERE account_id=?',
                         (account['account_id'],)).fetchone()
        if row is None:
            raise DomainError('NOT_FOUND', '贷款账户不存在', 404)
        raise DomainError('STALE', '账户版本已更新，换版失败并保留旧版', 409)
    return expected_version + 1


def save_account(account, expected_version=None):
    with connection() as db:
        if expected_version is None:
            return _insert_account(db, account)
        return _conditional_update(db, account, expected_version)


def get_account(account_id):
    account = get_account_or_none(account_id)
    if account is None:
        raise DomainError('NOT_FOUND', '贷款账户不存在', 404)
    return account


def get_account_or_none(account_id):
    with connection() as db:
        row = db.execute('SELECT * FROM loan_accounts WHERE account_id=?',
                         (account_id,)).fetchone()
    if row is None:
        return None
    account = json.loads(row['body'])
    account['version'] = row['version']
    return account


def list_accounts():
    with connection() as db:
        rows = db.execute('SELECT account_id, version FROM loan_accounts ORDER BY account_id').fetchall()
    return [dict(r) for r in rows]


# ---------- 追加式台账：只增不改，覆盖即无法恢复，故永不 UPDATE/DELETE ----------

def append_event(account_id, event_type, event_key, body, recorded_at):
    """同一 (account_id, event_type, event_key) 唯一：重跑不重复入账。"""
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True)
    with connection() as db:
        try:
            db.execute(
                'INSERT INTO loan_ledger(account_id,event_type,event_key,body,recorded_at) '
                'VALUES (?,?,?,?,?)',
                (account_id, event_type, str(event_key), payload, recorded_at))
        except sqlite3.IntegrityError:
            existing = db.execute(
                'SELECT body FROM loan_ledger WHERE account_id=? AND event_type=? AND event_key=?',
                (account_id, event_type, str(event_key))).fetchone()
            return json.loads(existing['body']), False
        return body, True


def get_event(account_id, event_type, event_key):
    with connection() as db:
        row = db.execute(
            'SELECT body FROM loan_ledger WHERE account_id=? AND event_type=? AND event_key=?',
            (account_id, event_type, str(event_key))).fetchone()
    return json.loads(row['body']) if row else None


def list_events(account_id):
    with connection() as db:
        rows = db.execute(
            'SELECT seq,event_type,event_key,body,recorded_at FROM loan_ledger '
            'WHERE account_id=? ORDER BY seq', (account_id,)).fetchall()
    out = []
    for r in rows:
        item = json.loads(r['body'])
        item.update(_seq=r['seq'], _event_type=r['event_type'], _event_key=r['event_key'],
                    _recorded_at=r['recorded_at'])
        out.append(item)
    return out


def post_payment_tx(account, event, recorded_at):
    """单事务：台账追加 + 账户条件换版同时成功或同时回滚。

    - 还款流水重复：返回 (原事件, False)，不新增台账。
    - 版本冲突：抛 STALE，事务回滚，旧版账户与旧台账均保留。
    """
    with connection() as db:
        dup = db.execute(
            'SELECT body FROM loan_ledger WHERE account_id=? AND event_type=? AND event_key=?',
            (account['account_id'], 'payment', event['payment_id'])).fetchone()
        if dup is not None:
            return json.loads(dup['body']), False
        db.execute(
            'INSERT INTO loan_ledger(account_id,event_type,event_key,body,recorded_at) '
            'VALUES (?,?,?,?,?)',
            (account['account_id'], 'payment', event['payment_id'],
             json.dumps(event, ensure_ascii=False, sort_keys=True), recorded_at))
        new_account = {k: v for k, v in account.items() if k != 'version'}
        new_account['loan_interest_due'] = event['remaining_interest']
        new_account['loan_principal'] = event['remaining_principal']
        new_account['unpaid_premium'] = event['remaining_premium']
        new_version = _conditional_update(db, new_account, account['version'])
        return event, True, new_version


def replay(account_id):
    """从追加式台账重放：建档快照 + 逐笔还款，重建当前余额，验证债权可追溯。"""
    with connection() as db:
        rows = db.execute(
            'SELECT event_type,body FROM loan_ledger WHERE account_id=? ORDER BY seq',
            (account_id,)).fetchall()
    snapshot = None
    payments = []
    for r in rows:
        event = json.loads(r['body'])
        if r['event_type'] == 'account_open':
            snapshot = {k: event[k] for k in
                        ('account_id', 'policy_id', 'currency', 'cash_value_current',
                         'cash_value_retro', 'loan_principal', 'loan_interest_due',
                         'unpaid_premium')}
        elif r['event_type'] == 'payment':
            payments.append(event)
            snapshot['loan_interest_due'] = str(event['remaining_interest'])
            snapshot['loan_principal'] = str(event['remaining_principal'])
            snapshot['unpaid_premium'] = str(event['remaining_premium'])
    return snapshot, payments


def seed_done(marker):
    with connection() as db:
        return db.execute('SELECT 1 FROM loan_seed_meta WHERE key=?', (marker,)).fetchone() is not None


def mark_seeded(marker):
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO loan_seed_meta(key,value) VALUES (?,?)',
                   (marker, '1'))


def reconcile_safe(account):
    """对平检查只读，不抛异常，供页面呈现未对平状态。"""
    return reconcile_cash_value(account)


def cash_value_gate(account_id):
    """发布/入账/结案前的对平闸门：未对平不得发布。"""
    account = get_account(account_id)
    check = reconcile_cash_value(account)
    if not check['matched']:
        raise DomainError('CASH_UNRECONCILED',
                          '现金价值与追溯版本未对平（当前 %s / 追溯 %s），禁止入账与结案'
                          % (check['current'], check['retro']))
    return check


def assert_conservation(account, event):
    """换版前资金守恒校验：来款 = 息+本+抵费+退还；新余额 = 旧余额 - 冲抵额。"""
    total = (money(event['interest_paid'], 'interest_paid')
             + money(event['principal_paid'], 'principal_paid')
             + money(event['premium_offset'], 'premium_offset')
             + money(event['refund'], 'refund'))
    if total != money(event['received'], 'received'):
        raise DomainError('CONSERVATION', '还款分配合计不等于来款，拒绝换版')
    pairs = (
        ('loan_interest_due', 'interest_paid', 'remaining_interest'),
        ('loan_principal', 'principal_paid', 'remaining_principal'),
        ('unpaid_premium', 'premium_offset', 'remaining_premium'),
    )
    for old_field, paid_field, new_field in pairs:
        if money(account[old_field], old_field) - money(event[paid_field], paid_field) \
                != money(event[new_field], new_field):
            raise DomainError('CONSERVATION', '余额结转不守恒，拒绝换版: ' + old_field)
