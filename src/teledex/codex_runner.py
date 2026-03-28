from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig
from .formatting import extract_first_bold_markdown


@dataclass(slots=True)
class CodexProcessHandle:
    process: subprocess.Popen[str]
    output_file: Path
    event_log_file: Path


@dataclass(slots=True)
class ParsedCodexEvent:
    status_text: str | None = None
    footer_statusline: str | None = None
    preview_text: str | None = None
    commentary_id: str | None = None
    commentary_text: str | None = None
    tool_output_text: str | None = None
    thread_id: str | None = None
    final_message: str | None = None


def _normalize_status_text(text: str) -> str:
    normalized = text.strip()
    if not normalized:
        return ""
    return {
        "正在准备会话...": "Working",
        "正在思考...": "Thinking",
        "正在整理回复...": "Working",
        "正在调用工具...": "Working",
        "正在执行命令...": "Working",
        "工具执行完成": "Working",
        "任务已中断": "Interrupted",
        "执行失败": "Failed",
        "已停止": "Stopped",
        "已完成": "Completed",
    }.get(normalized, normalized)


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

        footer_statusline = str(payload.get("footer_statusline") or "").strip() or None

        def _with_footer(event: ParsedCodexEvent) -> ParsedCodexEvent:
            if footer_statusline:
                event.footer_statusline = footer_statusline
            return event

        event_type = str(payload.get("type", ""))
        if event_type == "thread.started":
            return _with_footer(ParsedCodexEvent(
                status_text="Working",
                thread_id=payload.get("thread_id"),
            ))
        if event_type == "turn.started":
            return _with_footer(ParsedCodexEvent(status_text="Working"))
        if event_type == "turn.completed":
            return _with_footer(ParsedCodexEvent(status_text="Working"))
        if event_type == "statusline.updated":
            return ParsedCodexEvent(footer_statusline=footer_statusline)
        if event_type == "turn.interrupted":
            message = _normalize_status_text(
                str(payload.get("message") or "Interrupted")
            )
            return _with_footer(ParsedCodexEvent(status_text=message or "Interrupted"))
        if event_type == "turn.failed":
            message = _normalize_status_text(str(payload.get("message") or "Failed"))
            return _with_footer(ParsedCodexEvent(status_text=message or "Failed"))
        if event_type == "error":
            message = _normalize_status_text(str(payload.get("message") or "Failed"))
            return _with_footer(ParsedCodexEvent(status_text=message or "Failed"))
        if event_type == "status.updated":
            message = _normalize_status_text(str(payload.get("message") or ""))
            return _with_footer(ParsedCodexEvent(status_text=message or None))
        if event_type == "plan.updated":
            plan_id = str(payload.get("plan_id") or "").strip()
            text = str(payload.get("text") or "").strip()
            if not plan_id or not text:
                return ParsedCodexEvent(footer_statusline=footer_statusline)
            return _with_footer(ParsedCodexEvent(
                status_text="Working",
                commentary_id=plan_id,
                commentary_text=text,
            ))
        if event_type == "reasoning.updated":
            item_id = str(payload.get("item_id") or "").strip()
            text = str(payload.get("text") or "").strip()
            if not item_id or not text:
                return ParsedCodexEvent(footer_statusline=footer_statusline)
            return _with_footer(ParsedCodexEvent(
                status_text=extract_first_bold_markdown(text) or None,
                commentary_id=f"reasoning:{item_id}",
                commentary_text=text,
            ))
        if event_type == "command.output":
            text = str(payload.get("text") or "").strip()
            if not text:
                return _with_footer(ParsedCodexEvent(status_text="Working"))
            return _with_footer(ParsedCodexEvent(
                status_text="Working",
                tool_output_text=text,
            ))
        if event_type.startswith("exec.command.") or event_type.startswith("patch."):
            return _with_footer(ParsedCodexEvent(status_text="Working"))

        item = payload.get("item")
        if isinstance(item, dict):
            item_type = str(item.get("type", ""))
            if item_type == "agent_message":
                text = str(item.get("text", "")).strip()
                item_id = str(item.get("id", "")).strip() or None
                phase = str(item.get("phase", "")).strip()
                if phase == "commentary":
                    return _with_footer(ParsedCodexEvent(
                        status_text=None,
                        commentary_id=item_id,
                        commentary_text=text or None,
                    ))
                return _with_footer(ParsedCodexEvent(
                    status_text="Working" if phase == "final_answer" or text else None,
                    preview_text=text or None,
                    final_message=(text or None) if event_type == "item.completed" else None,
                ))
            if item_type == "plan":
                text = str(item.get("text", "")).strip()
                item_id = str(item.get("id", "")).strip() or "plan"
                if text:
                    return _with_footer(ParsedCodexEvent(
                        status_text="Working",
                        commentary_id=f"plan:{item_id}",
                        commentary_text=text,
                    ))
                return _with_footer(ParsedCodexEvent(status_text="Working"))
            if item_type == "reasoning":
                summary = item.get("summary")
                if isinstance(summary, list):
                    parts = []
                    for section in summary:
                        if isinstance(section, dict):
                            text = str(section.get("text") or "").strip()
                            if text:
                                parts.append(text)
                    summary_text = "\n\n".join(parts).strip()
                    if summary_text:
                        item_id = str(item.get("id", "")).strip() or "reasoning"
                        return _with_footer(ParsedCodexEvent(
                            status_text=extract_first_bold_markdown(summary_text) or None,
                            commentary_id=f"reasoning:{item_id}",
                            commentary_text=summary_text,
                        ))
            if item_type == "command_execution":
                command = str(item.get("command", "")).strip()
                aggregated_output = str(item.get("aggregatedOutput") or "").strip()
                if aggregated_output:
                    return _with_footer(ParsedCodexEvent(
                        status_text="Working",
                        tool_output_text=aggregated_output,
                    ))
                if command:
                    return _with_footer(ParsedCodexEvent(status_text="Working"))
                return _with_footer(ParsedCodexEvent(status_text="Working"))
            if "tool" in item_type or item_type in {"shell_call", "function_call"}:
                return _with_footer(ParsedCodexEvent(status_text="Working"))
            if item_type in {"reasoning", "assistant_reasoning"}:
                return _with_footer(ParsedCodexEvent(status_text="Thinking"))
        return ParsedCodexEvent(footer_statusline=footer_statusline)

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
        helper_path = Path(__file__).with_name("codex_app_server_exec.py").resolve()
        command: list[str] = [
            sys.executable,
            "-u",
            str(helper_path),
            "--codex-bin",
            self.config.codex_bin,
            "--cwd",
            str(cwd),
            "--output-file",
            str(output_file),
            "--exec-mode",
            self.config.codex_exec_mode,
            "--prompt",
            prompt,
        ]

        if thread_id:
            command.extend(["--thread-id", thread_id])

        if self.config.codex_model:
            command.extend(["--model", self.config.codex_model])

        if self.config.codex_enable_search:
            command.append("--search")
        if self.config.codex_persist_extended_history:
            command.append("--persist-extended-history")
        return command
