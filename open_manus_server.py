#!/usr/bin/env python3
"""
Open Manus — Web Server & REST API
Provides a web-based chat interface and REST API for interacting with the Open Manus agent.
This is the main entry point for Railway deployment.

Endpoints:
  GET  /           — Web UI (chat interface)
  GET  /health     — Health check
  POST /chat       — Send a message to the agent (JSON: {"message": "...", "session_id": "..."})
  GET  /sessions   — List active sessions
  POST /sessions   — Create a new session
  GET  /files      — List files in the workspace
  GET  /files/{path} — Download a file from the workspace
"""

import os
import sys
import json
import uuid
import asyncio
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent))

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize FastAPI
app = FastAPI(
    title="Open Manus",
    description="An autonomous general AI agent — an open-source replica of Manus.im",
    version="1.0.0"
)

# CORS middleware for web UI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Session storage
sessions: Dict[str, dict] = {}
sessions_lock = threading.Lock()

WORKSPACE_DIR = os.path.expanduser(os.environ.get("HERMES_WORKSPACE_DIR", "~/.hermes/workspace"))
os.makedirs(WORKSPACE_DIR, exist_ok=True)


# =============================================================================
# Pydantic Models
# =============================================================================

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    model: Optional[str] = None
    stream: Optional[bool] = False

class ChatResponse(BaseModel):
    session_id: str
    response: str
    timestamp: str
    model: str

class SessionCreate(BaseModel):
    name: Optional[str] = None
    model: Optional[str] = None


# =============================================================================
# Agent Management
# =============================================================================

def get_or_create_session(session_id: Optional[str] = None, model: Optional[str] = None) -> dict:
    """Get an existing session or create a new one."""
    with sessions_lock:
        if session_id and session_id in sessions:
            return sessions[session_id]

        new_id = session_id or str(uuid.uuid4())
        default_model = model or os.environ.get("LLM_MODEL", "anthropic/claude-opus-4.6")

        sessions[new_id] = {
            "id": new_id,
            "model": default_model,
            "history": [],
            "created_at": datetime.utcnow().isoformat(),
            "last_active": datetime.utcnow().isoformat(),
        }
        return sessions[new_id]


def run_agent_sync(message: str, session: dict) -> str:
    """Run the agent synchronously in a thread."""
    try:
        from run_agent import AIAgent

        # Read the Open Manus system prompt
        system_prompt_path = Path(__file__).parent / "open_manus_system_prompt.md"
        system_prompt = None
        if system_prompt_path.exists():
            with open(system_prompt_path) as f:
                content = f.read()
                # Replace the date placeholder
                system_prompt = content.replace("{{CURRENT_DATE}}", datetime.utcnow().strftime("%b %d, %Y"))

        agent = AIAgent(
            model=session["model"],
            max_iterations=90,
            quiet_mode=True,
            session_id=session["id"],
        )

        # Pass conversation history if available
        history = session.get("history", [])

        result = agent.run_conversation(
            user_message=message,
            system_message=system_prompt,
            conversation_history=history if history else None,
        )

        response_text = result.get("final_response", "I encountered an error processing your request.")

        # Update session history
        session["history"].append({"role": "user", "content": message})
        session["history"].append({"role": "assistant", "content": response_text})
        session["last_active"] = datetime.utcnow().isoformat()

        # Keep history to last 50 messages to avoid context overflow
        if len(session["history"]) > 50:
            session["history"] = session["history"][-50:]

        return response_text

    except Exception as e:
        logger.error(f"Agent error: {e}", exc_info=True)
        return f"I encountered an error: {str(e)}"


# =============================================================================
# API Endpoints
# =============================================================================

@app.get("/health")
async def health_check():
    """Health check endpoint for Railway."""
    return {
        "status": "healthy",
        "service": "Open Manus",
        "timestamp": datetime.utcnow().isoformat(),
        "active_sessions": len(sessions),
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, background_tasks: BackgroundTasks):
    """Send a message to the Open Manus agent."""
    session = get_or_create_session(request.session_id, request.model)

    # Run agent in a thread pool to avoid blocking the event loop
    loop = asyncio.get_event_loop()
    response_text = await loop.run_in_executor(
        None,
        run_agent_sync,
        request.message,
        session
    )

    return ChatResponse(
        session_id=session["id"],
        response=response_text,
        timestamp=datetime.utcnow().isoformat(),
        model=session["model"]
    )


