"""The CLI must preserve the library's settlement verdict for automation."""

import json
from types import SimpleNamespace

import pytest

from nano_pay import cli


@pytest.fixture
def command(monkeypatch):
    class Wallet:
        warmed = 0

        def load(self):
            return self

        def exists(self):
            return False

        def synced_account(self, rpc):
            return object()

        def _work_root(self, account):
            return "root"

        def prework(self, root, rpc):
            Wallet.warmed += 1

    monkeypatch.setattr(cli, "Wallet", Wallet)
    monkeypatch.setattr(cli, "RPC", lambda: object())
    args = SimpleNamespace(url="https://merchant.example/premium", json=None,
                           header=[], method="GET", max_xno="0.05",
                           body_only=False)

    def run(status, receipt, *, dry_run=False, body_only=False):
        response = SimpleNamespace(status_code=status, text='{"answer":42}',
                                   json=lambda: {"answer": 42})
        monkeypatch.setattr(cli, "request_with_payment",
                            lambda *a, **kw: (response, receipt))
        args.body_only = body_only
        with pytest.raises(SystemExit) as stopped:
            cli._do_request(args, dry_run=dry_run)
        return stopped.value.code, Wallet.warmed

    return run


@pytest.mark.parametrize("status,settled", [
    (402, False), (402, "indeterminate"), (500, True),
    (500, "indeterminate"), (200, "indeterminate"),
])
def test_failed_or_uncertain_paid_call_is_not_cli_success(command, capsys, status, settled):
    receipt = {"settled": settled, "block": "AB" * 32, "ledger": "absent"}
    code, warmed = command(status, receipt)
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert code == 1
    assert result["paid"] == settled
    assert result["payment"] == receipt
    assert warmed == 0
    assert "payment settled." not in captured.err
    assert receipt["block"] in captured.err


def test_body_only_failure_keeps_recovery_hash_on_stderr(command, capsys):
    receipt = {"settled": "indeterminate", "block": "CD" * 32}
    code, warmed = command(503, receipt, body_only=True)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"answer": 42}
    assert code == 1
    assert warmed == 0
    assert receipt["block"] in captured.err


def test_served_settled_payment_still_succeeds_and_warms_work(command, capsys):
    code, warmed = command(200, {"settled": True, "block": "EF" * 32})
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["paid"] is True
    assert warmed == 1
    assert "payment settled." in captured.err


@pytest.mark.parametrize("status,code", [(200, 0), (404, 1), (500, 1)])
def test_unpaid_response_exit_status_matches_http(command, capsys, status, code):
    actual, warmed = command(status, None)
    assert actual == code
    assert json.loads(capsys.readouterr().out)["paid"] is False
    assert warmed == 0


def test_quote_402_remains_successful_without_payment(command, capsys):
    code, warmed = command(402, {"amount_xno": "0.001"}, dry_run=True)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["paid"] is False
    assert warmed == 0
