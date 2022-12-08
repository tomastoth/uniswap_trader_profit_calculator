import pytest

from src import schemas, trade_processing
from tests import trade_utils


@pytest.fixture
def profit_calculator():
    return trade_processing.TraderProfitCalculator()


def create_buying_trade(value_usd: float, tokens_bought: float) -> schemas.TokenSwap:
    bought_spex = trade_utils.create_token(
        symbol="SPEX",
        address=trade_utils.DEFAULT_TEST_ADDRESS,
        amount=tokens_bought,
        value_usd=value_usd,
    )
    paid_usdt = trade_utils.create_token(
        symbol="USDT",
        address=trade_utils.SECOND_TEST_ADDRESS,
        amount=value_usd,
        value_usd=value_usd,
    )
    buy_trade = trade_utils.create_coin_swap_trade(
        usd_paid=value_usd,
        usd_received=value_usd,
        sold_tokens=[paid_usdt],
        bought_tokens=[bought_spex],
        transaction_hash="0x123456",
    )
    return buy_trade


def test_saving_open_trade(
    profit_calculator: trade_processing.TraderProfitCalculator,
) -> None:
    buying_eth_trade = create_buying_trade(100.0, 1.0)
    profit_calculator.receive_token_swap(buying_eth_trade)
    open_trade = profit_calculator.bought_tokens[trade_utils.DEFAULT_TEST_ADDRESS]
    token_bought = open_trade.token_bought
    assert token_bought.symbol == "SPEX"
    assert token_bought.address == trade_utils.DEFAULT_TEST_ADDRESS
    single_trade = open_trade.single_token_buys[0]
    assert single_trade.buy_time == buying_eth_trade.time
    assert single_trade.buy_price_usd == 100.0


def create_sell_trade(value_usd: float, tokens_sold: float) -> schemas.TokenSwap:
    sold_spex = trade_utils.create_token(
        symbol="SPEX",
        address=trade_utils.DEFAULT_TEST_ADDRESS,
        amount=tokens_sold,
        value_usd=value_usd,
    )
    received_usdt = trade_utils.create_token(
        symbol="USDT",
        address=trade_utils.SECOND_TEST_ADDRESS,
        amount=value_usd,
        value_usd=value_usd,
    )
    sell_trade = trade_utils.create_coin_swap_trade(
        usd_paid=value_usd,
        usd_received=value_usd,
        sold_tokens=[sold_spex],
        bought_tokens=[received_usdt],
        transaction_hash="0x123456",
    )
    return sell_trade


def test_extending_open_trade(
    profit_calculator: trade_processing.TraderProfitCalculator,
) -> None:
    first_eth_buy = create_buying_trade(100.0, 1)
    second_eth_buy = create_buying_trade(120.0, 1)
    profit_calculator.receive_token_swap(first_eth_buy)
    profit_calculator.receive_token_swap(second_eth_buy)
    spex_open_trade = profit_calculator.bought_tokens[trade_utils.DEFAULT_TEST_ADDRESS]
    assert spex_open_trade.average_buy_price_usd == pytest.approx(110.0)
    assert spex_open_trade.currently_held_amount == 2.0


def test_fully_exitting_open_trade(
    profit_calculator: trade_processing.TraderProfitCalculator,
) -> None:
    bought_token_address = trade_utils.DEFAULT_TEST_ADDRESS
    buy_trade = create_buying_trade(100.0, tokens_bought=1)
    sell_trade = create_sell_trade(100.0, tokens_sold=1)
    profit_calculator.receive_token_swap(buy_trade)
    profit_calculator.receive_token_swap(sell_trade)
    assert bought_token_address not in profit_calculator.bought_tokens.keys()
    finished_trade = profit_calculator.finished_trades[0]
    assert finished_trade.profit_usd == 0
    assert finished_trade.amount == 1


def test_exiting_more_than_we_opened(
    profit_calculator: trade_processing.TraderProfitCalculator,
) -> None:
    buy_trade = create_buying_trade(100.0, tokens_bought=1.0)
    sell_trade = create_sell_trade(120.0, tokens_sold=1.2)
    profit_calculator.receive_token_swap(buy_trade)
    profit_calculator.receive_token_swap(sell_trade)
    finished_trade = profit_calculator.finished_trades[0]
    assert finished_trade.sell_value_usd == 100.0
    assert finished_trade.profit_usd == 0.0
