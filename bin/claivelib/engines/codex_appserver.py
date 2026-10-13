"""Turn-scoped Codex App Server bridge for the existing Claive duplex supervisor.

Reads Claive's prompt/steer frames on stdin, speaks Codex JSON-RPC on a private
child pipe, and emits the normal Codex adapter events on stdout. This process
owns only one active turn; reusable Claive workers resume the retained thread in
a new bridge process, just as codex exec resume did previously.
"""
import argparse
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time
import tempfile


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def home_for_server(base, opt_in, target, ephemeral=False):
    """Avoid silently inheriting user tools/rules in an otherwise hermetic job."""
    if not ephemeral:
        # The first steerable turn must leave a rollout to resume later, even
        # on a host that has never launched Codex before.
        (base / "sessions").mkdir(parents=True, exist_ok=True)
    for name in ("auth.json", "sessions"):
        source = base / name
        if source.exists():
            (target / name).symlink_to(source, target_is_directory=source.is_dir())
    if opt_in:
        for name in ("config.toml", "skills"):
            source = base / name
            if source.exists():
                (target / name).symlink_to(source, target_is_directory=source.is_dir())


def run(args):
    base = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="claive-codex-home-") as home:
        home_for_server(base, args.user_config, Path(home), args.ephemeral)
        env = dict(os.environ, CODEX_HOME=home)
        command = [args.binary, "app-server", "--listen", "stdio://",
                   "-c", 'model_reasoning_effort="' + args.effort + '"',
                   "-c", 'approval_policy="never"',
                   "-c", 'sandbox_mode="' + args.sandbox + '"',
                   "-c", 'web_search="' + ("live" if args.web else "disabled") + '"',
                   "-c", "project_doc_max_bytes=0"]
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=sys.stderr, env=env, bufsize=0)
        incoming = {sys.stdin.fileno(): bytearray(), child.stdout.fileno(): bytearray()}
        selector = selectors.DefaultSelector()
        selector.register(sys.stdin.buffer, selectors.EVENT_READ)
        selector.register(child.stdout, selectors.EVENT_READ)
        thread_id = None
        turn_id = None
        prompt = None
        pending_steers = []
        last_text = ""
        finished = False
        code = 1

        def respond_unsupported(identifier):
            child.stdin.write((json.dumps({"id": identifier, "error": {"code": -32601,
                              "message": "Claive does not permit interactive App Server approvals"}}) + "\n").encode())
            child.stdin.flush()

        def send(method, params=None, identifier=None):
            message = {"method": method}
            if params is not None:
                message["params"] = params
            if identifier is not None:
                message["id"] = identifier
            child.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
            child.stdin.flush()

        def start_turn():
            nonlocal prompt
            if thread_id and prompt is not None:
                send("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": prompt}],
                                    "model": args.model, "effort": args.effort}, "turn")
                prompt = None

        def drain_steers():
            if thread_id and turn_id and not finished:
                for request in pending_steers[:]:
                    send("turn/steer", {"threadId": thread_id, "expectedTurnId": turn_id,
                                        "input": [{"type": "text", "text": request["message"]}]},
                         "steer:" + request["id"])
                    pending_steers.remove(request)

        startup_at = time.monotonic()
        send("initialize", {"clientInfo": {"name": "claive", "version": "1.0"},
                            "capabilities": {"experimentalApi": True}}, "initialize")
        try:
            while not finished:
                if turn_id is None and time.monotonic() - startup_at > 90:
                    raise RuntimeError("Codex App Server did not start an active turn within 90 seconds")
                if child.poll() is not None and child.stdout.fileno() not in selector.get_map():
                    raise RuntimeError("Codex App Server terminated before turn completion")
                for key, _ in selector.select(0.25):
                    fd = key.fileobj.fileno()
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        if fd == sys.stdin.fileno():
                            raise RuntimeError("Claive steering transport closed")
                        if not finished:
                            raise RuntimeError("Codex App Server closed before turn completion")
                        continue
                    incoming[fd].extend(chunk)
                    while b"\n" in incoming[fd]:
                        line, _, tail = incoming[fd].partition(b"\n")
                        incoming[fd] = bytearray(tail)
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        if fd == sys.stdin.fileno():
                            if event.get("type") == "prompt" and prompt is None:
                                prompt = event["message"]
                                start_turn()
                            elif event.get("type") == "steer":
                                pending_steers.append(event)
                                drain_steers()
                            continue
                        identifier = event.get("id")
                        if identifier is not None and event.get("method"):
                            # Server-originated requests must never receive implicit approval.
                            respond_unsupported(identifier)
                            continue
                        if identifier is not None:
                            if isinstance(identifier, str) and identifier.startswith("steer:"):
                                success = "error" not in event
                                emit({"type": "control_response", "id": identifier[6:], "command": "steer",
                                      "success": success, "data": event.get("result"),
                                      "error": event.get("error")})
                                continue
                            if "error" in event:
                                raise RuntimeError(f"App Server {identifier}: {event['error']}")
                            result = event.get("result") or {}
                            if identifier == "initialize":
                                send("initialized")
                                if args.resume:
                                    send("thread/resume", {"threadId": args.resume, "model": args.model,
                                                           "cwd": args.cwd, "sandbox": args.sandbox,
                                                           "modelProvider": "openai", "approvalPolicy": "never"}, "thread")
                                else:
                                    send("thread/start", {"cwd": args.cwd, "model": args.model,
                                                          "sandbox": args.sandbox, "modelProvider": "openai",
                                                          "approvalPolicy": "never", "ephemeral": args.ephemeral}, "thread")
                            elif identifier == "thread":
                                thread_id = result.get("thread", {}).get("id")
                                if not thread_id:
                                    raise RuntimeError("App Server did not return thread ID")
                                emit({"type": "thread.started", "thread_id": thread_id})
                                start_turn()
                            elif identifier == "turn":
                                turn_id = result.get("turn", {}).get("id") or turn_id
                                if not turn_id:
                                    raise RuntimeError("App Server did not return active turn ID")
                                emit({"type": "turn.started"})
                                drain_steers()
                            continue
                        method = event.get("method")
                        data = event.get("params") or {}
                        if method == "turn/started":
                            if data.get("threadId") == thread_id:
                                turn_id = (data.get("turn") or {}).get("id") or turn_id
                                drain_steers()
                        elif method in ("item/started", "item/completed"):
                            if data.get("threadId") != thread_id:
                                continue
                            item = data.get("item") or {}
                            kind = {"commandExecution": "command_execution", "mcpToolCall": "mcp_tool_call",
                                    "fileChange": "file_change", "agentMessage": "agent_message"}.get(item.get("type"), item.get("type"))
                            if kind == "agent_message" and method == "item/completed":
                                last_text = item.get("text") or last_text
                            emit({"type": "item.started" if method == "item/started" else "item.completed",
                                  "item": {"type": kind, "id": item.get("id"), "command": item.get("command"),
                                           "text": item.get("text"), "exit_code": item.get("exitCode")}})
                        elif method == "turn/completed":
                            if data.get("threadId") != thread_id or (turn_id and data.get("turn", {}).get("id") != turn_id):
                                continue
                            turn = data.get("turn") or {}
                            status = turn.get("status")
                            if status == "completed":
                                emit({"type": "item.completed", "item": {"type": "agent_message", "text": last_text}})
                                emit({"type": "turn.completed"})
                                code = 0
                            else:
                                emit({"type": "turn.failed", "error": {"message":
                                      (turn.get("error") or {}).get("message") or f"Codex turn {status}"}})
                            finished = True
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
            emit({"type": "turn.failed", "error": {"message": str(error)}})
        finally:
            selector.close()
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        return code


def main():
    parser = argparse.ArgumentParser()
    for name in ("binary", "cwd", "model", "effort", "sandbox"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--resume")
    parser.add_argument("--web", action="store_true")
    parser.add_argument("--user-config", action="store_true")
    parser.add_argument("--ephemeral", action="store_true")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
