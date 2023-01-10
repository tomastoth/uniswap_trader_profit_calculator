from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import dotenv
import eth_typing

import spec
import trade_processing

dotenv.load_dotenv()
import logging

from crypto_utils import exceptions
from crypto_utils import exceptions as utils_exceptions
from crypto_utils import http_utils, price, w3
from crypto_utils.config import config
from web3 import Web3
from web3 import exceptions as web3_exceptions

from src import schemas

log = logging.getLogger(__name__)

COVALENT_API_KEY = config.COVALENT_KEY
COVALENT_URL = "https://api.covalenthq.com/v1/1"
ETH_CHAIN_ID = 1


def _extract_swapped_token(erc20_token: schemas.Erc20Info) -> schemas.TradedToken:
    price_usd = erc20_token.value_usd / erc20_token.value
    return schemas.TradedToken(
        address=erc20_token.token_address,
        amount=erc20_token.value,
        value_usd=erc20_token.value_usd,
        symbol=erc20_token.symbol,
        price_usd=price_usd,
    )


def _extract_transfer_params(
    params: list[dict[str, Any]]
) -> tuple[Optional[str], Optional[str], Optional[float]]:
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


@dataclass
class SingleTransactionsMoves:
    received_transfers: list[schemas.Erc20Transfer]
    sent_transfers: list[schemas.Erc20Transfer]
    deposits: list[schemas.Erc20Info]
    withdrawals: list[schemas.Erc20Info]


