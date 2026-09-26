"""Isolated CRM UI verification harness; never connects to the fleet.

Run only for development: python scripts/crm_preview.py
All data is synthetic and disappears when this process stops. Production uses
the normal authenticated dashboard and shared Redis, never this harness.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Drop inherited credentials before importing the application. Do not read or
# print their values. Avoid loading the operator's normal config/home.
for key in list(os.environ):
    if key not in {"PATH", "LANG", "TZ", "PYTHONPATH", "LD_LIBRARY_PATH"}:
        os.environ.pop(key, None)
home = tempfile.TemporaryDirectory(prefix="crm-preview-")
os.environ["HOME"] = home.name
os.environ["HERMES_HOME"] = home.name

import fakeredis
import uvicorn
from fastapi.responses import JSONResponse
from crm import api as crm_api
from crm.service import CRMService
from hermes_cli import web_server

store = fakeredis.FakeRedis(decode_responses=True)
store.hset("crm:preview:metadata", "mode", "synthetic-only")
store.set("dispatch:roster:preview-agent", json.dumps({
    "agent": "preview-agent", "role": "Synthetic preview recipient",
}))
service = CRMService(store)
crm_api.service = lambda: service
web_server._warm_gateway_module = lambda: None
web_server._DASHBOARD_EMBEDDED_CHAT_ENABLED = False
web_server.app.state.auth_required = False
# The normal dashboard's WebSocket endpoints include terminal access. This
# harness exists only to exercise CRM and must never launch a shell.
from starlette.routing import WebSocketRoute
web_server.app.router.routes[:] = [
    route for route in web_server.app.router.routes
    if not isinstance(route, WebSocketRoute)
]


@web_server.app.middleware("http")
async def preview_boundary(request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/crm/"):
        fixtures = {
            "/api/config": {},
            "/api/dashboard/plugins": [],
            "/api/dashboard/themes": {"themes": []},
            "/api/profiles": {"profiles": []},
            "/api/status": {"version": "CRM isolated preview", "gateway_running": False},
        }
        if request.method == "GET" and path in fixtures:
            return JSONResponse(fixtures[path])
        return JSONResponse({"error": "Unavailable in isolated CRM preview"}, status_code=404)
    return await call_next(request)


web_server.mount_spa(web_server.app)
if __name__ == "__main__":
    uvicorn.run(web_server.app, host="0.0.0.0", port=5000, access_log=False)