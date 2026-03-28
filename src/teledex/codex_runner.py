from __future__ import annotations

import json
import logging
import hashlib
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import AppConfig
from .codex_app_server_exec import AppServerClient

_SYNCED_ENV_KEYS_VAR = "__TELEDEX_SYNCED_ENV_KEYS"
_SHELL_MANAGED_ENV_KEYS = {
    "COLUMNS",
    "LINES",
    "OLDPWD",
    "PWD",
    "SHLVL",
    "TMUX",
    "TMUX_PANE",
    "_",
}
_SHELL_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(slots=True)
class CodexProcessHandle:
    tmux_session_name: str
    tmux_target: str
    output_file: Path
    event_log_file: Path
    status_file: Path
    prompt_file: Path


@dataclass(slots=True)
class CodexProcessStatus:
    exit_code: int
    error_message: str | None = None


@dataclass(slots=True)
class ParsedCodexEvent:
    status_text: str | None = None
    footer_statusline: str | None = None
    preview_text: str | None = None
    commentary_id: str | None = None
    commentary_text: str | None = None
    commentary_completed_id: str | None = None
    tool_call_id: str | None = None
    tool_command_text: str | None = None
    tool_output_text: str | None = None
    thread_id: str | None = None
    final_message: str | None = None


@dataclass(slots=True)
class CodexThreadSummary:
    thread_id: str
    preview: str
    cwd: str
    updated_at: int
    name: str | None = None
    path: str | None = None


def _normalize_status_text(text: str) -> str:
    normalized = text.strip()
    if not normalized:
        return ""
    return {
        "正在准备会话...": "Thinking",
        "正在思考...": "Thinking",
        "正在整理回复...": "Thinking",
        "正在调用工具...": "Thinking",
        "正在执行命令...": "Thinking",
        "工具执行完成": "Thinking",
        "任务已中断": "Interrupted",
        "执行失败": "Failed",
        "已停止": "Stopped",
        "已完成": "Completed",
    }.get(normalized, normalized)


