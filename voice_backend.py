#!/usr/bin/env python3
"""Headless Vernest backend for the Tauri UI.

This keeps the existing audio/ASR/hotkey core usable while the desktop shell is
rewritten in Tauri + React. The API is intentionally small and bound to loopback.
"""

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(__file__))

import voice_core as core


AUTH_TOKEN = ""
PROTOCOL_VERSION = 1


class BackendState:
    def __init__(self):
        self.lock = threading.Lock()
        self.service = "starting"
        self.last_event = ""
        self.last_event_at = time.time()

    def touch(self, event):
        with self.lock:
            self.last_event = str(event)
            self.last_event_at = time.time()

    def snapshot(self):
        with self.lock:
            service = self.service
            last_event = self.last_event
            last_event_at = self.last_event_at
        return {
            "schema_version": PROTOCOL_VERSION,
            "service": service,
            "last_event": last_event,
            "last_event_at": last_event_at,
            "enabled": bool(core.state.get("enabled")),
            "recording": bool(core.state.get("recording")),
            "engine": core.state.get("engine") or "none",
            "last_text": core.state.get("last_text") or "",
            "raw_text": core.state.get("raw_text") or "",
            "last_error": core.state.get("last_error") or "",
            "audio_mode": core.state.get("audio_mode") or "共享",
            "mic_guarded": bool(core.state.get("mic_guarded")),
            "exclusive": bool(core.config.get("exclusive_device", True)),
            "floating_bubble": bool(core.config.get("floating_bubble", False)),
            "input_device_index": core.config.get("input_device_index"),
            "language": core.config.get("language", "auto"),
            "polish_enabled": bool(core.config.get("polish_enabled", False)),
            "polish_available": bool(core.state.get("polish_available")),
            "polish_model": core.state.get("polish_model") or core.POLISH_MODEL_FILE,
            "polish_last_error": core.state.get("polish_last_error") or "",
        }


backend_state = BackendState()


def _drain_ui_queue():
    while True:
        try:
            msg = core.ui_queue.get(timeout=0.2)
        except Exception:
            continue
        try:
            kind = msg[0]
            if kind == "result":
                core.state["last_text"] = msg[1]
                core.state["last_error"] = ""
            elif kind == "error":
                core.state["last_error"] = msg[1]
            elif kind == "recording":
                recording = bool(msg[1])
                if recording and not core.state.get("recording"):
                    core.log("backend ignored stale recording=true event after stop")
                    continue
                core.state["recording"] = recording
            if kind != "status":
                backend_state.touch(kind)
        except Exception as exc:
            core.log(f"backend queue drain failed: {exc!r}")


def _toggle_enabled():
    core.state["enabled"] = not core.state["enabled"]
    core.ui_queue.put(("toggled", core.state["enabled"]))
    backend_state.touch("toggle")
    if not core.state["enabled"] and core.state["recording"]:
        core.stop_recording()


class Handler(BaseHTTPRequestHandler):
    server_version = "VernestBackend/0.2"

    def log_message(self, fmt, *args):
        if urlparse(getattr(self, "path", "")).path in {"/api/status", "/api/health"}:
            return
        core.log("backend http: " + (fmt % args))

    def _send(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type, x-vernest-token")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        return not AUTH_TOKEN or self.headers.get("X-Vernest-Token") == AUTH_TOKEN

    def _require_auth(self):
        if self._authorized():
            return True
        self._send({"ok": False, "error": "forbidden"}, 403)
        return False

    def _read_json(self):
        length = int(self.headers.get("content-length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self._send({"ok": True})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            self._send({"ok": True, "service": "vernest-backend", "protocol_version": PROTOCOL_VERSION})
        elif not self._require_auth():
            return
        elif path == "/api/status":
            self._send({"ok": True, "state": backend_state.snapshot()})
        elif path == "/api/devices":
            devices = [
                {"index": idx, "name": name, "default": is_default}
                for idx, name, is_default in core.AudioRecorder.list_devices()
            ]
            self._send({"ok": True, "devices": devices})
        else:
            self._send({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._require_auth():
            return
        data = self._read_json()
        try:
            if path == "/api/toggle":
                _toggle_enabled()
                self._send({"ok": True, "state": backend_state.snapshot()})
            elif path == "/api/start":
                core.start_recording()
                backend_state.touch("start")
                self._send({"ok": True, "state": backend_state.snapshot()})
            elif path == "/api/stop":
                core.stop_recording()
                backend_state.touch("stop")
                self._send({"ok": True, "state": backend_state.snapshot()})
            elif path == "/api/config":
                if "exclusive_device" in data:
                    core.config["exclusive_device"] = bool(data["exclusive_device"])
                if "floating_bubble" in data:
                    core.config["floating_bubble"] = bool(data["floating_bubble"])
                if "polish_enabled" in data:
                    core.config["polish_enabled"] = bool(data["polish_enabled"])
                    core.state["polish_enabled"] = bool(data["polish_enabled"])
                    if not core.config["polish_enabled"]:
                        core.unload_polish_model()
                if "input_device_index" in data:
                    core.config["input_device_index"] = data["input_device_index"]
                core.refresh_polish_state()
                core.save_config()
                backend_state.touch("config")
                self._send({"ok": True, "state": backend_state.snapshot()})
            else:
                self._send({"ok": False, "error": "not found"}, 404)
        except Exception as exc:
            core.log(f"backend command failed: {exc!r}")
            self._send({"ok": False, "error": str(exc)}, 500)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=47632)
    parser.add_argument("--token", default="")
    parser.add_argument("--no-hotkeys", action="store_true")
    args = parser.parse_args()

    global AUTH_TOKEN
    AUTH_TOKEN = args.token

    core.log("Vernest backend starting")
    core.log("backend prompt audio is owned by Tauri")
    has_engine = core.load_config()
    backend_state.service = "ready" if has_engine else "engine_missing"
    threading.Thread(target=_drain_ui_queue, daemon=True).start()
    if args.no_hotkeys:
        core.log("backend hotkeys disabled; Tauri host handles global shortcuts")
    else:
        core.log("backend hotkeys are no longer implemented in Python; Tauri host handles shortcuts")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    core.log(f"Vernest backend listening on 127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    finally:
        try:
            core.stop_recording()
        except Exception:
            pass


if __name__ == "__main__":
    main()
