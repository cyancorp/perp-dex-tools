#!/usr/bin/env python3
"""
Risk Manager - Periodically prints account exposure and available margin
for Extended and Lighter.

Usage:
    uv run risk_manager.py --ticker SOL --interval 60 --env-file .env
"""

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from typing import Optional, Tuple

import dotenv
import requests

from x10.perpetual.positions import PositionSide, PositionStatus
from x10.utils.http import ResponseStatus

from hedge.hedge_mode_ext import Config  # Reuse lightweight config wrapper
from exchanges.extended import ExtendedClient
from lighter.signer_client import SignerClient
import lighter

LOGGER = logging.getLogger("risk_manager")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


@dataclass
class ExtendedMetrics:
    position_size: Decimal
    position_side: str
    available_for_trade: Decimal
    equity: Decimal


@dataclass
class LighterMetrics:
    position_size: Decimal
    position_side: str
    available_balance: Decimal
    collateral: Decimal


class RiskManager:
    def __init__(self, ticker: str, interval: int):
        self.ticker = ticker.upper()
        self.interval = interval
        self.extended_client: Optional[ExtendedClient] = None
        self.extended_contract_id: Optional[str] = None
        self.lighter_client: Optional[SignerClient] = None
        self.lighter_market_id: Optional[int] = None
        self.base_amount_multiplier: Optional[int] = None
        self.price_multiplier: Optional[int] = None
        self.account_index: Optional[int] = None
        self._stop = asyncio.Event()

    async def initialize(self) -> None:
        LOGGER.info("Initializing Extended connections")
        self.extended_client = self._initialize_extended_client()
        (
            self.extended_contract_id,
            _tick_size,
        ) = await self._get_extended_contract_info()

        LOGGER.info("Initializing Lighter connections")
        self.lighter_client = self._initialize_lighter_client()
        (
            self.lighter_market_id,
            self.base_amount_multiplier,
            self.price_multiplier,
        ) = self._get_lighter_market_config()
        self.account_index = int(os.getenv("LIGHTER_ACCOUNT_INDEX"))

    async def run(self) -> None:
        try:
            while not self._stop.is_set():
                await self.print_snapshot()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    continue
        except KeyboardInterrupt:
            LOGGER.info("Received interrupt, stopping risk manager")
        finally:
            await self.close()

    async def close(self) -> None:
        LOGGER.info("Closing clients")
        if self.extended_client:
            try:
                await self.extended_client.perpetual_trading_client.close()
            except Exception as exc:
                LOGGER.warning("Error closing Extended client session: %s", exc)
        if self.lighter_client:
            try:
                await self.lighter_client.api_client.close()
            except Exception as exc:
                LOGGER.warning("Error closing Lighter client session: %s", exc)

    async def print_snapshot(self) -> None:
        try:
            extended_metrics = await self._fetch_extended_metrics()
        except Exception as exc:
            LOGGER.error("Failed to fetch Extended metrics: %s", exc)
            extended_metrics = None

        try:
            lighter_metrics = await self._fetch_lighter_metrics()
        except Exception as exc:
            LOGGER.error("Failed to fetch Lighter metrics: %s", exc)
            lighter_metrics = None

        LOGGER.info("===== Risk Snapshot (%s) =====", self.ticker)
        if extended_metrics:
            LOGGER.info(
                "Extended | Position: %s %s | Available: %s %s | Equity: %s %s",
                self._format_decimal(extended_metrics.position_size),
                extended_metrics.position_side,
                self._format_decimal(extended_metrics.available_for_trade),
                self._quote_symbol(),
                self._format_decimal(extended_metrics.equity),
                self._quote_symbol(),
            )
        else:
            LOGGER.info("Extended | Metrics unavailable")

        if lighter_metrics:
            LOGGER.info(
                "Lighter  | Position: %s %s | Available: %s %s | Collateral: %s %s",
                self._format_decimal(lighter_metrics.position_size),
                lighter_metrics.position_side,
                self._format_decimal(lighter_metrics.available_balance),
                self._quote_symbol(),
                self._format_decimal(lighter_metrics.collateral),
                self._quote_symbol(),
            )
        else:
            LOGGER.info("Lighter  | Metrics unavailable")

    async def _fetch_extended_metrics(self) -> ExtendedMetrics:
        assert self.extended_client and self.extended_contract_id
        client = self.extended_client.perpetual_trading_client

        balance_resp = await client.account.get_balance()
        if balance_resp.status != ResponseStatus.OK or balance_resp.data is None:
            raise RuntimeError("Extended balance API returned error")
        balance = balance_resp.data

        position_size = Decimal("0")
        position_side = "FLAT"

        positions_resp = await client.account.get_positions(market_names=[self.extended_contract_id])
        if positions_resp.status == ResponseStatus.OK and positions_resp.data:
            for pos in positions_resp.data:
                if pos.market == self.extended_contract_id and pos.status == PositionStatus.OPENED:
                    signed_size = pos.size if pos.side == PositionSide.LONG else pos.size * Decimal("-1")
                    position_size = signed_size
                    position_side = pos.side.value
                    break

        return ExtendedMetrics(
            position_size=position_size,
            position_side=position_side,
            available_for_trade=balance.available_for_trade,
            equity=balance.equity,
        )

    async def _fetch_lighter_metrics(self) -> LighterMetrics:
        assert self.lighter_client and self.lighter_market_id is not None
        account_api = lighter.AccountApi(self.lighter_client.api_client)
        account_resp = await account_api.account(by="index", value=str(self.account_index))
        if not account_resp or not account_resp.accounts:
            raise RuntimeError("Lighter account API returned empty response")

        account = account_resp.accounts[0]
        available_balance = Decimal(account.available_balance or "0")
        collateral = Decimal(account.collateral or "0")

        position_size = Decimal("0")
        position_side = "FLAT"
        for pos in account.positions or []:
            if pos.market_id == self.lighter_market_id:
                raw_size = Decimal(pos.position or "0")
                sign = Decimal(pos.sign or 0)
                position_size = raw_size * (Decimal(1) if sign >= 0 else Decimal(-1))
                if position_size > 0:
                    position_side = "LONG"
                elif position_size < 0:
                    position_side = "SHORT"
                else:
                    position_side = "FLAT"
                break

        return LighterMetrics(
            position_size=position_size,
            position_side=position_side,
            available_balance=available_balance,
            collateral=collateral,
        )

    def _initialize_extended_client(self) -> ExtendedClient:
        required_env = [
            "EXTENDED_VAULT",
            "EXTENDED_STARK_KEY_PRIVATE",
            "EXTENDED_STARK_KEY_PUBLIC",
            "EXTENDED_API_KEY",
        ]
        missing = [var for var in required_env if not os.getenv(var)]
        if missing:
            raise EnvironmentError(f"Missing Extended env vars: {', '.join(missing)}")

        config_dict = {
            "ticker": self.ticker,
            "contract_id": f"{self.ticker}-USD",
            "quantity": Decimal("100"),
            "tick_size": Decimal("0.01"),
            "close_order_side": "sell",
        }
        config = Config(config_dict)
        return ExtendedClient(config)

    async def _get_extended_contract_info(self) -> Tuple[str, Decimal]:
        assert self.extended_client
        return await self.extended_client.get_contract_attributes()

    def _initialize_lighter_client(self) -> SignerClient:
        api_key_private_key = os.getenv("API_KEY_PRIVATE_KEY")
        if not api_key_private_key:
            raise EnvironmentError("API_KEY_PRIVATE_KEY environment variable not set")

        account_index = int(os.getenv("LIGHTER_ACCOUNT_INDEX"))
        api_key_index = int(os.getenv("LIGHTER_API_KEY_INDEX"))

        client = SignerClient(
            url="https://mainnet.zklighter.elliot.ai",
            private_key=api_key_private_key,
            account_index=account_index,
            api_key_index=api_key_index,
        )
        LOGGER.info("✅ Lighter signer client initialized")
        return client

    def _get_lighter_market_config(self) -> Tuple[int, int, int]:
        url = "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks"
        headers = {"accept": "application/json"}

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        if not response.text.strip():
            raise RuntimeError("Empty response from Lighter API")

        data = response.json()
        order_books = data.get("order_books", [])
        for market in order_books:
            if market.get("symbol") == self.ticker:
                market_id = market["market_id"]
                base_multiplier = pow(10, market["supported_size_decimals"])
                price_multiplier = pow(10, market["supported_price_decimals"])
                LOGGER.info("✅ Lighter market config loaded: %s (id=%s)", self.ticker, market_id)
                return market_id, base_multiplier, price_multiplier

        raise RuntimeError(f"Lighter market {self.ticker} not found")

    def _quote_symbol(self) -> str:
        # Extended USDC collateral by default
        return "USDC"

    @staticmethod
    def _format_decimal(value: Decimal, precision: int = 6) -> str:
        quant = Decimal(10) ** -precision
        try:
            return f"{value.quantize(quant, rounding=ROUND_DOWN)}"
        except InvalidOperation:
            return f"{value}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Risk monitor for Extended + Lighter")
    parser.add_argument("--ticker", type=str, default="BTC", help="Ticker symbol (default: BTC)")
    parser.add_argument("--interval", type=int, default=60, help="Refresh interval in seconds (default: 60)")
    parser.add_argument("--env-file", type=str, default=".env", help=".env file path (default: .env)")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    dotenv.load_dotenv(args.env_file)

    manager = RiskManager(ticker=args.ticker, interval=args.interval)
    await manager.initialize()
    await manager.run()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
