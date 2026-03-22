from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig
from .formatting import preview_text_for_agent_message, summarize_command


@dataclass(slots=True)
class CodexProcessHandle:
    process: subprocess.Popen[str]
    output_file: Path
    event_log_file: Path


@dataclass(slots=True)
class ParsedCodexEvent:
    status_text: str | None = None
    thread_id: str | None = None
    final_message: str | None = None


class CodexRunner:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = logging.getLogger("teledex.codex_runner")

    def start(
        self,
        prompt: str,
        cwd: Path,
        thread_id: str | None,
        runtime_dir: Path,
    ) -> CodexProcessHandle:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        output_file = Path(
            tempfile.mkstemp(prefix="codex-last-", suffix=".txt", dir=runtime_dir)[1]
        )
        event_log_file = Path(
            tempfile.mkstemp(prefix="codex-events-", suffix=".jsonl", dir=runtime_dir)[1]
        )
        command = self._build_command(
            prompt=prompt,
            cwd=cwd,
            thread_id=thread_id,
            output_file=output_file,
        )
        self.logger.info("启动 Codex 命令：%s", command)
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            preexec_fn=os.setsid,
        )
        return CodexProcessHandle(
            process=process,
            output_file=output_file,
            event_log_file=event_log_file,
        )

    def parse_event_line(self, line: str) -> ParsedCodexEvent:
        raw = line.strip()
        if not raw:
            return ParsedCodexEvent()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return ParsedCodexEvent()

        event_type = str(payload.get("type", ""))
        if event_type == "thread.started":
            return ParsedCodexEvent(
                status_text="正在准备会话...",
                thread_id=payload.get("thread_id"),
            )
        if event_type == "turn.started":
            return ParsedCodexEvent(status_text="正在思考...")
        if event_type.startswith("exec.command.") or event_type.startswith("patch."):
            return ParsedCodexEvent(status_text="正在调用工具...")

        item = payload.get("item")
        if isinstance(item, dict):
            item_type = str(item.get("type", ""))
            if item_type == "agent_message":
                text = str(item.get("text", "")).strip()
                return ParsedCodexEvent(
                    status_text=preview_text_for_agent_message(text),
                    final_message=text or None,
                )
            if item_type == "command_execution":
                command = str(item.get("command", "")).strip()
                if command:
                    return ParsedCodexEvent(
                        status_text=f"正在执行：{summarize_command(command)}"
                    )
                return ParsedCodexEvent(status_text="正在执行命令...")
            if "tool" in item_type or item_type in {"shell_call", "function_call"}:
                return ParsedCodexEvent(status_text="正在调用工具...")
            if item_type in {"reasoning", "assistant_reasoning"}:
                return ParsedCodexEvent(status_text="正在思考...")
        return ParsedCodexEvent()

    def read_output_file(self, output_file: Path) -> str | None:
        if not output_file.exists():
            return None
        text = output_file.read_text(encoding="utf-8", errors="replace").strip()
        return text or None

    def append_event_log(self, event_log_file: Path, line: str) -> None:
        with event_log_file.open("a", encoding="utf-8") as file:
            file.write(line)

    def tail_event_log(self, event_log_file: Path, max_lines: int = 20) -> str | None:
        if not event_log_file.exists():
            return None
        lines = event_log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines:
            return None
        return "\n".join(lines[-max_lines:])

    def terminate(self, handle: CodexProcessHandle) -> None:
        if handle.process.poll() is not None:
            return
        os.killpg(os.getpgid(handle.process.pid), signal.SIGTERM)

    def _build_command(
        self,
        prompt: str,
        cwd: Path,
        thread_id: str | None,
        output_file: Path,
    ) -> list[str]:
        command: list[str] = [self.config.codex_bin, "exec"]
        command.extend(
            [
                "--skip-git-repo-check",
                "--json",
                "--cd",
                str(cwd),
                "--output-last-message",
                str(output_file),
            ]
        )

        if self.config.codex_exec_mode == "full-auto":
            command.append("--full-auto")
        elif self.config.codex_exec_mode == "dangerous":
            command.append("--dangerously-bypass-approvals-and-sandbox")

        if self.config.codex_model:
            command.extend(["--model", self.config.codex_model])

        if self.config.codex_enable_search:
            command.append("--search")

        if thread_id:
            command.extend(["resume", thread_id, prompt])
        else:
            command.append(prompt)
        return command
