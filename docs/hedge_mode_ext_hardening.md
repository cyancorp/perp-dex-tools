# Hedge Mode (Extended) Hardening Plan

This document lists the key production hardening gaps observed in `hedge/hedge_mode_ext.py` and the concrete engineering work needed to mitigate them. Items are ordered by priority. Each section explains the risk, the relevant code, the intended behaviour, and an outline detailed enough for another agent to implement.

---

## P0 — Critical Before Production

### 1. Fix Extended Position Bookkeeping on Partial Fills
- **Problem**  
  - `handle_extended_order_update` only adjusts `self.extended_position` when `status == 'FILLED'` (`hedge/hedge_mode_ext.py:1048-1075`).  
  - Extended frequently emits partial fills followed by cancels/replacements (e.g., order `1981330673612984320` at `logs/extended_SOL_hedge_mode_log.txt:805-812`). The partial quantity (1.33 SOL) is filled on the exchange but never deducted locally, so the next replacement order sells the full amount a second time.  
  - When replacements finish, the bot believes it is flat while Extended still has residual exposure (observed +0.66 SOL during iteration 14). This breaks hedging and downstream risk checks.
- **What to Implement**  
  1. Track cumulative filled size per Extended order. Update `self.extended_position` on every fill event (`NEW`→`PARTIALLY_FILLED`, `PARTIALLY_FILLED`, and `CANCELED`/`CANCELLED` with `filled_size > 0`), not just on the terminal `FILLED`.  
  2. Store per-order state (e.g., `self.extended_order_fills[order_id] = Decimal`) so repeated WebSocket messages don’t double-count already-processed fills.  
  3. When an order is cancelled with a non-zero `filled_size`, ensure the partial amount is deducted and clear the entry.  
  4. When Step 2/Step 3 evaluate residual exposure, use the live `self.extended_position` rather than the original request quantity so we hedge only what is still open.
- **Touchpoints**  
  - `hedge/hedge_mode_ext.py:1040-1115` (order update handler) — add per-order fill tracking and expose helper for adjusting positions.  
  - `hedge/hedge_mode_ext.py:1310-1370` — ensure Step 2/3 pull fresh exposure after each order update.  
  - Consider augmenting `ExtendedClient` with an optional `fetch_position_snapshot()` call to reconcile if discrepancies are still detected.
- **Validation**  
  - Reproduce a partial-fill + cancel scenario (mock WebSocket payloads or use the live exchange) and verify the tracked position matches Extended after each message.  
  - Confirm that the imbalance guard no longer fires spuriously and the exchange position remains near zero after each full cycle.

### 2. Reconcile Positions on Startup and After Rounding
- **Problem**  
  - `self.extended_position`/`self.lighter_position` start at zero regardless of live exposure (`hedge/hedge_mode_ext.py:52-60`).  
  - On restart we assume flat, so the PnL guard at `hedge/hedge_mode_ext.py:1275-1279` fires once the first hedge completes.  
  - Lighter quantities are rounded down to allowed steps (`place_lighter_market_order`, `hedge/hedge_mode_ext.py:941-957`), leaving residual exposure that is ignored.
- **What to Implement**  
  1. After `self.initialize_extended_client()`/`self.initialize_lighter_client()` return, query both venues for current positions.  
     - Extended: add `await self.extended_client.get_account_positions()` (already used elsewhere in repo) and map the base balance for `self.extended_contract_id`.  
     - Lighter: expose a method on `SignerClient` (or raw REST call to `/api/v1/account`) to get base exposure.  
  2. Set `self.extended_position` and `self.lighter_position` from those values before entering the trading loop.  
  3. When we quantize hedge size, compute the residual (`original_qty - rounded_qty`). If residual is non-zero, record it (e.g., bump a `self.pending_lighter_residual`) and adjust the position diff logic to expect it. Optionally schedule a follow-up hedge using the residual.
- **Touchpoints**  
  - `hedge/hedge_mode_ext.py:1186-1215` (post-initialisation block)  
  - `hedge/hedge_mode_ext.py:941-956` (quantity rounding)  
  - `exchanges/extended.py` (add helper to fetch base position if not present)
- **Validation**  
  - Restart the bot with a known open position; it should continue trading without triggering the diff guard.  
  - Create unit/integration test stubs that simulate residual rounding and confirm the bot places a follow-up order.

### 2. Harden Hedge Failure Handling
- **Problem**  
  - If Lighter order placement/signing fails, we log and continue (`place_lighter_market_order`, `hedge/hedge_mode_ext.py:969-981`), leaving unhedged exposure.  
  - `monitor_lighter_order` times out after 30s and sets `self.lighter_order_filled = True` with no actual fill (`hedge/hedge_mode_ext.py:1003-1016`), masking the risk.
