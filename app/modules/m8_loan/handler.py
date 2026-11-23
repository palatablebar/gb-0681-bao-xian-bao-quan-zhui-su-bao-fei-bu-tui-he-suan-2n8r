from flask import Blueprint, Response, jsonify, render_template, request

from app.config import MODULES
from app.errors import DomainError
from app.modules.m8_loan import ledger, service
from app.modules.m8_loan.export_out import checksum, export_account

bp = Blueprint('m8_loan', __name__)


@bp.get('/m8_loan')
def screen():
    service.seed_if_empty()
    account_id = request.args.get('account_id')
    accounts = ledger.list_accounts()
    if account_id is None and accounts:
        account_id = accounts[0]['account_id']
    if account_id is None:
        return render_template('m8_loan.html', title='贷款还款去向裁决', short='loan',
                               modules=MODULES, current=len(MODULES) + 1,
                               data=None, accounts=[], selected='')
    view = service.account_view(account_id)
    view['sha256'] = checksum(view)
    return render_template('m8_loan.html', title='贷款还款去向裁决', short='loan',
                           modules=MODULES, current=len(MODULES) + 1,
                           data=view, accounts=accounts, selected=account_id)


@bp.get('/api/m8_loan/accounts')
def accounts():
    service.seed_if_empty()
    return jsonify(ledger.list_accounts())


@bp.get('/api/m8_loan/accounts/<account_id>')
def detail(account_id):
    return jsonify(service.account_view(account_id))


@bp.post('/api/m8_loan/accounts/<account_id>/payments')
def pay(account_id):
    data = request.get_json(silent=True) or {}
    event, inserted = service.post_repayment(account_id, data)
    return jsonify({'event': event, 'reposted': not inserted,
                    'view': service.account_view(account_id)})


@bp.get('/api/m8_loan/accounts/<account_id>/replay')
def replay(account_id):
    return jsonify(service.replay_view(account_id))


@bp.post('/api/m8_loan/accounts/<account_id>/endorsements')
def endorsement(account_id):
    data = request.get_json(silent=True) or {}
    return jsonify(service.add_endorsement(account_id, data))


@bp.get('/api/m8_loan/accounts/<account_id>/export.csv')
def download(account_id):
    view, csv_body, digest = export_account(account_id)
    return Response(
        csv_body, mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition':
                 'attachment; filename="loan-%s.csv"' % account_id,
                 'X-Evidence-SHA256': digest})


@bp.errorhandler(DomainError)
def domain_error(error):
    return jsonify(code=error.code, message=error.message), error.status
