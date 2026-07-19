from wca.pm.account import DEVELOPER_ADDRESS, closed_positions, read_account


class Resp:
    def __init__(self, value):
        self.value = value
    def raise_for_status(self):
        return None
    def json(self):
        return self.value


class Session:
    def __init__(self, open_rows, closed_rows):
        self.rows = {"/positions": open_rows, "/closed-positions": closed_rows}
    def get(self, url, timeout=0):
        path = "/" + url.split("/", 3)[-1].split("?", 1)[0]
        return Resp(self.rows[path])


def test_read_account_uses_proxy_and_auditable_equity():
    s = Session(
        [{"currentValue": 200.25}],
        [{"realizedPnl": 3021.75}],
    )
    d = read_account(DEVELOPER_ADDRESS, session=s, now_utc="now")
    assert d["address"].lower() == DEVELOPER_ADDRESS.lower()
    assert d["balance_usd"] == 3222.0
    assert d["open_value_usd"] == 200.25
    assert d["resolved_pnl_usd"] == 3021.75
    assert "not cash" in d["method"]


def test_closed_positions_are_projected_with_balance():
    s = Session([], [{
        "asset": "abc", "conditionId": "cond", "title": "Fixture",
        "outcome": "Yes", "avgPrice": 0.4, "totalBought": 10,
        "realizedPnl": 6.0, "timestamp": 0,
    }])
    rows = closed_positions(DEVELOPER_ADDRESS, session=s,
                            balance={"balance_usd": 3222.0, "available": True})
    assert rows[0]["currency"] == "USD"
    assert rows[0]["pl"] == 6.0
    assert rows[0]["pm_balance_usd"] == 3222.0
    assert rows[0]["pm_quarter_kelly_usd"] == 805.5
