'use strict';
const form = document.getElementById('pay-form');
const feedback = document.getElementById('feedback');
const accountId = new URLSearchParams(window.location.search).get('account_id');

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const payload = {
    payment_id: document.getElementById('payment_id').value.trim(),
    amount: document.getElementById('amount').value.trim(),
    received_at: document.getElementById('received_at').value.trim() || undefined,
  };
  if (!payload.payment_id || !payload.amount) {
    feedback.textContent = '还款流水号与金额必填';
    return;
  }
  try {
    const response = await fetch('/api/m8_loan/accounts/' + accountId + '/payments', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.message || body.code);
    feedback.textContent = (body.reposted ? '该还款已入账，返回原裁决（幂等未重复入账）' : '入账完成')
      + ' / 息' + body.event.interest_paid
      + ' 本' + body.event.principal_paid
      + ' 抵费' + body.event.premium_offset
      + ' 退' + body.event.refund;
    window.setTimeout(() => window.location.reload(), 900);
  } catch (error) {
    feedback.textContent = error.message + '，入账被拒绝';
  }
});
