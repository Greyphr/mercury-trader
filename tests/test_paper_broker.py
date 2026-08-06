from mercury.services.execution.broker import PaperBrokerAdapter


def test_paper_open_and_tp_close():
    broker = PaperBrokerAdapter(starting_balance=10_000.0, contract_size=100.0)
    assert broker.connect()
    result = broker.open_market_order(
        symbol="XAUUSD", direction="long", volume=0.1, sl=2340.0, tp=2360.0
    )
    assert result.success and result.ticket
    assert result.price == 2350.0  # default paper entry price

    settled = broker.check_exits({"XAUUSD": {"bid": 2361.0, "ask": 2361.5}})
    assert len(settled) == 1
    assert settled[0].close_reason == "tp"
    assert settled[0].pnl > 0
    assert broker.get_positions() == []


def test_paper_sl_close_loses_money():
    broker = PaperBrokerAdapter()
    broker.connect()
    broker.open_market_order(
        symbol="XAUUSD", direction="long", volume=0.1, sl=2340.0, tp=2360.0
    )
    settled = broker.check_exits({"XAUUSD": {"bid": 2339.0, "ask": 2339.5}})
    assert len(settled) == 1
    assert settled[0].close_reason == "sl"
    assert settled[0].pnl < 0


def test_paper_equity_updates_after_close():
    broker = PaperBrokerAdapter()
    broker.connect()
    broker.open_market_order(symbol="XAUUSD", direction="long", volume=0.1, sl=2340.0, tp=2360.0)
    broker.check_exits({"XAUUSD": {"bid": 2361.0, "ask": 2361.5}})
    assert broker.balance > broker.starting_balance
    assert broker.account_equity() == broker.balance
