#!/usr/bin/env python3
"""
Hedge Mode Entry Point

This script serves as the main entry point for hedge mode trading.
It imports and runs the appropriate hedge mode implementation based on the exchange parameter.

Usage:
    python hedge_mode.py --exchange <exchange> [other arguments]

Supported exchanges:
    - backpack: Uses HedgeBot from hedge_mode_bp.py (Backpack + Lighter)
    - extended: Uses HedgeBot from hedge_mode_ext.py (Extended + Lighter)
    - apex: Uses HedgeBot from hedge_mode_apex.py (Apex + Lighter)
    - grvt: Uses HedgeBot from hedge_mode_grvt.py (GRVT + Lighter)

Cross-platform compatibility:
    - Works on Linux, macOS, and Windows
    - Direct imports instead of subprocess calls for better performance
"""

import asyncio
import sys
import argparse
from decimal import Decimal
from pathlib import Path
import dotenv

def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Hedge Mode Trading Bot Entry Point',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python hedge_mode.py --exchange backpack --ticker BTC --size 0.002 --iter 10
    python hedge_mode.py --exchange extended --ticker ETH --size 0.1 --iter 5
    python hedge_mode.py --exchange apex --ticker BTC --size 0.002 --iter 10
    python hedge_mode.py --exchange grvt --ticker BTC --size 0.05 --iter 10
        """
    )
    
    parser.add_argument('--exchange', type=str, required=True,
                        help='Exchange to use (backpack, extended, apex, or grvt)')
    parser.add_argument('--ticker', type=str, default='BTC',
                        help='Ticker symbol (default: BTC)')
    parser.add_argument('--size', type=str, required=True,
                        help='Number of tokens to buy/sell per order')
    parser.add_argument('--iter', type=int, required=True,
                        help='Number of iterations to run')
    parser.add_argument('--fill-timeout', type=int, default=5,
                        help='Timeout in seconds for maker order fills (default: 5)')
    parser.add_argument('--env-file', type=str, default=".env",
                        help=".env file path (default: .env)")
    parser.add_argument('--size-min', type=str,
                        help='Minimum order size when randomizing order quantity')
    parser.add_argument('--size-max', type=str,
                        help='Maximum order size when randomizing order quantity')
    parser.add_argument('--size-step', type=str,
                        help='Step size to align randomized order quantities')
    parser.add_argument('--delay-min', type=float,
                        help='Minimum delay in seconds between placing Extended orders')
    parser.add_argument('--delay-max', type=float,
                        help='Maximum delay in seconds between placing Extended orders')
    parser.add_argument('--buy-offset-bps-min', type=float,
                        help='Minimum markup in basis points for Lighter hedge buys')
    parser.add_argument('--buy-offset-bps-max', type=float,
                        help='Maximum markup in basis points for Lighter hedge buys')
    parser.add_argument('--sell-offset-bps-min', type=float,
                        help='Minimum markdown in basis points for Lighter hedge sells')
    parser.add_argument('--sell-offset-bps-max', type=float,
                        help='Maximum markdown in basis points for Lighter hedge sells')
    parser.add_argument('--direction-mode', type=str, choices=['buy', 'sell', 'random'],
                        help='(Extended only) Opening order direction: always buy, always sell, or random')
    parser.add_argument('--hold-min', type=float,
                        help='(Extended only) Minimum seconds to keep exposure open before hedging back')
    parser.add_argument('--hold-max', type=float,
                        help='(Extended only) Maximum seconds to keep exposure open before hedging back')
    parser.add_argument('--bot-name', type=str,
                        help='Identifier used in alerts/logs for this bot')
    
    return parser.parse_args()


def validate_exchange(exchange):
    """Validate that the exchange is supported."""
    supported_exchanges = ['backpack', 'extended', 'apex', 'grvt']
    if exchange.lower() not in supported_exchanges:
        print(f"Error: Unsupported exchange '{exchange}'")
        print(f"Supported exchanges: {', '.join(supported_exchanges)}")
        sys.exit(1)


def get_hedge_bot_class(exchange):
    """Import and return the appropriate HedgeBot class."""
    try:
        if exchange.lower() == 'backpack':
            from hedge.hedge_mode_bp import HedgeBot
            return HedgeBot
        elif exchange.lower() == 'extended':
            from hedge.hedge_mode_ext import HedgeBot
            return HedgeBot
        elif exchange.lower() == 'apex':
            from hedge.hedge_mode_apex import HedgeBot
            return HedgeBot
        elif exchange.lower() == 'grvt':
            from hedge.hedge_mode_grvt import HedgeBot
            return HedgeBot
        else:
            raise ValueError(f"Unsupported exchange: {exchange}")
    except ImportError as e:
        print(f"Error importing hedge mode implementation: {e}")
        sys.exit(1)


async def main():
    """Main entry point that creates and runs the appropriate hedge bot."""
    args = parse_arguments()

    env_path = Path(args.env_file)
    if not env_path.exists():
        print(f"Env file not find: {env_path.resolve()}")
        sys.exit(1)
    dotenv.load_dotenv(args.env_file)
    
    # Validate exchange
    validate_exchange(args.exchange)
    
    # Get the appropriate HedgeBot class
    try:
        HedgeBotClass = get_hedge_bot_class(args.exchange)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
    
    print(f"Starting hedge mode for {args.exchange} exchange...")
    print(f"Ticker: {args.ticker}, Size: {args.size}, Iterations: {args.iter}")
    print("-" * 50)
    
    try:
        # Create the hedge bot instance
        bot_kwargs = {
            'ticker': args.ticker.upper(),
            'order_quantity': Decimal(args.size),
            'fill_timeout': args.fill_timeout,
            'iterations': args.iter,
            'bot_name': args.bot_name
        }

        if args.exchange.lower() == 'extended':
            bot_kwargs.update({
                'order_size_min': Decimal(args.size_min) if args.size_min else None,
                'order_size_max': Decimal(args.size_max) if args.size_max else None,
                'order_size_step': Decimal(args.size_step) if args.size_step else None,
                'order_delay_min': args.delay_min,
                'order_delay_max': args.delay_max,
                'lighter_buy_offset_bps_min': Decimal(str(args.buy_offset_bps_min)) if args.buy_offset_bps_min is not None else None,
                'lighter_buy_offset_bps_max': Decimal(str(args.buy_offset_bps_max)) if args.buy_offset_bps_max is not None else None,
                'lighter_sell_offset_bps_min': Decimal(str(args.sell_offset_bps_min)) if args.sell_offset_bps_min is not None else None,
                'lighter_sell_offset_bps_max': Decimal(str(args.sell_offset_bps_max)) if args.sell_offset_bps_max is not None else None,
                'direction_mode': args.direction_mode or 'random',
                'hold_min': args.hold_min,
                'hold_max': args.hold_max,
            })

        bot = HedgeBotClass(**bot_kwargs)
        
        # Run the bot
        await bot.run()
        
    except KeyboardInterrupt:
        print("\nHedge mode interrupted by user")
        return 1
    except Exception as e:
        print(f"Error running hedge mode: {e}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
