import csv
import hashlib
import io

from app.errors import DomainError
from app.modules.m8_loan import service

EXPORT_HEADER = [
    '账户', '保单', '还款流水', '到账金额', '冲抵欠息', '压减本金', '抵作保费',
    '退还客户', '剩余欠息', '剩余本金', '欠交保费', '贷款状态', '对平', '结案状态',
]


def _rows(view):
    account = view['account']
    check = view['cash_value_check']
    payments = view['payments']
    if not payments:
        yield [account['account_id'], account['policy_id'], '', account['loan_interest_due'],
               '', '', '', '', account['loan_interest_due'], account['loan_principal'],
               account['unpaid_premium'], view['loan_status_label'],
               '是' if check['matched'] else '否', '不可结案']
        return
    for event in payments:
        closed_at_event = event['loan_status'] in ('CLOSED_REFUND', 'CLOSED_EVEN')
        yield [
            account['account_id'], account['policy_id'], event['payment_id'],
            event['received'], event['interest_paid'], event['principal_paid'],
            event['premium_offset'], event['refund'],
            event['remaining_interest'], event['remaining_principal'],
            event['remaining_premium'], event['loan_status_label'],
            '是' if check['matched'] else '否',
            '可结案' if closed_at_event else '不可结案',
        ]


def bundle(view):
    """结案 CSV：数字直接取自台账事件，复核页与下载页因此永远是同一份本息。"""
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(EXPORT_HEADER)
    for row in _rows(view):
        writer.writerow(row)
    return '﻿' + out.getvalue()


def checksum(view):
    return hashlib.sha256(bundle(view).encode('utf-8')).hexdigest()


def export_account(account_id):
    """结案下载闸门：对平未过或贷款未清，一律拒绝发布。"""
    view = service.account_view(account_id)
    if not view['cash_value_check']['matched']:
        raise DomainError('CASH_UNRECONCILED', '现金价值与追溯版本未对平，禁止结案下载')
    if not view['closable']:
        raise DomainError('NOT_CLOSABLE',
                          '贷款状态为「%s」，不可结案下载' % view['loan_status_label'])
    return view, bundle(view), checksum(view)
