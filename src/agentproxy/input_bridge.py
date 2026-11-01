"""Private, nonblocking transport between Claude's command hooks and its proxy."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shlex
import socket
import sys
import tempfile
import time

MAX_MESSAGE_BYTES = 1024 * 1024


class HookConnection:
    def __init__(self, sock):
        self.sock = sock
        self.buffer = bytearray()
        self.output = bytearray()
        self.payload = None
        self.received_at = time.monotonic()
        self.closed = False
        self.on_delivered = None
        self.awaiting_receipt = False

    def respond(self, output: dict) -> None:
        if not self.closed and not self.output:
            self.output.extend(json.dumps(output).encode() + b"\n")

    def close(self):
        self.closed = True
        self.sock.close()


class InputBridge:
    def __init__(self):
        self._dir = tempfile.TemporaryDirectory(prefix="ap-input-")
        os.chmod(self._dir.name, 0o700)
        self.path = str(Path(self._dir.name) / "hook.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(16)
        self.listener.setblocking(False)
        self.connections = []

    def poll(self, on_request, on_disconnect):
        while True:
            try:
                sock, _ = self.listener.accept()
            except BlockingIOError:
                break
            sock.setblocking(False)
            self.connections.append(HookConnection(sock))
        for c in list(self.connections):
            try:
                if c.output:
                    written = c.sock.send(c.output)
                    del c.output[:written]
                    if not c.output:
                        if c.on_delivered is None:
                            c.close()
                        else:
                            c.awaiting_receipt = True
                else:
                    data = c.sock.recv(65536)
                    if not data:
                        if c.payload is not None:
                            on_disconnect(c)
                        c.close()
                    elif c.payload is None:
                        c.buffer.extend(data)
                        if len(c.buffer) > MAX_MESSAGE_BYTES:
                            c.close()
                        elif b"\n" in c.buffer:
                            c.payload = json.loads(bytes(c.buffer).split(b"\n", 1)[0])
                            if not isinstance(c.payload, dict):
                                raise ValueError("hook payload must be an object")
                            c.buffer.clear()
                            on_request(c, c.payload)
                    elif c.awaiting_receipt:
                        c.buffer.extend(data)
                        if len(c.buffer) > MAX_MESSAGE_BYTES:
                            raise ValueError("oversized hook receipt")
                        if b"\n" in c.buffer:
                            if json.loads(bytes(c.buffer).split(b"\n", 1)[0]) != {"delivered": True}:
                                raise ValueError("invalid hook receipt")
                            c.on_delivered()
                            c.close()
                if c.payload is None and time.monotonic() - c.received_at > 10:
                    c.close()
            except BlockingIOError:
                pass
            except (OSError, ValueError):
                on_disconnect(c)
                c.close()
            if c.closed:
                self.connections.remove(c)

    def close(self):
        for c in self.connections:
            c.close()
        self.connections.clear()
        self.listener.close()
        self._dir.cleanup()

    def argv(self, args: list[str], timeout_s: float, *, permission_approvals: bool = False) -> list[str]:
        """Merge a launch-local hook without replacing caller settings or hooks."""
        prefix, suffix = (args[:args.index("--")], args[args.index("--"):]) if "--" in args else (list(args), [])
        if "--bare" in prefix:
            raise ValueError("--user-input cannot be combined with --bare (hooks are required)")
        settings, out = {}, []
        i = 0
        while i < len(prefix):
            arg = prefix[i]
            if arg == "--settings" or arg.startswith("--settings="):
                if arg == "--settings":
                    i += 1
                    if i >= len(prefix):
                        raise ValueError("--settings needs a file or JSON")
                    value = prefix[i]
                else:
                    value = arg.split("=", 1)[1]
                try:
                    loaded = json.loads(value) if value.lstrip().startswith("{") else json.loads(Path(value).read_text())
                except (ValueError, OSError) as exc:
                    raise ValueError("--user-input could not read caller --settings") from exc
                if not isinstance(loaded, dict):
                    raise ValueError("--settings must contain an object")
                # A repeated settings flag means the last value to the CLI.
                settings = loaded
            else:
                out.append(arg)
            i += 1
        sources = {"user", "project", "local"}
        for i, arg in enumerate(prefix):
            if arg == "--setting-sources" and i + 1 < len(prefix):
                sources = set(prefix[i + 1].split(","))
            elif arg.startswith("--setting-sources="):
                sources = set(arg.split("=", 1)[1].split(","))
        paths = []
        if "user" in sources:
            paths.append(Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "settings.json")
        for directory in reversed((Path.cwd(), *Path.cwd().parents)):
            if "project" in sources:
                paths.append(directory / ".claude/settings.json")
            if "local" in sources:
                paths.append(directory / ".claude/settings.local.json")
        disabled = False
        for path in paths:
            if path.is_file():
                try:
                    inherited = json.loads(path.read_text())
                    if isinstance(inherited, dict) and "disableAllHooks" in inherited:
                        disabled = bool(inherited["disableAllHooks"])
                except (ValueError, OSError) as exc:
                    raise ValueError("--user-input could not inspect inherited hook settings") from exc
        disabled = settings.get("disableAllHooks", disabled)
        managed = Path("/etc/claude-code/managed-settings.json")
        if managed.is_file():
            try:
                policy = json.loads(managed.read_text())
                disabled = policy.get("disableAllHooks", disabled)
            except (ValueError, OSError) as exc:
                raise ValueError("--user-input could not inspect managed hook settings") from exc
        if disabled:
            raise ValueError("--user-input requires hooks; disableAllHooks is set")
        hooks = settings.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("settings hooks must be an object")
        command = shlex.join([sys.executable, "-m", "agentproxy.input_hook", self.path])
        events = [(e, "AskUserQuestion") for e in ("PreToolUse", "PostToolUse", "PostToolUseFailure")]
        if permission_approvals:
            events.extend([("PermissionRequest", ".*"), ("Notification", "permission_prompt")])
        for event, matcher in events:
            entries = hooks.setdefault(event, [])
            if not isinstance(entries, list):
                raise ValueError("settings hook entries must be arrays")
            entries.append({"matcher": matcher, "hooks": [{
                "type": "command", "command": command,
                "timeout": math.ceil(timeout_s + 120), "onFailure": "block",
            }]})
        # Keep caller settings out of the child's command line as well as out
        # of saved project/user configuration.
        config_path = Path(self._dir.name) / "settings.json"
        with os.fdopen(os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as config:
            json.dump(settings, config)
        return out + ["--settings", str(config_path)] + suffix
