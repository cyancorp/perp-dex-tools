# Lighter Hedge Latency Optimisation Ideas

1. **[Done]** Minimise work inside the Extended fill callback. Introduced non-blocking trade logging and lightweight net-position calculation so the hedge is triggered immediately after a fill.
2. **[Done]** Eliminate random or artificial delays while exposed. Random delays now skip when exposure isn’t flat, keeping hedges instantaneous.
3. **[Done]** Keep the Lighter order book cache hot. Added heartbeat tracking and on-demand refresh via the official API when the WebSocket data goes stale.
4. **[Done]** Keep Lighter auth/nonce state warm. Nonce manager now refreshes at startup to prevent the first hedge from incurring a slow nonce fetch.
5. **[Done]** Offload CPU-bound work. Trade logging and similar tasks now run outside the critical path so hedging coroutines don’t block.
6. Pre-compute price/quantity logic ahead of signing. (Deferred for later; current logic stays simple but this remains a future optimisation.)
7. **[Done]** Instrument latency end-to-end. Hedge send/fill timings are recorded to quantify performance and catch regressions.