- **What to Implement**  
  1. Propagate placement errors: if `sign_create_order` or `send_tx` fails, set a dedicated flag (e.g., `self.hedge_failed = True`) and trigger a recovery path. Recovery options:  
     - Immediately place a market order on Extended in the opposite direction to flatten.  
     - Or re-attempt the hedge with a different offset (retry loop capped N times).  
  2. In `monitor_lighter_order`, replace the “mark filled” fallback with:  
     - Cancel the stale order via the Lighter API.  
     - Fetch the filled size so far; if zero, raise an exception to break the trading loop and surface the risk.  
     - Optionally re-price and re-submit once or twice before aborting.  
  3. When the fallback ultimately fails, set `self.stop_flag = True` and raise so the CLI can alert operators.
- **Touchpoints**  
  - `hedge/hedge_mode_ext.py:948-1016`  
  - Add helper(s) to `SignerClient` if cancel APIs aren’t available yet.
- **Validation**  
  - Simulate network failure (mock `send_tx` raising) and confirm the bot cancels/alerts without silently continuing.  
  - Unit test for monitor timeout verifying it raises and closes out the Extended exposure.

### 3. Graceful Shutdown & Resource Cleanup
- **Problem**  
  - `shutdown()` is synchronous and never awaits `ExtendedClient.disconnect()` or closes the aiohttp session in `ExtendedClient`, leading to warnings (`Unclosed client session`).  
  - The extended depth websocket task created in `setup_extended_depth_websocket()` is not tracked/cancelled.
- **What to Implement**  
  1. Convert `shutdown` to `async def shutdown()` and ensure it is awaited from `run()` and signal handlers via `asyncio.create_task`.  
  2. Call `await self.extended_client.disconnect()` and close/await Lighter websocket task.  
  3. Store the depth websocket task (e.g., `self.extended_depth_task`) so it can be cancelled/joined.  
  4. Ensure any HTTP clients inside `ExtendedClient`/`SignerClient` provide an async `close()` method, and invoke it.
- **Touchpoints**  
  - `hedge/hedge_mode_ext.py:128-216` (constructor & shutdown)  
  - `hedge/hedge_mode_ext.py:1123-1176` (depth websocket setup)  
  - `hedge/hedge_mode_ext.py:1389-1421` (run/cleanup)  
  - `exchanges/extended.py` (expose async close)  
  - `lighter/...` client (ensure closing method)
- **Validation**  
  - Trigger an early exit (Ctrl+C or induced error) and check no “unclosed session” logs appear.  
  - Use `asyncio.all_tasks()` in a debug build to ensure no background tasks remain.

---

## P1 — High Priority

### 4. Dynamic Exposure Guard & Alerting
- **Problem**  
  - The imbalance guard hard-codes `0.2` (`hedge/hedge_mode_ext.py:1275-1278`), which is inappropriate once order sizes randomise up to ~3‒4 SOL.  
  - No alerting path is triggered before breaking the loop.
- **Implementation Outline**  
  1. Replace the constant with a derived threshold: `max(self.order_size_max, self.current_cycle_quantity)` times a tolerance (e.g., 5%).  
  2. When breached, attempt to auto-flatten (place an offsetting market order) before exiting.  
  3. Send an out-of-band notification (Telegram/Lark) describing the imbalance and actions taken.
- **Files**: `hedge/hedge_mode_ext.py:1275-1299`, `helpers/telegram_bot.py`
- **Verification**: Unit test or integration script that forces a 50% mismatch and asserts flatten + alert is executed.

### 5. Pre-Loop Exposure Sweep and Auto-Flatten
- **Problem**  
  - Because long-running sessions and partial fills can leave residual exposure on either venue, the bot may start a new cycle already skewed. Currently, Step 1 assumes a flat book based solely on internal counters.  
  - If the exchange still holds a position when Step 1 begins, we immediately double down on the existing direction instead of flattening, compounding risk.
- **What to Implement**  
  1. Before each iteration (or at least before Step 1), fetch the live Extended and Lighter positions (`get_account_positions` / Lighter account API).  
  2. If either side is non-zero beyond a small tolerance, log a warning and issue market orders to flatten on the venue that currently has net exposure (e.g., place a market order on Extended for the residual size).  
  3. Update the in-memory trackers with the reconciled values before proceeding with the usual Step 1 logic.
- **Touchpoints**  
  - `hedge/hedge_mode_ext.py:1290-1370` — inject an async check at the start of each loop.  
  - `RiskManager` (`risk_manager.py`) can share helper functions for fetching live balances to avoid duplicate code.  
  - Add optional config toggles/env vars in case we need to disable/modify this behaviour.
- **Validation**  
  - Force a residual position (e.g., stop the bot mid-cycle, restart) and confirm the sweep detects and flattens it before Step 1 kicks off.  
  - Ensure the extra API calls do not add measurable latency between Extended fills and the Lighter hedge; cache sessions and reuse HTTP clients.

