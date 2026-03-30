#!/usr/bin/env python3
"""
Agent Email System - REST API Service
FastAPI-based service for secure agent-to-agent email communication.
"""

import os
import sys
import json
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime
from pathlib import Path

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, Field
from contextlib import asynccontextmanager

try:
    from gmail_client import GmailClient
    from safeguards import Safeguards, SafeguardResult
except ImportError:
    # Allow importing from skill directory
    sys.path.insert(0, "/app/skills/agent-email-system/scripts")
    from gmail_client import GmailClient
    from safeguards import Safeguards

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load API key from environment (MUST be set before starting)
_API_KEY = os.environ.get('AGENT_EMAIL_API_KEY')
if not _API_KEY:
    raise RuntimeError(
        "AGENT_EMAIL_API_KEY environment variable is required. "
        "Generate a secure random key with: openssl rand -hex 32"
    )

# Load Google Service Account credentials
_SERVICE_ACCOUNT_JSON = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')

# Initialize components
safeguards = Safeguards()
gmail_client = None
if _SERVICE_ACCOUNT_JSON:
    try:
        gmail_client = GmailClient(_SERVICE_ACCOUNT_JSON)
        logger.info("Gmail client initialized")
    except Exception as e:
        logger.error(f"Failed to initialize Gmail client: {e}")
else:
    logger.warning("GOOGLE_SERVICE_ACCOUNT_JSON not set - email sending unavailable")

security = HTTPBearer(auto_error=False)


class EmailRequest(BaseModel):
    to: EmailStr = Field(..., description="Recipient email address")
    subject: str = Field(..., min_length=1, max_length=998)
    body: str = Field(..., min_length=1)
    cc: Optional[List[EmailStr]] = None
    bcc: Optional[List[EmailStr]] = None
    html: bool = False
    require_approval: bool = False


class EmailResponse(BaseModel):
    success: bool
    message_id: Optional[str] = None
    thread_id: Optional[str] = None
    timestamp: str
    safeguard_result: Optional[Dict] = None
    error: Optional[str] = None


async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="Authorization header missing")
    if credentials.credentials != _API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return credentials.credentials


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Agent Email System API v1.0.0")
    yield
    logger.info("Shutting down Agent Email System API")


app = FastAPI(
    title="Agent Email System API",
    description="Secure email service with comprehensive safeguards",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "gmail_connected": gmail_client is not None,
        "timestamp": datetime.utcnow().isoformat(),
        "version": "1.0.0"
    }


@app.post("/api/v1/email/send", response_model=EmailResponse)
async def send_email(
    request: EmailRequest,
    token: str = Depends(verify_token),
    x_sender: Optional[str] = Header(default="api-client")
):
    if not gmail_client:
        raise HTTPException(status_code=503, detail="Gmail service not available")
    
    # Run safeguard checks
    safeguard_result = safeguards.check_email(
        to=request.to,
        subject=request.subject,
        body=request.body,
        sender=x_sender,
        require_approval=request.require_approval
    )
    
    if not safeguard_result.allowed:
        logger.warning(f"Email blocked by safeguards: {safeguard_result.reason}")
        
        if safeguard_result.action_required == "approval":
            return EmailResponse(
                success=False,
                timestamp=datetime.utcnow().isoformat(),
                safeguard_result={
                    "allowed": False,
                    "reason": safeguard_result.reason,
                    "approval_id": safeguard_result.details.get("approval_id")
                },
                error=f"Email queued for approval (ID: {safeguard_result.details.get('approval_id')})"
            )
        
        return EmailResponse(
            success=False,
            timestamp=datetime.utcnow().isoformat(),
            safeguard_result={
                "allowed": False,
                "reason": safeguard_result.reason,
                "details": safeguard_result.details
            },
            error=safeguard_result.reason
        )
    
    # Send email
    result = gmail_client.send_email(
        to=request.to,
        subject=request.subject,
        body=request.body,
        cc=request.cc,
        bcc=request.bcc,
        html=request.html
    )
    
    if result['success']:
        return EmailResponse(
            success=True,
            message_id=result["message_id"],
            thread_id=result.get("thread_id"),
            timestamp=result["timestamp"],
            safeguard_result={"allowed": True, "reason": safeguard_result.reason}
        )
    else:
        raise HTTPException(status_code=500, detail=result.get("error", "Send failed"))


@app.get("/api/v1/approvals/pending")
async def get_pending_approvals(token: str = Depends(verify_token)):
    pending = safeguards.approval_queue.get_pending()
    return {"approvals": pending, "count": len(pending)}


@app.post("/api/v1/approvals/{approval_id}/approve")
async def approve_email(approval_id: str, token: str = Depends(verify_token), approved_by: Optional[str] = Header(default="admin")):
    pending = safeguards.approval_queue.get_pending()
    email_data = None
    for p in pending:
        if p["id"] == approval_id:
            email_data = p
            break
    
    if not email_data:
        raise HTTPException(status_code=404, detail="Approval not found")
    
    if not gmail_client:
        raise HTTPException(status_code=503, detail="Gmail unavailable")
    
    result = gmail_client.send_email(
        to=email_data["to_email"],
        subject=email_data["subject"],
        body=email_data["body"]
    )
    
    if result["success"]:
        safeguards.approval_queue.approve(approval_id, approved_by)
        return {"success": True, "message_id": result["message_id"], "approval_id": approval_id}
    else:
        raise HTTPException(status_code=500, detail=result.get("error"))


@app.post("/api/v1/approvals/{approval_id}/reject")
async def reject_email(approval_id: str, token: str = Depends(verify_token), rejected_by: Optional[str] = Header(default="admin")):
    safeguards.approval_queue.reject(approval_id, rejected_by)
    return {"success": True, "approval_id": approval_id}


@app.get("/api/v1/stats/{sender}")
async def get_sender_stats(sender: str, token: str = Depends(verify_token)):
    allowed, stats = safeguards.rate_limiter.check_rate_limit(sender)
    return stats


@app.post("/api/v1/allowlist/emails")
async def add_email_to_allowlist(email: str, token: str = Depends(verify_token)):
    safeguards.allowlist.add_email(email)
    return {"success": True, "email": email}


@app.post("/api/v1/allowlist/domains")
async def add_domain_to_allowlist(domain: str, token: str = Depends(verify_token)):
    safeguards.allowlist.add_domain(domain)
    return {"success": True, "domain": domain}


@app.get("/api/v1/allowlist/check")
async def check_allowlist(email: str, token: str = Depends(verify_token)):
    allowed = safeguards.allowlist.is_allowed(email)
    return {"email": email, "allowed": allowed}


@app.get("/api/v1/inbox/list")
async def list_inbox(query: Optional[str] = None, max_results: int = 10, token: str = Depends(verify_token)):
    if not gmail_client:
        raise HTTPException(status_code=503, detail="Gmail not available")
    messages = gmail_client.list_messages(query=query, max_results=max_results)
    return {"messages": messages, "count": len(messages)}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("EMAIL_SERVICE_PORT", "8080"))
    host = os.environ.get("EMAIL_SERVICE_HOST", "0.0.0.0")
    logger.info(f"Starting Agent Email System on {host}:{port}")
    uvicorn.run(app, host=host, port=port)
