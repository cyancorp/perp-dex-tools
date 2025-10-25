#!/usr/bin/env python3
"""Orchestrator for running multiple hedge bots with scheduling and restart logic."""

import argparse
import asyncio
import json
import logging
import os
import re
import signal
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import dotenv_values

from helpers.alerting import send_telegram_message

ANSI_RESET = "\033[0m"
ANSI_WHITE = "\033[37m"
COLOR_PALETTE = [
    "\033[31m",  # red
    "\033[32m",  # green
    "\033[33m",  # yellow
    "\033[34m",  # blue
    "\033[35m",  # magenta
    "\033[36m",  # cyan
]

ITERATION_RE = re.compile(r"(?:Trading loop iteration|iteration_start) (\\d+)")

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    yaml = None  # type: ignore

LOGGER = logging.getLogger("hedge_manager")


@dataclass
class ScheduleWindow:
    start: time
    stop: time

    @classmethod
    def from_strings(cls, start_str: str, stop_str: str) -> "ScheduleWindow":
        start_h, start_m = map(int, start_str.split(":"))
        stop_h, stop_m = map(int, stop_str.split(":"))
        return cls(time(start_h, start_m), time(stop_h, stop_m))

    def in_window(self, now: datetime) -> bool:
        current = now.time()
        if self.start <= self.stop:
            return self.start <= current < self.stop
        # window wraps past midnight
        return current >= self.start or current < self.stop


@dataclass
class BotConfig:
    name: str
    env_file: Optional[Path]
    cli_args: Dict[str, Any]
    schedule: ScheduleWindow
    env_vars: Dict[str, str] = field(default_factory=dict)
    alerts_chat_id: Optional[str] = None
    alerts_token: Optional[str] = None


