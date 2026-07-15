#!/usr/bin/env python3
"""
Gmail API Client using Google Service Account authentication.
Supports sending emails, reading inbox, and managing labels.
"""

import os
import json
import base64
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Dict, Optional, Any
from datetime import datetime

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class GmailClient:
    """Gmail API client wrapper for Service Account authentication."""
    
    SCOPES = [
        'https://www.googleapis.com/auth/gmail.send',
        'https://www.googleapis.com/auth/gmail.readonly',
        'https://www.googleapis.com/auth/gmail.modify',
        'https://www.googleapis.com/auth/gmail.labels'
    ]
    
    def __init__(self, service_account_json: Optional[str] = None, delegated_user: Optional[str] = None):
        """
        Initialize Gmail client.
        
        Args:
            service_account_json: JSON string or path to service account key file
            delegated_user: Email address to impersonate (required for service accounts)
        """
        self.service = None
        self.delegated_user = delegated_user
        self._authenticate(service_account_json)
    
    def _authenticate(self, service_account_json: Optional[str] = None):
        """Authenticate with Google using Service Account."""
        try:
            # Get credentials from environment or parameter
            creds_json = service_account_json or os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
            
            if not creds_json:
                raise ValueError("No service account credentials provided. Set GOOGLE_SERVICE_ACCOUNT_JSON env var.")
            
            # Parse credentials
            if creds_json.startswith('{'):
                # JSON string
                creds_info = json.loads(creds_json)
            else:
                # File path
                with open(creds_json, 'r') as f:
                    creds_info = json.load(f)
            
            # Create credentials with delegation
            credentials = service_account.Credentials.from_service_account_info(
                creds_info,
                scopes=self.SCOPES
            )
            
            # Delegate to user if specified
            if self.delegated_user:
                credentials = credentials.with_subject(self.delegated_user)
            elif 'client_email' in creds_info:
                # Try to use the service account email as sender
                self.delegated_user = creds_info.get('client_email')
            
            # Build Gmail service
            self.service = build('gmail', 'v1', credentials=credentials)
            logger.info(f"Successfully authenticated Gmail API for {self.delegated_user}")
            
        except Exception as e:
            logger.error(f"Failed to authenticate Gmail API: {e}")
            raise
    
    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        html: bool = False,
        sender: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Send an email via Gmail API.
        
        Args:
            to: Recipient email address
            subject: Email subject
            body: Email body content
            cc: CC recipients
            bcc: BCC recipients
            html: Whether body is HTML
            sender: Override sender email (must be authorized in Google Workspace)
        
        Returns:
            Dict with message_id, thread_id, and status
        """
        try:
            # Create message
            msg = MIMEMultipart('alternative') if html else MIMEText(body, 'html' if html else 'plain')
            
            if html and not isinstance(msg, MIMEMultipart):
                # Recreate as multipart for HTML
                msg = MIMEMultipart('alternative')
                msg.attach(MIMEText(body, 'html'))
            
            msg['To'] = to
            msg['Subject'] = subject
            
            # Set sender
            from_email = sender or self.delegated_user
            if not from_email:
                raise ValueError("No sender email specified")
            msg['From'] = from_email
            
            # Add CC/BCC
            if cc:
                msg['Cc'] = ', '.join(cc)
            if bcc:
                msg['Bcc'] = ', '.join(bcc)
            
            # Encode and send
            raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
            body_params = {'raw': raw_message}
            
            result = self.service.users().messages().send(userId='me', body=body_params).execute()
            
            logger.info(f"Email sent successfully to {to}, message_id: {result['id']}")
            return {
                'success': True,
                'message_id': result['id'],
                'thread_id': result.get('threadId'),
                'timestamp': datetime.utcnow().isoformat()
            }
            
        except HttpError as e:
            logger.error(f"Gmail API error sending email: {e}")
            return {
                'success': False,
                'error': str(e),
                'error_code': e.resp.status if hasattr(e, 'resp') else None
            }
        except Exception as e:
            logger.error(f"Unexpected error sending email: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def list_messages(
        self,
        query: Optional[str] = None,
        max_results: int = 10,
        label_ids: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        List emails from inbox.
        
        Args:
            query: Gmail search query (e.g., "from:example@domain.com")
            max_results: Maximum number of messages to return
            label_ids: Filter by label IDs
        
        Returns:
            List of message summaries
        """
        try:
            params = {
                'userId': 'me',
                'maxResults': max_results
            }
            if query:
                params['q'] = query
            if label_ids:
                params['labelIds'] = label_ids
            
            response = self.service.users().messages().list(**params).execute()
            messages = response.get('messages', [])
            
            # Get full message details
            detailed_messages = []
            for msg in messages:
                detail = self.get_message(msg['id'])
                if detail:
                    detailed_messages.append(detail)
            
            return detailed_messages
            
        except Exception as e:
            logger.error(f"Error listing messages: {e}")
            return []
    
    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        """Get full details of a specific message."""
        try:
            result = self.service.users().messages().get(
                userId='me',
                id=message_id,
                format='full'
            ).execute()
            
            # Parse headers
            headers = result.get('payload', {}).get('headers', [])
            header_map = {h['name'].lower(): h['value'] for h in headers}
            
            # Extract body
            body = self._extract_body(result.get('payload', {}))
            
            return {
                'id': result['id'],
                'thread_id': result.get('threadId'),
                'subject': header_map.get('subject', ''),
                'from': header_map.get('from', ''),
                'to': header_map.get('to', ''),
                'date': header_map.get('date', ''),
                'body': body,
                'labels': result.get('labelIds', []),
                'snippet': result.get('snippet', '')
            }
            
        except Exception as e:
            logger.error(f"Error getting message {message_id}: {e}")
            return None
    
    def _extract_body(self, payload: Dict) -> str:
        """Extract text body from message payload."""
        if 'parts' in payload:
            for part in payload['parts']:
                if part.get('mimeType') == 'text/plain':
                    data = part.get('body', {}).get('data', '')
                    if data:
                        return base64.urlsafe_b64decode(data).decode('utf-8')
                elif part.get('mimeType') == 'text/html':
                    data = part.get('body', {}).get('data', '')
                    if data:
                        return base64.urlsafe_b64decode(data).decode('utf-8')
        else:
            data = payload.get('body', {}).get('data', '')
            if data:
                return base64.urlsafe_b64decode(data).decode('utf-8')
        return ""
    
    def trash_message(self, message_id: str) -> bool:
        """Move a message to trash."""
        try:
            self.service.users().messages().trash(userId='me', id=message_id).execute()
            logger.info(f"Message {message_id} moved to trash")
            return True
        except Exception as e:
            logger.error(f"Error trashing message {message_id}: {e}")
            return False
    
    def list_labels(self) -> List[Dict[str, str]]:
        """List all Gmail labels."""
        try:
            response = self.service.users().labels().list(userId='me').execute()
            return response.get('labels', [])
        except Exception as e:
            logger.error(f"Error listing labels: {e}")
            return []


# CLI usage
if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Gmail API Client')
    parser.add_argument('--send', action='store_true', help='Send an email')
    parser.add_argument('--to', type=str, help='Recipient email')
    parser.add_argument('--subject', type=str, help='Email subject')
    parser.add_argument('--body', type=str, help='Email body')
    parser.add_argument('--list', action='store_true', help='List recent emails')
    parser.add_argument('--query', type=str, help='Search query')
    
    args = parser.parse_args()
    
    client = GmailClient()
    
    if args.send:
        if not all([args.to, args.subject, args.body]):
            print("Error: --to, --subject, and --body are required for sending")
            exit(1)
        result = client.send_email(args.to, args.subject, args.body)
        print(json.dumps(result, indent=2))
    elif args.list:
        messages = client.list_messages(query=args.query)
        print(json.dumps(messages, indent=2))
    else:
        parser.print_help()