### 6. Audit Logging & Alert Routing
- **Problem**  
  - Telegram alerts are currently sent by running a synchronous helper inside a thread executor. This duplicates responsibility between the trading bot and risk manager and makes routing changes cumbersome.  
  - The bot lacks structured audit logs for incident follow-up; we only log to text files.
- **What to Implement**  
  1. Refactor alerting into a shared asynchronous helper (e.g., `helpers/alerting.py`) that can target Telegram, Lark, or future channels from a single API.  
  2. Provide structured payloads (JSON dict with fields such as `event`, `ticker`, `details`, timestamps) so downstream tooling can parse incidents.  
  3. Investigate adding optional persistent storage (SQLite or S3) for critical incidents if the run terminates before ops review the text log.
- **Touchpoints**  
  - `hedge/hedge_mode_ext.py` replace `_send_telegram_message` usage with new helper.  
  - `risk_manager.py` to reuse the same helper for future alerts.
- **Validation**  
  - Run a simulated hedge failure and confirm the alert helper dispatches once, with structured payload, and that the bot continues cleanly.

### 5. Limit Random Delays When Exposed
- **Problem**  
  - Random delays of 15–60 seconds run even when we hold open exposure (`_maybe_random_delay`, called in Steps 2 & 3). This increases directional risk.  
  - Delays should apply only to neutral states (e.g., Step 1 before opening).
- **Status**  
  - ✅ Implemented — `_maybe_random_delay` now skips whenever net exposure is non-flat, removing long pauses while hedges are outstanding (`hedge/hedge_mode_ext.py`).
- **Implementation Outline**  
  - Gate `_maybe_random_delay` so it runs only when both positions are ~0.  
  - Or introduce shorter max delays while exposed (e.g., clamp to 1–2 seconds) controlled by new config flags.
- **Files**: `hedge/hedge_mode_ext.py:312-318`, call sites at `hedge/hedge_mode_ext.py:1291,1322,1356`
- **Verification**: Log statements should show no long waits once `extended_position != 0`.

### 6. Improve Lighter Order Repricing
- **Problem**  
  - `monitor_lighter_order` never adjusts price; we rely on account updates for fills. If the market moves through us, we just wait until timeout.  
  - There is a `modify_lighter_order` helper but nothing invokes it.
- **Implementation Outline**  
  - Track elapsed time; if we’re still live after N seconds, fetch latest `best_bid/ask` and call `modify_lighter_order()` with a tighter offset.  
  - Limit retries to avoid spam; include exponential backoff and final abort path.
- **Files**: `hedge/hedge_mode_ext.py:1002-1041`  
- **Verification**: Integration test mocking the Lighter book to move away and ensure modify is called.

---

## P2 — Medium / Nice-to-Have

### 7. Structured Metrics & Health Checks
- Emit Prometheus-style metrics for position, hedge latency, failure counts.  
- **Status**  
  - ✅ Implemented — Prometheus metrics facade added (`helpers/metrics.py`) and wired into the Extended hedge bot for positions, latency timing, iteration counts, and failure tracking (exposed via `--metrics-port` or `HEDGE_METRICS_PORT`).
- Implementation: integrate `prometheus_client` or lightweight HTTP server; update counters when trades/log events fire.

### 8. Config-Driven Risk Limits
- Externalise `order_size_min/max`, max imbalance tolerance, timeout durations into the env file so ops can adjust without code changes.  
- Provide validation and logging on load.

### 9. Unit Test Coverage
- Add tests that simulate websocket payloads (fills, orderbook snapshots) to validate position bookkeeping and error paths, using pytest + `pytest-asyncio`.

---

## Suggested Execution Order
1. Implement P0 items sequentially (start with position reconciliation, then hedge failure handling, then shutdown hygiene).  
2. Once stable, tackle P1 items (dynamic guard, exposure-aware delays, repricing).  
3. Schedule P2 items as follow-up once the core loop is resilient.

Each task above references the exact functions/lines to modify and the validations required so another engineer/agent can implement safely.

---

## Implementation Decisions (Latest)
- **Startup reconciliation**: Query live positions on both Extended and Lighter during init; seed `self.extended_position` / `self.lighter_position`. Residuals from Lighter lot rounding should roll into the next cycle rather than forced immediately.  
- **Hedge failure fallback**: Retry the Lighter hedge a few times with tighter pricing; if still failing, flatten on Extended, stop trading, and emit a Telegram alert (todo: revisit alert channel wiring later).  
- **Shutdown cleanup**: Convert `shutdown()` to async, await all client/session closes, and track any spawned websocket tasks for cancellation.  
- **Dynamic exposure guard**: Use tolerance = `max(order_size_max, current_cycle_quantity) * 5%`. When breached, cancel open orders, auto-flatten via market order on the venue with the larger exposure, send a Telegram alert, and continue the run if recovery succeeds.
