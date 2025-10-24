import asyncio
import json
import signal
import logging
import os
import sys
import time
import random
import requests
import argparse
import traceback
import csv
from decimal import Decimal, ROUND_DOWN
from typing import Tuple, Optional

from lighter.signer_client import SignerClient
import lighter
from x10.utils.http import ResponseStatus
from x10.perpetual.positions import PositionStatus, PositionSide
from x10.perpetual.orders import OrderSide, TimeInForce
from x10.utils.date import utc_now
from helpers.alerting import send_telegram_message
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exchanges.extended import ExtendedClient
import websockets
from datetime import datetime, timedelta
import pytz


class Config:
    """Simple config class to wrap dictionary for Extended client."""
    def __init__(self, config_dict):
        for key, value in config_dict.items():
            setattr(self, key, value)


class HedgeBot:
    """Trading bot that places post-only orders on Extended and hedges with market orders on Lighter."""

    def __init__(
            self,
            ticker: str,
            order_quantity: Decimal,
            fill_timeout: int = 5,
            iterations: int = 20,
            order_size_min: Optional[Decimal] = None,
            order_size_max: Optional[Decimal] = None,
            order_size_step: Optional[Decimal] = None,
            order_delay_min: Optional[float] = None,
            order_delay_max: Optional[float] = None,
            lighter_buy_offset_bps_min: Optional[Decimal] = None,
            lighter_buy_offset_bps_max: Optional[Decimal] = None,
            lighter_sell_offset_bps_min: Optional[Decimal] = None,
            lighter_sell_offset_bps_max: Optional[Decimal] = None,
            direction_mode: str = 'random',
            hold_min: Optional[float] = None,
            hold_max: Optional[float] = None,
            bot_name: Optional[str] = None,
    ):
        self.ticker = ticker
        self.order_quantity = order_quantity
        self.fill_timeout = fill_timeout
        self.lighter_order_filled = False
        self.iterations = iterations
        self.extended_position = Decimal('0')
        self.lighter_position = Decimal('0')
        self.current_order = {}
        self.extended_order_fills = {}
        self.lighter_residual_quantity = Decimal('0')
        self.latest_extended_available = Decimal('0')
        self.latest_extended_equity = Decimal('0')
        self.latest_lighter_available = Decimal('0')
        self.latest_lighter_collateral = Decimal('0')
        self.loop = None
        self.last_extended_fill_monotonic = None
        self.last_lighter_send_monotonic = None
        self.position_epsilon = Decimal('0.0001')
        self.last_exception: Optional[Exception] = None
        self.hold_min = hold_min if hold_min is not None else 0.0
        self.hold_max = hold_max if hold_max is not None else self.hold_min
        if self.hold_min < 0 or self.hold_max < 0:
            raise ValueError("Hold duration bounds must be non-negative")
        if self.hold_max < self.hold_min:
            raise ValueError("hold_max cannot be less than hold_min")
        self.bot_name = bot_name or os.getenv("HEDGE_BOT_NAME") or f"hedge-{self.ticker}"
        self.extended_volume_base = Decimal('0')
        self.extended_volume_notional = Decimal('0')
        self.last_cycle_open_quantity: Optional[Decimal] = None
        self.last_hold_duration: float = 0.0

        # Randomization configuration
        self._rng = random.SystemRandom()
        self.order_size_min = order_size_min if order_size_min is not None else order_quantity
        self.order_size_max = order_size_max if order_size_max is not None else order_quantity
        if self.order_size_min > self.order_size_max:
            raise ValueError("order_size_min cannot be greater than order_size_max")
        self.order_size_step = order_size_step
        if self.order_size_step is not None and self.order_size_step <= 0:
            raise ValueError("order_size_step must be positive")
        if self.order_size_step is not None:
            self.quantity_precision = self.order_size_step
        else:
            self.quantity_precision = Decimal('0.00000001')

        self.order_delay_min = order_delay_min
        self.order_delay_max = order_delay_max
        if (self.order_delay_min is not None) != (self.order_delay_max is not None):
            if self.order_delay_min is None:
                self.order_delay_min = self.order_delay_max
            else:
                self.order_delay_max = self.order_delay_min
        if self.order_delay_min is not None and self.order_delay_max is not None:
            if self.order_delay_min > self.order_delay_max:
                raise ValueError("order_delay_min cannot be greater than order_delay_max")

        self.lighter_buy_offset_bps_min = lighter_buy_offset_bps_min if lighter_buy_offset_bps_min is not None else Decimal('20')
        self.lighter_buy_offset_bps_max = lighter_buy_offset_bps_max if lighter_buy_offset_bps_max is not None else self.lighter_buy_offset_bps_min
        if self.lighter_buy_offset_bps_min > self.lighter_buy_offset_bps_max:
            raise ValueError("lighter_buy_offset_bps_min cannot be greater than lighter_buy_offset_bps_max")

        self.lighter_sell_offset_bps_min = lighter_sell_offset_bps_min if lighter_sell_offset_bps_min is not None else Decimal('20')
        self.lighter_sell_offset_bps_max = lighter_sell_offset_bps_max if lighter_sell_offset_bps_max is not None else self.lighter_sell_offset_bps_min
        if self.lighter_sell_offset_bps_min > self.lighter_sell_offset_bps_max:
            raise ValueError("lighter_sell_offset_bps_min cannot be greater than lighter_sell_offset_bps_max")

        self.lighter_offset_precision = Decimal('0.01')
        self.lighter_price_step = None
        self.lighter_quantity_step = None
        self.lighter_market_index = None
        self.current_cycle_quantity = None
        self.current_cycle_open_side = None
        self.extended_depth_task = None
        allowed_directions = {'buy', 'sell', 'random'}
        if direction_mode not in allowed_directions:
            raise ValueError(f"direction_mode must be one of {allowed_directions}")
        self.direction_mode = direction_mode

        # Initialize logging to file
        os.makedirs("logs", exist_ok=True)
        self.log_filename = f"logs/extended_{ticker}_hedge_mode_log.txt"
        self.csv_filename = f"logs/extended_{ticker}_hedge_mode_trades.csv"
        self.original_stdout = sys.stdout

        # Initialize CSV file with headers if it doesn't exist
        self._initialize_csv_file()

        # Setup logger
        self.logger = logging.getLogger(f"hedge_bot_{ticker}")
        self.logger.setLevel(logging.INFO)

        # Clear any existing handlers to avoid duplicates
        self.logger.handlers.clear()

        # Disable verbose logging from external libraries
        logging.getLogger('urllib3').setLevel(logging.WARNING)
        logging.getLogger('requests').setLevel(logging.WARNING)
        logging.getLogger('websockets').setLevel(logging.WARNING)

        # Create file handler
        file_handler = logging.FileHandler(self.log_filename)
        file_handler.setLevel(logging.INFO)

        # Create console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)

        # Create different formatters for file and console
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_formatter = logging.Formatter('%(levelname)s:%(name)s:%(message)s')

        file_handler.setFormatter(file_formatter)
        console_handler.setFormatter(console_formatter)

        # Add handlers to logger
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

        # Prevent propagation to root logger to avoid duplicate messages
        self.logger.propagate = False

        if self.order_size_min != self.order_size_max:
            self.logger.info(f"🎲 Order size range active: {self.order_size_min} → {self.order_size_max} "
                             f"(step: {self.order_size_step or 'continuous'})")
        if self.order_delay_min is not None and self.order_delay_max is not None:
            self.logger.info(f"⏱️ Random delay range active: {self.order_delay_min}s → {self.order_delay_max}s")
        if (self.lighter_buy_offset_bps_min != self.lighter_buy_offset_bps_max or
                self.lighter_sell_offset_bps_min != self.lighter_sell_offset_bps_max):
            self.logger.info("📐 Random Lighter hedge offsets enabled: "
                             f"buy +{self.lighter_buy_offset_bps_min}→{self.lighter_buy_offset_bps_max} bps | "
                             f"sell -{self.lighter_sell_offset_bps_min}→{self.lighter_sell_offset_bps_max} bps")
        if self.direction_mode == 'random':
            self.logger.info("🔀 Opening direction mode: random")
        else:
            self.logger.info(f"➡️ Opening direction mode: always {self.direction_mode.upper()}")

        # State management
        self.stop_flag = False
        self.order_counter = 0

        # Extended state
        self.extended_client = None
        self.extended_contract_id = None
        self.extended_tick_size = None
        self.extended_order_status = None

        # Extended order book state for websocket-based BBO
        self.extended_order_book = {'bids': {}, 'asks': {}}
        self.extended_best_bid = None
        self.extended_best_ask = None
        self.extended_order_book_ready = False

        # Lighter order book state
        self.lighter_client = None
        self.lighter_order_book = {"bids": {}, "asks": {}}
        self.lighter_best_bid = None
        self.lighter_best_ask = None
        self.lighter_order_book_ready = False
        self.last_lighter_orderbook_update = 0.0
        self.lighter_order_book_offset = 0
        self.lighter_order_book_sequence_gap = False
        self.lighter_snapshot_loaded = False
        self.lighter_order_book_lock = asyncio.Lock()

        # Lighter WebSocket state
        self.lighter_ws_task = None
        self.lighter_order_result = None

        # Lighter order management
        self.lighter_order_status = None
        self.lighter_order_price = None
        self.lighter_order_side = None
        self.lighter_order_size = None
        self.lighter_order_start_time = None

        # Strategy state
        self.waiting_for_lighter_fill = False
        self.wait_start_time = None

        # Order execution tracking
        self.order_execution_complete = False

        # Current order details for immediate execution
        self.current_lighter_side = None
        self.current_lighter_quantity = None
        self.current_lighter_price = None
        self.lighter_order_info = None

        # Lighter API configuration
        self.lighter_base_url = "https://mainnet.zklighter.elliot.ai"
        self.account_index = int(os.getenv('LIGHTER_ACCOUNT_INDEX'))
        self.api_key_index = int(os.getenv('LIGHTER_API_KEY_INDEX'))

        # Extended configuration
        self.extended_vault = os.getenv('EXTENDED_VAULT')
        self.extended_stark_key_private = os.getenv('EXTENDED_STARK_KEY_PRIVATE')
        self.extended_stark_key_public = os.getenv('EXTENDED_STARK_KEY_PUBLIC')
        self.extended_api_key = os.getenv('EXTENDED_API_KEY')

        self.shutdown_in_progress = False

    def shutdown(self, signum=None, frame=None):
        """Signal handler that schedules async shutdown."""
        target_loop = self.loop or asyncio.get_event_loop()
        target_loop.call_soon_threadsafe(lambda: asyncio.create_task(self.shutdown_async()))

    async def shutdown_async(self):
        if self.shutdown_in_progress:
            return
        self.shutdown_in_progress = True
        self.stop_flag = True
        self.logger.info("\n🛑 Stopping...")

        try:
            await self._cancel_all_extended_orders()
        except Exception as cancel_error:
            self.logger.error(f"⚠️ Error cancelling outstanding Extended orders during shutdown: {cancel_error}")

        try:
            await self._cancel_all_lighter_orders()
        except Exception as cancel_error:
            self.logger.error(f"⚠️ Error cancelling outstanding Lighter orders during shutdown: {cancel_error}")

        if self.extended_client:
            try:
                await self.extended_client.disconnect()
            except Exception as e:
                self.logger.error(f"Error disconnecting Extended WebSocket: {e}")

        if self.lighter_ws_task and not self.lighter_ws_task.done():
            self.lighter_ws_task.cancel()
            try:
                await self.lighter_ws_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self.logger.error(f"Error awaiting Lighter websocket task: {e}")

        if self.lighter_client:
            try:
                await self.lighter_client.api_client.close()
            except Exception as e:
                self.logger.error(f"Error closing Lighter API client: {e}")

        if self.extended_depth_task and not self.extended_depth_task.done():
            self.extended_depth_task.cancel()
            try:
                await self.extended_depth_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self.logger.error(f"Error awaiting Extended depth task: {e}")

        for handler in self.logger.handlers[:]:
            try:
                handler.close()
                self.logger.removeHandler(handler)
            except Exception:
                pass

    def _initialize_csv_file(self):
        """Initialize CSV file with headers if it doesn't exist."""
        if not os.path.exists(self.csv_filename):
            with open(self.csv_filename, 'w', newline='') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(['exchange', 'timestamp', 'side', 'price', 'quantity'])

    def _write_trade_to_csv(self, exchange: str, side: str, price: str, quantity: str):
        """Blocking helper to append trade info to CSV."""
        timestamp = datetime.now(pytz.UTC).isoformat()

        with open(self.csv_filename, 'a', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow([
                exchange,
                timestamp,
                side,
                price,
                quantity
            ])

    async def _log_trade_to_csv_async(self, exchange: str, side: str, price: str, quantity: str):
        await asyncio.to_thread(self._write_trade_to_csv, exchange, side, price, quantity)
        self.logger.info(f"📊 Trade logged to CSV: {exchange} {side} {quantity} @ {price}")

    def _schedule_trade_log(self, exchange: str, side: str, price: str, quantity: str):
        try:
            loop = self.loop or asyncio.get_running_loop()
            loop.create_task(self._log_trade_to_csv_async(exchange, side, price, quantity))
        except RuntimeError:
            # Fallback if no running loop (e.g., during shutdown)
            self._write_trade_to_csv(exchange, side, price, quantity)
            self.logger.info(f"📊 Trade logged to CSV: {exchange} {side} {quantity} @ {price}")

    def _random_bps(self, minimum: Decimal, maximum: Decimal) -> Decimal:
        """Select a random basis-point value within range."""
        if minimum == maximum:
            return minimum
        random_value = Decimal(str(self._rng.uniform(float(minimum), float(maximum))))
        return random_value.quantize(self.lighter_offset_precision, rounding=ROUND_DOWN)

    def _choose_lighter_multiplier(self, lighter_side: str) -> Tuple[Decimal, Decimal]:
        """Select randomized multiplier and return multiplier with chosen bps."""
        if lighter_side.lower() == 'buy':
            bps = self._random_bps(self.lighter_buy_offset_bps_min, self.lighter_buy_offset_bps_max)
            multiplier = Decimal('1') + (bps / Decimal('10000'))
        else:
            bps = self._random_bps(self.lighter_sell_offset_bps_min, self.lighter_sell_offset_bps_max)
            multiplier = Decimal('1') - (bps / Decimal('10000'))
        return multiplier, bps

    def _choose_order_quantity(self) -> Decimal:
        """Select randomized order quantity."""
        if self.order_size_min == self.order_size_max:
            return self.order_size_min

        if self.order_size_step is not None:
            span = (self.order_size_max - self.order_size_min) / self.order_size_step
            step_count = int(span.to_integral_value(rounding=ROUND_DOWN))
            step_index = self._rng.randint(0, max(step_count, 1))
            quantity = self.order_size_min + self.order_size_step * Decimal(step_index)
            if quantity > self.order_size_max:
                quantity = self.order_size_max
            return quantity.quantize(self.order_size_step, rounding=ROUND_DOWN)

        span = self.order_size_max - self.order_size_min
        random_fraction = Decimal(str(self._rng.random()))
        quantity = (self.order_size_min + span * random_fraction).quantize(self.quantity_precision, rounding=ROUND_DOWN)
        if quantity < self.order_size_min:
            quantity = self.order_size_min
        if quantity > self.order_size_max:
            quantity = self.order_size_max
        return quantity

    async def _maybe_random_delay(self, context: str, require_flat: bool = True):
        """Introduce randomized delay between actions when safe to do so."""
        if self.order_delay_min is None or self.order_delay_max is None:
            return
        if require_flat:
            if (abs(self.extended_position) > self.position_epsilon or
                    abs(self.lighter_position) > self.position_epsilon):
                return
        delay = self._rng.uniform(self.order_delay_min, self.order_delay_max)
        self.logger.info(f"⏳ Random delay before {context}: {delay:.2f}s")
        await asyncio.sleep(delay)

    async def _fetch_extended_snapshot(self) -> Tuple[Decimal, Decimal, Decimal]:
        if not self.extended_client:
            raise Exception("Extended client not initialized")
        client = self.extended_client.perpetual_trading_client
        balance_resp = await client.account.get_balance()
        if balance_resp.status != ResponseStatus.OK or balance_resp.data is None:
            raise RuntimeError(f"Failed to fetch Extended balance: {balance_resp.error}")
        balance = balance_resp.data

        position_value = Decimal('0')
        positions_resp = await client.account.get_positions(market_names=[self.extended_contract_id])
        if positions_resp.status == ResponseStatus.OK and positions_resp.data:
            for pos in positions_resp.data:
                if pos.market == self.extended_contract_id and pos.status == PositionStatus.OPENED:
                    multiplier = Decimal('1') if pos.side == PositionSide.LONG else Decimal('-1')
                    position_value = pos.size * multiplier
                    break

        return (
            position_value,
            balance.available_for_trade,
            balance.equity,
        )

    async def _fetch_lighter_snapshot(self) -> Tuple[Decimal, Decimal, Decimal]:
        if not self.lighter_client:
            raise Exception("Lighter client not initialized")

        account_api = lighter.AccountApi(self.lighter_client.api_client)
        account_resp = await account_api.account(by="index", value=str(self.account_index))
        if not account_resp or not account_resp.accounts:
            raise RuntimeError("Failed to fetch Lighter account snapshot")

        account = account_resp.accounts[0]
        available_balance = Decimal(account.available_balance or "0")
        collateral = Decimal(account.collateral or "0")
        position_value = Decimal('0')
        for pos in account.positions or []:
            if pos.market_id == self.lighter_market_index:
                raw_size = Decimal(pos.position or "0")
                sign = Decimal(pos.sign or 0)
                position_value = raw_size if sign >= 0 else -raw_size
                break

        return position_value, available_balance, collateral

    async def _ensure_lighter_order_book_fresh(self):
        if not self.lighter_client or self.lighter_market_index is None:
            return

        now = time.monotonic()
        if now - self.last_lighter_orderbook_update <= 1.0:
            return

        self.logger.warning("⚠️ Lighter order book stale; fetching latest snapshot")
        try:
            order_api = lighter.OrderApi(self.lighter_client.api_client)
            details = await order_api.order_book_details(market_id=self.lighter_market_index)
            if not details or not details.order_book_details:
                return

            ob = details.order_book_details[0]
            if ob.bids:
                best_bid_price = Decimal(ob.bids[0].price)
                best_bid_size = Decimal(ob.bids[0].size)
                self.lighter_order_book['bids'][best_bid_price] = best_bid_size
                self.lighter_best_bid = best_bid_price
            if ob.asks:
                best_ask_price = Decimal(ob.asks[0].price)
                best_ask_size = Decimal(ob.asks[0].size)
                self.lighter_order_book['asks'][best_ask_price] = best_ask_size
                self.lighter_best_ask = best_ask_price
            self.last_lighter_orderbook_update = now
        except Exception as e:
            self.logger.error(f"⚠️ Failed to refresh Lighter order book snapshot: {e}")

    async def _hold_position(self, duration: float, context: str) -> None:
        if duration <= 0:
            return
        self.logger.info(f"⏸️ Holding exposure for {duration:.2f}s ({context})")
        self.last_hold_duration = duration
        deadline = time.monotonic() + duration
        while not self.stop_flag and time.monotonic() < deadline:
            await asyncio.sleep(min(1.0, deadline - time.monotonic()))
            try:
                await self.reconcile_positions(f"hold_{context}")
            except Exception as reconcile_error:
                self.logger.error(f"⚠️ Failed to reconcile during hold: {reconcile_error}")
            if not await self._ensure_balanced_positions(f"hold_{context}"):
                if self.stop_flag:
                    break
        self.logger.info(f"▶️ Hold complete ({context})")

    async def _send_iteration_summary(self, iteration_number: int):
        if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
            return

        message = (
            f"[{self.bot_name}] Iteration {iteration_number} complete\n"
            f"Open size: {self.last_cycle_open_quantity or Decimal('0')}\n"
            f"Total volume: {self.extended_volume_base} (notional {self.extended_volume_notional})\n"
            f"Last hold: {self.last_hold_duration:.2f}s"
        )
        try:
            await send_telegram_message(message)
        except Exception as exc:
            self.logger.error(f"⚠️ Failed to send iteration summary: {exc}")

    def handle_lighter_order_result(self, order_data):
        """Handle Lighter order result from WebSocket."""
        try:
            order_data["avg_filled_price"] = (Decimal(order_data["filled_quote_amount"]) /
                                              Decimal(order_data["filled_base_amount"]))
            if order_data["is_ask"]:
                order_data["side"] = "SHORT"
                self.lighter_position -= Decimal(order_data["filled_base_amount"])
            else:
                order_data["side"] = "LONG"
                self.lighter_position += Decimal(order_data["filled_base_amount"])

            self.logger.info(f"📊 Lighter order filled: {order_data['side']} "
                             f"{order_data['filled_base_amount']} @ {order_data['avg_filled_price']}")

            self._schedule_trade_log(
                exchange='Lighter',
                side=order_data['side'],
                price=str(order_data['avg_filled_price']),
                quantity=str(order_data['filled_base_amount'])
            )

            # Mark execution as complete
            self.lighter_order_filled = True  # Mark order as filled
            self.order_execution_complete = True

        except Exception as e:
            self.logger.error(f"Error handling Lighter order result: {e}")

    async def reset_lighter_order_book(self):
        """Reset Lighter order book state."""
        async with self.lighter_order_book_lock:
            self.lighter_order_book["bids"].clear()
            self.lighter_order_book["asks"].clear()
            self.lighter_order_book_offset = 0
            self.lighter_order_book_sequence_gap = False
            self.lighter_snapshot_loaded = False
            self.lighter_best_bid = None
            self.lighter_best_ask = None

    def update_lighter_order_book(self, side: str, levels: list):
        """Update Lighter order book with new levels."""
        for level in levels:
            # Handle different data structures - could be list [price, size] or dict {"price": ..., "size": ...}
            if isinstance(level, list) and len(level) >= 2:
                price = Decimal(level[0])
                size = Decimal(level[1])
            elif isinstance(level, dict):
                price = Decimal(level.get("price", 0))
                size = Decimal(level.get("size", 0))
            else:
                self.logger.warning(f"⚠️ Unexpected level format: {level}")
                continue

            if size > 0:
                self.lighter_order_book[side][price] = size
            else:
                # Remove zero size orders
                self.lighter_order_book[side].pop(price, None)

    def validate_order_book_offset(self, new_offset: int) -> bool:
        """Validate order book offset sequence."""
        if new_offset <= self.lighter_order_book_offset:
            self.logger.warning(
                f"⚠️ Out-of-order update: new_offset={new_offset}, current_offset={self.lighter_order_book_offset}")
            return False
        return True

    def validate_order_book_integrity(self) -> bool:
        """Validate order book integrity."""
        # Check for negative prices or sizes
        for side in ["bids", "asks"]:
            for price, size in self.lighter_order_book[side].items():
                if price <= 0 or size <= 0:
                    self.logger.error(f"❌ Invalid order book data: {side} price={price}, size={size}")
                    return False
        return True

    def get_lighter_best_levels(self) -> Tuple[Tuple[Decimal, Decimal], Tuple[Decimal, Decimal]]:
        """Get best bid and ask levels from Lighter order book."""
        best_bid = None
        best_ask = None

        if self.lighter_order_book["bids"]:
            best_bid_price = max(self.lighter_order_book["bids"].keys())
            best_bid_size = self.lighter_order_book["bids"][best_bid_price]
            best_bid = (best_bid_price, best_bid_size)

        if self.lighter_order_book["asks"]:
            best_ask_price = min(self.lighter_order_book["asks"].keys())
            best_ask_size = self.lighter_order_book["asks"][best_ask_price]
            best_ask = (best_ask_price, best_ask_size)

        return best_bid, best_ask

    async def get_lighter_mid_price(self) -> Decimal:
        """Get mid price from Lighter order book."""

        await self._ensure_lighter_order_book_fresh()

        best_bid, best_ask = self.get_lighter_best_levels()

        if best_bid is None or best_ask is None:
            raise Exception("Cannot calculate mid price - missing order book data")

        mid_price = (best_bid[0] + best_ask[0]) / Decimal('2')
        return mid_price

    def get_lighter_order_price(self, is_ask: bool) -> Decimal:
        """Get order price from Lighter order book."""
        best_bid, best_ask = self.get_lighter_best_levels()

        if best_bid is None or best_ask is None:
            raise Exception("Cannot calculate order price - missing order book data")

        if is_ask:
            order_price = best_bid[0] + Decimal('0.1')
        else:
            order_price = best_ask[0] - Decimal('0.1')

        return order_price

    def calculate_adjusted_price(self, original_price: Decimal, side: str, adjustment_percent: Decimal) -> Decimal:
        """Calculate adjusted price for order modification."""
        adjustment = original_price * adjustment_percent

        if side.lower() == 'buy':
            # For buy orders, increase price to improve fill probability
            return original_price + adjustment
        else:
            # For sell orders, decrease price to improve fill probability
            return original_price - adjustment

    async def request_fresh_snapshot(self, ws):
        """Request fresh order book snapshot."""
        await ws.send(json.dumps({"type": "subscribe", "channel": f"order_book/{self.lighter_market_index}"}))

    async def handle_lighter_ws(self):
        """Handle Lighter WebSocket connection and messages."""
        url = "wss://mainnet.zklighter.elliot.ai/stream"
        cleanup_counter = 0

        while not self.stop_flag:
            timeout_count = 0
            try:
                # Reset order book state before connecting
                await self.reset_lighter_order_book()

                async with websockets.connect(url) as ws:
                    # Subscribe to order book updates
                    await ws.send(json.dumps({"type": "subscribe", "channel": f"order_book/{self.lighter_market_index}"}))

                    # Subscribe to account orders updates
                    account_orders_channel = f"account_orders/{self.lighter_market_index}/{self.account_index}"

                    # Get auth token for the subscription
                    try:
                        # Set auth token to expire in 10 minutes
                        ten_minutes_deadline = int(time.time() + 10 * 60)
                        auth_token, err = self.lighter_client.create_auth_token_with_expiry(ten_minutes_deadline)
                        if err is not None:
                            self.logger.warning(f"⚠️ Failed to create auth token for account orders subscription: {err}")
                        else:
                            auth_message = {
                                "type": "subscribe",
                                "channel": account_orders_channel,
                                "auth": auth_token
                            }
                            await ws.send(json.dumps(auth_message))
                            self.logger.info("✅ Subscribed to account orders with auth token (expires in 10 minutes)")
                    except Exception as e:
                        self.logger.warning(f"⚠️ Error creating auth token for account orders subscription: {e}")

                    while not self.stop_flag:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1)

                            try:
                                data = json.loads(msg)
                            except json.JSONDecodeError as e:
                                self.logger.warning(f"⚠️ JSON parsing error in Lighter websocket: {e}")
                                continue

                            # Reset timeout counter on successful message
                            timeout_count = 0

                            async with self.lighter_order_book_lock:
                                if data.get("type") == "subscribed/order_book":
                                    # Initial snapshot - clear and populate the order book
                                    self.lighter_order_book["bids"].clear()
                                    self.lighter_order_book["asks"].clear()

                                    # Handle the initial snapshot
                                    order_book = data.get("order_book", {})
                                    if order_book and "offset" in order_book:
                                        self.lighter_order_book_offset = order_book["offset"]
                                        self.logger.info(f"✅ Initial order book offset set to: {self.lighter_order_book_offset}")

                                    # Debug: Log the structure of bids and asks
                                    bids = order_book.get("bids", [])
                                    asks = order_book.get("asks", [])
                                    if bids:
                                        self.logger.debug(f"📊 Sample bid structure: {bids[0] if bids else 'None'}")
                                    if asks:
                                        self.logger.debug(f"📊 Sample ask structure: {asks[0] if asks else 'None'}")

                                    self.update_lighter_order_book("bids", bids)
                                    self.update_lighter_order_book("asks", asks)
                                    self.lighter_snapshot_loaded = True
                                    self.lighter_order_book_ready = True
                                    self.last_lighter_orderbook_update = time.monotonic()

                                    self.logger.info(f"✅ Lighter order book snapshot loaded with "
                                                     f"{len(self.lighter_order_book['bids'])} bids and "
                                                     f"{len(self.lighter_order_book['asks'])} asks")

                                elif data.get("type") == "update/order_book" and self.lighter_snapshot_loaded:
                                    # Extract offset from the message
                                    order_book = data.get("order_book", {})
                                    if not order_book or "offset" not in order_book:
                                        self.logger.warning("⚠️ Order book update missing offset, skipping")
                                        continue

                                    new_offset = order_book["offset"]

                                    # Validate offset sequence
                                    if not self.validate_order_book_offset(new_offset):
                                        self.lighter_order_book_sequence_gap = True
                                        break

                                    # Update the order book with new data
                                    self.update_lighter_order_book("bids", order_book.get("bids", []))
                                    self.update_lighter_order_book("asks", order_book.get("asks", []))
                                    self.last_lighter_orderbook_update = time.monotonic()

                                    # Validate order book integrity after update
                                    if not self.validate_order_book_integrity():
                                        self.logger.warning("🔄 Order book integrity check failed, requesting fresh snapshot...")
                                        break

                                    # Get the best bid and ask levels
                                    best_bid, best_ask = self.get_lighter_best_levels()

                                    # Update global variables
                                    if best_bid is not None:
                                        self.lighter_best_bid = best_bid[0]
                                    if best_ask is not None:
                                        self.lighter_best_ask = best_ask[0]

                                elif data.get("type") == "ping":
                                    # Respond to ping with pong
                                    await ws.send(json.dumps({"type": "pong"}))
                                elif data.get("type") == "update/account_orders":
                                    # Handle account orders updates
                                    orders = data.get("orders", {}).get(str(self.lighter_market_index), [])
                                    if len(orders) == 1:
                                        order_data = orders[0]
                                        if order_data.get("status") == "filled":
                                            self.handle_lighter_order_result(order_data)
                                elif data.get("type") == "update/order_book" and not self.lighter_snapshot_loaded:
                                    # Ignore updates until we have the initial snapshot
                                    continue

                            # Periodic cleanup outside the lock
                            cleanup_counter += 1
                            if cleanup_counter >= 1000:
                                cleanup_counter = 0

                            # Handle sequence gap and integrity issues outside the lock
                            if self.lighter_order_book_sequence_gap:
                                try:
                                    await self.request_fresh_snapshot(ws)
                                    self.lighter_order_book_sequence_gap = False
                                except Exception as e:
                                    self.logger.error(f"⚠️ Failed to request fresh snapshot: {e}")
                                    break

                        except asyncio.TimeoutError:
                            timeout_count += 1
                            if timeout_count % 3 == 0:
                                self.logger.warning(f"⏰ No message from Lighter websocket for {timeout_count} seconds")
                            continue
                        except websockets.exceptions.ConnectionClosed as e:
                            self.logger.warning(f"⚠️ Lighter websocket connection closed: {e}")
                            break
                        except websockets.exceptions.WebSocketException as e:
                            self.logger.warning(f"⚠️ Lighter websocket error: {e}")
                            break
                        except Exception as e:
                            self.logger.error(f"⚠️ Error in Lighter websocket: {e}")
                            self.logger.error(f"⚠️ Full traceback: {traceback.format_exc()}")
                            break
            except Exception as e:
                self.logger.error(f"⚠️ Failed to connect to Lighter websocket: {e}")

            # Wait a bit before reconnecting
            await asyncio.sleep(2)

    def setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown."""
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.get_event_loop()

        def handler(signum, frame):
            self.shutdown(signum, frame)

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def initialize_lighter_client(self):
        """Initialize the Lighter client."""
        if self.lighter_client is None:
            api_key_private_key = os.getenv('API_KEY_PRIVATE_KEY')
            if not api_key_private_key:
                raise Exception("API_KEY_PRIVATE_KEY environment variable not set")

            self.lighter_client = SignerClient(
                url=self.lighter_base_url,
                private_key=api_key_private_key,
                account_index=self.account_index,
                api_key_index=self.api_key_index,
            )

            try:
                self.lighter_client.nonce_manager.hard_refresh_nonce(self.api_key_index)
            except Exception as nonce_error:
                self.logger.warning(f"⚠️ Failed to refresh Lighter nonce at init: {nonce_error}")

            # Check client
            err = self.lighter_client.check_client()
            if err is not None:
                raise Exception(f"CheckClient error: {err}")

            self.logger.info("✅ Lighter client initialized successfully")
        return self.lighter_client

    def initialize_extended_client(self):
        """Initialize the Extended client."""
        if not all([self.extended_vault, self.extended_stark_key_private, self.extended_stark_key_public, self.extended_api_key]):
            raise ValueError("EXTENDED_VAULT, EXTENDED_STARK_KEY_PRIVATE, EXTENDED_STARK_KEY_PUBLIC, and EXTENDED_API_KEY must be set in environment variables")

        # Create config for Extended client
        config_dict = {
            'ticker': self.ticker,
            'contract_id': '',  # Will be set when we get contract info
            'quantity': self.order_size_max,
            'tick_size': Decimal('0.01'),  # Will be updated when we get contract info
            'close_order_side': 'sell'  # Default, will be updated based on strategy
        }

        # Wrap in Config class for Extended client
        config = Config(config_dict)

        # Initialize Extended client
        self.extended_client = ExtendedClient(config)

        self.logger.info("✅ Extended client initialized successfully")
        return self.extended_client

    def get_lighter_market_config(self) -> Tuple[int, int, int]:
        """Get Lighter market configuration."""
        url = f"{self.lighter_base_url}/api/v1/orderBooks"
        headers = {"accept": "application/json"}

        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()

            if not response.text.strip():
                raise Exception("Empty response from Lighter API")

            data = response.json()

            if "order_books" not in data:
                raise Exception("Unexpected response format")

            for market in data["order_books"]:
                if market["symbol"] == self.ticker:
                    return (market["market_id"],
                            pow(10, market["supported_size_decimals"]),
                            pow(10, market["supported_price_decimals"]))

            raise Exception(f"Ticker {self.ticker} not found")

        except Exception as e:
            self.logger.error(f"⚠️ Error getting market config: {e}")
            raise

    async def get_extended_contract_info(self) -> Tuple[str, Decimal]:
        """Get Extended contract ID and tick size."""
        if not self.extended_client:
            raise Exception("Extended client not initialized")

        contract_id, tick_size = await self.extended_client.get_contract_attributes()

        min_allowed = getattr(self.extended_client, "min_order_size", None)
        if min_allowed is None:
            min_allowed = self.extended_client.config.quantity

        if self.order_size_max < min_allowed:
            raise ValueError(
                f"Configured maximum order quantity ({self.order_size_max}) is less than exchange minimum "
                f"{min_allowed}")

        if self.order_size_min < min_allowed:
            self.logger.warning(
                f"⚠️ size-min {self.order_size_min} is below exchange minimum {min_allowed}; clamping to {min_allowed}")
            self.order_size_min = min_allowed

        return contract_id, tick_size

    async def fetch_extended_bbo_prices(self) -> Tuple[Decimal, Decimal]:
        """Fetch best bid/ask prices from Extended using websocket data."""
        # Use WebSocket data if available
        if self.extended_order_book_ready and self.extended_best_bid and self.extended_best_ask:
            if self.extended_best_bid > 0 and self.extended_best_ask > 0 and self.extended_best_bid < self.extended_best_ask:
                return self.extended_best_bid, self.extended_best_ask

        # Fallback to REST API if websocket data is not available
        self.logger.warning("WebSocket BBO data not available, falling back to REST API")
        if not self.extended_client:
            raise Exception("Extended client not initialized")

        best_bid, best_ask = await self.extended_client.fetch_bbo_prices(self.extended_contract_id)

        return best_bid, best_ask

    def round_to_tick(self, price: Decimal) -> Decimal:
        """Round price to tick size."""
        if self.extended_tick_size is None:
            return price
        return (price / self.extended_tick_size).quantize(Decimal('1')) * self.extended_tick_size

    async def place_bbo_order(self, side: str, quantity: Decimal):
        # Get best bid/ask prices
        best_bid, best_ask = await self.fetch_extended_bbo_prices()

        min_allowed = getattr(self.extended_client, "min_order_size", Decimal('0'))
        attempt_quantity = quantity
        reduction_attempts = 0
        max_reductions = 5

        while reduction_attempts <= max_reductions:
            # Place the order using Extended client
            order_result = await self.extended_client.place_open_order(
                contract_id=self.extended_contract_id,
                quantity=attempt_quantity,
                direction=side.lower()
            )

            if order_result.success:
                return order_result.order_id, order_result.price, attempt_quantity

            error_message = order_result.error_message or ''
            lower_error = error_message.lower()
            if ("cost exceeds available balance" in lower_error or "1140" in lower_error) and reduction_attempts < max_reductions:
                self.logger.warning(
                    f"⚠️ Extended rejected size {attempt_quantity} ({error_message}); reducing by 20% and retrying")
                reduced_quantity = (attempt_quantity * Decimal('0.8')).quantize(self.quantity_precision, rounding=ROUND_DOWN)

                # Ensure reduction actually changes the quantity and stays above minimum
                if reduced_quantity == attempt_quantity:
                    reduced_quantity = attempt_quantity - self.quantity_precision
                    reduced_quantity = reduced_quantity.quantize(self.quantity_precision, rounding=ROUND_DOWN)

                if min_allowed and reduced_quantity < min_allowed:
                    raise Exception(
                        f"Insufficient margin even after reductions (min {min_allowed}, attempted {reduced_quantity})")
                if reduced_quantity <= 0:
                    raise Exception("Reduced order quantity reached zero; cannot place order")

                attempt_quantity = reduced_quantity
                reduction_attempts += 1
                continue

            raise Exception(f"Failed to place order: {error_message}")

        raise Exception("Failed to place order after reducing size due to insufficient balance")

    async def place_extended_post_only_order(self, side: str, quantity: Decimal):
        """Place a post-only order on Extended."""
        if not self.extended_client:
            raise Exception("Extended client not initialized")

        quantity = quantity.quantize(self.quantity_precision, rounding=ROUND_DOWN)
        self.extended_order_status = None
        order_id, order_price, placed_quantity = await self.place_bbo_order(side, quantity)
        if placed_quantity != quantity:
            self.logger.warning(f"[OPEN] [Extended] [{side}] Adjusted order size from {quantity} to {placed_quantity} due to margin limits")
        quantity = placed_quantity
        self.logger.info(f"[OPEN] [Extended] [{side}] Placing Extended POST-ONLY order | quantity: {quantity}")

        start_time = time.time()
        last_cancel_time = 0
        
        while not self.stop_flag:
            if self.extended_order_status in ['CANCELED', 'CANCELLED']:
                self.logger.info(f"Order {order_id} was canceled, placing new order")
                previously_filled = self.extended_order_fills.pop(order_id, Decimal('0'))
                if previously_filled > 0:
                    quantity = (quantity - previously_filled).quantize(self.quantity_precision, rounding=ROUND_DOWN)
                    if quantity <= 0:
                        self.logger.info("No remaining quantity after cancellation; skipping new order placement")
                        self.extended_order_status = 'FILLED'
                        break
                self.extended_order_status = None  # Reset to None to trigger new order
                order_id, order_price, placed_quantity = await self.place_bbo_order(side, quantity)
                if placed_quantity != quantity:
                    self.logger.warning(f"[RETRY] [Extended] [{side}] Adjusted order size from {quantity} to {placed_quantity} due to margin limits")
                quantity = placed_quantity
                self.logger.info(f"[RETRY] [Extended] [{side}] Placing Extended POST-ONLY order | quantity: {quantity}")
                start_time = time.time()
                last_cancel_time = 0  # Reset cancel timer
                await asyncio.sleep(0.5)
            elif self.extended_order_status in ['NEW', 'OPEN', 'PENDING', 'CANCELING', 'PARTIALLY_FILLED']:
                await asyncio.sleep(0.5)
                
                # Check if we need to cancel and replace the order
                should_cancel = False
                if side == 'buy':
                    if order_price < self.extended_best_bid:
                        should_cancel = True
                else:
                    if order_price > self.extended_best_ask:
                        should_cancel = True

                # Cancel order if it's been too long or price is off
                current_time = time.time()
                if current_time - start_time > 10:
                    if should_cancel and current_time - last_cancel_time > 5:  # Prevent rapid cancellations
                        try:
                            self.logger.info(f"Canceling order {order_id} due to timeout/price mismatch")
                            cancel_result = await self.extended_client.cancel_order(order_id)
                            self.logger.info(f"cancel_result: {cancel_result}")
                            if cancel_result.success:
                                last_cancel_time = current_time
                                # Don't reset start_time here, let the cancellation trigger new order
                            else:
                                self.logger.error(f"❌ Error canceling Extended order: {cancel_result.error_message}")
                        except Exception as e:
                            self.logger.error(f"❌ Error canceling Extended order: {e}")
                    elif not should_cancel:
                        self.logger.info(f"Waiting for Extended order to be filled (order price is at best bid/ask)")
            elif self.extended_order_status == 'FILLED':
                self.logger.info(f"Order {order_id} filled successfully")
                break
            else:
                if self.extended_order_status is not None:
                    self.logger.error(f"❌ Unknown Extended order status: {self.extended_order_status}")
                    break
                else:
                    await asyncio.sleep(0.5)

    def handle_extended_order_book_update(self, message):
        """Handle Extended order book updates from WebSocket."""
        try:
            if isinstance(message, str):
                message = json.loads(message)

            self.logger.debug(f"Received Extended order book message: {message}")

            # Check if this is an order book update message
            if message.get("type") in ["SNAPSHOT", "DELTA"]:
                data = message.get("data", {})

                if data:
                    # Handle SNAPSHOT - replace entire order book
                    if message.get("type") == "SNAPSHOT":
                        self.extended_order_book['bids'].clear()
                        self.extended_order_book['asks'].clear()

                    # Update bids - Extended format is [{"p": "price", "q": "size"}, ...]
                    bids = data.get('b', [])
                    for bid in bids:
                        if isinstance(bid, dict):
                            price = Decimal(bid.get('p', '0'))
                            size = Decimal(bid.get('q', '0'))
                        else:
                            # Fallback for array format [price, size]
                            price = Decimal(bid[0])
                            size = Decimal(bid[1])
                        
                        if size > 0:
                            self.extended_order_book['bids'][price] = size
                        else:
                            # Remove zero size orders
                            self.extended_order_book['bids'].pop(price, None)

                    # Update asks - Extended format is [{"p": "price", "q": "size"}, ...]
                    asks = data.get('a', [])
                    for ask in asks:
                        if isinstance(ask, dict):
                            price = Decimal(ask.get('p', '0'))
                            size = Decimal(ask.get('q', '0'))
                        else:
                            # Fallback for array format [price, size]
                            price = Decimal(ask[0])
                            size = Decimal(ask[1])
                        
                        if size > 0:
                            self.extended_order_book['asks'][price] = size
                        else:
                            # Remove zero size orders
                            self.extended_order_book['asks'].pop(price, None)

                    # Update best bid and ask
                    if self.extended_order_book['bids']:
                        self.extended_best_bid = max(self.extended_order_book['bids'].keys())
                    if self.extended_order_book['asks']:
                        self.extended_best_ask = min(self.extended_order_book['asks'].keys())

                    if not self.extended_order_book_ready:
                        self.extended_order_book_ready = True
                        self.logger.info(f"📊 Extended order book ready - Best bid: {self.extended_best_bid}, "
                                         f"Best ask: {self.extended_best_ask}")
                    else:
                        self.logger.debug(f"📊 Order book updated - Best bid: {self.extended_best_bid}, "
                                          f"Best ask: {self.extended_best_ask}")

        except Exception as e:
            self.logger.error(f"Error handling Extended order book update: {e}")
            self.logger.error(f"Message content: {message}")

    def handle_extended_order_update(self, order_data):
        """Handle Extended order updates from WebSocket."""
        side = order_data.get('side', '').lower()
        filled_size = Decimal(order_data.get('filled_size', '0'))
        price = Decimal(order_data.get('price', '0'))

        self.last_extended_fill_monotonic = time.monotonic()

        net_position = self.extended_position + self.lighter_position
        if abs(net_position) <= self.position_epsilon:
            self.waiting_for_lighter_fill = False
            self.current_lighter_quantity = Decimal('0')
            self.lighter_order_info = None
            self.logger.info("📋 Net exposure within tolerance; no Lighter order required")
            return

        lighter_side = 'sell' if net_position > 0 else 'buy'
        quantity = abs(net_position)

        self.current_lighter_side = lighter_side
        self.current_lighter_quantity = quantity
        self.current_lighter_price = price

        self.lighter_order_info = {
            'lighter_side': lighter_side,
            'quantity': quantity,
            'price': price
        }

        self.waiting_for_lighter_fill = True

        self.logger.info(f"📋 Ready to place Lighter order: {lighter_side} {quantity} @ {price} (net {net_position})")

    async def place_lighter_market_order(self, lighter_side: str, quantity: Decimal, price: Decimal):
        if not self.lighter_client:
            await self.initialize_lighter_client()

        best_bid, best_ask = self.get_lighter_best_levels()

        # Ensure order book data is available before proceeding
        retries = 0
        max_retries = 20
        required_level_missing = (
            (lighter_side.lower() == 'buy' and best_ask is None) or
            (lighter_side.lower() == 'sell' and best_bid is None)
        )
        while required_level_missing and retries < max_retries and not self.stop_flag:
            if retries == 0:
                self.logger.warning("⚠️ Missing Lighter order book best levels, retrying...")
            await asyncio.sleep(0.1)
            best_bid, best_ask = self.get_lighter_best_levels()
            required_level_missing = (
                (lighter_side.lower() == 'buy' and best_ask is None) or
                (lighter_side.lower() == 'sell' and best_bid is None)
            )
            retries += 1

        if required_level_missing:
            self.logger.error("❌ Unable to determine Lighter best levels; aborting market order placement")
            return None

        if quantity <= self.position_epsilon:
            self.logger.info("⚠️ Lighter quantity below tolerance; skipping hedge")
            return None

        raw_quantity = quantity + self.lighter_residual_quantity
        if self.lighter_quantity_step:
            quantized_quantity = raw_quantity.quantize(self.lighter_quantity_step, rounding=ROUND_DOWN)
            residual = raw_quantity - quantized_quantity
            if residual < 0:
                residual = Decimal('0')
            self.lighter_residual_quantity = residual
            quantity = quantized_quantity
        else:
            quantity = raw_quantity
            self.lighter_residual_quantity = Decimal('0')

        if quantity <= self.position_epsilon:
            self.logger.info("⚠️ Lighter quantity rounded below tolerance; residual deferred")
            return None

        # Determine order parameters
        if lighter_side.lower() == 'buy':
            is_ask = False
            multiplier, bps_offset = self._choose_lighter_multiplier(lighter_side)
            raw_price = best_ask[0] * multiplier
        else:
            is_ask = True
            multiplier, bps_offset = self._choose_lighter_multiplier(lighter_side)
            raw_price = best_bid[0] * multiplier

        max_attempts = 3
        last_error = None

        for attempt in range(max_attempts):
            if self.stop_flag:
                return None
            try:
                aggressiveness = Decimal('1') + Decimal('0.0005') * attempt
                best_bid, best_ask = self.get_lighter_best_levels()
                if best_bid is None or best_ask is None:
                    raise RuntimeError("Lighter best levels unavailable during hedge")

                multiplier, bps_offset = self._choose_lighter_multiplier(lighter_side)
                if lighter_side.lower() == 'buy':
                    is_ask = False
                    raw_price = best_ask[0] * multiplier * aggressiveness
                else:
                    is_ask = True
                    raw_price = best_bid[0] * multiplier / aggressiveness

                if self.lighter_price_step:
                    price = raw_price.quantize(self.lighter_price_step, rounding=ROUND_DOWN)
                else:
                    price = raw_price

                self.logger.info(
                    f"Placing Lighter market order (attempt {attempt + 1}/{max_attempts}): "
                    f"{lighter_side} {quantity} | is_ask: {is_ask} | hedge offset: {bps_offset} bps | price: {price}"
                )

                # Reset order state
                self.lighter_order_filled = False
                self.lighter_order_price = price
                self.lighter_order_side = lighter_side
                self.lighter_order_size = quantity

                client_order_index = int(time.time() * 1000)
                tx_info, error = self.lighter_client.sign_create_order(
                    market_index=self.lighter_market_index,
                    client_order_index=client_order_index,
                    base_amount=int(quantity * self.base_amount_multiplier),
                    price=int(price * self.price_multiplier),
                    is_ask=is_ask,
                    order_type=self.lighter_client.ORDER_TYPE_LIMIT,
                    time_in_force=self.lighter_client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                    reduce_only=False,
                    trigger_price=0,
                )
                if error is not None:
                    raise Exception(f"Sign error: {error}")

                tx_hash = await self.lighter_client.send_tx(
                    tx_type=self.lighter_client.TX_TYPE_CREATE_ORDER,
                    tx_info=tx_info
                )
                self.logger.info(f"🚀 Lighter limit order sent: {lighter_side} {quantity} (tx: {tx_hash})")
                send_time = time.monotonic()
                if self.last_extended_fill_monotonic is not None:
                    self.logger.info(
                        f"⏱️ Hedge send latency: {(send_time - self.last_extended_fill_monotonic) * 1000:.1f} ms"
                    )
                self.last_lighter_send_monotonic = send_time
                filled = await self.monitor_lighter_order(client_order_index)
                if filled:
                    now = time.monotonic()
                    if self.last_lighter_send_monotonic is not None:
                        self.logger.info(
                            f"⏱️ Hedge fill latency (send→fill): {(now - self.last_lighter_send_monotonic) * 1000:.1f} ms"
                        )
                    if self.last_extended_fill_monotonic is not None:
                        self.logger.info(
                            f"⏱️ Hedge total latency (fill→fill): {(now - self.last_extended_fill_monotonic) * 1000:.1f} ms"
                        )
                    return tx_hash

                last_error = RuntimeError("Timeout awaiting Lighter fill")
                self.logger.error("❌ Lighter order timed out; stopping retries")
                break
            except Exception as e:
                last_error = e
                self.logger.error(f"❌ Lighter order attempt {attempt + 1} failed: {e}")
                await asyncio.sleep(0.2)
                continue

        self.waiting_for_lighter_fill = False
        await self._handle_hedge_failure("lighter_order_failure", str(last_error))
        return None

    async def monitor_lighter_order(self, client_order_index: int) -> bool:
        """Monitor Lighter order and adjust price if needed."""
        self.logger.info(f"🔍 Starting to monitor Lighter order - Order ID: {client_order_index}")

        start_time = time.time()
        while not self.lighter_order_filled and not self.stop_flag:
            # Check for timeout (30 seconds total)
            if time.time() - start_time > 30:
                self.logger.error(f"❌ Timeout waiting for Lighter order fill after {time.time() - start_time:.1f}s")
                try:
                    await self.lighter_client.cancel_order(
                        market_index=self.lighter_market_index,
                        order_index=client_order_index
                    )
                except Exception as cancel_error:
                    self.logger.error(f"❌ Failed to cancel timed-out Lighter order: {cancel_error}")
                self.waiting_for_lighter_fill = False
                self.order_execution_complete = False
                return False

            await asyncio.sleep(0.1)  # Check every 100ms
        return True

    async def modify_lighter_order(self, client_order_index: int, new_price: Decimal):
        """Modify current Lighter order with new price using client_order_index."""
        try:
            if client_order_index is None:
                self.logger.error("❌ Cannot modify order - no order ID available")
                return

            # Calculate new Lighter price
            lighter_price = int(new_price * self.price_multiplier)

            self.logger.info(f"🔧 Attempting to modify order - Market: {self.lighter_market_index}, "
                             f"Client Order Index: {client_order_index}, New Price: {lighter_price}")

            # Use the native SignerClient's modify_order method
            tx_info, tx_hash, error = await self.lighter_client.modify_order(
                market_index=self.lighter_market_index,
                order_index=client_order_index,  # Use client_order_index directly
                base_amount=int(self.lighter_order_size * self.base_amount_multiplier),
                price=lighter_price,
                trigger_price=0
            )

            if error is not None:
                self.logger.error(f"❌ Lighter order modification error: {error}")
                return

            self.lighter_order_price = new_price
            self.logger.info(f"🔄 Lighter order modified successfully: {self.lighter_order_side} "
                             f"{self.lighter_order_size} @ {new_price}")

        except Exception as e:
            self.logger.error(f"❌ Error modifying Lighter order: {e}")
            import traceback
            self.logger.error(f"❌ Full traceback: {traceback.format_exc()}")

    async def reconcile_positions(self, reason: str = "iteration") -> Tuple[Decimal, Decimal]:
        """Refresh internal position trackers from live exchange snapshots."""
        extended_position, extended_available, extended_equity = await self._fetch_extended_snapshot()
        lighter_position, lighter_available, lighter_collateral = await self._fetch_lighter_snapshot()

        self.extended_position = extended_position
        self.lighter_position = lighter_position
        self.latest_extended_available = extended_available
        self.latest_extended_equity = extended_equity
        self.latest_lighter_available = lighter_available
        self.latest_lighter_collateral = lighter_collateral

        self.logger.info(
            f"🔁 Reconciled positions ({reason}) | Extended: {self.extended_position} | "
            f"Lighter: {self.lighter_position}"
        )
        return extended_position, lighter_position

    async def _cancel_all_extended_orders(self):
        if not self.extended_client:
            return
        try:
            orders = await self.extended_client.get_active_orders(self.extended_contract_id)
            for order in orders:
                try:
                    await self.extended_client.cancel_order(order.order_id)
                except Exception as cancel_error:
                    self.logger.error(f"⚠️ Failed to cancel Extended order {order.order_id}: {cancel_error}")
        except Exception as e:
            self.logger.error(f"⚠️ Error fetching Extended active orders: {e}")

    async def _cancel_all_lighter_orders(self):
        if not self.lighter_client:
            return
        try:
            expiry = int(time.time() + 60)
            auth_token, err = self.lighter_client.create_auth_token_with_expiry(expiry)
            if err is not None:
                self.logger.warning(f"⚠️ Failed to create auth token for cancelling Lighter orders: {err}")
                return
            order_api = lighter.OrderApi(self.lighter_client.api_client)
            orders_response = await order_api.account_active_orders(
                account_index=self.account_index,
                market_id=self.lighter_market_index,
                auth=auth_token
            )
            if not orders_response or not getattr(orders_response, "orders", None):
                return
            for order in orders_response.orders:
                try:
                    await self.lighter_client.cancel_order(
                        market_index=self.lighter_market_index,
                        order_index=order.order_index
                    )
                except Exception as cancel_error:
                    self.logger.error(f"⚠️ Failed to cancel Lighter order {order.order_index}: {cancel_error}")
        except Exception as e:
            self.logger.error(f"⚠️ Error cancelling Lighter orders: {e}")

    async def _place_aggressive_extended_order(self, side: str, quantity: Decimal) -> None:
        if quantity <= 0 or not self.extended_client:
            return
        best_bid, best_ask = await self.fetch_extended_bbo_prices()
        if best_bid <= 0 or best_ask <= 0:
            raise RuntimeError("Extended BBO unavailable for flattening")

        if side == 'sell':
            price = best_bid * Decimal('0.999')
            order_side = OrderSide.SELL
        else:
            price = best_ask * Decimal('1.001')
            order_side = OrderSide.BUY

        price = self.round_to_tick(price)
        client = self.extended_client.perpetual_trading_client
        response = await client.place_order(
            market_name=self.extended_contract_id,
            amount_of_synthetic=quantity,
            price=price,
            side=order_side,
            post_only=False,
            time_in_force=TimeInForce.IOC,
            expire_time=utc_now() + timedelta(minutes=1),
        )
        if response.status != ResponseStatus.OK:
            raise RuntimeError(f"Extended flatten order failed: {response.error}")

    async def _place_aggressive_lighter_order(self, side: str, quantity: Decimal) -> None:
        if quantity <= 0 or not self.lighter_client:
            return

        best_bid, best_ask = self.get_lighter_best_levels()
        if best_bid is None or best_ask is None:
            raise RuntimeError("Lighter BBO unavailable for flattening")

        if side == 'sell':
            raw_price = best_bid[0] * Decimal('0.999')
            is_ask = True
        else:
            raw_price = best_ask[0] * Decimal('1.001')
            is_ask = False

        if self.lighter_price_step:
            price = raw_price.quantize(self.lighter_price_step, rounding=ROUND_DOWN)
        else:
            price = raw_price

        if self.lighter_quantity_step:
            quantity = quantity.quantize(self.lighter_quantity_step, rounding=ROUND_DOWN)

        client_order_index = int(time.time() * 1000)
        tx_info, error = self.lighter_client.sign_create_order(
            market_index=self.lighter_market_index,
            client_order_index=client_order_index,
            base_amount=int(quantity * self.base_amount_multiplier),
            price=int(price * self.price_multiplier),
            is_ask=is_ask,
            order_type=self.lighter_client.ORDER_TYPE_LIMIT,
            time_in_force=self.lighter_client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            reduce_only=False,
            trigger_price=0,
        )
        if error is not None:
            raise RuntimeError(f"Lighter flatten sign error: {error}")

        tx_hash = await self.lighter_client.send_tx(
            tx_type=self.lighter_client.TX_TYPE_CREATE_ORDER,
            tx_info=tx_info
        )
        self.logger.info(f"🚨 Lighter flatten order submitted (tx: {tx_hash})")

    async def _send_telegram_message(self, message: str):
        if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
            self.logger.warning("Telegram credentials not set; skipping alert")
            return

        await send_telegram_message(message)

    async def _handle_hedge_failure(self, reason: str, details: str):
        self.logger.error(f"🚨 Hedge failure detected ({reason}): {details}")
        failure_exception = None
        try:
            await self._cancel_all_extended_orders()
        except Exception as extend_cancel_error:
            self.logger.error(f"⚠️ Failed to cancel Extended orders during hedge failure: {extend_cancel_error}")
        try:
            await self._cancel_all_lighter_orders()
        except Exception as lighter_cancel_error:
            self.logger.error(f"⚠️ Failed to cancel Lighter orders during hedge failure: {lighter_cancel_error}")

        await self.reconcile_positions("hedge_failure_pre_flatten")

        tolerance = Decimal('0.01')
        try:
            if abs(self.extended_position) >= abs(self.lighter_position):
                side = 'sell' if self.extended_position > 0 else 'buy'
                await self._place_aggressive_extended_order(side, abs(self.extended_position))
            else:
                side = 'sell' if self.lighter_position > 0 else 'buy'
                await self._place_aggressive_lighter_order(side, abs(self.lighter_position))
        except Exception as flatten_error:
            self.logger.error(f"❌ Auto-flatten failed: {flatten_error}")

        await self.reconcile_positions("hedge_failure_post_flatten")

        if abs(self.extended_position) > tolerance or abs(self.lighter_position) > tolerance:
            message = "Residual exposure remains after auto-flatten; stopping bot for safety"
            self.logger.error(f"⚠️ {message}")
            self.stop_flag = True
            self.order_execution_complete = False
            failure_exception = RuntimeError(message)
        else:
            self.logger.info("✅ Exposure neutralised after hedge failure")
            self.order_execution_complete = True
            failure_exception = None

        self.waiting_for_lighter_fill = False

        await self._send_telegram_message(
            f"[{self.ticker}] Hedge failure handled ({reason}). Details: {details}. "
            f"Extended pos: {self.extended_position}, Lighter pos: {self.lighter_position}"
        )

        if failure_exception:
            self.last_exception = failure_exception
        else:
            self.last_exception = None

    def _compute_dynamic_tolerance(self, target_quantity: Optional[Decimal] = None) -> Decimal:
        base = target_quantity if target_quantity is not None else self.order_size_max
        if base is None or base <= 0:
            base = Decimal('0.2')
        tolerance = base * Decimal('0.05')
        return max(tolerance, Decimal('0.02'))

    async def _ensure_balanced_positions(self, context: str, target_quantity: Optional[Decimal] = None) -> bool:
        tolerance = self._compute_dynamic_tolerance(target_quantity)
        imbalance = self.extended_position + self.lighter_position
        if abs(imbalance) <= tolerance:
            return True

        self.logger.warning(
            f"⚠️ Position imbalance detected ({context}): {imbalance} exceeds tolerance {tolerance}"
        )
        await self._handle_hedge_failure(f"imbalance_{context}", f"imbalance={imbalance}, tolerance={tolerance}")
        return abs(self.extended_position + self.lighter_position) <= tolerance


    async def setup_extended_websocket(self):
        """Setup Extended websocket for order updates and order book data."""
        if not self.extended_client:
            raise Exception("Extended client not initialized")

        def order_update_handler(order_data):
            """Handle order updates from Extended WebSocket."""
            if order_data.get('contract_id') != self.extended_contract_id:
                self.logger.info(f"Ignoring order update from {order_data.get('contract_id')}")
                return

            try:
                order_id = order_data.get('order_id')
                status = order_data.get('status')
                side = order_data.get('side', '').lower()
                filled_size = Decimal(order_data.get('filled_size', '0'))
                size = Decimal(order_data.get('size', '0'))
                price = order_data.get('price', '0')

                if side == 'buy':
                    order_type = "OPEN"
                else:
                    order_type = "CLOSE"

                previous_filled = self.extended_order_fills.get(order_id, Decimal('0'))
                delta_fill = filled_size - previous_filled
                if delta_fill < 0:
                    self.logger.warning(
                        f"⚠️ Received decreasing filled size for order {order_id}: {filled_size} < {previous_filled}. "
                        f"Resetting tracker.")
                    delta_fill = filled_size
                if delta_fill > 0:
                    if side == 'buy':
                        self.extended_position += delta_fill
                    else:
                        self.extended_position -= delta_fill
                self.extended_order_fills[order_id] = filled_size

                if delta_fill > 0:
                    price_decimal = Decimal(price)
                    volume_delta = abs(delta_fill)
                    self.extended_volume_base += volume_delta
                    self.extended_volume_notional += volume_delta * abs(price_decimal)
                    self._schedule_trade_log(
                        exchange='Extended',
                        side=side,
                        price=str(price),
                        quantity=str(delta_fill)
                    )

                    self.handle_extended_order_update({
                        'order_id': order_id,
                        'side': side,
                        'status': status,
                        'size': size,
                        'price': price,
                        'contract_id': self.extended_contract_id,
                        'filled_size': delta_fill
                    })

                # Handle the order update
                if status == 'FILLED':
                    self.logger.info(f"[{order_id}] [{order_type}] [Extended] [{status}]: {filled_size} @ {price}")
                    self.extended_order_status = status
                    self.extended_order_fills.pop(order_id, None)
                else:
                    if status == 'OPEN':
                        self.logger.info(f"[{order_id}] [{order_type}] [Extended] [{status}]: {size} @ {price}")
                    else:
                        self.logger.info(f"[{order_id}] [{order_type}] [Extended] [{status}]: {filled_size} @ {price}")
                    # Update order status for all non-filled statuses
                    if status == 'PARTIALLY_FILLED':
                        self.extended_order_status = "OPEN"
                    elif status in ['CANCELED', 'CANCELLED']:
                        self.extended_order_status = status
                    elif status in ['NEW', 'OPEN', 'PENDING', 'CANCELING']:
                        self.extended_order_status = status
                    else:
                        self.logger.warning(f"Unknown order status: {status}")
                        self.extended_order_status = status

            except Exception as e:
                self.logger.error(f"Error handling Extended order update: {e}")

        try:
            # Setup order update handler
            self.extended_client.setup_order_update_handler(order_update_handler)
            self.logger.info("✅ Extended WebSocket order update handler set up")

            # Connect to Extended WebSocket
            await self.extended_client.connect()
            self.logger.info("✅ Extended WebSocket connection established")

            # Setup separate WebSocket connection for depth updates
            await self.setup_extended_depth_websocket()

        except Exception as e:
            self.logger.error(f"Could not setup Extended WebSocket handlers: {e}")

    async def setup_extended_depth_websocket(self):
        """Setup separate WebSocket connection for Extended depth updates."""
        try:
            import websockets

            async def handle_depth_websocket():
                """Handle depth WebSocket connection."""
                # Use the correct Extended WebSocket URL for order book stream
                market_name = f"{self.ticker}-USD"  # Extended uses format like BTC-USD
                url = f"wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks/{market_name}?depth=1"

                while not self.stop_flag:
                    try:
                        async with websockets.connect(url) as ws:
                            self.logger.info(f"✅ Connected to Extended order book stream for {market_name}")

                            # Listen for messages
                            async for message in ws:
                                if self.stop_flag:
                                    break

                                try:
                                    # Handle ping frames
                                    if isinstance(message, bytes) and message == b'\x09':
                                        await ws.pong()
                                        continue

                                    data = json.loads(message)
                                    self.logger.debug(f"Received Extended order book message: {data}")

                                    # Handle order book updates
                                    if data.get("type") in ["SNAPSHOT", "DELTA"]:
                                        self.handle_extended_order_book_update(data)

                                except json.JSONDecodeError as e:
                                    self.logger.warning(f"Failed to parse Extended order book message: {e}")
                                except Exception as e:
                                    self.logger.error(f"Error handling Extended order book message: {e}")

                    except websockets.exceptions.ConnectionClosed:
                        self.logger.warning("Extended order book WebSocket connection closed, reconnecting...")
                    except Exception as e:
                        self.logger.error(f"Extended order book WebSocket error: {e}")

                    # Wait before reconnecting
                    if not self.stop_flag:
                        await asyncio.sleep(2)

            # Start depth WebSocket in background
            self.extended_depth_task = asyncio.create_task(handle_depth_websocket())
            self.logger.info("✅ Extended order book WebSocket task started")

        except Exception as e:
            self.logger.error(f"Could not setup Extended order book WebSocket: {e}")

    async def trading_loop(self):
        """Main trading loop implementing the new strategy."""
        self.logger.info(f"🚀 Starting hedge bot for {self.ticker}")
        self.last_exception = None

        # Initialize clients
        try:
            self.initialize_lighter_client()
            self.initialize_extended_client()

            # Get contract info
            self.extended_contract_id, self.extended_tick_size = await self.get_extended_contract_info()
            self.lighter_market_index, self.base_amount_multiplier, self.price_multiplier = self.get_lighter_market_config()
            if self.price_multiplier:
                self.lighter_price_step = Decimal('1') / Decimal(self.price_multiplier)
            if self.base_amount_multiplier:
                self.lighter_quantity_step = Decimal('1') / Decimal(self.base_amount_multiplier)

            self.logger.info(f"Contract info loaded - Extended: {self.extended_contract_id}, "
                             f"Lighter: {self.lighter_market_index}")

        except Exception as e:
            self.logger.error(f"❌ Failed to initialize: {e}")
            return

        # Setup Extended websocket
        try:
            await self.setup_extended_websocket()
            self.logger.info("✅ Extended WebSocket connection established")

            # Wait for initial order book data with timeout
            self.logger.info("⏳ Waiting for initial order book data...")
            timeout = 10  # seconds
            start_time = time.time()
            while not self.extended_order_book_ready and not self.stop_flag:
                if time.time() - start_time > timeout:
                    self.logger.warning(f"⚠️ Timeout waiting for WebSocket order book data after {timeout}s")
                    break
                await asyncio.sleep(0.5)

            if self.extended_order_book_ready:
                self.logger.info("✅ WebSocket order book data received")
            else:
                self.logger.warning("⚠️ WebSocket order book not ready, will use REST API fallback")

        except Exception as e:
            self.logger.error(f"❌ Failed to setup Extended websocket: {e}")
            return

        # Setup Lighter websocket
        try:
            self.lighter_ws_task = asyncio.create_task(self.handle_lighter_ws())
            self.logger.info("✅ Lighter WebSocket task started")

            # Wait for initial Lighter order book data with timeout
            self.logger.info("⏳ Waiting for initial Lighter order book data...")
            timeout = 10  # seconds
            start_time = time.time()
            while not self.lighter_order_book_ready and not self.stop_flag:
                if time.time() - start_time > timeout:
                    self.logger.warning(f"⚠️ Timeout waiting for Lighter WebSocket order book data after {timeout}s")
                    break
                await asyncio.sleep(0.5)

            if self.lighter_order_book_ready:
                self.logger.info("✅ Lighter WebSocket order book data received")
            else:
                self.logger.warning("⚠️ Lighter WebSocket order book not ready")

        except Exception as e:
            self.logger.error(f"❌ Failed to setup Lighter websocket: {e}")
            return

        try:
            await self.reconcile_positions("startup")
        except Exception as reconcile_error:
            self.logger.error(f"⚠️ Failed to reconcile positions at startup: {reconcile_error}")

        await self._send_telegram_message(
            f"[{self.ticker}] Hedge bot starting - hold window {self.hold_min}s to {self.hold_max}s"
        )

        await asyncio.sleep(5)

        iterations = 0
        while iterations < self.iterations and not self.stop_flag:
            iterations += 1
            self.logger.info("-----------------------------------------------")
            self.logger.info(f"🔄 Trading loop iteration {iterations}")
            self.logger.info("-----------------------------------------------")

            try:
                await self.reconcile_positions(f"iteration_{iterations}_start")
            except Exception as reconcile_error:
                self.logger.error(f"⚠️ Failed to reconcile positions at iteration start: {reconcile_error}")

            if not await self._ensure_balanced_positions(f"iteration_{iterations}_start"):
                if self.last_exception:
                    break
                if self.stop_flag:
                    break
                await asyncio.sleep(1)
                continue

            self.current_cycle_quantity = None
            self.current_cycle_open_side = None
            self.last_hold_duration = 0.0

            self.logger.info(f"[STEP 1] Extended position: {self.extended_position} | Lighter position: {self.lighter_position}")

            self.order_execution_complete = False
            self.waiting_for_lighter_fill = False
            try:
                if self.direction_mode == 'random':
                    self.current_cycle_open_side = 'buy' if self._rng.random() < 0.5 else 'sell'
                else:
                    self.current_cycle_open_side = self.direction_mode
                await self._maybe_random_delay(f"placing Extended {self.current_cycle_open_side.upper()} order (step 1)")
                self.current_cycle_quantity = self._choose_order_quantity()
                self.last_cycle_open_quantity = self.current_cycle_quantity
                self.logger.info(f"🎯 Step 1 order size selected: {self.current_cycle_quantity} "
                                 f"({self.current_cycle_open_side})")
                await self._ensure_balanced_positions(
                    f"iteration_{iterations}_pre_step1", self.current_cycle_quantity
                )
                await self.place_extended_post_only_order(self.current_cycle_open_side, self.current_cycle_quantity)
            except Exception as e:
                self.logger.error(f"⚠️ Error in trading loop: {e}")
                self.logger.error(f"⚠️ Full traceback: {traceback.format_exc()}")
                self.last_exception = e
                self.stop_flag = True
                break

            start_time = time.time()
            while not self.order_execution_complete and not self.stop_flag:
                # Check if Extended order filled and we need to place Lighter order
                if self.waiting_for_lighter_fill:
                    await self.place_lighter_market_order(
                        self.current_lighter_side,
                        self.current_lighter_quantity,
                        self.current_lighter_price
                    )
                    break

                await asyncio.sleep(0.01)
                if time.time() - start_time > 180:
                    self.logger.error("❌ Timeout waiting for trade completion")
                    break

            if not self.stop_flag and not self.last_exception:
                asyncio.create_task(self._send_iteration_summary(iterations))

            if self.stop_flag:
                break

            if not await self._ensure_balanced_positions(f"iteration_{iterations}_end"):
                if self.last_exception:
                    break
                if self.stop_flag:
                    break

            if not self.stop_flag and self.hold_max > 0:
                hold_duration = self.hold_min if self.hold_max == self.hold_min else self._rng.uniform(self.hold_min, self.hold_max)
                if hold_duration > 0:
                    await self._hold_position(hold_duration, f"iteration_{iterations}_pre_step2")
                    if self.stop_flag:
                        break

            if self.stop_flag:
                break

            # Close position
            self.logger.info(f"[STEP 2] Extended position: {self.extended_position} | Lighter position: {self.lighter_position}")
            self.order_execution_complete = False
            self.waiting_for_lighter_fill = False
            try:
                close_quantity = abs(self.extended_position)
                if close_quantity == 0:
                    self.logger.info("No Extended exposure to neutralize in Step 2, skipping order")
                    self.order_execution_complete = True
                else:
                    if self.extended_position > 0:
                        close_side = 'sell'
                    else:
                        close_side = 'buy'
                    await self._maybe_random_delay(
                        f"placing Extended {close_side.upper()} order (step 2)",
                        require_flat=False
                    )
                    self.logger.info(f"🎯 Step 2 order size selected: {close_quantity} ({close_side})")
                    await self.place_extended_post_only_order(close_side, close_quantity)
            except Exception as e:
                self.logger.error(f"⚠️ Error in trading loop: {e}")
                self.logger.error(f"⚠️ Full traceback: {traceback.format_exc()}")
                self.last_exception = e
                self.stop_flag = True
                break

            while not self.order_execution_complete and not self.stop_flag:
                # Check if Extended order filled and we need to place Lighter order
                if self.waiting_for_lighter_fill:
                    await self.place_lighter_market_order(
                        self.current_lighter_side,
                        self.current_lighter_quantity,
                        self.current_lighter_price
                    )
                    break

                await asyncio.sleep(0.01)
                if time.time() - start_time > 180:
                    self.logger.error("❌ Timeout waiting for trade completion")
                    break

            # Close remaining position
            self.logger.info(f"[STEP 3] Extended position: {self.extended_position} | Lighter position: {self.lighter_position}")
            self.order_execution_complete = False
            self.waiting_for_lighter_fill = False
            if self.extended_position == 0:
                continue
            elif self.extended_position > 0:
                side = 'sell'
            else:
                side = 'buy'

            try:
                # Determine side based on some logic (for now, alternate)
                quantity = abs(self.extended_position)
                await self._maybe_random_delay(
                    f"placing Extended {side.upper()} order (step 3)",
                    require_flat=False
                )
                self.logger.info(f"🎯 Step 3 residual order size: {quantity} ({side})")
                await self.place_extended_post_only_order(side, quantity)
            except Exception as e:
                self.logger.error(f"⚠️ Error in trading loop: {e}")
                self.logger.error(f"⚠️ Full traceback: {traceback.format_exc()}")
                self.last_exception = e
                self.stop_flag = True
                break

            # Wait for order to be filled via WebSocket
            while not self.order_execution_complete and not self.stop_flag:
                # Check if Extended order filled and we need to place Lighter order
                if self.waiting_for_lighter_fill:
                    await self.place_lighter_market_order(
                        self.current_lighter_side,
                        self.current_lighter_quantity,
                        self.current_lighter_price
                    )
                    break

                await asyncio.sleep(0.01)
                if time.time() - start_time > 180:
                    self.logger.error("❌ Timeout waiting for trade completion")
                    break

        if self.last_exception:
            raise self.last_exception

    async def run(self):
        """Run the hedge bot."""
        self.setup_signal_handlers()

        try:
            await self.trading_loop()
        except KeyboardInterrupt:
            self.logger.info("\n🛑 Received interrupt signal...")
        finally:
            self.logger.info("🔄 Cleaning up...")
            await self.shutdown_async()


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description='Trading bot for Extended and Lighter')
    parser.add_argument('--exchange', type=str,
                        help='Exchange')
    parser.add_argument('--ticker', type=str, default='BTC',
                        help='Ticker symbol (default: BTC)')
    parser.add_argument('--size', type=str,
                        help='Number of tokens to buy/sell per order')
    parser.add_argument('--iter', type=int,
                        help='Number of iterations to run')
    parser.add_argument('--fill-timeout', type=int, default=5,
                        help='Timeout in seconds for maker order fills (default: 5)')

    return parser.parse_args()