class Covalent:
    def __init__(
        self,
        cex_price_provider: price.CexPriceProvider,
        transaction_price_provider: price.TransactionValueUsdProvider,
    ):
        self._cex_price_provider = cex_price_provider
        self._transaction_price_provider = transaction_price_provider

    @staticmethod
    def request_transactions(
        address: eth_typing.ChecksumAddress, page_size: int = 10, page: int = 1
    ) -> Any:
        url = f"{COVALENT_URL}/address/{address}/transactions_v2/"
        params = {
            "quote-currency": "USD",
            "format": " JSON",
            "block-signed-at-asc": True,
            "no-logs": False,
            "page-number": page,
            "page-size": page_size,
            "key": COVALENT_API_KEY,
        }
        transactions_json = http_utils.request(url, params)
        return transactions_json

    @staticmethod
    def _extract_token_swap(
        single_transaction_moves: SingleTransactionsMoves,
        block_time: datetime,
        transaction_hash: str,
    ) -> schemas.TokenSwap | None:
        sold_tokens: list[schemas.TradedToken] = []
        bought_tokens: list[schemas.TradedToken] = []
        usd_paid = 0.0
        usd_received = 0.0
        for sent_transfer in single_transaction_moves.sent_transfers:
            sold_token = _extract_swapped_token(sent_transfer)
            usd_paid += sold_token.value_usd
            sold_tokens.append(sold_token)
        for received_transfer in single_transaction_moves.received_transfers:
            bought_token = _extract_swapped_token(received_transfer)
            usd_received += bought_token.value_usd
            bought_tokens.append(bought_token)
        for deposit in single_transaction_moves.deposits:
            usd_paid += deposit.value_usd
            sold_token = _extract_swapped_token(deposit)
            sold_tokens.append(sold_token)
        for withdrawal in single_transaction_moves.withdrawals:
            usd_received += withdrawal.value_usd
            bought_token = _extract_swapped_token(withdrawal)
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

    @staticmethod
    def _extract_transfer(
        trader_address: eth_typing.ChecksumAddress,
        decoded: dict[str, Any],
        token_address: eth_typing.ChecksumAddress,
        transaction_usd_value: float,
    ) -> schemas.Erc20Transfer | None:
        params = decoded["params"]
        address_from, address_to, transferred_value = _extract_transfer_params(params)
        if not all([address_from, address_to, transferred_value]):
            raise exceptions.MissingDataError()
        trader_sender = address_from.lower() == trader_address.lower()  # type: ignore
        trader_receiver = address_to.lower() == trader_address.lower()  # type: ignore
        if (not trader_receiver) and (not trader_sender):
            # we can have transfer that is from other contract to other contract
            return None
        is_trader_sender = False
        if trader_sender and not trader_receiver:
            is_trader_sender = True
        token_info = w3.get_erc20_info(token_address)
        token_decimals = token_info.decimals
        token_value_divided = transferred_value / 10**token_decimals
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

    def _extract_deposit_or_withdraw(
        self,
        decoded: dict[Any, Any],
        token_address: eth_typing.ChecksumAddress,
        block_time: datetime,
        single_transaction_moves: SingleTransactionsMoves,
        is_deposit: bool,
    ) -> None:
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
        if not value_deposited:
            raise exceptions.MissingDataError()
        coin_info = w3.get_erc20_info(token_address)
        value_divided = float(value_deposited) / 10**coin_info.decimals
        price = self._cex_price_provider.get_price_of_token(
            symbol="ETH", at_time=block_time
        )
        value_usd = value_divided * price
        erc20_move = schemas.Erc20Info(
            token_address=token_address,
            value=value_divided,
            value_usd=value_usd,
            symbol=coin_info.symbol,
        )
        if is_deposit:
            single_transaction_moves.deposits.append(erc20_move)
        else:
            single_transaction_moves.withdrawals.append(erc20_move)

    def _extract_single_transfer(
        self,
        tx_hash: eth_typing.ChecksumAddress,
        trader_address: eth_typing.ChecksumAddress,
        decoded: dict[str, Any],
        token_address: eth_typing.ChecksumAddress,
        single_transaction_moves: SingleTransactionsMoves,
    ) -> None:
        try:
            transaction_usd_value = (
                self._transaction_price_provider.get_usd_value_of_transaction(tx_hash)
            )
        except utils_exceptions.CantExtractUsdValueError:
            return None
        transfer = self._extract_transfer(
            trader_address, decoded, token_address, transaction_usd_value
        )
        if transfer:
            if transfer.trader_sender:
                single_transaction_moves.sent_transfers.append(transfer)
            else:
                single_transaction_moves.received_transfers.append(transfer)

    def _extract_single_log(
        self,
        block_time: datetime,
        log_event: dict[str, Any],
        single_transaction_moves: SingleTransactionsMoves,
        trader_address: eth_typing.ChecksumAddress,
        tx_hash: eth_typing.ChecksumAddress,
    ) -> None:
        decoded = log_event["decoded"]
        event_name = decoded["name"]
        token_address = Web3.toChecksumAddress(log_event["sender_address"])
        if event_name == "Transfer":
            self._extract_single_transfer(
                tx_hash,
                trader_address,
                decoded,
                token_address,
                single_transaction_moves,
            )
        elif event_name == "Deposit":
            self._extract_deposit_or_withdraw(
                decoded,
                token_address,
                block_time,
                single_transaction_moves,
                is_deposit=True,
            )
        elif event_name == "Withdrawal":
            self._extract_deposit_or_withdraw(
                decoded,
                token_address,
                block_time,
                single_transaction_moves,
                is_deposit=False,
            )

    def extract_single_transaction_swap(
        self,
        item: dict[str, Any],
        trader_address: spec.Address,
    ) -> schemas.TokenSwap | None:
        log_events = item["log_events"]
        block_time = datetime.strptime(item["block_signed_at"], "%Y-%m-%dT%H:%M:%SZ")
        tx_hash = item["tx_hash"]
        single_transaction_moves = SingleTransactionsMoves([], [], [], [])
        for log_event in log_events:
            decoded = log_event["decoded"]
            if not decoded:
                return None
            self._extract_single_log(
                block_time, log_event, single_transaction_moves, trader_address, tx_hash
            )
        return self._extract_token_swap(
            single_transaction_moves,
            block_time,
            tx_hash,
        )

    def _extract_token_swaps(
        self,
        tx_json: dict[str, Any],
        trader_address: eth_typing.ChecksumAddress,
    ) -> list[schemas.TokenSwap]:
        token_swaps: list[schemas.TokenSwap] = []
        tx_data = tx_json["data"]
        if not tx_data:
            raise exceptions.MissingDataError()
        tx_items = tx_data["items"]
        if not tx_items:
            raise exceptions.MissingDataError()
        for item in tx_items:
            try:
                token_swap = self.extract_single_transaction_swap(item, trader_address)
                if token_swap:
                    token_swaps.append(token_swap)
            except exceptions.CantFindTokenPriceError:
                log.warning(f"Could not extract price for trade, ignoring swap {item}")
            except exceptions.MissingDataError as missing_data_error:
                log.warning(
                    f"There is missing data error {missing_data_error},"
                    f" ignoring item: {item}"
                )
            except web3_exceptions.ContractLogicError:
                log.warning(f"Contract call reverted, ignoring item: {item}")

        return token_swaps


if __name__ == "__main__":
    """
    When we buy token BAO we buy 100 tokens and pay 100$ -> buy price is 1$

    When we sell token BAO we sell 80 tokens and receive 160$ -> sell price is 2$

    We have 20 tokens left we sell these for 40 $ -> sell price is 2$


    """
    address = Web3.toChecksumAddress("0xEeE7FA9f2148e9499D6d857DC09E29864203b138")
    trade_profit_calculator = trade_processing.TraderProfitCalculator()
    cov = Covalent(
        price.BinancePriceProvider(), price.UniswapTransactionValueUsdProvider()
    )
    for page in range(1, 15):
        transactions_json = cov.request_transactions(address, page_size=50, page=page)
        token_swaps = cov._extract_token_swaps(transactions_json, address)
        [trade_profit_calculator.receive_token_swap(swap) for swap in token_swaps]
    [print(a) for a in trade_profit_calculator.finished_trades]
