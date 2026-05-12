# Feishu Codex Bridge

Use a Feishu bot to run Codex through `codex app-server` and stream progress back to Feishu cards.

## Features

- Receives Feishu bot message events.
- Runs `codex app-server --listen stdio://` and controls Codex through JSON-RPC.
- Updates one Feishu card with streaming Codex output.
- Supports Codex approval requests for command execution and file changes.
- Supports `/status` and `/cancel <task_id>`.
- Supports basic shell navigation commands inside allowlisted repos.
- Keeps repo and user access behind explicit allowlists.

## Setup

Create a Feishu enterprise app, enable the bot capability, and subscribe to:

- `im.message.receive_v1`

Then copy the environment file:

```bash
cp .env.example .env
```

Fill in:

```env
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_VERIFICATION_TOKEN=xxx
CODEX_REPOS=feishu_codex=/Users/renyi/code/python/feishu_codex
DEFAULT_REPO=feishu_codex
CODEX_APPROVAL_POLICY=on-request
```

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Start the bridge in HTTP callback mode:

```bash
python run.py
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

If you use "send events to developer server", configure Feishu event callback URL:

```text
https://your-public-domain/feishu/events
```

If you want Feishu card approval buttons to work, configure the card callback URL:

```text
https://your-public-domain/feishu/card
```

For local development, expose port `8000` with a tunnel such as `ngrok`, `cloudflared`, or a reverse proxy.

## WebSocket Mode

If your Feishu event subscription uses "receive events through long connection", you do not need a callback URL.

In Feishu Developer Console:

- Open your app.
- Go to "Events and Callbacks".
- Set event subscription method to "receive events through long connection".
- Add event `im.message.receive_v1`.
- Publish the app version if Feishu asks you to.

Then start this bridge with:

```bash
python run_ws.py
```

Keep this process running. The SDK opens a WebSocket connection from your machine to Feishu, and Feishu pushes bot events through that connection.

Long connection mode receives message events, but card button callbacks may still need the `/feishu/card` HTTP callback depending on your Feishu app configuration. Approval can always be completed with text commands.

## Feishu Commands

Run Codex in the default repo:

```text
/codex 修改 README，补充部署说明
```

Run Codex in a specific allowlisted repo:

```text
/codex feishu_codex 给 FastAPI 服务增加单元测试
```

Check tasks:

```text
/status
```

List pending Codex approvals:

```text
/approvals
```

Approve or reject a pending Codex request:

```text
/approve <approval_id>
/approve-session <approval_id>
/deny <approval_id>
/abort <approval_id>
```

When Codex requests approval, the bridge replies with an approval card that includes the same `approval_id`.

Run basic shell commands directly in the bridge process:

```text
pwd
ls
ll
cd app
cd ..
cd feishu_codex
```

These commands are restricted to directories under `CODEX_REPOS`.

After `cd`, Codex also runs in the current session directory:

```text
cd app
/codex 修改 handlers.py
```

This runs Codex with `-C /.../feishu_codex/app`.

On Windows, `ls` and `ll` run through `cmd /c dir`. Drive inputs are accepted in a short slash form:

```text
cd c/test_dir
cd d/work/project
cd /
```

These map to:

```text
C:\test_dir
D:\work\project
C:\
```

For Windows, expose drive roots or project roots through `CODEX_REPOS`, for example:

```env
CODEX_REPOS=c=C:\,d=D:\,project=C:\work\project
DEFAULT_REPO=project
```

Cancel a running task:

```text
/cancel <task_id>
```

## Security Notes

- Set `ALLOWED_USERS` before using this in a real group.
- Keep `CODEX_REPOS` narrow. Do not expose your whole home directory.
- The default Codex mode is `codex app-server --listen stdio://`, with turns sent using `approvalPolicy=on-request` and `sandboxPolicy=workspaceWrite`.
- Avoid `--dangerously-bypass-approvals-and-sandbox` unless the bridge runs in an isolated disposable environment.
- HTTP callback mode does not decrypt Feishu encrypted callbacks yet. WebSocket mode is the recommended path for local development.

## Probe Codex Approval Protocol

To inspect how your installed Codex CLI emits approval-related JSONL events:

```bash
python scripts/probe_codex_approval.py
```

The script runs Codex in a temporary directory with:

```text
codex --ask-for-approval on-request exec --json --sandbox workspace-write
```

It prints every JSONL line and marks lines that look approval-related. Run this from your normal terminal, not from a restricted sandbox, so Codex can access its own `~/.codex` state.

Available scenarios:

```bash
python scripts/probe_codex_approval.py --scenario outside-workspace-write
python scripts/probe_codex_approval.py --scenario network
python scripts/probe_codex_approval.py --scenario tmp-write
```
