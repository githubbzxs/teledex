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
APP_SERVER_CLIENT_VERSION = "0.1.0"


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
                "experimental_api": True,
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
    latest_agent_message_by_id: dict[str, str],
) -> dict[str, Any] | None:
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
        return {
            "type": "turn.completed",
            "usage": turn.get("usage") if isinstance(turn, dict) else None,
        }
    if method == "error":
        return {
            "type": "error",
            "message": str(params.get("message") or "未知 app-server 错误"),
        }
    if method == "item/started":
        return {
            "type": "item.started",
            "item": _normalize_item(params.get("item")),
        }
    if method == "item/completed":
        item = _normalize_item(params.get("item"))
        if (
            isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("id"), str)
            and isinstance(item.get("text"), str)
        ):
            latest_agent_message_by_id[item["id"]] = item["text"]
        return {
            "type": "item.completed",
            "item": item,
        }
    if method == "item/agentMessage/delta":
        item_id = params.get("itemId")
        delta = params.get("delta")
        if not isinstance(item_id, str) or not isinstance(delta, str):
            raise RuntimeError("item/agentMessage/delta 缺少必要字段")
        latest_text = latest_agent_message_by_id.get(item_id, "")
        latest_text += delta
        latest_agent_message_by_id[item_id] = latest_text
        return {
            "type": "item.updated",
            "item": {
                "type": "agent_message",
                "id": item_id,
                "text": latest_text,
            },
        }
    return None


def run(args: argparse.Namespace) -> int:
    client: AppServerClient | None = None
    final_response = ""
    latest_agent_message_by_id: dict[str, str] = {}
    try:
        cwd = Path(args.cwd).resolve()
        client = AppServerClient.start(args.codex_bin, cwd)
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

        _emit_event({"type": "thread.started", "thread_id": thread_id})
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
                final_response = item["text"]

            if event["type"] in {"turn.completed", "turn.failed"}:
                turn_completed = True

        Path(args.output_file).write_text(final_response, encoding="utf-8")
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
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
