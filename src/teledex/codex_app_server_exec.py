from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any


APP_SERVER_CLIENT_NAME = "teledex"
APP_SERVER_CLIENT_VERSION = "0.2.0"
_STREAM_TEXT_MAX_CHARS = 200_000
_REASONING_TEXT_MAX_CHARS = 12_000
_COMMAND_OUTPUT_MAX_CHARS = 4_000
_PLAN_TEXT_MAX_CHARS = 8_000
_BASELINE_TOKENS = 12_000


class AppServerClient:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise RuntimeError("codex app-server 标准流不可用")
        self.process = process
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr
        self.next_request_id = 0
        self.stderr_tail: deque[str] = deque(maxlen=40)
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            daemon=True,
        )
        self._stderr_thread.start()

    @classmethod
    def start(cls, codex_bin: str, cwd: Path) -> "AppServerClient":
        process = subprocess.Popen(
            [codex_bin, "app-server", "--listen", "stdio://"],
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        client = cls(process)
        client.initialize()
        return client

    def initialize(self) -> None:
        params = {
            "clientInfo": {
                "name": APP_SERVER_CLIENT_NAME,
                "version": APP_SERVER_CLIENT_VERSION,
            },
            "capabilities": {
                "experimentalApi": True,
            },
        }
        self.request_simple("initialize", params)
        self.send_notification("initialized")

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)

    def _drain_stderr(self) -> None:
        for line in self.stderr:
            text = line.rstrip()
            if text:
                self.stderr_tail.append(text)

    def stderr_summary(self) -> str:
        if not self.stderr_tail:
            return ""
        return "\n".join(self.stderr_tail)

    def send_payload(self, payload: dict[str, Any]) -> None:
        self.stdin.write(json.dumps(payload, ensure_ascii=False))
        self.stdin.write("\n")
        self.stdin.flush()

    def send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        self.send_payload(payload)

    def send_request(self, method: str, params: dict[str, Any]) -> int:
        request_id = self.next_request_id
        self.next_request_id += 1
        self.send_payload(
            {
                "id": request_id,
                "method": method,
                "params": params,
                "trace": None,
            }
        )
        return request_id

    def request_simple(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.send_request(method, params)
        while True:
            message = self.read_message()
            kind = message["kind"]
            if kind == "response" and message["id"] == request_id:
                return message["result"]
            if kind == "error" and message["id"] == request_id:
                details = message.get("data")
                if details is None:
                    raise RuntimeError(f"{method} 失败：{message['message']}")
                raise RuntimeError(
                    f"{method} 失败：{message['message']} ({json.dumps(details, ensure_ascii=False)})"
                )
            if kind == "request":
                self.reject_server_request(message["id"], message["method"])

    def reject_server_request(self, request_id: int, method: str) -> None:
        self.send_payload(
            {
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"teledex 不支持 app-server 服务端请求 `{method}`",
                },
            }
        )

    def read_message(self) -> dict[str, Any]:
        line = self.stdout.readline()
        if not line:
            status = self.process.poll()
            stderr_summary = self.stderr_summary()
            if stderr_summary:
                raise RuntimeError(
                    f"codex app-server 意外退出：{status}\n最近 stderr：\n{stderr_summary}"
                )
            raise RuntimeError(f"codex app-server 意外退出：{status}")
        raw = json.loads(line)
        if raw.get("method") is not None:
            method = str(raw["method"])
            if raw.get("id") is not None:
                return {
                    "kind": "request",
                    "id": int(raw["id"]),
                    "method": method,
                }
            return {
                "kind": "notification",
                "method": method,
                "params": raw.get("params") or {},
            }
        if raw.get("result") is not None:
            return {
                "kind": "response",
                "id": int(raw["id"]),
                "result": raw["result"],
            }
        if raw.get("error") is not None:
            error = raw["error"]
            return {
                "kind": "error",
                "id": int(raw["id"]),
                "message": str(error.get("message") or "未知 app-server 错误"),
                "data": error.get("data"),
            }
        raise RuntimeError(f"未知 app-server 消息：{line.strip()}")