@dataclass
class BotState:
    config: BotConfig
    process: Optional[asyncio.subprocess.Process] = None
    log_file: Optional[Any] = None
    stopping: bool = False
    restart_backoff: int = 30
    last_exit_code: Optional[int] = None
    task: Optional[asyncio.Task] = None
    restart_on_completion: bool = True
    completed: bool = False
    color: str = ANSI_WHITE
    base_cli_args: Dict[str, Any] = field(default_factory=dict)
    total_iterations_goal: Optional[int] = None
    remaining_iterations: Optional[int] = None
    base_iter: int = 1
    iterations_planned_current: int = 0
    iterations_completed_current: int = 0
    iterations_completed_prior: int = 0

    async def start(self) -> None:
        if self.process or self.completed:
            return

        cli_args = dict(self.base_cli_args)
        if self.total_iterations_goal is not None:
            remaining = self.remaining_iterations if self.remaining_iterations is not None else self.total_iterations_goal
            planned = max(1, min(self.base_iter, remaining))
            self.iterations_planned_current = planned
            self.remaining_iterations = max(0, remaining - planned)
            cli_args["iter"] = planned
        else:
            planned = int(cli_args.get("iter", self.base_iter))
            self.iterations_planned_current = planned

        self.iterations_completed_current = 0

        cmd = [
            "uv",
            "run",
            "hedge_mode.py",
        ]

        for key, value in cli_args.items():
            if value is None:
                continue
            flag = f"--{key.replace('_', '-')}"
            if isinstance(value, bool):
                if value:
                    cmd.append(flag)
                continue
            cmd.extend([flag, str(value)])

        LOGGER.info("%sStarting bot %s with command: %s%s", self.color, self.config.name, " ".join(cmd), ANSI_RESET)

        env = os.environ.copy()
        if self.config.env_file:
            env.update(dotenv_values(self.config.env_file))
        if self.config.env_vars:
            expanded = {k: os.path.expandvars(str(v)) for k, v in self.config.env_vars.items()}
            env.update(expanded)

        log_dir = Path("logs") / self.config.name
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "manager.log"
        log_handle = open(log_path, "a", buffering=1)

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(Path(__file__).resolve().parent),
            env=env,
        )
        self.log_file = log_handle
        self.stopping = False
        self.completed = False
        self.task = asyncio.create_task(self._watch_process())
        asyncio.create_task(self._stream_output())
        await send_telegram_message(
            f"[manager] Started bot {self.config.name}",
            token=self.config.alerts_token,
            chat_id=self.config.alerts_chat_id,
        )

    async def stop(self, reason: str) -> None:
        if not self.process:
            return
        LOGGER.info("Stopping bot %s (reason: %s)", self.config.name, reason)
        self.stopping = True
        self.process.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(self.process.wait(), timeout=30)
        except asyncio.TimeoutError:
            LOGGER.warning("Bot %s did not exit after SIGINT; sending SIGTERM", self.config.name)
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=15)
            except asyncio.TimeoutError:
                LOGGER.error("Bot %s unresponsive; killing", self.config.name)
                self.process.kill()
                await self.process.wait()

        await self._finalise_process()
        await send_telegram_message(
            f"[manager] Stopped bot {self.config.name} ({reason})",
            token=self.config.alerts_token,
            chat_id=self.config.alerts_chat_id,
        )

    async def _finalise_process(self) -> None:
        if self.log_file:
            self.log_file.flush()
            self.log_file.close()
        self.log_file = None
        self.process = None
        if self.task:
            self.task.cancel()
        self.task = None

    async def _stream_output(self) -> None:
        assert self.process is not None
        if not self.process.stdout:
            return
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            decoded = line.decode(errors="replace").rstrip()
            LOGGER.info("%s[%s] %s%s", self.color, self.config.name, decoded, ANSI_RESET)
            if self.log_file:
                self.log_file.write(decoded + "\n")
                self.log_file.flush()
            match = ITERATION_RE.search(decoded)
            if match:
                try:
                    iteration_number = int(match.group(1))
                    self.iterations_completed_current = max(self.iterations_completed_current, iteration_number)
                except ValueError:
                    pass

    async def _watch_process(self) -> None:
        assert self.process is not None
        returncode = await self.process.wait()
        self.last_exit_code = returncode
        LOGGER.info("%sBot %s exited with code %s%s", self.color, self.config.name, returncode, ANSI_RESET)
        await self._finalise_process()

        if self.total_iterations_goal is not None:
            actual = min(self.iterations_planned_current, self.iterations_completed_current)
            deficit = max(0, self.iterations_planned_current - actual)
            self.iterations_completed_prior += actual
            if self.remaining_iterations is not None:
                self.remaining_iterations += deficit
            if self.iterations_completed_prior >= self.total_iterations_goal:
                self.completed = True

        if self.stopping:
            self.stopping = False
            self.restart_backoff = 30
            return

        if self.completed and self.total_iterations_goal is not None:
            self.restart_backoff = 30
            LOGGER.info("%sBot %s completed total iterations (%s)%s", self.color, self.config.name, self.total_iterations_goal, ANSI_RESET)
            return

        if returncode == 0 and not self.restart_on_completion:
            self.completed = True
            self.restart_backoff = 30
            LOGGER.info("Bot %s completed execution (run-once mode)", self.config.name)
            return

        await send_telegram_message(
            f"[manager] Bot {self.config.name} exited unexpectedly (code {returncode}); restarting after {self.restart_backoff}s",
            token=self.config.alerts_token,
            chat_id=self.config.alerts_chat_id,
        )
        await asyncio.sleep(self.restart_backoff)
        self.restart_backoff = min(self.restart_backoff * 2, 300)
        await self.start()

    async def ensure_state(self, now: datetime) -> None:
        in_window = self.config.schedule.in_window(now)
        if in_window:
            if not self.process:
                await self.start()
        else:
            if self.process:
                await self.stop("schedule")
            self.completed = False


def convert_override_value(raw: str):
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def parse_cli_overrides(extra: List[str]) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    i = 0
    while i < len(extra):
        token = extra[i]
        if not token.startswith("--"):
            raise SystemExit(f"Unexpected argument: {token}")
        key = token[2:]
        value: Any = True
        if "=" in key:
            key, raw_value = key.split("=", 1)
            value = convert_override_value(raw_value)
        else:
            if i + 1 < len(extra) and not extra[i + 1].startswith("--"):
                i += 1
                value = convert_override_value(extra[i])
        overrides[key.replace("_", "-")] = value
        i += 1
    return overrides


