import math
from datetime import datetime

import dotenv
import eth_typing

import spec
import trade_processing

dotenv.load_dotenv()
import logging

from crypto_utils import exceptions, http_utils, price, w3
from crypto_utils.config import config
from crypto_utils import exceptions as utils_exceptions
from web3 import Web3
from web3 import exceptions as web3_exceptions

from src import schemas

log = logging.getLogger(__name__)

COVALENT_API_KEY = config.COVALENT_KEY
COVALENT_URL = "https://api.covalenthq.com/v1/1"
ETH_CHAIN_ID = 1


def request_transactions(
        address: eth_typing.ChecksumAddress, page_size: int = 10, page: int = 1
) -> any:
    url = (
        f"{COVALENT_URL}/address/{address}/transactions_v2/"
    )
    params = {
        "quote-currency": "USD",
        "format":" JSON",
        "block-signed-at-asc": True,
        "no-logs": False,
        "page-number": page,
        "page-size": page_size,
        "key": COVALENT_API_KEY
    }
    transactions_json = http_utils.request(url, params)
    return transactions_json


def _extract_swapped_token(erc20_token: schemas.Erc20Info) -> schemas.TradedToken:
    price_usd = erc20_token.value_usd / erc20_token.value
    return schemas.TradedToken(
        address=erc20_token.token_address,
        amount=erc20_token.value,
        value_usd=erc20_token.value_usd,
        symbol=erc20_token.symbol,
        price_usd=price_usd,
    )


def _extract_token_swap(
        sent_transfers: list[schemas.Erc20Transfer],
        received_transfers: list[schemas.Erc20Transfer],
        deposits: list[schemas.Erc20Info],
        withdrawals: list[schemas.Erc20Info],
        block_time: datetime,
        transaction_hash: str,
) -> schemas.TokenSwap or None:
    sold_tokens: list[schemas.TradedToken] = []
    bought_tokens: list[schemas.TradedToken] = []
    usd_paid = 0.0
    usd_received = 0.0
    for sent_transfer in sent_transfers:
        sold_token = _extract_swapped_token(sent_transfer)
        usd_paid += sold_token.value_usd
        sold_tokens.append(sold_token)
    for received_transfer in received_transfers:
        bought_token = _extract_swapped_token(received_transfer)
        usd_received += bought_token.value_usd
        bought_tokens.append(bought_token)
    for deposit in deposits:
        usd_paid += deposit.value_usd
        sold_token = _extract_swapped_token(deposit)
        sold_tokens.append(sold_token)
    for withrawal in withdrawals:
        usd_received += withrawal.value_usd
        bought_token = _extract_swapped_token(withrawal)
        bought_tokens.append(bought_token)

    block_datetime = block_time
    if not usd_paid:
        return None
    return schemas.TokenSwap(
        block_datetime,
        usd_paid,
        usd_received,
        sold_tokens,
        bought_tokens,
        transaction_hash=transaction_hash,
    )


def _extract_transfer(
        trader_address: eth_typing.ChecksumAddress,
        decoded: dict[any, any],
        token_address: eth_typing.ChecksumAddress,
        transaction_usd_value: float,
) -> schemas.Erc20Transfer | None:
    params = decoded["params"]
    address_from, address_to, transferred_value = _extract_transfer_params(params)
    if not all([address_from, bool(address_to), bool(transferred_value)]):
        raise exceptions.MissingDataError()
    trader_sender = address_from.lower() == trader_address.lower()
    trader_receiver = address_to.lower() == trader_address.lower()
    if (not trader_receiver) and (not trader_sender):
        # we can have transfer that is from other contract to other contract
        return None
    is_trader_sender = False
    if trader_sender and not trader_receiver:
        is_trader_sender = True
    token_info = w3.get_erc20_info(token_address)
    token_decimals = token_info.decimals
    token_value_divided = transferred_value / 10 ** token_decimals
    try:
        token_price = transaction_usd_value / token_value_divided
    except exceptions.CantFindTokenPriceError as e:
        log.warning(e)
        return None
    except exceptions.MissingDataError as missing_data_error:
        log.warning(f"Missing transfer data, skipping swap {missing_data_error}")
        return None
    token_value_usd = token_price * token_value_divided
    return schemas.Erc20Transfer(
        token_address=token_address,
        value=token_value_divided,
        value_usd=token_value_usd,
        trader_sender=is_trader_sender,
        symbol=token_info.symbol,
    )


def _extract_transfer_params(params):
    address_from = None
    address_to = None
    transferred_value = None
    for param in params:
        name = param["name"]
        value = param["value"]
        match name:
            case "from":
                address_from = value
            case "to":
                address_to = value
            case "value":
                transferred_value = float(value)
    return address_from, address_to, transferred_value


