from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.errors import DomainError

CENT = Decimal('0.01')
# 唯一去向裁决（《民法典》第五百六十一条）：
# 还款依次冲抵 应付利息 → 贷款本金 → 欠交保费；仍有剩余方可退还客户。
# 禁止"先本后息"——同一笔还款只允许一套本息、抵费与退还数字。
WATERFALL = ('interest', 'principal', 'premium', 'refund')
STEP_LABELS = {
    'interest': '结清欠息',
    'principal': '压减贷款本金',
    'premium': '抵作欠交保费',
    'refund': '退还客户',
}

MONEY_FIELDS = ('cash_value_current', 'cash_value_retro', 'loan_principal',
                'loan_interest_due', 'unpaid_premium', 'amount')


def money(value, field):
    text = str(value).strip().replace(',', '')
    try:
        result = Decimal(text).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, AttributeError):
        raise ValueError('金额无效: %s=%r' % (field, value))
    if not result.is_finite() or result < 0:
        raise ValueError('金额不能为负或非有限值: %s=%r' % (field, value))
    return result


def reconcile_cash_value(account):
    """现金价值与追溯版本对平检查：未对平不得入账、不得结案下载。"""
    current = money(account['cash_value_current'], 'cash_value_current')
    retro = money(account['cash_value_retro'], 'cash_value_retro')
    return {
        'current': str(current),
        'retro': str(retro),
        'matched': current == retro,
        'below_loan': current < money(account['loan_principal'], 'loan_principal')
                                  + money(account['loan_interest_due'], 'loan_interest_due'),
    }


def validate_account(account):
    missing = [f for f in ('account_id', 'policy_id') if not str(account.get(f, '')).strip()]
    if missing:
        raise DomainError('MISSING', '账户缺少必填字段: %s' % ','.join(missing))
    for field in MONEY_FIELDS:
        if field == 'amount':
            continue
        money(account[field], field)
    recon = reconcile_cash_value(account)
    if not recon['matched']:
        raise DomainError('CASH_UNRECONCILED',
                          '现金价值与追溯版本未对平（当前 %s / 追溯 %s）'
                          % (recon['current'], recon['retro']))
    return account


def _take(payment, remaining):
    used = min(payment, remaining) if remaining > 0 else Decimal('0.00')
    return used, payment - used


def allocate(amount, interest_due, principal, unpaid_premium):
    """固定瀑布分摊。返回每一档用途与冲抵后余额，资金守恒由测试锁定。"""
    payment = money(amount, 'amount')
    buckets = {
        'interest': money(interest_due, 'loan_interest_due'),
        'principal': money(principal, 'loan_principal'),
        'premium': money(unpaid_premium, 'unpaid_premium'),
    }
    if payment <= 0:
        raise DomainError('MISSING', '还款金额必须大于零')
    leftover = payment
    applied = {}
    for step in WATERFALL[:-1]:
        used, leftover = _take(leftover, buckets[step])
        applied[step] = used
        buckets[step] -= used
    applied['refund'] = leftover
    # 资金守恒：各档分配合计必须等于来款。
    if sum(applied.values(), Decimal('0.00')) != payment:
        raise DomainError('CONSERVATION', '还款分配合计不等于来款')
    return {
        'received': payment,
        'applied': {step: applied[step] for step in WATERFALL},
        'remaining_interest': buckets['interest'],
        'remaining_principal': buckets['principal'],
        'remaining_premium': buckets['premium'],
        'refund': applied['refund'],
    }


def loan_status(remaining_interest, remaining_principal, remaining_premium, refund):
    loan_open = remaining_interest > 0 or remaining_principal > 0
    if loan_open:
        return 'LOAN_OPEN', '贷款未清，不可结案'
    if remaining_premium > 0:
        return 'PREMIUM_DUE', '贷款已清，保费仍欠'
    if refund > 0:
        return 'CLOSED_REFUND', '已结清，应退还客户'
    return 'CLOSED_EVEN', '已结清，无退款'


def adjudicate(account, payment):
    """对一笔还款作出唯一去向裁决，并给出冲抵后的完整快照。"""
    validate_account(account)
    result = allocate(payment['amount'], account['loan_interest_due'],
                      account['loan_principal'], account['unpaid_premium'])
    code, label = loan_status(result['remaining_interest'], result['remaining_principal'],
                              result['remaining_premium'], result['refund'])
    return {
        'account_id': account['account_id'],
        'payment_id': payment['payment_id'],
        'received_at': payment['received_at'],
        'received': str(result['received']),
        'waterfall': [
            {'step': step, 'label': STEP_LABELS[step], 'amount': str(result['applied'][step])}
            for step in WATERFALL
        ],
        'interest_paid': str(result['applied']['interest']),
        'principal_paid': str(result['applied']['principal']),
        'premium_offset': str(result['applied']['premium']),
        'refund': str(result['refund']),
        'remaining_interest': str(result['remaining_interest']),
        'remaining_principal': str(result['remaining_principal']),
        'remaining_premium': str(result['remaining_premium']),
        'loan_status': code,
        'loan_status_label': label,
        'cash_value_check': reconcile_cash_value(account),
    }


def naive_principal_first(amount, interest_due, principal, unpaid_premium):
    """错误对照：财务主张的'先本后息'，仅用于测试证明两种做法结果不同。"""
    payment = money(amount, 'amount')
    p = money(principal, 'loan_principal')
    i = money(interest_due, 'loan_interest_due')
    u = money(unpaid_premium, 'unpaid_premium')
    to_principal, left = _take(payment, p)
    p -= to_principal
    to_interest, left = _take(left, i)
    i -= to_interest
    to_premium, left = _take(left, u)
    u -= to_premium
    return {
        'interest_paid': to_interest, 'principal_paid': to_principal,
        'premium_offset': to_premium, 'refund': left,
        'remaining_interest': i, 'remaining_principal': p, 'remaining_premium': u,
    }