def _emit_event(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _normalize_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    item_type = item.get("type")
    if not isinstance(item_type, str):
        return item
    normalized_type = {
        "userMessage": "user_message",
        "agentMessage": "agent_message",
        "commandExecution": "command_execution",
        "fileChange": "file_change",
        "mcpToolCall": "mcp_tool_call",
        "dynamicToolCall": "dynamic_tool_call",
        "collabToolCall": "collab_tool_call",
        "collabAgentToolCall": "collab_agent_tool_call",
        "webSearch": "web_search",
        "imageView": "image_view",
        "imageGeneration": "image_generation",
        "enteredReviewMode": "entered_review_mode",
        "exitedReviewMode": "exited_review_mode",
        "contextCompaction": "context_compaction",
    }.get(item_type, item_type)
    normalized = dict(item)
    normalized["type"] = normalized_type
    return normalized


def _append_capped(base: str, delta: str, max_chars: int) -> str:
    if not delta:
        return base
    merged = f"{base}{delta}"
    if len(merged) <= max_chars:
        return merged
    return merged[-max_chars:]


def _summarize_plan(explanation: Any, plan: Any) -> str:
    lines: list[str] = []
    explanation_text = str(explanation or "").strip()
    if explanation_text:
        lines.append(explanation_text)

    if isinstance(plan, list):
        status_labels = {
            "pending": "待办",
            "inProgress": "进行中",
            "in_progress": "进行中",
            "completed": "已完成",
        }
        for index, raw_step in enumerate(plan, start=1):
            if not isinstance(raw_step, dict):
                continue
            step = str(raw_step.get("step") or "").strip()
            if not step:
                continue
            status = str(raw_step.get("status") or "").strip()
            status_text = status_labels.get(status, status or "未知")
            lines.append(f"{index}. [{status_text}] {step}")

    text = "\n".join(lines).strip()
    if len(text) <= _PLAN_TEXT_MAX_CHARS:
        return text
    return text[: _PLAN_TEXT_MAX_CHARS - 3].rstrip() + "..."


def _extract_error_message(params: dict[str, Any]) -> str:
    error = params.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        if message:
            return message
        return json.dumps(error, ensure_ascii=False)
    return str(params.get("message") or "未知 app-server 错误").strip() or "未知 app-server 错误"


def _reasoning_effort_label(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return {
        "minimal": "minimal",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "xhigh",
    }.get(normalized, "default")


def _format_directory_display(directory: Path) -> str:
    try:
        home = Path.home().resolve()
        resolved = directory.resolve()
        relative = resolved.relative_to(home)
        return "~" if not str(relative) else f"~/{relative.as_posix()}"
    except Exception:
        return directory.as_posix()


def _compute_context_remaining_percent(token_usage: dict[str, Any]) -> int:
    model_context_window = token_usage.get("modelContextWindow")
    if not isinstance(model_context_window, int):
        return 100
    if model_context_window <= _BASELINE_TOKENS:
        return 0

    last_usage = token_usage.get("last")
    if not isinstance(last_usage, dict):
        return 100
    total_tokens = int(last_usage.get("totalTokens") or 0)
    effective_window = model_context_window - _BASELINE_TOKENS
    used = max(total_tokens - _BASELINE_TOKENS, 0)
    remaining = max(effective_window - used, 0)
    return int(round(max(0.0, min(100.0, remaining * 100.0 / effective_window))))


def _build_footer_statusline(status_line_state: dict[str, Any]) -> str:
    model = str(status_line_state.get("model") or "").strip() or "loading"
    reasoning_effort = _reasoning_effort_label(status_line_state.get("reasoning_effort"))
    current_dir = _format_directory_display(Path(status_line_state["cwd"]))
    context_remaining = status_line_state.get("context_remaining_percent")

    segments = [f"{model} {reasoning_effort}"]
    if isinstance(context_remaining, int):
        segments.append(f"{context_remaining}% left")
    segments.append(current_dir)
    return " · ".join(segment for segment in segments if segment)


def _statusline_event_if_changed(status_line_state: dict[str, Any]) -> dict[str, Any] | None:
    line = _build_footer_statusline(status_line_state)
    previous = str(status_line_state.get("last_emitted_line") or "")
    if not line or line == previous:
        return None
    status_line_state["last_emitted_line"] = line
    return {
        "type": "statusline.updated",
        "footer_statusline": line,
    }


def _update_agent_message(
    latest_agent_message_by_id: dict[str, dict[str, Any]],
    item_id: str,
    delta: str,
) -> dict[str, Any]:
    latest_item = dict(latest_agent_message_by_id.get(item_id, {}))
    latest_text = str(latest_item.get("text") or "")
    latest_text = _append_capped(latest_text, delta, _STREAM_TEXT_MAX_CHARS)
    latest_item.update(
        {
            "type": "agent_message",
            "id": item_id,
            "text": latest_text,
        }
    )
    latest_agent_message_by_id[item_id] = latest_item
    return latest_item


def _update_plan_item(
    latest_plan_text_by_id: dict[str, str],
    item_id: str,
    delta: str,
) -> dict[str, Any]:
    latest_plan_text = latest_plan_text_by_id.get(item_id, "")
    latest_plan_text = _append_capped(latest_plan_text, delta, _PLAN_TEXT_MAX_CHARS)
    latest_plan_text_by_id[item_id] = latest_plan_text
    return {
        "type": "plan",
        "id": item_id,
        "text": latest_plan_text,
    }


def _render_reasoning_summary(
    reasoning_summary_by_id: dict[str, dict[int, str]],
    item_id: str,
) -> str:
    parts = reasoning_summary_by_id.get(item_id, {})
    if not parts:
        return ""
    text = "\n\n".join(parts[index] for index in sorted(parts) if parts[index]).strip()
    if len(text) <= _REASONING_TEXT_MAX_CHARS:
        return text
    return text[: _REASONING_TEXT_MAX_CHARS - 3].rstrip() + "..."


def _update_reasoning_summary(
    reasoning_summary_by_id: dict[str, dict[int, str]],
    item_id: str,
    summary_index: int,
    delta: str,
) -> str:
    summaries = reasoning_summary_by_id.setdefault(item_id, {})
    current = summaries.get(summary_index, "")
    summaries[summary_index] = _append_capped(
        current,
        delta,
        _REASONING_TEXT_MAX_CHARS,
    )
    return _render_reasoning_summary(reasoning_summary_by_id, item_id)


def _ensure_reasoning_summary_index(
    reasoning_summary_by_id: dict[str, dict[int, str]],
    item_id: str,
    summary_index: int,
) -> None:
    reasoning_summary_by_id.setdefault(item_id, {}).setdefault(summary_index, "")


def _update_command_output(
    command_output_by_id: dict[str, str],
    item_id: str,
    delta: str,
) -> str:
    current = command_output_by_id.get(item_id, "")
    updated = _append_capped(current, delta, _COMMAND_OUTPUT_MAX_CHARS)
    command_output_by_id[item_id] = updated
    return updated


def _execution_overrides(exec_mode: str) -> dict[str, str]:
    if exec_mode == "dangerous":
        return {
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
    if exec_mode == "full-auto":
        return {
            "approvalPolicy": "on-request",
            "sandbox": "workspace-write",
        }
    return {}


def _build_thread_start_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {
        "cwd": str(Path(args.cwd)),
        "ephemeral": False,
    }
    if args.persist_extended_history:
        params["persistExtendedHistory"] = True
    params.update(_execution_overrides(args.exec_mode))
    if args.model:
        params["model"] = args.model
    if args.search:
        params["config"] = {"web_search": True}
    return params


def _build_thread_resume_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {
        "threadId": args.thread_id,
    }
    if args.persist_extended_history:
        params["persistExtendedHistory"] = True
    params.update(_execution_overrides(args.exec_mode))
    if args.model:
        params["model"] = args.model
    if args.search:
        params["config"] = {"web_search": True}
    return params


def _build_turn_start_params(thread_id: str, prompt: str) -> dict[str, Any]:
    return {
        "threadId": thread_id,
        "input": [
            {
                "type": "text",
                "text": prompt,
                "text_elements": [],
            }
        ],
    }


def _resolve_thread_binding(result: dict[str, Any]) -> tuple[str, str | None]:
    thread = result.get("thread")
    if not isinstance(thread, dict):
        raise RuntimeError("app-server 响应缺少 thread")
    thread_id = thread.get("id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise RuntimeError("app-server thread 缺少 id")
    cwd = thread.get("cwd") or result.get("cwd")
    return thread_id, str(cwd) if cwd else None


def _map_notification(
    method: str,
    params: dict[str, Any],
    latest_agent_message_by_id: dict[str, dict[str, Any]],
    latest_plan_text_by_id: dict[str, str],
    reasoning_summary_by_id: dict[str, dict[int, str]],
    command_output_by_id: dict[str, str],
    status_line_state: dict[str, Any],
) -> dict[str, Any] | None:
    if method == "thread/started":
        thread = params.get("thread")
        if not isinstance(thread, dict):
            return None
        thread_id = str(thread.get("id") or "").strip()
        if not thread_id:
            return None
        return {
            "type": "thread.started",
            "thread_id": thread_id,
            "cwd": thread.get("cwd"),
            "status": thread.get("status"),
            "footer_statusline": _build_footer_statusline(status_line_state),
        }
    if method == "turn/started":
        return {"type": "turn.started"}
    if method == "turn/completed":
        turn = params.get("turn")
        if isinstance(turn, dict) and turn.get("status") == "failed":
            error = turn.get("error")
            return {
                "type": "turn.failed",
                "message": json.dumps(error, ensure_ascii=False)
                if error is not None
                else "执行失败",
            }
        if isinstance(turn, dict) and turn.get("status") == "interrupted":
            return {
                "type": "turn.interrupted",
                "message": "任务已中断",
            }
        return {
            "type": "turn.completed",
            "usage": turn.get("usage") if isinstance(turn, dict) else None,
        }
    if method == "turn/plan/updated":
        return {
            "type": "plan.updated",
            "plan_id": f"turn-plan:{str(params.get('turnId') or 'current')}",
            "text": _summarize_plan(
                params.get("explanation"),
                params.get("plan"),
            ),
        }
    if method == "model/rerouted":
        from_model = str(params.get("fromModel") or "").strip()
        to_model = str(params.get("toModel") or "").strip()
        reason = str(params.get("reason") or "").strip()
        if to_model:
            status_line_state["model"] = to_model
        message = "模型路由已调整"
        if from_model and to_model:
            message = f"模型已切换：{from_model} -> {to_model}"
        if reason:
            message = f"{message}（{reason}）"
        footer_statusline = _build_footer_statusline(status_line_state)
        return {
            "type": "status.updated",
            "message": message,
            "footer_statusline": footer_statusline,
        }
    if method == "thread/tokenUsage/updated":
        token_usage = params.get("tokenUsage")
        if not isinstance(token_usage, dict):
            return None
        status_line_state["context_remaining_percent"] = _compute_context_remaining_percent(
            token_usage
        )
        return _statusline_event_if_changed(status_line_state)
    if method == "error":
        return {
            "type": "error",
            "message": _extract_error_message(params),
        }
    if method == "item/started":
        item = _normalize_item(params.get("item"))
        if (
            isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("id"), str)
        ):
            latest_agent_message_by_id[item["id"]] = dict(item)
        if isinstance(item, dict) and item.get("type") == "plan" and isinstance(item.get("id"), str):
            latest_plan_text_by_id[item["id"]] = str(item.get("text") or "")
        if (
            isinstance(item, dict)
            and item.get("type") == "command_execution"
            and isinstance(item.get("id"), str)
            and isinstance(item.get("aggregatedOutput"), str)
        ):
            command_output_by_id[item["id"]] = item["aggregatedOutput"]
        return {
            "type": "item.started",
            "item": item,
        }
    if method == "item/completed":
        item = _normalize_item(params.get("item"))
        if (
            isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("id"), str)
        ):
            latest_agent_message_by_id[item["id"]] = dict(item)
        if isinstance(item, dict) and item.get("type") == "plan" and isinstance(item.get("id"), str):
            latest_plan_text_by_id[item["id"]] = str(item.get("text") or "")
        if (
            isinstance(item, dict)
            and item.get("type") == "command_execution"
            and isinstance(item.get("id"), str)
            and isinstance(item.get("aggregatedOutput"), str)
        ):
            command_output_by_id[item["id"]] = item["aggregatedOutput"]
        return {
            "type": "item.completed",
            "item": item,
        }
    if method == "item/agentMessage/delta":
        item_id = params.get("itemId")
        delta = params.get("delta")
        if not isinstance(item_id, str) or not isinstance(delta, str):
            raise RuntimeError("item/agentMessage/delta 缺少必要字段")
        latest_item = _update_agent_message(latest_agent_message_by_id, item_id, delta)
        return {
            "type": "item.updated",
            "item": latest_item,
        }
    if method == "agent/messageDelta":
        delta = params.get("delta")
        role = str(params.get("role") or "assistant").strip()
        if role != "assistant" or not isinstance(delta, str):
            return None
        latest_item = _update_agent_message(
            latest_agent_message_by_id,
            "agent_message_fallback",
            delta,
        )
        return {
            "type": "item.updated",
            "item": latest_item,
        }
    if method == "item/plan/delta":
        item_id = params.get("itemId")
        delta = params.get("delta")
        if not isinstance(item_id, str) or not isinstance(delta, str):
            raise RuntimeError("item/plan/delta 缺少必要字段")
        return {
            "type": "item.updated",
            "item": _update_plan_item(latest_plan_text_by_id, item_id, delta),
        }
    if method == "item/reasoning/summaryPartAdded":
        item_id = params.get("itemId")
        summary_index = params.get("summaryIndex")
        if not isinstance(item_id, str) or not isinstance(summary_index, int):
            raise RuntimeError("item/reasoning/summaryPartAdded 缺少必要字段")
        _ensure_reasoning_summary_index(reasoning_summary_by_id, item_id, summary_index)
        return None
    if method == "item/reasoning/summaryTextDelta":
        item_id = params.get("itemId")
        summary_index = params.get("summaryIndex")
        delta = params.get("delta")
        if (
            not isinstance(item_id, str)
            or not isinstance(summary_index, int)
            or not isinstance(delta, str)
        ):
            raise RuntimeError("item/reasoning/summaryTextDelta 缺少必要字段")
        return {
            "type": "reasoning.updated",
            "item_id": item_id,
            "text": _update_reasoning_summary(
                reasoning_summary_by_id,
                item_id,
                summary_index,
                delta,
            ),
        }
    if method == "reasoning/summaryTextDelta":
        delta = params.get("delta")
        if not isinstance(delta, str):
            return None
        return {
            "type": "reasoning.updated",
            "item_id": "reasoning_summary_fallback",
            "text": _update_reasoning_summary(
                reasoning_summary_by_id,
                "reasoning_summary_fallback",
                0,
                delta,
            ),
        }
    if method == "item/commandExecution/outputDelta":
        item_id = params.get("itemId")
        delta = params.get("delta")
        if not isinstance(item_id, str) or not isinstance(delta, str):
            raise RuntimeError("item/commandExecution/outputDelta 缺少必要字段")
        return {
            "type": "command.output",
            "item_id": item_id,
            "text": _update_command_output(command_output_by_id, item_id, delta),
        }
    return None


def run(args: argparse.Namespace) -> int:
    client: AppServerClient | None = None
    final_response = ""
    latest_agent_message_by_id: dict[str, dict[str, Any]] = {}
    latest_plan_text_by_id: dict[str, str] = {}
    reasoning_summary_by_id: dict[str, dict[int, str]] = {}
    command_output_by_id: dict[str, str] = {}
    fallback_agent_response = ""
    try:
        cwd = Path(args.cwd).resolve()
        client = AppServerClient.start(args.codex_bin, cwd)
        try:
            config_read = client.request_simple(
                "config/read",
                {
                    "cwd": str(cwd),
                    "includeLayers": False,
                },
            )
        except RuntimeError:
            config_read = {}
        config = config_read.get("config") if isinstance(config_read, dict) else {}
        status_line_state = {
            "cwd": cwd,
            "model": (
                str((config or {}).get("model") or "").strip()
                or str(args.model or "").strip()
                or "loading"
            ),
            "reasoning_effort": (config or {}).get("modelReasoningEffort"),
            "context_remaining_percent": 100,
            "last_emitted_line": "",
        }
        if args.thread_id:
            binding = client.request_simple(
                "thread/resume",
                _build_thread_resume_params(args),
            )
        else:
            binding = client.request_simple(
                "thread/start",
                _build_thread_start_params(args),
            )
        thread_id, thread_cwd = _resolve_thread_binding(binding)
        if thread_cwd and Path(thread_cwd).resolve() != cwd:
            raise RuntimeError(
                f"Codex 会话目录不一致：期望 {cwd}，实际 {thread_cwd}"
            )

        initial_statusline = _statusline_event_if_changed(status_line_state)
        _emit_event(
            {
                "type": "thread.started",
                "thread_id": thread_id,
                **({"footer_statusline": initial_statusline["footer_statusline"]} if initial_statusline else {}),
            }
        )
        request_id = client.send_request(
            "turn/start",
            _build_turn_start_params(thread_id, args.prompt),
        )
        request_acked = False
        turn_completed = False

        while not (request_acked and turn_completed):
            message = client.read_message()
            kind = message["kind"]
            if kind == "response" and message["id"] == request_id:
                request_acked = True
                continue
            if kind == "error" and message["id"] == request_id:
                details = message.get("data")
                if details is None:
                    raise RuntimeError(f"turn/start 失败：{message['message']}")
                raise RuntimeError(
                    f"turn/start 失败：{message['message']} ({json.dumps(details, ensure_ascii=False)})"
                )
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
                status_line_state,
            )
            if event is None:
                continue
            _emit_event(event)

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
                turn_completed = True

        Path(args.output_file).write_text(
            final_response or fallback_agent_response,
            encoding="utf-8",
        )
        return 0
    except Exception as exc:
        _emit_event({"type": "error", "message": str(exc)})
        return 1
    finally:
        if client is not None:
            client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-bin", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--exec-mode", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--thread-id")
    parser.add_argument("--model")
    parser.add_argument("--search", action="store_true")
    parser.add_argument("--persist-extended-history", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
