from datetime import datetime

import eth_typing
from web3 import Web3

from src import schemas

DEFAULT_TEST_ADDRESS = Web3.toChecksumAddress(
    "0x0000000000000000000000000000000000000000"
)
SECOND_TEST_ADDRESS = Web3.toChecksumAddress(
    "0x1111111111111111111111111111111111111111"
)


def create_coin_swap_trade(
    usd_paid: float,
    usd_received: float,
    sold_tokens: list[schemas.TradedToken],
    bought_tokens: list[schemas.TradedToken],
    transaction_hash: str,
    time: datetime = datetime(2022, 1, 1, 1, 1, 1),
) -> schemas.TokenSwap:
    return schemas.TokenSwap(
        time=time,
        usd_paid=usd_paid,
        usd_received=usd_received,
        sold_tokens=sold_tokens,
        bought_tokens=bought_tokens,
        transaction_hash=transaction_hash,
    )


def create_token(
    symbol: str = "SPEX",
    address: eth_typing.ChecksumAddress = DEFAULT_TEST_ADDRESS,
    amount: float = 1.0,
    value_usd: float = 100.0,
) -> schemas.TradedToken:
    price_usd = value_usd / amount
    return schemas.TradedToken(
        address=address,
        symbol=symbol,
        amount=amount,
        value_usd=value_usd,
        price_usd=price_usd,
    )
