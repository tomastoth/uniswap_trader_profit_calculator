from dataclasses import dataclass
from datetime import datetime

import eth_typing


@dataclass
class Token:
    address: eth_typing.ChecksumAddress
    symbol: str


@dataclass
class Erc20Info:
    token_address: eth_typing.ChecksumAddress
    value: float
    value_usd: float
    symbol: str


@dataclass
class Erc20Transfer(Erc20Info):
    trader_sender: bool


@dataclass
class TradedToken(Token):
    amount: float
    value_usd: float
    price_usd: float


@dataclass
class TokenSwap:
    time: datetime
    usd_paid: float
    usd_received: float
    sold_tokens: list[TradedToken]
    bought_tokens: list[TradedToken]
    transaction_hash: str


@dataclass
class SingleTokenBuy:
    buy_time: datetime
    buy_price_usd: float
    bought_token_amount: float
    transaction_hash: str
    value_usd: float
    token_bought: Token


@dataclass
class BoughtToken:
    token_bought: Token
    currently_held_amount: float
    average_buy_price_usd: float
    single_token_buys: list[SingleTokenBuy]


@dataclass
class FinishedTrade:
    token_bought: Token
    amount: float
    buy_price_usd: float
    sell_price_usd: float
    buy_value_usd: float
    sell_value_usd: float
    profit_usd: float
    sell_time: datetime
    sell_transaction: str
