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
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from .config import AppConfig
from .codex_app_server_exec import (
    AppServerClient,
    _build_footer_statusline,
    _build_thread_resume_params,
    _build_thread_start_params,
    _build_turn_start_params,
    _extract_reasoning_effort,
    _extract_service_tier,
    _extract_status_line_items,
    _map_notification,
    _resolve_thread_binding,
    _statusline_event_if_changed,
    _update_status_line_from_binding,
    _write_status,
)

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
    session_id: int
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
    preview_is_final: bool = False
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


@dataclass(slots=True)
class _PersistentRuntime:
    session_id: int
    cwd: Path
    tmux_session_name: str
    client: AppServerClient | None = None
    bound_thread_id: str | None = None
    status_line_state: dict[str, Any] | None = None
    current_turn_id: str | None = None
    interrupt_requested: bool = False
    pending_aux_request_ids: set[int] = field(default_factory=set)
    turn_worker: threading.Thread | None = None
    state_lock: threading.RLock = field(default_factory=threading.RLock)
    send_lock: threading.RLock = field(default_factory=threading.RLock)


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
        self._runtime_lock = threading.RLock()
        self._runtimes: dict[int, _PersistentRuntime] = {}

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
        handle = CodexProcessHandle(
            session_id=session_id,
            tmux_session_name=tmux_session_name,
            tmux_target=tmux_target,
            output_file=output_file,
            event_log_file=event_log_file,
            status_file=status_file,
            prompt_file=prompt_file,
        )
        self.logger.info("%s", self._format_start_log_message(cwd, thread_id, settings or {}))
        runtime = self._ensure_runtime(session_id, cwd, tmux_session_name)
        self._start_runtime_turn(
            runtime,
            handle,
            prompt=prompt,
            thread_id=thread_id,
            settings=settings or {},
        )
        return handle

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
                        preview_text=(text or None) if phase == "final_answer" else None,
                        preview_is_final=(phase == "final_answer" and bool(text)),
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
        raw = status_file.read_text(encoding="utf-8", errors="replace").strip()
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        exit_code = int(payload.get("exit_code", 1))
        error_message = str(payload.get("error_message") or "").strip() or None
        return CodexProcessStatus(exit_code=exit_code, error_message=error_message)

    def terminate(self, handle: CodexProcessHandle) -> None:
        runtime = self._get_runtime(handle.session_id)
        if runtime is None:
            return
        self._interrupt_runtime(runtime)

    def reset_session_runtime(self, session_id: int) -> None:
        with self._runtime_lock:
            runtime = self._runtimes.pop(session_id, None)
        if runtime is None:
            return
        self._close_runtime(runtime)

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

    def _format_start_log_message(
        self,
        cwd: Path,
        thread_id: str | None,
        settings: dict[str, Any],
    ) -> str:
        effective_settings = settings or {}
        model = str(effective_settings.get("model") or self.config.codex_model or "default")
        effort = str(effective_settings.get("reasoning_effort") or "default")
        approval = str(effective_settings.get("approval_policy") or "default")
        sandbox = str(effective_settings.get("sandbox_mode") or "default")
        collaboration = str(effective_settings.get("collaboration_mode") or "default")
        thread_state = thread_id if thread_id else "new"
        search_state = "on" if self.config.codex_enable_search else "off"
        return (
            "通过持久 runtime 执行 Codex 会话："
            f" cwd={cwd} thread={thread_state} model={model}"
            f" effort={effort} approval={approval}"
            f" sandbox={sandbox} collab={collaboration}"
            f" exec_mode={self.config.codex_exec_mode} search={search_state}"
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

    def _ensure_runtime(
        self,
        session_id: int,
        cwd: Path,
        tmux_session_name: str,
    ) -> _PersistentRuntime:
        resolved_cwd = cwd.expanduser().resolve()
        with self._runtime_lock:
            runtime = self._runtimes.get(session_id)
            if runtime is not None and runtime.cwd != resolved_cwd:
                self._runtimes.pop(session_id, None)
                self._close_runtime(runtime)
                runtime = None
            if runtime is None:
                runtime = _PersistentRuntime(
                    session_id=session_id,
                    cwd=resolved_cwd,
                    tmux_session_name=tmux_session_name,
                )
                self._runtimes[session_id] = runtime
            else:
                runtime.tmux_session_name = tmux_session_name
            return runtime

    def _get_runtime(self, session_id: int) -> _PersistentRuntime | None:
        with self._runtime_lock:
            return self._runtimes.get(session_id)

    def _close_runtime(self, runtime: _PersistentRuntime) -> None:
        with runtime.state_lock:
            client = runtime.client
            runtime.client = None
            runtime.bound_thread_id = None
            runtime.status_line_state = None
            runtime.current_turn_id = None
            runtime.interrupt_requested = False
            runtime.pending_aux_request_ids.clear()
        if client is not None:
            client.close()

    def _start_runtime_turn(
        self,
        runtime: _PersistentRuntime,
        handle: CodexProcessHandle,
        *,
        prompt: str,
        thread_id: str | None,
        settings: dict[str, Any],
    ) -> None:
        with runtime.state_lock:
            if runtime.turn_worker is not None and runtime.turn_worker.is_alive():
                raise RuntimeError(f"会话 #{runtime.session_id} 当前已有运行中的 Codex turn")
            runtime.current_turn_id = None
            runtime.interrupt_requested = False
            runtime.pending_aux_request_ids.clear()
            worker = threading.Thread(
                target=self._run_runtime_turn,
                args=(runtime, handle, prompt, thread_id, settings),
                daemon=True,
            )
            runtime.turn_worker = worker
        worker.start()

    def _run_runtime_turn(
        self,
        runtime: _PersistentRuntime,
        handle: CodexProcessHandle,
        prompt: str,
        thread_id: str | None,
        settings: dict[str, Any],
    ) -> None:
        event_writer = handle.event_log_file.open("a", encoding="utf-8")
        final_response = ""
        fallback_agent_response = ""
        latest_agent_message_by_id: dict[str, dict[str, Any]] = {}
        latest_plan_text_by_id: dict[str, str] = {}
        reasoning_summary_by_id: dict[str, dict[int, str]] = {}
        command_output_by_id: dict[str, str] = {}
        interrupted = False
        failed_message: str | None = None
        try:
            client, bound_thread_id = self._ensure_runtime_binding(runtime, thread_id, settings)
            self._write_runtime_event(
                {
                    "type": "thread.started",
                    "thread_id": bound_thread_id,
                    "footer_statusline": self._runtime_footer_statusline(runtime),
                },
                event_writer,
            )

            args = self._runtime_args(runtime.cwd, settings, thread_id=bound_thread_id)
            request_id = self._send_client_request(
                runtime,
                client,
                "turn/start",
                _build_turn_start_params(
                    bound_thread_id,
                    prompt,
                    args,
                    self._runtime_model(runtime),
                    _extract_reasoning_effort(runtime.status_line_state or {}),
                ),
            )
            request_acked = False
            turn_completed = False

            while not (request_acked and turn_completed):
                message = client.read_message()
                kind = message["kind"]
                if kind == "response":
                    message_id = int(message["id"])
                    if message_id == request_id:
                        request_acked = True
                        result = message["result"]
                        turn = result.get("turn") if isinstance(result, dict) else None
                        if isinstance(turn, dict):
                            turn_id = str(turn.get("id") or "").strip() or None
                            if turn_id:
                                with runtime.state_lock:
                                    runtime.current_turn_id = turn_id
                                    interrupt_requested = runtime.interrupt_requested
                                if interrupt_requested:
                                    self._send_turn_interrupt(runtime, client)
                        continue
                    if self._consume_aux_request(runtime, message_id):
                        continue
                if kind == "error":
                    message_id = int(message["id"])
                    if message_id == request_id:
                        details = message.get("data")
                        if details is None:
                            raise RuntimeError(f"turn/start 失败：{message['message']}")
                        raise RuntimeError(
                            f"turn/start 失败：{message['message']} "
                            f"({json.dumps(details, ensure_ascii=False)})"
                        )
                    if self._consume_aux_request(runtime, message_id):
                        continue
                if kind == "request":
                    client.reject_server_request(message["id"], message["method"])
                    continue
                if kind != "notification":
                    continue

                event = _map_notification(
                    message["method"],
                    message.get("params") or {},
                    latest_agent_message_by_id,
                    latest_plan_text_by_id,
                    reasoning_summary_by_id,
                    command_output_by_id,
                    runtime.status_line_state or {},
                )
                if event is None:
                    continue
                self._write_runtime_event(event, event_writer)

                item = event.get("item")
                if (
                    isinstance(item, dict)
                    and item.get("type") == "agent_message"
                    and isinstance(item.get("text"), str)
                ):
                    phase = str(item.get("phase") or "").strip()
                    if event["type"] == "item.completed":
                        if phase == "final_answer":
                            final_response = item["text"]
                        elif phase != "commentary":
                            fallback_agent_response = item["text"]

                if event["type"] in {"turn.completed", "turn.failed", "turn.interrupted"}:
                    if event["type"] == "turn.interrupted":
                        interrupted = True
                    elif event["type"] == "turn.failed":
                        failed_message = str(event.get("message") or "执行失败").strip() or "执行失败"
                    turn_completed = True

            handle.output_file.write_text(
                final_response or fallback_agent_response,
                encoding="utf-8",
            )
            if interrupted:
                _write_status(handle.status_file, exit_code=130, error_message="任务已中断")
            elif failed_message:
                _write_status(handle.status_file, exit_code=1, error_message=failed_message)
            else:
                _write_status(handle.status_file, exit_code=0)
        except Exception as exc:
            self.logger.exception("持久 runtime 执行会话 #%s 失败", runtime.session_id)
            self._write_runtime_event({"type": "error", "message": str(exc)}, event_writer)
            _write_status(handle.status_file, exit_code=1, error_message=str(exc))
            self._close_runtime(runtime)
        finally:
            with runtime.state_lock:
                runtime.current_turn_id = None
                runtime.interrupt_requested = False
                runtime.pending_aux_request_ids.clear()
                runtime.turn_worker = None
            event_writer.close()

    def _ensure_runtime_binding(
        self,
        runtime: _PersistentRuntime,
        requested_thread_id: str | None,
        settings: dict[str, Any],
    ) -> tuple[AppServerClient, str]:
        with runtime.state_lock:
            client = runtime.client
            bound_thread_id = runtime.bound_thread_id
            status_line_state = runtime.status_line_state

        if client is None:
            client = AppServerClient.start(self.config.codex_bin, runtime.cwd)
            config_read: dict[str, Any]
            try:
                config_read = client.request_simple(
                    "config/read",
                    {
                        "cwd": str(runtime.cwd),
                        "includeLayers": False,
                    },
                )
            except RuntimeError:
                config_read = {}
            config = config_read.get("config") if isinstance(config_read, dict) else {}
            status_line_state = {
                "cwd": runtime.cwd,
                "model": (
                    str((config or {}).get("model") or "").strip()
                    or str(settings.get("model") or self.config.codex_model or "").strip()
                    or "loading"
                ),
                "reasoning_effort": (
                    str(settings.get("reasoning_effort") or "").strip()
                    or _extract_reasoning_effort(config)
                ),
                "service_tier": (
                    str(settings.get("service_tier") or "").strip() or _extract_service_tier(config)
                ),
                "status_line_items": _extract_status_line_items(config),
                "context_remaining_percent": 100,
                "last_emitted_line": "",
            }
            with runtime.state_lock:
                runtime.client = client
                runtime.status_line_state = status_line_state
                bound_thread_id = runtime.bound_thread_id

        if requested_thread_id and bound_thread_id and requested_thread_id != bound_thread_id:
            self._close_runtime(runtime)
            return self._ensure_runtime_binding(runtime, requested_thread_id, settings)

        if bound_thread_id:
            return client, bound_thread_id

        args = self._runtime_args(runtime.cwd, settings, thread_id=requested_thread_id)
        if requested_thread_id:
            binding = client.request_simple(
                "thread/resume",
                _build_thread_resume_params(args),
            )
        else:
            binding = client.request_simple(
                "thread/start",
                _build_thread_start_params(args),
            )
        status_line_state = runtime.status_line_state or {}
        _update_status_line_from_binding(status_line_state, binding)
        bound_thread_id, thread_cwd = _resolve_thread_binding(binding)
        if thread_cwd and Path(thread_cwd).resolve() != runtime.cwd:
            raise RuntimeError(f"Codex 会话目录不一致：期望 {runtime.cwd}，实际 {thread_cwd}")
        _statusline_event_if_changed(status_line_state)
        with runtime.state_lock:
            runtime.client = client
            runtime.status_line_state = status_line_state
            runtime.bound_thread_id = bound_thread_id
        return client, bound_thread_id

    def _interrupt_runtime(self, runtime: _PersistentRuntime) -> None:
        with runtime.state_lock:
            client = runtime.client
            worker = runtime.turn_worker
            runtime.interrupt_requested = True
            turn_id = runtime.current_turn_id
            thread_id = runtime.bound_thread_id
        if client is None or worker is None or not worker.is_alive() or not turn_id or not thread_id:
            return
        self._send_turn_interrupt(runtime, client)

    def _send_turn_interrupt(
        self,
        runtime: _PersistentRuntime,
        client: AppServerClient,
    ) -> None:
        with runtime.state_lock:
            thread_id = runtime.bound_thread_id
            turn_id = runtime.current_turn_id
        if not thread_id or not turn_id:
            return
        request_id = self._send_client_request(
            runtime,
            client,
            "turn/interrupt",
            {
                "threadId": thread_id,
                "turnId": turn_id,
            },
        )
        with runtime.state_lock:
            runtime.pending_aux_request_ids.add(request_id)

    def _send_client_request(
        self,
        runtime: _PersistentRuntime,
        client: AppServerClient,
        method: str,
        params: dict[str, Any],
    ) -> int:
        with runtime.send_lock:
            return client.send_request(method, params)

    def _consume_aux_request(self, runtime: _PersistentRuntime, request_id: int) -> bool:
        with runtime.state_lock:
            if request_id not in runtime.pending_aux_request_ids:
                return False
            runtime.pending_aux_request_ids.discard(request_id)
            return True

    def _runtime_args(
        self,
        cwd: Path,
        settings: dict[str, Any],
        *,
        thread_id: str | None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            cwd=str(cwd),
            thread_id=thread_id,
            exec_mode=self.config.codex_exec_mode,
            model=settings.get("model") or self.config.codex_model,
            reasoning_effort=settings.get("reasoning_effort"),
            service_tier=settings.get("service_tier"),
            personality=settings.get("personality"),
            approval_policy=settings.get("approval_policy"),
            sandbox_mode=settings.get("sandbox_mode"),
            collaboration_mode=settings.get("collaboration_mode"),
            search=self.config.codex_enable_search,
            persist_extended_history=self.config.codex_persist_extended_history,
        )

    def _runtime_model(self, runtime: _PersistentRuntime) -> str | None:
        status_line_state = runtime.status_line_state or {}
        model = str(status_line_state.get("model") or "").strip()
        return model or None

    def _runtime_footer_statusline(self, runtime: _PersistentRuntime) -> str | None:
        status_line_state = runtime.status_line_state or {}
        line = _build_footer_statusline(status_line_state)
        return line or None

    def _write_runtime_event(self, payload: dict[str, Any], event_writer) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        event_writer.write(line)
        event_writer.write("\n")
        event_writer.flush()

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