class CodexRunner:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = logging.getLogger("teledex.codex_runner")

    def ensure_terminal(self, session_id: int, cwd: Path) -> str:
        tmux_session_name = self._tmux_session_name(session_id, cwd)
        if self._tmux_session_exists(tmux_session_name):
            return tmux_session_name
        self._run_tmux(
            [
                self.config.tmux_bin,
                "new-session",
                "-d",
                "-s",
                tmux_session_name,
                "-c",
                str(cwd),
                self.config.tmux_shell,
            ]
        )
        return tmux_session_name

    def reset_terminal(self, session_id: int, cwd: Path | None = None) -> None:
        session_names = [self._tmux_session_name(session_id)]
        if cwd is not None:
            current_name = self._tmux_session_name(session_id, cwd)
            if current_name not in session_names:
                session_names.insert(0, current_name)
        for tmux_session_name in session_names:
            if not self._tmux_session_exists(tmux_session_name):
                continue
            self._run_tmux([self.config.tmux_bin, "kill-session", "-t", tmux_session_name])

    def start(
        self,
        prompt: str,
        cwd: Path,
        thread_id: str | None,
        runtime_dir: Path,
        session_id: int,
        settings: dict[str, Any] | None = None,
    ) -> CodexProcessHandle:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        output_file = Path(
            tempfile.mkstemp(prefix="codex-last-", suffix=".txt", dir=runtime_dir)[1]
        )
        event_log_file = Path(
            tempfile.mkstemp(prefix="codex-events-", suffix=".jsonl", dir=runtime_dir)[1]
        )
        status_file = Path(
            tempfile.mkstemp(prefix="codex-status-", suffix=".json", dir=runtime_dir)[1]
        )
        prompt_file = Path(
            tempfile.mkstemp(prefix="codex-prompt-", suffix=".txt", dir=runtime_dir)[1]
        )
        prompt_file.write_text(prompt, encoding="utf-8")
        status_file.unlink(missing_ok=True)

        tmux_session_name = self.ensure_terminal(session_id, cwd)
        tmux_target = f"{tmux_session_name}:0.0"
        command = self._build_command(
            cwd=cwd,
            thread_id=thread_id,
            output_file=output_file,
            event_log_file=event_log_file,
            status_file=status_file,
            prompt_file=prompt_file,
            settings=settings or {},
        )
        shell_command = self._build_shell_command(cwd, command)
        self.logger.info("通过 tmux 启动 Codex 命令：%s", shell_command)
        self._run_tmux([self.config.tmux_bin, "send-keys", "-t", tmux_target, "C-c"])
        self._run_tmux(
            [self.config.tmux_bin, "send-keys", "-t", tmux_target, shell_command, "Enter"]
        )
        return CodexProcessHandle(
            tmux_session_name=tmux_session_name,
            tmux_target=tmux_target,
            output_file=output_file,
            event_log_file=event_log_file,
            status_file=status_file,
            prompt_file=prompt_file,
        )

    def wait(
        self,
        handle: CodexProcessHandle,
        on_event_line: Callable[[str], None],
        poll_interval_seconds: float = 0.1,
    ) -> CodexProcessStatus:
        offset = 0
        while True:
            offset = self._drain_event_log(handle.event_log_file, offset, on_event_line)
            status = self.read_status_file(handle.status_file)
            if status is not None:
                offset = self._drain_event_log(handle.event_log_file, offset, on_event_line)
                return status
            time.sleep(poll_interval_seconds)

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
            return _with_footer(
                ParsedCodexEvent(
                    status_text="Thinking",
                    thread_id=payload.get("thread_id"),
                )
            )
        if event_type == "turn.started":
            return _with_footer(ParsedCodexEvent(status_text="Thinking"))
        if event_type == "turn.completed":
            return _with_footer(ParsedCodexEvent(status_text="Thinking"))
        if event_type == "statusline.updated":
            return ParsedCodexEvent(footer_statusline=footer_statusline)
        if event_type == "turn.interrupted":
            message = _normalize_status_text(str(payload.get("message") or "Interrupted"))
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
            return ParsedCodexEvent(footer_statusline=footer_statusline)
        if event_type == "reasoning.updated":
            return ParsedCodexEvent(footer_statusline=footer_statusline)
        if event_type == "command.output":
            text = str(payload.get("text") or "").rstrip()
            item_id = str(payload.get("item_id") or "").strip() or None
            if not text:
                return _with_footer(ParsedCodexEvent(status_text="Thinking"))
            return _with_footer(
                ParsedCodexEvent(
                    status_text="Thinking",
                    tool_call_id=item_id,
                    tool_output_text=text,
                )
            )
        if event_type.startswith("exec.command.") or event_type.startswith("patch."):
            return _with_footer(ParsedCodexEvent(status_text="Thinking"))

        item = payload.get("item")
        if isinstance(item, dict):
            item_type = str(item.get("type", ""))
            if item_type == "agent_message":
                text = str(item.get("text", "")).rstrip()
                item_id = str(item.get("id", "")).strip() or None
                phase = str(item.get("phase", "")).strip()
                if phase == "commentary":
                    return _with_footer(
                        ParsedCodexEvent(
                            status_text="Thinking" if text else None,
                            commentary_id=item_id,
                            commentary_text=text or None,
                            commentary_completed_id=(
                                item_id if event_type == "item.completed" and item_id else None
                            ),
                        )
                    )
                return _with_footer(
                    ParsedCodexEvent(
                        status_text="Thinking" if phase == "final_answer" or text else None,
                        preview_text=text or None,
                        final_message=(text or None) if event_type == "item.completed" else None,
                    )
                )
            if item_type == "plan":
                return ParsedCodexEvent(footer_statusline=footer_statusline)
            if item_type == "reasoning":
                return ParsedCodexEvent(footer_statusline=footer_statusline)
            if item_type == "command_execution":
                item_id = str(item.get("id", "")).strip() or None
                command = str(item.get("command", "")).strip()
                aggregated_output = str(item.get("aggregatedOutput") or "").rstrip()
                if aggregated_output or command:
                    return _with_footer(
                        ParsedCodexEvent(
                            status_text="Thinking",
                            tool_call_id=item_id,
                            tool_command_text=command or None,
                            tool_output_text=aggregated_output or None,
                        )
                    )
                return _with_footer(ParsedCodexEvent(status_text="Thinking"))
            if "tool" in item_type or item_type in {"shell_call", "function_call"}:
                return _with_footer(ParsedCodexEvent(status_text="Thinking"))
            if item_type in {"reasoning", "assistant_reasoning"}:
                return _with_footer(ParsedCodexEvent(status_text="Thinking"))
        return ParsedCodexEvent(footer_statusline=footer_statusline)

    def read_output_file(self, output_file: Path) -> str | None:
        if not output_file.exists():
            return None
        text = output_file.read_text(encoding="utf-8", errors="replace").rstrip()
        return text or None

    def tail_event_log(self, event_log_file: Path, max_lines: int = 20) -> str | None:
        if not event_log_file.exists():
            return None
        lines = event_log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines:
            return None
        return "\n".join(lines[-max_lines:])

    def read_status_file(self, status_file: Path) -> CodexProcessStatus | None:
        if not status_file.exists():
            return None
        payload = json.loads(status_file.read_text(encoding="utf-8"))
        exit_code = int(payload.get("exit_code", 1))
        error_message = str(payload.get("error_message") or "").strip() or None
        return CodexProcessStatus(exit_code=exit_code, error_message=error_message)

    def terminate(self, handle: CodexProcessHandle) -> None:
        self._run_tmux([self.config.tmux_bin, "send-keys", "-t", handle.tmux_target, "C-c"])

    def list_threads(self, cwd: Path, limit: int = 10) -> list[CodexThreadSummary]:
        def _request(client: AppServerClient) -> list[CodexThreadSummary]:
            response = client.request_simple(
                "thread/list",
                {
                    "limit": limit,
                    "cwd": str(cwd),
                    "archived": False,
                },
            )
            data = response.get("data") if isinstance(response, dict) else []
            results: list[CodexThreadSummary] = []
            if not isinstance(data, list):
                return results
            for item in data:
                if not isinstance(item, dict):
                    continue
                thread_id = str(item.get("id") or "").strip()
                if not thread_id:
                    continue
                results.append(
                    CodexThreadSummary(
                        thread_id=thread_id,
                        preview=str(item.get("preview") or "").strip(),
                        cwd=str(item.get("cwd") or "").strip(),
                        updated_at=int(item.get("updatedAt") or 0),
                        name=str(item.get("name")).strip() if item.get("name") else None,
                        path=str(item.get("path")).strip() if item.get("path") else None,
                    )
                )
            return results

        return self._with_app_server(cwd, _request)

    def read_thread(self, cwd: Path, thread_id: str) -> dict[str, Any]:
        return self._with_app_server(
            cwd,
            lambda client: client.request_simple(
                "thread/read",
                {
                    "threadId": thread_id,
                    "includeTurns": False,
                },
            ),
        )

    def set_thread_name(self, cwd: Path, thread_id: str, name: str) -> None:
        self._with_app_server(
            cwd,
            lambda client: client.request_simple(
                "thread/name/set",
                {
                    "threadId": thread_id,
                    "name": name,
                },
            ),
        )

    def fork_thread(
        self,
        cwd: Path,
        thread_id: str,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "cwd": str(cwd),
            "persistExtendedHistory": self.config.codex_persist_extended_history,
        }
        params.update(self._thread_settings_args(settings or {}))
        return self._with_app_server(
            cwd,
            lambda client: client.request_simple("thread/fork", params),
        )

    def compact_thread(self, cwd: Path, thread_id: str) -> None:
        self._with_app_server(
            cwd,
            lambda client: client.request_simple(
                "thread/compact/start",
                {
                    "threadId": thread_id,
                },
            ),
        )

    def review_thread(self, cwd: Path, thread_id: str, instructions: str | None = None) -> None:
        target: dict[str, Any]
        if instructions and instructions.strip():
            target = {
                "type": "custom",
                "instructions": instructions.strip(),
            }
        else:
            target = {"type": "uncommittedChanges"}
        self._with_app_server(
            cwd,
            lambda client: client.request_simple(
                "review/start",
                {
                    "threadId": thread_id,
                    "target": target,
                },
            ),
        )

    def list_models(self, cwd: Path, limit: int = 50) -> list[dict[str, Any]]:
        response = self._with_app_server(
            cwd,
            lambda client: client.request_simple(
                "model/list",
                {
                    "limit": limit,
                },
            ),
        )
        data = response.get("data") if isinstance(response, dict) else []
        return data if isinstance(data, list) else []

    def list_collaboration_modes(self, cwd: Path) -> list[dict[str, Any]]:
        response = self._with_app_server(
            cwd,
            lambda client: client.request_simple("collaborationMode/list", {}),
        )
        data = response.get("data") if isinstance(response, dict) else []
        return data if isinstance(data, list) else []

    def read_config(self, cwd: Path) -> dict[str, Any]:
        return self._with_app_server(
            cwd,
            lambda client: client.request_simple(
                "config/read",
                {
                    "cwd": str(cwd),
                    "includeLayers": True,
                },
            ),
        )

    def list_mcp_servers(self, cwd: Path, limit: int = 50) -> list[dict[str, Any]]:
        response = self._with_app_server(
            cwd,
            lambda client: client.request_simple(
                "mcpServerStatus/list",
                {"limit": limit},
            ),
        )
        data = response.get("data") if isinstance(response, dict) else []
        return data if isinstance(data, list) else []

    def list_apps(
        self,
        cwd: Path,
        thread_id: str | None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        response = self._with_app_server(
            cwd,
            lambda client: client.request_simple(
                "app/list",
                {
                    "limit": limit,
                    "threadId": thread_id,
                },
            ),
        )
        data = response.get("data") if isinstance(response, dict) else []
        return data if isinstance(data, list) else []

    def list_skills(self, cwd: Path) -> list[dict[str, Any]]:
        response = self._with_app_server(
            cwd,
            lambda client: client.request_simple(
                "skills/list",
                {
                    "cwds": [str(cwd)],
                },
            ),
        )
        data = response.get("data") if isinstance(response, dict) else []
        return data if isinstance(data, list) else []

    def list_experimental_features(self, cwd: Path, limit: int = 50) -> list[dict[str, Any]]:
        response = self._with_app_server(
            cwd,
            lambda client: client.request_simple(
                "experimentalFeature/list",
                {"limit": limit},
            ),
        )
        data = response.get("data") if isinstance(response, dict) else []
        return data if isinstance(data, list) else []

    def clean_background_terminals(self, cwd: Path, thread_id: str) -> None:
        self._with_app_server(
            cwd,
            lambda client: client.request_simple(
                "thread/backgroundTerminals/clean",
                {"threadId": thread_id},
            ),
        )

    def _build_command(
        self,
        cwd: Path,
        thread_id: str | None,
        output_file: Path,
        event_log_file: Path,
        status_file: Path,
        prompt_file: Path,
        settings: dict[str, Any] | None = None,
    ) -> list[str]:
        effective_settings = settings or {}
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
            "--event-log-file",
            str(event_log_file),
            "--status-file",
            str(status_file),
            "--prompt-file",
            str(prompt_file),
            "--exec-mode",
            self.config.codex_exec_mode,
        ]

        if thread_id:
            command.extend(["--thread-id", thread_id])

        if self.config.codex_model:
            command.extend(["--model", self.config.codex_model])
        if effective_settings.get("model"):
            command.extend(["--model", str(effective_settings["model"])])
        if effective_settings.get("reasoning_effort"):
            command.extend(["--reasoning-effort", str(effective_settings["reasoning_effort"])])
        if effective_settings.get("service_tier"):
            command.extend(["--service-tier", str(effective_settings["service_tier"])])
        if effective_settings.get("personality"):
            command.extend(["--personality", str(effective_settings["personality"])])
        if effective_settings.get("approval_policy"):
            command.extend(["--approval-policy", str(effective_settings["approval_policy"])])
        if effective_settings.get("sandbox_mode"):
            command.extend(["--sandbox-mode", str(effective_settings["sandbox_mode"])])
        if effective_settings.get("collaboration_mode"):
            command.extend(["--collaboration-mode", str(effective_settings["collaboration_mode"])])

        if self.config.codex_enable_search:
            command.append("--search")
        if self.config.codex_persist_extended_history:
            command.append("--persist-extended-history")
        return command

    def _build_shell_command(self, cwd: Path, command: list[str]) -> str:
        shell_body = "cd {cwd} && {command}".format(
            sync_env=self._build_shell_env_sync_command(),
            cwd=shlex.quote(str(cwd)),
            command=" ".join(shlex.quote(part) for part in command),
        )
        exact_env_command = self._build_exact_env_command(shell_body)
        return "{sync_env}{command}".format(
            sync_env=self._build_shell_env_sync_command(),
            command=exact_env_command,
        )

    def _build_shell_env_sync_command(self) -> str:
        syncable_env = self._build_syncable_env()
        keys = sorted(syncable_env)
        names_blob = " ".join(keys)
        commands = [
            f"for __teledex_env_key in ${{{_SYNCED_ENV_KEYS_VAR}-}}; do "
            f'case " {names_blob} " in '
            '*" ${__teledex_env_key} "*) ;; '
            '*) unset "$__teledex_env_key" ;; '
            "esac; "
            "done"
        ]
        commands.extend(
            f"export {key}={shlex.quote(value)}" for key, value in sorted(syncable_env.items())
        )
        commands.append(f"export {_SYNCED_ENV_KEYS_VAR}={shlex.quote(names_blob)}")
        commands.append("unset __teledex_env_key")
        return "; ".join(commands) + "; "

    def _build_exact_env_command(self, shell_body: str) -> str:
        syncable_env = self._build_syncable_env()
        env_args = " ".join(
            f"{key}={shlex.quote(value)}" for key, value in sorted(syncable_env.items())
        )
        return "env -i {env_args} {shell} -lc {shell_body}".format(
            env_args=env_args,
            shell=shlex.quote(self.config.tmux_shell),
            shell_body=shlex.quote(shell_body),
        )

    def _build_syncable_env(self) -> dict[str, str]:
        syncable_env = {
            key: value
            for key, value in os.environ.items()
            if _SHELL_ENV_KEY_RE.match(key) and key not in _SHELL_MANAGED_ENV_KEYS
        }
        syncable_env.setdefault("HOME", str(Path.home()))
        syncable_env.setdefault("PATH", os.defpath)
        return syncable_env

    def _tmux_session_name(self, session_id: int, cwd: Path | None = None) -> str:
        if cwd is None:
            return f"teledex-session-{session_id}"
        resolved = cwd.expanduser().resolve()
        leaf_name = resolved.name.strip() or "root"
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", leaf_name).strip("._-") or "root"
        suffix = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:6]
        return f"teledex-{slug}-{suffix}"

    def _tmux_session_exists(self, session_name: str) -> bool:
        result = subprocess.run(
            [self.config.tmux_bin, "has-session", "-t", session_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def _run_tmux(self, command: list[str]) -> None:
        subprocess.run(command, check=True, capture_output=True, text=True)

    def _with_app_server(self, cwd: Path, callback: Callable[[AppServerClient], Any]) -> Any:
        client = AppServerClient.start(self.config.codex_bin, cwd.resolve())
        try:
            return callback(client)
        finally:
            client.close()

    def _thread_settings_args(self, settings: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if settings.get("model"):
            params["model"] = str(settings["model"])
        if settings.get("service_tier"):
            params["serviceTier"] = str(settings["service_tier"])
        if settings.get("personality"):
            params["personality"] = str(settings["personality"])
        if settings.get("approval_policy"):
            params["approvalPolicy"] = str(settings["approval_policy"])
        if settings.get("sandbox_mode"):
            params["sandbox"] = str(settings["sandbox_mode"])
        return params

    def _drain_event_log(
        self,
        event_log_file: Path,
        offset: int,
        on_event_line: Callable[[str], None],
    ) -> int:
        if not event_log_file.exists():
            return offset
        with event_log_file.open("r", encoding="utf-8", errors="replace") as file:
            file.seek(offset)
            while True:
                line = file.readline()
                if not line:
                    break
                on_event_line(line)
            return file.tell()
