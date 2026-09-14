#!/usr/bin/env python3
"""Drive the MCP server over stdio and check it behaves.

MCP Inspector is the usual tool for this, but it needs Node, and Ubuntu 22.04
ships Node 12 while the SDK needs 18+. This speaks the protocol directly with
nothing but the standard library, so it runs anywhere the server does.

    python3 evaluation/smoke_test.py              # protocol only, no radio needed
    python3 evaluation/smoke_test.py --live       # also call the read-only tools

--live talks to real hardware: it reads status, lists devices and measures a
spectrum. It never transmits and never changes the radio's tuning.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROTOCOL_VERSION = "2025-06-18"

READ_ONLY_TOOLS = [
    "sdr_get_status",
    "sdr_tx_chain_state",
    "sdr_list_devices",
    "sdr_board_health",
    "sdr_tx_status",
]


class Server:
    """A running server plus the JSON-RPC framing to talk to it."""

    def __init__(self, allow_tx: bool | None = None):
        """allow_tx None leaves the variable unset, which now means PERMITTED -
        the gate is opt-out. Pass False to start a server with it closed."""
        env = dict(os.environ)
        env.pop("SDR_MCP_ALLOW_TX", None)
        if allow_tx is True:
            env["SDR_MCP_ALLOW_TX"] = "1"
        elif allow_tx is False:
            env["SDR_MCP_ALLOW_TX"] = "0"
        # Never let the smoke test reconfigure the user's radio.
        env["SDR_MCP_NO_TX_QUIESCE"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "fishball_sdr_mcp"],
            cwd=ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        self._id = 0

    def _send(self, method: str, params: dict | None = None, notify: bool = False):
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                err = self.proc.stderr.read()
                raise RuntimeError(f"server closed stdout.\nstderr:\n{err}")
            line = line.strip()
            if not line:
                continue
            reply = json.loads(line)
            if reply.get("id") == self._id:
                return reply

    def initialize(self):
        reply = self._send("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "smoke_test", "version": "1.0"}})
        self._send("notifications/initialized", {}, notify=True)
        return reply

    def list_tools(self):
        return self._send("tools/list", {})

    def call(self, name: str, arguments: dict | None = None):
        return self._send("tools/call", {"name": name, "arguments": arguments or {}})

    def stop(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        return self.proc.stderr.read()


def text_of(reply: dict) -> str:
    result = reply.get("result", {})
    parts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="also call read-only tools against real hardware")
    args = ap.parse_args()

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
        if not ok:
            failures.append(name)

    print("=== protocol ===")
    s = Server()
    try:
        init = s.initialize()
        info = init.get("result", {}).get("serverInfo", {})
        check("initialize", "result" in init, f"{info.get('name')} {info.get('version')}")

        listed = s.list_tools()
        tools = listed.get("result", {}).get("tools", [])
        names = sorted(t["name"] for t in tools)
        check("tools/list", len(tools) >= 15, f"{len(tools)} tools")

        # Every tool needs a description and an input schema, or an agent cannot
        # use it correctly.
        undocumented = [t["name"] for t in tools if len(t.get("description", "")) < 40]
        check("all tools documented", not undocumented, ", ".join(undocumented) or "ok")
        unschemad = [t["name"] for t in tools if not t.get("inputSchema")]
        check("all tools have schemas", not unschemad, ", ".join(unschemad) or "ok")

        # Read-only tools must be annotated as such.
        ann = {t["name"]: (t.get("annotations") or {}) for t in tools}
        wrong = [n for n in READ_ONLY_TOOLS
                 if n in ann and not ann[n].get("readOnlyHint")]
        check("read-only tools annotated", not wrong, ", ".join(wrong) or "ok")

        # Transmit tools must be flagged destructive.
        tx_tools = ["sdr_tx_tone", "sdr_transmit_iq", "sdr_transmit_waveform"]
        not_flagged = [n for n in tx_tools
                       if n in ann and not ann[n].get("destructiveHint")]
        check("transmit tools flagged destructive", not not_flagged,
              ", ".join(not_flagged) or "ok")

        # The gate is opt-OUT: transmitting is permitted unless
        # SDR_MCP_ALLOW_TX=0. So this check runs a SECOND server with the gate
        # explicitly closed. It must never be run against the default server -
        # calling a transmit tool there would transmit for real, on whatever
        # the board happens to be connected to.
        print("\n=== transmit gate (a second server, SDR_MCP_ALLOW_TX=0) ===")
        closed = Server(allow_tx=False)
        try:
            closed.initialize()
            for name, arguments in (
                    ("sdr_tx_tone", {"lo_hz": 2_400_000_000}),
                    ("sdr_transmit_iq", {"path": "/nonexistent.iq16",
                                         "lo_hz": 2_400_000_000}),
                    ("sdr_transmit_waveform", {"lo_hz": 2_400_000_000})):
                body = text_of(closed.call(name, arguments))
                refused = "SDR_MCP_ALLOW_TX" in body
                check(f"{name} refuses when the gate is closed", refused,
                      body.splitlines()[0][:60] if body else "empty")
            body = text_of(closed.call("sdr_tx_status"))
            check("sdr_tx_status still works with the gate closed",
                  "Transmit status" in body,
                  body.splitlines()[0][:60] if body else "empty")
            body = text_of(closed.call("sdr_tx_disable"))
            check("sdr_tx_disable is never gated",
                  "SDR_MCP_ALLOW_TX" not in body,
                  body.splitlines()[0][:60] if body else "empty")
        finally:
            closed.stop()

        print("\n=== the default server permits transmitting ===")
        body = text_of(s.call("sdr_tx_status"))
        check("sdr_tx_status reports transmitting allowed",
              "yes" in body.lower().split("transmitting allowed")[-1][:40]
              if "transmitting allowed" in body.lower() else False,
              body.splitlines()[0][:60] if body else "empty")
        # Deliberately NOT calling a transmit tool here. With the gate open it
        # would key the radio, and a protocol test must not put RF on a port
        # whose cabling it knows nothing about.

        if args.live:
            print("\n=== live hardware (read-only) ===")
            for name in READ_ONLY_TOOLS:
                body = text_of(s.call(name))
                ok = bool(body) and not body.startswith("ERROR:")
                check(name, ok, body.splitlines()[0][:70] if body else "empty")
            body = text_of(s.call("sdr_spectrum", {"samples": 4096, "plot": False}))
            check("sdr_spectrum", not body.startswith("ERROR:"),
                  body.splitlines()[0][:70] if body else "empty")
            body = text_of(s.call("sdr_get_status", {"response_format": "json"}))
            try:
                parsed = json.loads(body)
                check("json response_format parses", isinstance(parsed, dict),
                      f"hw_model={parsed.get('hw_model')}")
            except json.JSONDecodeError:
                check("json response_format parses", False, body[:60])

        print("\n=== error handling ===")
        body = text_of(s.call("sdr_read_attribute",
                              {"device": "no-such-device", "attribute": "nope"}))
        check("unknown device gives an actionable error",
              "sdr_list_devices" in body, body.splitlines()[0][:70] if body else "empty")
    finally:
        stderr = s.stop()

    print("\n=== stdout hygiene ===")
    check("diagnostics went to stderr, not stdout", "fishball_sdr_mcp" in stderr,
          f"{len(stderr.splitlines())} stderr lines")

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