def _extract_deposit_or_withdraw(
        decoded: dict[any, any],
        token_address: eth_typing.ChecksumAddress,
        price_provider: price.CexPriceProvider,
        block_time: datetime,
) -> schemas.Erc20Info | None:
    params = decoded["params"]
    if not params:
        return None
    value_deposited = None
    for param in params:
        name = param["name"]
        value = param["value"]
        if name == "wad":
            value_deposited = value
            break
    coin_info = w3.get_erc20_info(token_address)
    value_divided = float(value_deposited) / 10 ** coin_info.decimals
    price = price_provider.get_price_of_token(
        symbol="ETH", at_time=block_time
    )
    value_usd = value_divided * price
    return schemas.Erc20Info(
        token_address=token_address,
        value=value_divided,
        value_usd=value_usd,
        symbol=coin_info.symbol,
    )


def extract_single_transaction_swap(
        item: dict[str, any],
        transaction_price_provider: price.TransactionValueUsdProvider,
        price_provider: price.CexPriceProvider,
        trader_address: spec.Address,
) -> schemas.TokenSwap | None:
    log_events = item["log_events"]
    sent_transfers: list[schemas.Erc20Transfer] = []
    received_transfers: list[schemas.Erc20Transfer] = []
    deposits: list[schemas.Erc20Info] = []
    withdrawals: list[schemas.Erc20Info] = []
    block_time =  datetime.strptime(item["block_signed_at"], "%Y-%m-%dT%H:%M:%SZ")

    tx_hash = item["tx_hash"]
    for log_event in log_events:
        decoded = log_event["decoded"]
        if not decoded:
            return None
        event_name = decoded["name"]
        token_address = Web3.toChecksumAddress(log_event["sender_address"])
        if event_name == "Transfer":
            try:
                transaction_usd_value = transaction_price_provider.get_usd_value_of_transaction(tx_hash)
            except utils_exceptions.CantExtractUsdValueError:
                return None  # TODO add support for non uniswap trades such as Sushiswap
            transfer = _extract_transfer(
                trader_address, decoded, token_address, transaction_usd_value
            )
            if transfer:
                if transfer.trader_sender:
                    sent_transfers.append(transfer)
                else:
                    received_transfers.append(transfer)
        elif event_name == "Deposit":
            deposit = _extract_deposit_or_withdraw(
                decoded, token_address, price_provider, block_time
            )
            if deposit:
                deposits.append(deposit)
        elif event_name == "Withdrawal":
            withdrawal = _extract_deposit_or_withdraw(
                decoded, token_address, price_provider, block_time
            )
            if withdrawal:
                withdrawals.append(withdrawal)
    return _extract_token_swap(
        sent_transfers, received_transfers, deposits, withdrawals, block_time, tx_hash
    )


def _extract_token_swaps(
        tx_json: dict[any, any],
        trader_address: eth_typing.ChecksumAddress,
        price_provider: price.CexPriceProvider= price.BinancePriceProvider(),
        transaction_price_provider: price.TransactionValueUsdProvider = price.UniswapTransactionValueUsdProvider()
):
    token_swaps: list[schemas.TokenSwap] = []
    tx_data = tx_json["data"]
    if not tx_data:
        raise exceptions.MissingDataError()
    tx_items = tx_data["items"]
    if not tx_items:
        raise exceptions.MissingDataError()
    for item in tx_items:
        try:
            token_swap = extract_single_transaction_swap(
                item, transaction_price_provider, price_provider, trader_address
            )
            if token_swap:
                token_swaps.append(token_swap)
        except exceptions.CantFindTokenPriceError:
            log.warning(f"Could not extract price for trade, ignoring swap {item}")
        except exceptions.MissingDataError as missing_data_error:
            log.warning(
                f"There is missing data error {missing_data_error}, ignoring item: {item}"
            )
        except web3_exceptions.ContractLogicError:
            log.warning(
                f"Contract call reverted, ignoring item: {item}"
            )

    return token_swaps


if __name__ == "__main__":
    """
    When we buy token BAO we buy 100 tokens and pay 100$ -> buy price is 1$

    When we sell token BAO we sell 80 tokens and receive 160$ -> sell price is 2$

    We have 20 tokens left we sell these for 40 $ -> sell price is 2$


    """
    address = Web3.toChecksumAddress("0xEeE7FA9f2148e9499D6d857DC09E29864203b138")
    trade_profit_calculator = trade_processing.TraderProfitCalculator()
    for page in range(1, 8):
        transactions_json = request_transactions(address, page_size=50, page=page)
        token_swaps = _extract_token_swaps(transactions_json, address)
        [trade_profit_calculator.receive_token_swap(swap) for swap in token_swaps]
    [print(a) for a in trade_profit_calculator.finished_trades]
