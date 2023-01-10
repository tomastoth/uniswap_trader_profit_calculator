import dataclasses
import logging
from abc import ABC, abstractmethod

from src import exceptions, schemas

log = logging.getLogger(__name__)
DIFF_DIVIDER = 10.0
IGNORED_BUY_TOKEN_SYMBOLS = ["WETH", "USDT", "USDC", "DAI", "RAI"]


class TokenSwapProcessor(ABC):
    @abstractmethod
    def receive_token_swap(self, trade: schemas.TokenSwap) -> None:
        pass


class TraderProfitCalculator(TokenSwapProcessor):
    def __init__(self) -> None:
        self._finished_trades: list[schemas.FinishedTrade] = []
        self._bought_tokens: dict[str, schemas.BoughtToken] = {}

    @staticmethod
    def _create_finished_trade(
        open_trade: schemas.BoughtToken,
        token_sold: schemas.TradedToken,
        sell_trade: schemas.TokenSwap,
    ) -> schemas.FinishedTrade:
        sell_price_usd = token_sold.price_usd
        amount_owned = open_trade.currently_held_amount
        amount_sold = token_sold.amount
        if amount_sold > amount_owned:
            amount_sold = amount_owned  # TODO think of better implementation for this
            log.warning(
                f"Trade for {token_sold.symbol} would be selling more"
                f" amount than we owned, adjusting to max we owned"
            )
            token_sold.amount = amount_sold
        buy_price_usd = open_trade.average_buy_price_usd
        buy_value_usd = amount_sold * buy_price_usd
        sell_value_usd = amount_sold * sell_price_usd
        profit_usd = sell_value_usd - buy_value_usd  # TODO add fees
        return schemas.FinishedTrade(
            token_bought=open_trade.token_bought,
            buy_price_usd=buy_price_usd,
            sell_price_usd=sell_price_usd,
            buy_value_usd=buy_value_usd,
            sell_value_usd=sell_value_usd,
            profit_usd=profit_usd,
            amount=amount_sold,
            sell_time=sell_trade.time,
            sell_transaction=sell_trade.transaction_hash,
        )

    def _adjust_bought_token(
        self, bought_token: schemas.BoughtToken, new_token_sold: schemas.TradedToken
    ) -> None:
        adjusted_bought_token = dataclasses.replace(bought_token)
        bought_token_address = bought_token.token_bought.address
        assert new_token_sold.amount <= bought_token.currently_held_amount
        new_token_amount = bought_token.currently_held_amount - new_token_sold.amount
        adjusted_bought_token.currently_held_amount = new_token_amount
        if new_token_amount > 0:
            self._bought_tokens[bought_token_address] = adjusted_bought_token
        else:
            del self._bought_tokens[bought_token_address]

    def _check_closing_trade(
        self, new_trade: schemas.TokenSwap
    ) -> schemas.FinishedTrade | None:
        new_tokens_sold = new_trade.sold_tokens
        for new_token_sold in new_tokens_sold:
            for token_address, bought_token in self._bought_tokens.items():
                if new_token_sold.address == token_address:
                    finished_trade = self._create_finished_trade(
                        bought_token, new_token_sold, new_trade
                    )
                    self._adjust_bought_token(bought_token, new_token_sold)
                    return finished_trade
        return None

    @classmethod
    def _calculate_new_average_price(
        cls,
        current_amount: float,
        current_price: float,
        new_amount: float,
        new_price: float,
    ) -> float:
        return ((current_price * current_amount) + (new_price * new_amount)) / (
            current_amount + new_amount
        )

    def _extend_bought_token(
        self, new_single_token_buy: schemas.SingleTokenBuy
    ) -> None:
        token_address = new_single_token_buy.token_bought.address
        current_bought_token = self._bought_tokens[token_address]
        new_quantity = (
            current_bought_token.currently_held_amount
            + new_single_token_buy.bought_token_amount
        )
        new_average_price = self._calculate_new_average_price(
            current_bought_token.currently_held_amount,
            current_bought_token.average_buy_price_usd,
            new_single_token_buy.bought_token_amount,
            new_single_token_buy.buy_price_usd,
        )
        current_bought_token.single_token_buys.append(new_single_token_buy)
        current_bought_token.average_buy_price_usd = new_average_price
        current_bought_token.currently_held_amount = new_quantity

    def _create_bought_token(
        self, new_single_token_buy: schemas.SingleTokenBuy
    ) -> schemas.BoughtToken:
        return schemas.BoughtToken(
            token_bought=new_single_token_buy.token_bought,
            currently_held_amount=new_single_token_buy.bought_token_amount,
            average_buy_price_usd=new_single_token_buy.buy_price_usd,
            single_token_buys=[new_single_token_buy],
        )

    def receive_token_swap(self, token_swap: schemas.TokenSwap) -> None:
        finished_trade = self._check_closing_trade(token_swap)
        if finished_trade:
            self._finished_trades.append(finished_trade)
        else:
            new_single_token_buys = (
                TraderProfitCalculator._convert_token_swap_to_single_token_buy(
                    token_swap
                )
            )
            for new_single_token_buy in new_single_token_buys:
                token_bought_address = new_single_token_buy.token_bought.address
                if token_bought_address in self._bought_tokens:
                    self._extend_bought_token(new_single_token_buy)
                else:
                    new_bought_token = self._create_bought_token(new_single_token_buy)
                    self._bought_tokens[token_bought_address] = new_bought_token

    @staticmethod
    def _are_numbers_equal(
        num_1: float, num_2: float, diff_divider: float = DIFF_DIVIDER
    ) -> bool:
        """
        :param num_1:  number to compare with second number
        :param num_2:  number to compare with first number
        :param diff_divider:  divider to divide each number by
        :return: whether numbers are close enough
        """
        div_num_1 = num_1 / diff_divider
        div_num_2 = num_2 / diff_divider
        max_difference = sum([div_num_1, div_num_2]) / 2.0
        return abs(div_num_1 - div_num_2) < max_difference

    @classmethod
    def _create_single_token_buy(
        cls,
        token_swap: schemas.TokenSwap,
        bought_token: schemas.TradedToken,
        transaction_hash: str,
    ) -> schemas.SingleTokenBuy:
        for token_paid in token_swap.sold_tokens:
            if cls._are_numbers_equal(bought_token.value_usd, token_paid.value_usd):
                bought_token_price = bought_token.value_usd / bought_token.amount
                return schemas.SingleTokenBuy(
                    buy_time=token_swap.time,
                    buy_price_usd=bought_token_price,
                    bought_token_amount=bought_token.amount,
                    transaction_hash=transaction_hash,
                    value_usd=bought_token.value_usd,
                    token_bought=bought_token,
                )
        raise exceptions.UnassignedTradedTokenError()

    @classmethod
    def _filter_out_ignored_token_trades(
        cls, new_open_trades: list[schemas.SingleTokenBuy]
    ) -> list[schemas.SingleTokenBuy]:
        return [
            trade
            for trade in new_open_trades
            if trade.token_bought.symbol not in IGNORED_BUY_TOKEN_SYMBOLS
        ]

    @classmethod
    def _convert_token_swap_to_single_token_buy(
        cls, token_swap: schemas.TokenSwap
    ) -> list[schemas.SingleTokenBuy]:
        new_single_token_buys: list[schemas.SingleTokenBuy] = []
        for bought_token in token_swap.bought_tokens:
            try:
                single_token_buy = cls._create_single_token_buy(
                    token_swap, bought_token, token_swap.transaction_hash
                )
            except exceptions.UnassignedTradedTokenError as e:
                log.warning(f"Could not assign one token in token swap, {e}")
                continue
            new_single_token_buys.append(single_token_buy)
        filtered_new_single_token_buys = cls._filter_out_ignored_token_trades(
            new_single_token_buys
        )
        return filtered_new_single_token_buys

    @property
    def finished_trades(self) -> list[schemas.FinishedTrade]:
        return self._finished_trades

    @property
    def bought_tokens(self) -> dict[str, schemas.BoughtToken]:
        return self._bought_tokens
