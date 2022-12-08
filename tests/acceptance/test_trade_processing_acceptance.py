import dataclasses
from datetime import datetime

import pytest

from src import schemas, trade_processing
from tests import trade_utils


def test_processing_trade() -> None:
    # first we buy ETH for USDT
    bought_spex = trade_utils.create_token(
        symbol="SPEX",
        address=trade_utils.DEFAULT_TEST_ADDRESS,
        amount=1,
        value_usd=100.0,
    )
    paid_usdt = trade_utils.create_token(
        symbol="USDT",
        address=trade_utils.SECOND_TEST_ADDRESS,
        amount=100.0,
        value_usd=100.0,
    )
    buying_spec_trade = trade_utils.create_coin_swap_trade(
        usd_paid=100.0,
        usd_received=100.0,
        sold_tokens=[paid_usdt],
        bought_tokens=[bought_spex],
        transaction_hash="0x123456",
    )

    # second we sell ETH for USD
    sold_spex = dataclasses.replace(bought_spex)
    sold_spex.value_usd = 120.0
    sold_spex.price_usd = 120.0
    bought_usdt = dataclasses.replace(paid_usdt)
    bought_usdt.value_usd = 120.0
    selling_spex_trade = trade_utils.create_coin_swap_trade(
        usd_paid=120.0,
        usd_received=120.0,
        sold_tokens=[sold_spex],
        bought_tokens=[bought_usdt],
        time=datetime(2022, 1, 1, 1, 1, 2),
        transaction_hash="0x123457",
    )
    profit_calculator = trade_processing.TraderProfitCalculator()
    profit_calculator.receive_token_swap(buying_spec_trade)
    profit_calculator.receive_token_swap(selling_spex_trade)
    finished_trades: list[schemas.FinishedTrade] = profit_calculator.finished_trades
    finished_trade = finished_trades[0]
    token_bought = finished_trade.token_bought
    assert token_bought.symbol == "SPEX"
    assert finished_trade.profit_usd == pytest.approx(20.0)
    assert finished_trade.amount == 1
    assert token_bought.address == trade_utils.DEFAULT_TEST_ADDRESS
    assert finished_trade.buy_value_usd == 100.0
    assert finished_trade.sell_value_usd == 120.0
    assert finished_trade.sell_time == selling_spex_trade.time

    # we want to determine trade profit