def load_config(path: Path, overrides: Dict[str, Any]) -> List[BotConfig]:
    with open(path, "r", encoding="utf-8") as fh:
        if path.suffix in {".yaml", ".yml"}:
            if yaml is None:
                raise RuntimeError("PyYAML is required to parse YAML configs")
            raw = yaml.safe_load(fh)
        else:
            raw = json.load(fh)

    global_cli_args = dict(raw.get("cli_args", {}) or {})
    bots_cfg = raw.get("bots", [])
    configs: List[BotConfig] = []
    for entry in bots_cfg:
        name = entry["name"]
        env_file_value = entry.get("env_file")
        env_file = Path(env_file_value).expanduser() if env_file_value else None
        schedule_cfg = entry["schedule"]
        schedule = ScheduleWindow.from_strings(schedule_cfg["start"], schedule_cfg["stop"])
        cli_args = dict(global_cli_args)
        cli_args.update(entry.get("cli_args", {}))
        cli_args.update(overrides)
        env_vars = entry.get("env", {})
        alerts = entry.get("alerts", {})
        configs.append(
            BotConfig(
                name=name,
                env_file=env_file,
                cli_args=cli_args,
                schedule=schedule,
                env_vars=env_vars,
                alerts_chat_id=alerts.get("chat_id"),
                alerts_token=alerts.get("token"),
            )
        )
    return configs


async def run_manager(
    config_path: Path,
    poll_interval: int,
    overrides: Dict[str, Any],
    run_once: bool,
    stop_event: asyncio.Event,
) -> None:
    configs = load_config(config_path, overrides)
    if not configs:
        LOGGER.warning("No bots defined in config %s", config_path)
        return

    states = []
    for idx, cfg in enumerate(configs):
        color = COLOR_PALETTE[idx % len(COLOR_PALETTE)]
        state = BotState(cfg, restart_on_completion=not run_once, color=color, base_cli_args=dict(cfg.cli_args))
        iter_value = state.base_cli_args.get("iter")
        if iter_value is not None:
            try:
                state.base_iter = max(1, int(iter_value))
            except ValueError:
                state.base_iter = 1
            state.total_iterations_goal = state.base_iter
            state.remaining_iterations = state.base_iter
        else:
            state.base_iter = 1
            state.total_iterations_goal = None
            state.remaining_iterations = None
        state.iterations_completed_prior = 0
        state.iterations_planned_current = 0
        state.iterations_completed_current = 0
        states.append(state)
    try:
        while True:
            if stop_event.is_set():
                LOGGER.info("Stop signal received; stopping bots and exiting manager loop")
                for state in states:
                    if state.process:
                        await state.stop("shutdown")
                break

            now = datetime.now(timezone.utc)
            for state in states:
                try:
                    await state.ensure_state(now)
                except Exception as exc:  # pragma: no cover - defensive
                    LOGGER.exception("Error while managing bot %s", state.config.name)
                    await send_telegram_message(
                        f"[manager] Exception while managing {state.config.name}: {exc}",
                        token=state.config.alerts_token,
                        chat_id=state.config.alerts_chat_id,
                    )

            if run_once:
                all_done = all((st.completed or not st.process) for st in states)
                any_running = any(st.process for st in states)
                if all_done and not any_running:
                    LOGGER.info("All bots completed run-once execution; exiting manager loop")
                    break

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:  # pragma: no cover - shutdown path
        LOGGER.info("Manager loop cancelled")
    finally:
        for state in states:
            if state.process:
                await state.stop("shutdown")


def parse_args() -> (argparse.Namespace, Dict[str, Any]):
    parser = argparse.ArgumentParser(description="Hedge bot orchestrator")
    parser.add_argument("--config", required=True, help="Path to orchestration config (YAML or JSON)")
    parser.add_argument("--poll-interval", type=int, default=30, help="Scheduler tick in seconds (default: 30)")
    parser.add_argument("--log-level", default="INFO", help="Logging level (default: INFO)")
    parser.add_argument("--run-once", action="store_true", help="Do not restart bots automatically on clean exit")
    args, unknown = parser.parse_known_args()
    overrides = parse_cli_overrides(unknown)
    return args, overrides


def main() -> None:
    args, overrides = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    config_path = Path(args.config).expanduser()
    if not config_path.exists():
        raise SystemExit(f"Config file not found: {config_path}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_event = asyncio.Event()

    def _handle_signal(signum):
        LOGGER.info("Received signal %s; shutting down", signum)
        stop_event.set()

    handlers = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
            handlers.append(sig)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda s, f, sig=sig: loop.call_soon_threadsafe(stop_event.set))

    try:
        loop.run_until_complete(run_manager(config_path, args.poll_interval, overrides, args.run_once, stop_event))
    finally:
        for sig in handlers:
            try:
                loop.remove_signal_handler(sig)
            except Exception:
                pass
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


if __name__ == "__main__":
    main()
