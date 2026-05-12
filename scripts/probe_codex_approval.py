from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path


PROMPTS = {
    "tmp-write": (
        "Run a shell command that writes the word approval-probe to /tmp/codex-approval-probe.txt, "
        "then report whether it succeeded."
    ),
    "outside-workspace-write": (
        "Run a shell command that writes the word approval-probe to "
        "/usr/local/codex-approval-probe.txt, then report whether it succeeded. "
        "Do not use sudo."
    ),
    "network": (
        "Run a shell command that fetches https://example.com with curl and prints only the HTTP "
        "status code, then report whether it succeeded."
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Codex CLI approval JSONL behavior.")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument(
        "--scenario",
        choices=sorted(PROMPTS),
        default="outside-workspace-write",
        help="Probe scenario to run.",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Override the scenario prompt.",
    )
    parser.add_argument(
        "--pipe-stdin",
        action="store_true",
        help=(
            "Open stdin as a pipe. Codex exec treats piped stdin as extra prompt input, "
            "so this is only for debugging and may block unless stdin is closed."
        ),
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="codex-approval-probe-") as tmp:
        cwd = Path(tmp)
        command = [
            args.codex_bin,
            "--ask-for-approval",
            "on-request",
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "-C",
            str(cwd),
            args.prompt or PROMPTS[args.scenario],
        ]

        print("COMMAND:", " ".join(command), flush=True)
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if args.pipe_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        assert proc.stdout is not None
        started = time.monotonic()
        approval_events: list[dict] = []

        while True:
            if time.monotonic() - started > args.timeout:
                print("TIMEOUT: terminating process", flush=True)
                proc.terminate()
                return 124

            line = proc.stdout.readline()
            if not line:
                break

            stripped = line.rstrip("\n")
            print("JSONL:", stripped, flush=True)

            payload = parse_json(stripped)
            if payload and looks_like_approval(payload):
                approval_events.append(payload)
                print("APPROVAL_EVENT_DETECTED:", json.dumps(payload, ensure_ascii=False), flush=True)
                if proc.stdin:
                    # This is intentionally experimental. In current Codex CLI, piped stdin is
                    # normally consumed as additional prompt input, not as an approval channel.
                    proc.stdin.write(json.dumps({"type": "approval_response", "decision": "deny"}) + "\n")
                    proc.stdin.flush()
                    print("STDIN_SENT: approval_response deny", flush=True)

        return_code = proc.wait(timeout=5)
        print("EXIT_CODE:", return_code, flush=True)
        print("APPROVAL_EVENT_COUNT:", len(approval_events), flush=True)
        return return_code


def parse_json(line: str) -> dict | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def looks_like_approval(value: object) -> bool:
    if not isinstance(value, dict):
        return False

    event_type = str(value.get("type", "")).lower()
    if any(word in event_type for word in ("approval", "approve", "permission", "escalat")):
        return True

    item = value.get("item")
    if isinstance(item, dict):
        item_type = str(item.get("type", "")).lower()
        status = str(item.get("status", "")).lower()
        return any(
            word in f"{item_type} {status}"
            for word in ("approval", "approve", "permission", "escalat", "waiting")
        )

    return False


if __name__ == "__main__":
    raise SystemExit(main())
