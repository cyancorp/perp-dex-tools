#!/usr/bin/env python3
"""Orchestrator for running multiple hedge bots with scheduling and restart logic."""

import argparse
import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import dotenv_values

from helpers.alerting import send_telegram_message

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
    env_file: Path
    cli_args: Dict[str, Any]
    schedule: ScheduleWindow
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

    async def start(self) -> None:
        if self.process:
            return

        cmd = [
            "uv",
            "run",
            "hedge_mode.py",
        ]

        for key, value in self.config.cli_args.items():
            if value is None:
                continue
            flag = f"--{key.replace('_', '-')}"
            if isinstance(value, bool):
                if value:
                    cmd.append(flag)
                continue
            cmd.extend([flag, str(value)])

        LOGGER.info("Starting bot %s with command: %s", self.config.name, " ".join(cmd))

        env = os.environ.copy()
        env.update(dotenv_values(self.config.env_file))
        log_path = Path("logs") / f"manager_{self.config.name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(log_path, "a", buffering=1)

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=log_handle,
            stderr=log_handle,
            cwd=str(Path(__file__).resolve().parent),
            env=env,
        )
        self.log_file = log_handle
        self.stopping = False
        self.task = asyncio.create_task(self._watch_process())
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

    async def _watch_process(self) -> None:
        assert self.process is not None
        returncode = await self.process.wait()
        self.last_exit_code = returncode
        LOGGER.info("Bot %s exited with code %s", self.config.name, returncode)
        await self._finalise_process()

        if self.stopping:
            self.stopping = False
            self.restart_backoff = 30
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


def load_config(path: Path) -> List[BotConfig]:
    with open(path, "r", encoding="utf-8") as fh:
        if path.suffix in {".yaml", ".yml"}:
            if yaml is None:
                raise RuntimeError("PyYAML is required to parse YAML configs")
            raw = yaml.safe_load(fh)
        else:
            raw = json.load(fh)

    bots_cfg = raw.get("bots", [])
    configs: List[BotConfig] = []
    for entry in bots_cfg:
        name = entry["name"]
        env_file = Path(entry["env_file"]).expanduser()
        schedule_cfg = entry["schedule"]
        schedule = ScheduleWindow.from_strings(schedule_cfg["start"], schedule_cfg["stop"])
        cli_args = entry.get("cli_args", {})
        alerts = entry.get("alerts", {})
        configs.append(
            BotConfig(
                name=name,
                env_file=env_file,
                cli_args=cli_args,
                schedule=schedule,
                alerts_chat_id=alerts.get("chat_id"),
                alerts_token=alerts.get("token"),
            )
        )
    return configs


async def run_manager(config_path: Path, poll_interval: int) -> None:
    configs = load_config(config_path)
    if not configs:
        LOGGER.warning("No bots defined in config %s", config_path)
        return

    states = [BotState(cfg) for cfg in configs]
    try:
        while True:
            now = datetime.utcnow()
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
            await asyncio.sleep(poll_interval)
    except asyncio.CancelledError:  # pragma: no cover - shutdown path
        LOGGER.info("Manager loop cancelled")
    finally:
        for state in states:
            if state.process:
                await state.stop("shutdown")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hedge bot orchestrator")
    parser.add_argument("--config", required=True, help="Path to orchestration config (YAML or JSON)")
    parser.add_argument("--poll-interval", type=int, default=30, help="Scheduler tick in seconds (default: 30)")
    parser.add_argument("--log-level", default="INFO", help="Logging level (default: INFO)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    config_path = Path(args.config).expanduser()
    if not config_path.exists():
        raise SystemExit(f"Config file not found: {config_path}")

    asyncio.run(run_manager(config_path, args.poll_interval))


if __name__ == "__main__":
    main()
