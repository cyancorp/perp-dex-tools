## Hedge Mode Orchestration Plan

This document captures the requirements and design considerations for a future "wrapper" process that coordinates multiple hedge bots. The goal is to keep the individual bots lightweight while providing an operationally solid supervisor.

### Objectives

1. Run multiple hedge bots concurrently (different wallets, tickers, schedules, risk settings).
2. Start/stop bots at configured windows each day.
3. Automatically restart bots that exit unexpectedly.
4. Surface lifecycle events (start, stop, restarts, failures) through structured logs and Telegram alerts.
5. Provide an easily extensible configuration format so we can add/remove strategies without code edits.

### Configuration Design

Use a single orchestration config file (YAML/JSON) that defines an array of bot jobs. Each entry should contain:

- `name`: human-readable label for logging/alerts.
- `exchange`: currently `extended`.
- `env` (optional): mapping of environment variables (API keys, secrets, etc.) to inject for this bot. Values support `${VAR}` expansion.
- `env_file` (optional): path to a `.env` file if you prefer to keep credentials there. If both `env_file` and `env` are provided, their values are merged, with explicit `env` keys taking precedence.
- `schedule`: cron-like window (start/end UTC or repeated intervals).
- `cli_args`: hedge-mode arguments (`--ticker`, `--size-min`, etc.).
- `alerts`: overrides for Telegram chat ID or severity thresholds (optional).

Example (YAML):

```yaml
bots:
  - name: sol_wallet_a
    env:
      EXTENDED_API_KEY: ${EXTENDED_API_KEY}
      EXTENDED_STARK_KEY_PRIVATE: ${EXTENDED_STARK_KEY_PRIVATE}
      EXTENDED_STARK_KEY_PUBLIC: ${EXTENDED_STARK_KEY_PUBLIC}
      EXTENDED_VAULT: ${EXTENDED_VAULT}
      API_KEY_PRIVATE_KEY: ${API_KEY_PRIVATE_KEY}
      LIGHTER_ACCOUNT_INDEX: ${LIGHTER_ACCOUNT_INDEX}
      LIGHTER_API_KEY_INDEX: ${LIGHTER_API_KEY_INDEX}
    schedule:
      start: "00:05"   # UTC
      stop:  "06:55"
    cli_args:
      exchange: extended
      ticker: SOL
      iter: 40
      size_min: 1.5
      size_max: 2.0
      size_step: 0.01
      delay_min: 15
      delay_max: 60
      fill_timeout: 5
```

### Supervisor Responsibilities

1. **Process management**
   - Launch `uv run hedge_mode.py ...` as subprocesses with dedicated log files.
   - Capture stdout/stderr; stream into central logger.
   - On exit, inspect return code and stdout for error markers; decide whether to restart immediately, after backoff, or stay stopped.

2. **Scheduling**
   - Track current UTC time, start bots when entering their window, stop gracefully when exiting.
   - For each bot, maintain state: `IDLE`, `STARTING`, `RUNNING`, `STOPPING`, `ERROR`.
   - If a stop request occurs while the bot is handling a hedging cycle, send a SIGINT first, escalate to SIGTERM if not shut down within a grace period.

3. **Telemetry & alerts**
   - Log structured JSON events for each lifecycle transition.
   - Send Telegram alert on: start success, unexpected exit (with reason), repeated restarts, manual disable.
   - Include key metadata: bot name, ticker, exit code, last log lines, hedge latency metrics (if available).

4. **Error propagation**
   - The wrapped bot should exit non-zero (or emit a dedicated exit file) when it detects irrecoverable risk. The wrapper should treat those as hard stops, alert, and skip restart until manual intervention.

5. **Resource isolation**
   - Each bot run should use its own virtualenv or rely on the same environment but ensure no shared state (temp files, CSV paths). Encourage per-bot `logs/<name>_...` naming to avoid overlap.

### Implementation Outline

1. **Wrapper entry point** (`hedge_manager.py` placeholder):
   - Parse orchestration config.
   - For each job, create a `BotRunner` object that knows how to start/stop the subprocess and track retries.
   - Use asyncio or `asyncio.create_subprocess_exec` to supervise—and to integrate with existing async alert helpers.

2. **BotRunner responsibilities**
   - Spawn subprocess with env vars loaded from the bot's `.env` file.
   - Wait for completion; capture `returncode` and tail of logs.
   - If `returncode != 0` or output contains `Hedge failure` / `Residual exposure`, raise `BotCrashed` to manager.
   - Implement exponential backoff with max retry count before marking bot as `ERROR`.

3. **Alert helper refactor**
   - Move current `_send_telegram_message` logic into `helpers/alerting.py` so it can be reused by wrapper and bots.
   - Convert to fully async function to avoid repeat thread pool usage.

4. **Logging**
   - Standardise on JSON log lines for the wrapper: `{"timestamp": ..., "bot": ..., "event": "start"}` etc.
   - For bots, ensure meaningful log names (already `logs/extended_{ticker}_...`). Wrapper should include path in its summary.

5. **Graceful stop contract**
   - Bots must handle SIGINT/SIGTERM cleanly (already supported via `shutdown_async`), exit with `0` when shutdown is operator/scheduler initiated.
   - On stop, write a line like `STOP_REASON=scheduler` to logs to verify expected exit.

### Implementation Status

- ✅ Shared alerting helper (`helpers/alerting.py`) provides async Telegram messaging for both bots and the manager.
- ✅ `hedge_manager.py` implements config parsing, scheduling, process supervision, restart logic, and alerting as described above.
- ⏳ Integration tests and advanced features (per-bot metrics, dashboards, rate limiting) remain future work.

### Next Steps

1. Extend the manager with health probes (latency, PnL) and expose metrics for monitoring dashboards.
2. Add integration tests: simulate bot crash (non-zero exit), ensure wrapper restarts and Telegram alert fires.
3. Support richer scheduling (cron expressions, blackout periods) and per-bot concurrency limits.
