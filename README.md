# agy-mobile-web

A lightweight, zero-install mobile web client and bridge for **Google Antigravity** (AGY).

Control, inspect, and prompt your desktop Antigravity coding agents directly from your phone's browser when you're away from your keyboard.

---

## Highlights

- 📱 **Mobile-First Interface**: Responsive dark/light theme designed for mobile touch screens, swipe gestures, and full-screen PWA mode.
- 📷 **Camera & Photo Upload**: Snap photos directly with your phone's camera or select screenshots from your album. Automatically scales images on the client side using Canvas to ensure sub-second upload speeds.
- ⚡ **Real-Time Streaming**: Live streaming of agent thoughts, tool execution steps, and responses via lightweight HTTP polling and JSON-RPC bridge.
- 📊 **Quota & Resource Meters**: Track active model quotas (Claude, GPT, Gemini) and system memory/CPU at a glance.
- 🔒 **Token / PIN Authentication**: Protect your remote server with custom URL key parameters and session cookies.

---

## Architecture Overview

```
 [ Mobile Browser (Phone) ]
            │  HTTP / JSON-RPC
            ▼
   [ agy-mobile-web (server.py) ]
            │  Local IPC / JSON-RPC (HTTP POST)
            ▼
 [ Antigravity Language Server (Localhost) ]
            │
            ▼
    [ Coding Agent Session ]
```

1. **Frontend**: Pure vanilla JS + HTML5 + CSS (single file `index.html`), bundled with `marked.js` for markdown and syntax rendering. No Node.js build pipeline required.
2. **Backend**: FastAPI + Uvicorn server (`server.py`). Dynamically discovers running Antigravity Language Server processes, tails `transcript.jsonl` logs, and forwards multi-modal prompts.

---

## Quick Start

### 1. Requirements

- Python 3.10+
- Antigravity installed and running locally
- (Optional) Cloudflare Tunnel or router port forwarding if accessing outside your local Wi-Fi.

### 2. Installation

Clone the repository and install dependencies:

```bash
git clone https://github.com/ChubbyKuma/agy-mobile-web.git
cd agy-mobile-web
pip install -r requirements.txt
```

### 3. Running the Server

Start the bridge server:

```bash
python server.py
```

By default, the server binds to `0.0.0.0:8765`.

### 4. Configuration

You can customize the settings via environment variables:

| Variable | Description | Default |
| :--- | :--- | :--- |
| `AGY_SECRET_KEY` | Access token for mobile URL authentication | `chanson888` |
| `AGY_BRAIN_DIR` | Path to Antigravity brain directory | `~/.gemini/antigravity/brain` |
| `PORT` | Listening HTTP port | `8765` |

### 5. Accessing from Mobile

Open your mobile browser (Safari, Chrome, etc.) and navigate to:

```text
http://<YOUR_LOCAL_IP_OR_DOMAIN>:8765/?key=<YOUR_SECRET_KEY>
```

Add the page to your home screen for an app-like full-screen experience.

---

## Development & Contributing

Contributions, bug reports, and suggestions are welcome! Feel free to open an issue or submit a pull request.

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feat/my-feature`)
3. Commit your Changes (`git commit -m 'feat: add speech input'`)
4. Push to the Branch (`git push origin feat/my-feature`)
5. Open a Pull Request

---

## License

This project is licensed under the [MIT License](LICENSE).
