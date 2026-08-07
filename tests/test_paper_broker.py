import pytest

from mercury.services.execution.broker import PaperBrokerAdapter


def test_paper_open_and_tp_close():
    broker = PaperBrokerAdapter(starting_balance=10_000.0, contract_size=100.0)
    assert broker.connect()
    broker.update_prices({"XAUUSD": {"bid": 2399.0, "ask": 2401.0}})
    result = broker.open_market_order(
        symbol="XAUUSD", direction="long", volume=0.1, sl=2390.0, tp=2410.0
    )
    assert result.success and result.ticket
    assert result.price == 2400.0  # mid of the supplied live quote, not a hardcoded default

    settled = broker.check_exits({"XAUUSD": {"bid": 2411.0, "ask": 2411.5}})
    assert len(settled) == 1
    assert settled[0].close_reason == "tp"
    assert settled[0].pnl > 0
    assert broker.get_positions() == []


def test_paper_sl_close_loses_money():
    broker = PaperBrokerAdapter()
    broker.connect()
    broker.update_prices({"XAUUSD": {"bid": 2399.0, "ask": 2401.0}})
    broker.open_market_order(
        symbol="XAUUSD", direction="long", volume=0.1, sl=2390.0, tp=2410.0
    )
    settled = broker.check_exits({"XAUUSD": {"bid": 2389.0, "ask": 2389.5}})
    assert len(settled) == 1
    assert settled[0].close_reason == "sl"
    assert settled[0].pnl < 0


def test_paper_equity_updates_after_close():
    broker = PaperBrokerAdapter()
    broker.connect()
    broker.update_prices({"XAUUSD": {"bid": 2399.0, "ask": 2401.0}})
    broker.open_market_order(symbol="XAUUSD", direction="long", volume=0.1, sl=2390.0, tp=2410.0)
    broker.check_exits({"XAUUSD": {"bid": 2411.0, "ask": 2411.5}})
    assert broker.balance > broker.starting_balance
    assert broker.account_equity() == broker.balance


def test_paper_equity_marks_to_market_from_live_quotes():
    broker = PaperBrokerAdapter()
    broker.connect()
    broker.update_prices({"XAUUSD": {"bid": 2399.0, "ask": 2401.0}})
    broker.open_market_order(symbol="XAUUSD", direction="long", volume=0.1, sl=2390.0, tp=2410.0)
    broker.update_prices({"XAUUSD": {"bid": 2409.0, "ask": 2411.0}})
    assert broker.account_equity() == pytest.approx(broker.balance + 100.0)


def test_paper_order_fails_loudly_without_quote():
    broker = PaperBrokerAdapter()
    broker.connect()
    result = broker.open_market_order(
        symbol="XAUUSD", direction="long", volume=0.1, sl=2390.0, tp=2410.0
    )
    assert not result.success
    assert "no quote" in result.error
    assert broker.get_positions() == []


def test_paper_equity_none_when_position_has_no_quote():
    broker = PaperBrokerAdapter()
    broker.connect()
    broker.update_prices({"XAUUSD": {"bid": 2399.0, "ask": 2401.0}})
    broker.open_market_order(symbol="XAUUSD", direction="long", volume=0.1, sl=2390.0, tp=2410.0)
    broker.update_prices({})
    assert broker.account_equity() is None
