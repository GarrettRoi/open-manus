#!/usr/bin/env python3
"""
Safeguards module for the Agent Email System.
Provides: recipient allowlisting, rate limiting, keyword filtering, and approval queue.
"""

import re
import json
import time
import logging
import sqlite3
from typing import List, Dict, Optional, Set, Tuple, Any
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from pathlib import Path
from functools import wraps

logger = logging.getLogger(__name__)


@dataclass
class SafeguardResult:
    """Result of a safeguard check."""
    allowed: bool
    reason: str
    action_required: Optional[str] = None
    details: Optional[Dict] = None


class RecipientAllowlist:
    """Manages allowed email addresses and domains."""
    
    def __init__(self, allowlist_path: Optional[str] = None):
        self.allowlist_path = allowlist_path or "/app/skills/agent-email-system/data/allowlist.txt"
        self._emails: Set[str] = set()
        self._domains: Set[str] = set()
        self._wildcards: List[str] = []
        self._load_allowlist()
    
    def _load_allowlist(self):
        """Load allowlist from file."""
        path = Path(self.allowlist_path)
        if not path.exists():
            logger.warning(f"Allowlist file not found: {self.allowlist_path}")
            return
        
        try:
            with open(path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    
                    line = line.lower()
                    if line.startswith('*@'):
                        self._domains.add(line[2:])
                    elif line.startswith('*.'):
                        self._wildcards.append(line[2:])
                    elif '@' in line:
                        self._emails.add(line)
                    else:
                        self._domains.add(line)
            
            logger.info(f"Loaded allowlist: {len(self._emails)} emails, {len(self._domains)} domains")
            
        except Exception as e:
            logger.error(f"Error loading allowlist: {e}")
    
    def is_allowed(self, email: str) -> bool:
        """Check if an email address is allowed."""
        email = email.lower().strip()
        
        if email in self._emails:
            return True
        
        if '@' not in email:
            return False
        
        domain = email.split('@')[1]
        
        if domain in self._domains:
            return True
        
        for wildcard in self._wildcards:
            if domain.endswith('.' + wildcard) or domain == wildcard:
                return True
        
        return False
    
    def add_email(self, email: str, save: bool = True):
        """Add an email to the allowlist."""
        self._emails.add(email.lower().strip())
        if save:
            self._save_allowlist()
    
    def add_domain(self, domain: str, save: bool = True):
        """Add a domain to the allowlist."""
        self._domains.add(domain.lower().strip())
        if save:
            self._save_allowlist()
    
    def _save_allowlist(self):
        """Save allowlist to file."""
        path = Path(self.allowlist_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(path, 'w') as f:
            f.write("# Agent Email System - Recipient Allowlist\n")
            f.write("# One entry per line\n")
            f.write("# Use *@domain.com for domain wildcard\n\n")
            
            for email in sorted(self._emails):
                f.write(f"{email}\n")
            for domain in sorted(self._domains):
                f.write(f"*@{domain}\n")


class RateLimiter:
    """Rate limiting with per-sender quotas."""
    
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or "/app/skills/agent-email-system/data/rate_limits.db"
        self._init_db()
    
    def _init_db(self):
        """Initialize SQLite database for rate limiting."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
CREATE TABLE IF NOT EXISTS email_counts (
    sender TEXT,
    date TEXT,
    count INTEGER DEFAULT 0,
    PRIMARY KEY (sender, date)
)
        """)
        conn.commit()
        conn.close()
    
    def check_rate_limit(self, sender: str, max_per_hour: int = 10, max_per_day: int = 50):
        """Check if sender is within rate limits."""
        now = datetime.utcnow()
        today = now.strftime('%Y-%m-%d')
        hour = now.strftime('%Y-%m-%d-%H')
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT SUM(count) FROM email_counts WHERE sender = ? AND date LIKE ?",
            (sender, f"{today}%")
        )
        daily_count = cursor.fetchone()[0] or 0
        
        cursor.execute(
            "SELECT count FROM email_counts WHERE sender = ? AND date = ?",
            (sender, hour)
        )
        hourly_count = cursor.fetchone()
        hourly_count = hourly_count[0] if hourly_count else 0
        
        conn.close()
        
        stats = {
            'hourly_count': hourly_count,
            'daily_count': daily_count,
            'hourly_limit': max_per_hour,
            'daily_limit': max_per_day,
            'remaining_hourly': max(0, max_per_hour - hourly_count),
            'remaining_daily': max(0, max_per_day - daily_count)
        }
        
        if hourly_count >= max_per_hour:
            return False, {**stats, 'reason': 'Hourly rate limit exceeded'}
        
        if daily_count >= max_per_day:
            return False, {**stats, 'reason': 'Daily rate limit exceeded'}
        
        return True, stats
    
    def record_send(self, sender: str):
        """Record that an email was sent."""
        now = datetime.utcnow()
        hour = now.strftime('%Y-%m-%d-%H')
        
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
INSERT INTO email_counts (sender, date, count) VALUES (?, ?, 1)
ON CONFLICT(sender, date) DO UPDATE SET count = count + 1
        """, (sender, hour))
        conn.commit()
        conn.close()


class KeywordFilter:
    """Filter emails based on prohibited keywords."""
    
    DEFAULT_BLOCKED = ['password', 'credit card', 'ssn', 'bank account', 'wire transfer']
    
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or "/app/skills/agent-email-system/data/keywords.json"
        self.blocked_keywords: List[str] = []
        self._load_config()
    
    def _load_config(self):
        path = Path(self.config_path)
        if path.exists():
            try:
                with open(path, 'r') as f:
                    config = json.load(f)
                    self.blocked_keywords = config.get('blocked', [])
            except Exception as e:
                logger.error(f"Error loading keyword config: {e}")
        
        if not self.blocked_keywords:
            self.blocked_keywords = self.DEFAULT_BLOCKED.copy()
    
    def check_content(self, subject: str, body: str):
        """Check if content contains blocked keywords."""
        text = f"{subject} {body}".lower()
        matched = []
        
        for keyword in self.blocked_keywords:
            if keyword.lower() in text:
                matched.append(keyword)
        
        return len(matched) == 0, matched


class ApprovalQueue:
    """Manual approval queue for sensitive emails."""
    
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or "/app/skills/agent-email-system/data/approval_queue.db"
        self._init_db()
    
    def _init_db(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
CREATE TABLE IF NOT EXISTS pending_approvals (
    id TEXT PRIMARY KEY,
    to_email TEXT,
    subject TEXT,
    body TEXT,
    sender TEXT,
    timestamp TEXT,
    status TEXT DEFAULT 'pending',
    approved_by TEXT,
    approved_at TEXT
)
        """)
        conn.commit()
        conn.close()
    
    def add_to_queue(self, email_data: Dict) -> str:
        """Add an email to the approval queue."""
        approval_id = f"APV-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
        
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
INSERT INTO pending_approvals (id, to_email, subject, body, sender, timestamp)
VALUES (?, ?, ?, ?, ?, ?)
        """, (
            approval_id,
            email_data.get('to'),
            email_data.get('subject'),
            email_data.get('body'),
            email_data.get('sender', 'system'),
            datetime.utcnow().isoformat()
        ))
        conn.commit()
        conn.close()
        
        logger.info(f"Added email to approval queue: {approval_id}")
        return approval_id
    
    def get_pending(self) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pending_approvals WHERE status = 'pending' ORDER BY timestamp")
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        conn.close()
        return [dict(zip(columns, row)) for row in rows]
    
    def approve(self, approval_id: str, approved_by: str = "admin"):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE pending_approvals SET status = 'approved', approved_by = ?, approved_at = ? WHERE id = ?",
            (approved_by, datetime.utcnow().isoformat(), approval_id)
        )
        conn.commit()
        conn.close()
        logger.info(f"Approved email: {approval_id}")
    
    def reject(self, approval_id: str, rejected_by: str = "admin"):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE pending_approvals SET status = 'rejected', approved_by = ?, approved_at = ? WHERE id = ?",
            (rejected_by, datetime.utcnow().isoformat(), approval_id)
        )
        conn.commit()
        conn.close()


class Safeguards:
    """Main safeguards coordinator."""
    
    def __init__(self):
        self.allowlist = RecipientAllowlist()
        self.rate_limiter = RateLimiter()
        self.keyword_filter = KeywordFilter()
        self.approval_queue = ApprovalQueue()
    
    def check_email(self, to: str, subject: str, body: str, sender: str = "system", require_approval: bool = False):
        """Run all safeguard checks on an email."""
        
        # 1. Check allowlist
        if not self.allowlist.is_allowed(to):
            return SafeguardResult(
                allowed=False,
                reason=f"Recipient '{to}' is not on the allowlist",
                details={'check': 'allowlist'}
            )
        
        # 2. Check rate limits
        allowed, stats = self.rate_limiter.check_rate_limit(sender)
        if not allowed:
            return SafeguardResult(
                allowed=False,
                reason=stats['reason'],
                details={'check': 'rate_limit', 'stats': stats}
            )
        
        # 3. Check keyword filter
        allowed, keywords = self.keyword_filter.check_content(subject, body)
        if not allowed:
            return SafeguardResult(
                allowed=False,
                reason=f"Email contains blocked keywords: {', '.join(keywords)}",
                details={'check': 'keyword_filter', 'keywords': keywords}
            )
        
        # 4. Approval queue check
        if require_approval:
            approval_id = self.approval_queue.add_to_queue({
                'to': to,
                'subject': subject,
                'body': body,
                'sender': sender
            })
            return SafeguardResult(
                allowed=False,
                reason="Email queued for manual approval",
                action_required="approval",
                details={'approval_id': approval_id, 'check': 'approval_queue'}
            )
        
        # Record successful rate limit check
        self.rate_limiter.record_send(sender)
        
        return SafeguardResult(
            allowed=True,
            reason="All safeguards passed",
            details={'checks': ['allowlist', 'rate_limit', 'keyword_filter']}
        )
