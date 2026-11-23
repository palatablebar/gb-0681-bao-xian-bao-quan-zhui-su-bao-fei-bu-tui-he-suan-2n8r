import csv
import io

import pytest

NEW_ACCOUNT = {
    'account_id': 'L-HTTP', 'policy_id': 'P-HTTP', 'currency': 'CNY',
    'cash_value_current': '11800.00', 'cash_value_retro': '11800.00',
    'loan_principal': '12000.00', 'loan_interest_due': '860.50',
    'unpaid_premium': '1380.00',
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / 'test.db')
    monkeypatch.setenv('DB_PATH', db_path)
    from app.main import create_app
    from app.modules.m8_loan import ledger, service
    ledger.DB_PATH = db_path
    app = create_app()
    app.config['TESTING'] = True
    c = app.test_client()
    service.open_account(dict(NEW_ACCOUNT))
    return c


def test_loan_page_and_nav(client):
    page = client.get('/m8_loan')
    assert page.status_code == 200
    assert '贷款还款裁决' in page.data.decode()
    # 主导航对既有 7 模块页面仍可用
    assert client.get('/m6_reconcile').status_code == 200


def test_seeded_accounts_visible(client):
    rows = client.get('/api/m8_loan/accounts').get_json()
    ids = {r['account_id'] for r in rows}
    assert {'L-P002', 'L-P009', 'L-HTTP'} <= ids


def test_seeded_waterfall_numbers(client):
    view = client.get('/api/m8_loan/accounts/L-P002').get_json()
    first, second = view['payments']
    assert (first['interest_paid'], first['principal_paid'],
            first['premium_offset'], first['refund']) == ('860.50', '2139.50', '0.00', '0.00')
    assert (second['interest_paid'], second['principal_paid'],
            second['premium_offset'], second['refund']) == ('0.00', '9860.50', '1380.00', '180.00')
    assert view['closable'] is True


def test_repost_is_idempotent_over_http(client):
    payload = {'payment_id': 'PAY-HTTP-1', 'amount': '500.00'}
    a = client.post('/api/m8_loan/accounts/L-HTTP/payments', json=payload)
    assert a.status_code == 200 and a.get_json()['reposted'] is False
    b = client.post('/api/m8_loan/accounts/L-HTTP/payments', json=payload)
    assert b.status_code == 200 and b.get_json()['reposted'] is True
    view = client.get('/api/m8_loan/accounts/L-HTTP').get_json()
    assert len([p for p in view['payments'] if p['payment_id'] == 'PAY-HTTP-1']) == 1


def test_unreconciled_account_blocked(client):
    r = client.post('/api/m8_loan/accounts/L-P009/payments',
                    json={'payment_id': 'PAY-X', 'amount': '100.00'})
    assert r.status_code == 422 and r.get_json()['code'] == 'CASH_UNRECONCILED'
    r = client.get('/api/m8_loan/accounts/L-P009/export.csv')
    assert r.status_code == 422 and r.get_json()['code'] == 'CASH_UNRECONCILED'


def test_full_repayment_then_export_single_source(client):
    client.post('/api/m8_loan/accounts/L-HTTP/payments',
                json={'payment_id': 'PAY-A', 'amount': '3000.00'})
    client.post('/api/m8_loan/accounts/L-HTTP/payments',
                json={'payment_id': 'PAY-B', 'amount': '11420.50'})
    view = client.get('/api/m8_loan/accounts/L-HTTP').get_json()
    assert view['closable'] is True
    r = client.get('/api/m8_loan/accounts/L-HTTP/export.csv')
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.data.decode('utf-8-sig'))))
    assert rows[0][:8] == ['账户', '保单', '还款流水', '到账金额', '冲抵欠息',
                           '压减本金', '抵作保费', '退还客户']
    assert rows[1][-1] == '不可结案'  # 第一笔部分还款
    last = rows[-1]
    assert last[2] == 'PAY-B' and last[7] == '180.00' and last[-1] == '可结案'
    assert len(r.headers['X-Evidence-SHA256']) == 64


def test_export_blocked_while_loan_open(client):
    client.post('/api/m8_loan/accounts/L-HTTP/payments',
                json={'payment_id': 'PAY-A', 'amount': '3000.00'})
    r = client.get('/api/m8_loan/accounts/L-HTTP/export.csv')
    assert r.status_code == 422 and r.get_json()['code'] == 'NOT_CLOSABLE'


def test_replay_endpoint(client):
    client.post('/api/m8_loan/accounts/L-HTTP/payments',
                json={'payment_id': 'PAY-A', 'amount': '3000.00'})
    r = client.get('/api/m8_loan/accounts/L-HTTP/replay')
    assert r.status_code == 200
    body = r.get_json()
    assert body['consistent'] is True and body['payment_count'] == 1


def test_unknown_account_404(client):
    assert client.get('/api/m8_loan/accounts/NOPE').status_code == 404


def test_endorsement_duplicate_rejected(client):
    body = {'endorsement_id': 'E-P002-RETRO-01', 'kind': 'retro_reduce',
            'effective': '2026-10-01'}
    r = client.post('/api/m8_loan/accounts/L-P002/endorsements', json=body)
    # 种子已含同号批改：重复提交被 409 拒绝，原记录保留
    assert r.status_code == 409 and r.get_json()['code'] == 'DUPLICATE_EVENT'


def test_endorsement_append_only(client):
    body = {'endorsement_id': 'E-HTTP-1', 'kind': 'retro_reduce',
            'effective': '2026-10-01', 'note': '只追加'}
    r = client.post('/api/m8_loan/accounts/L-HTTP/endorsements', json=body)
    assert r.status_code == 200
    view = client.get('/api/m8_loan/accounts/L-HTTP').get_json()
    assert any(e['endorsement_id'] == 'E-HTTP-1' for e in view['endorsements'])