@app.get("/sessions")
async def list_sessions():
    """List all active sessions."""
    with sessions_lock:
        return {
            "sessions": [
                {
                    "id": s["id"],
                    "model": s["model"],
                    "created_at": s["created_at"],
                    "last_active": s["last_active"],
                    "message_count": len(s["history"]),
                }
                for s in sessions.values()
            ]
        }


@app.post("/sessions")
async def create_session(request: SessionCreate):
    """Create a new session."""
    session = get_or_create_session(model=request.model)
    return {
        "session_id": session["id"],
        "model": session["model"],
        "created_at": session["created_at"],
    }


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session."""
    with sessions_lock:
        if session_id not in sessions:
            raise HTTPException(status_code=404, detail="Session not found")
        del sessions[session_id]
    return {"status": "deleted", "session_id": session_id}


@app.get("/files")
async def list_files():
    """List files in the workspace."""
    files = []
    workspace = Path(WORKSPACE_DIR)
    if workspace.exists():
        for f in sorted(workspace.iterdir()):
            if f.is_file():
                stat = f.stat()
                files.append({
                    "name": f.name,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "path": str(f),
                })
    return {"files": files, "workspace": WORKSPACE_DIR}


@app.get("/files/{filename}")
async def download_file(filename: str):
    """Download a file from the workspace."""
    file_path = Path(WORKSPACE_DIR) / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    # Security check: ensure the path is within workspace
    if not str(file_path.resolve()).startswith(str(Path(WORKSPACE_DIR).resolve())):
        raise HTTPException(status_code=403, detail="Access denied")
    return FileResponse(str(file_path), filename=filename)


# =============================================================================
# Web UI
# =============================================================================

WEB_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Open Manus — Autonomous AI Agent</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f0f1a; color: #e0e0ff; height: 100vh; display: flex; flex-direction: column; }
        header { background: #1a1a2e; padding: 16px 24px; border-bottom: 1px solid #2a2a4e; display: flex; align-items: center; gap: 12px; }
        header h1 { font-size: 1.4rem; font-weight: 700; color: #a78bfa; }
        header span { font-size: 0.85rem; color: #6b7280; }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; background: #10b981; animation: pulse 2s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
        #chat-container { flex: 1; overflow-y: auto; padding: 24px; display: flex; flex-direction: column; gap: 16px; }
        .message { max-width: 80%; padding: 12px 16px; border-radius: 12px; line-height: 1.6; white-space: pre-wrap; word-wrap: break-word; }
        .message.user { background: #3730a3; color: #e0e0ff; align-self: flex-end; border-radius: 12px 12px 4px 12px; }
        .message.assistant { background: #1e1e3f; color: #e0e0ff; align-self: flex-start; border-radius: 12px 12px 12px 4px; border: 1px solid #2a2a5e; }
        .message.system { background: #1a2e1a; color: #6ee7b7; align-self: center; font-size: 0.85rem; padding: 8px 16px; border-radius: 20px; }
        .message-meta { font-size: 0.75rem; color: #6b7280; margin-top: 4px; }
        #input-area { background: #1a1a2e; border-top: 1px solid #2a2a4e; padding: 16px 24px; display: flex; gap: 12px; align-items: flex-end; }
        #message-input { flex: 1; background: #0f0f1a; border: 1px solid #2a2a4e; border-radius: 8px; padding: 12px; color: #e0e0ff; font-size: 0.95rem; resize: none; min-height: 48px; max-height: 200px; outline: none; }
        #message-input:focus { border-color: #7c3aed; }
        #send-btn { background: #7c3aed; color: white; border: none; border-radius: 8px; padding: 12px 20px; cursor: pointer; font-size: 0.95rem; font-weight: 600; transition: background 0.2s; white-space: nowrap; }
        #send-btn:hover { background: #6d28d9; }
        #send-btn:disabled { background: #374151; cursor: not-allowed; }
        .thinking { display: flex; gap: 4px; align-items: center; padding: 12px 16px; }
        .thinking span { width: 8px; height: 8px; border-radius: 50%; background: #7c3aed; animation: bounce 1.4s infinite; }
        .thinking span:nth-child(2) { animation-delay: 0.2s; }
        .thinking span:nth-child(3) { animation-delay: 0.4s; }
        @keyframes bounce { 0%, 80%, 100% { transform: scale(0.8); opacity: 0.5; } 40% { transform: scale(1.2); opacity: 1; } }
        #session-info { font-size: 0.75rem; color: #6b7280; padding: 4px 0; }
        code { background: #0f0f1a; padding: 2px 6px; border-radius: 4px; font-family: monospace; font-size: 0.9em; }
        pre { background: #0f0f1a; padding: 12px; border-radius: 8px; overflow-x: auto; margin: 8px 0; border: 1px solid #2a2a4e; }
        pre code { background: none; padding: 0; }
    </style>
</head>
<body>
    <header>
        <div class="status-dot" id="status-dot"></div>
        <h1>Open Manus</h1>
        <span>Autonomous AI Agent</span>
        <span style="margin-left: auto; font-size: 0.8rem;" id="session-info">Initializing...</span>
    </header>
    <div id="chat-container">
        <div class="message system">Open Manus is ready. Ask me anything or give me a task to complete.</div>
    </div>
    <div id="input-area">
        <textarea id="message-input" placeholder="Ask Open Manus anything..." rows="1"></textarea>
        <button id="send-btn" onclick="sendMessage()">Send</button>
    </div>
    <script>
        let sessionId = null;
        let isThinking = false;

        async function initSession() {
            try {
                const res = await fetch('/sessions', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({}) });
                const data = await res.json();
                sessionId = data.session_id;
                document.getElementById('session-info').textContent = `Session: ${sessionId.slice(0,8)}... | Model: ${data.model}`;
            } catch (e) {
                document.getElementById('session-info').textContent = 'Connection error';
                document.getElementById('status-dot').style.background = '#ef4444';
            }
        }

        function addMessage(role, content) {
            const container = document.getElementById('chat-container');
            const div = document.createElement('div');
            div.className = `message ${role}`;
            // Simple markdown-like rendering
            let html = content
                .replace(/```([\\s\\S]*?)```/g, '<pre><code>$1</code></pre>')
                .replace(/`([^`]+)`/g, '<code>$1</code>')
                .replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>')
                .replace(/\\*([^*]+)\\*/g, '<em>$1</em>');
            div.innerHTML = html;
            container.appendChild(div);
            container.scrollTop = container.scrollHeight;
            return div;
        }

        function showThinking() {
            const container = document.getElementById('chat-container');
            const div = document.createElement('div');
            div.className = 'message assistant thinking';
            div.id = 'thinking-indicator';
            div.innerHTML = '<span></span><span></span><span></span>';
            container.appendChild(div);
            container.scrollTop = container.scrollHeight;
        }

        function hideThinking() {
            const el = document.getElementById('thinking-indicator');
            if (el) el.remove();
        }

        async function sendMessage() {
            if (isThinking) return;
            const input = document.getElementById('message-input');
            const message = input.value.trim();
            if (!message) return;

            input.value = '';
            input.style.height = 'auto';
            addMessage('user', message);
            showThinking();
            isThinking = true;
            document.getElementById('send-btn').disabled = true;

            try {
                const res = await fetch('/chat', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ message, session_id: sessionId })
                });
                const data = await res.json();
                hideThinking();
                addMessage('assistant', data.response);
            } catch (e) {
                hideThinking();
                addMessage('system', 'Error: Could not reach the agent. Please try again.');
            } finally {
                isThinking = false;
                document.getElementById('send-btn').disabled = false;
                input.focus();
            }
        }

        document.getElementById('message-input').addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });

        document.getElementById('message-input').addEventListener('input', function() {
            this.style.height = 'auto';
            this.style.height = Math.min(this.scrollHeight, 200) + 'px';
        });

        initSession();
    </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def web_ui():
    """Serve the Open Manus web UI."""
    return HTMLResponse(content=WEB_UI_HTML)


# =============================================================================
# Entry Point
# =============================================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get("HOST", "0.0.0.0")

    logger.info(f"Starting Open Manus server on {host}:{port}")
    logger.info(f"Model: {os.environ.get('LLM_MODEL', 'anthropic/claude-opus-4.6')}")
    logger.info(f"Workspace: {WORKSPACE_DIR}")

    uvicorn.run(
        "open_manus_server:app",
        host=host,
        port=port,
        log_level="info",
        reload=False,
        workers=1,  # Single worker to maintain session state
    )
