#!/usr/bin/env python3
"""
Google Drive Client — File management for Open Manus agents.

Uses Google Service Account with domain-wide delegation to access
Garrett's vowsok.com Google Workspace files.

Environment variables:
    GOOGLE_SERVICE_ACCOUNT_JSON  — Service account JSON string
    GOOGLE_DELEGATED_USER        — Email to impersonate (default: garrett@vowsok.com)

Usage:
    python3 gdrive_client.py list
    python3 gdrive_client.py search --query "wedding contract"
    python3 gdrive_client.py read-doc --id "DOCUMENT_ID"
    python3 gdrive_client.py upload --file /path/to/file.pdf --folder "Contracts"
    python3 gdrive_client.py share --id "FILE_ID" --email "client@example.com"
"""
import argparse
import json
import os
import sys
from typing import Dict, List, Optional

DELEGATED_USER = os.getenv("GOOGLE_DELEGATED_USER", "garrett@vowsok.com")
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]


def get_credentials():
    """Get Google credentials from service account."""
    if not SERVICE_ACCOUNT_JSON:
        raise ValueError(
            "GOOGLE_SERVICE_ACCOUNT_JSON not set. "
            "Run: python3 /app/skills/vault_client/vault_client.py export"
        )
    
    try:
        from google.oauth2 import service_account
    except ImportError:
        os.system("pip install google-auth google-auth-httplib2 google-api-python-client -q")
        from google.oauth2 import service_account
    
    # Parse JSON (could be a string or a file path)
    if SERVICE_ACCOUNT_JSON.startswith("{"):
        sa_info = json.loads(SERVICE_ACCOUNT_JSON)
    else:
        with open(SERVICE_ACCOUNT_JSON) as f:
            sa_info = json.load(f)
    
    credentials = service_account.Credentials.from_service_account_info(
        sa_info, scopes=SCOPES
    )
    return credentials.with_subject(DELEGATED_USER)


def get_drive_service():
    """Get Google Drive API service."""
    from googleapiclient.discovery import build
    creds = get_credentials()
    return build("drive", "v3", credentials=creds)


def get_docs_service():
    """Get Google Docs API service."""
    from googleapiclient.discovery import build
    creds = get_credentials()
    return build("docs", "v1", credentials=creds)


def get_sheets_service():
    """Get Google Sheets API service."""
    from googleapiclient.discovery import build
    creds = get_credentials()
    return build("sheets", "v4", credentials=creds)


def list_files(folder_id: Optional[str] = None, max_results: int = 20) -> List[Dict]:
    """List files in Drive."""
    service = get_drive_service()
    
    query = "trashed = false"
    if folder_id:
        query += f" and '{folder_id}' in parents"
    
    result = service.files().list(
        q=query,
        pageSize=max_results,
        fields="files(id, name, mimeType, modifiedTime, size)",
        orderBy="modifiedTime desc"
    ).execute()
    
    return result.get("files", [])


def search_files(query_text: str, max_results: int = 20) -> List[Dict]:
    """Search for files by name or content."""
    service = get_drive_service()
    
    query = f"fullText contains '{query_text}' and trashed = false"
    result = service.files().list(
        q=query,
        pageSize=max_results,
        fields="files(id, name, mimeType, modifiedTime)"
    ).execute()
    
    return result.get("files", [])


def read_document(doc_id: str) -> str:
    """Read a Google Doc and return its text content."""
    service = get_docs_service()
    doc = service.documents().get(documentId=doc_id).execute()
    
    content = []
    for element in doc.get("body", {}).get("content", []):
        if "paragraph" in element:
            for run in element["paragraph"].get("elements", []):
                if "textRun" in run:
                    content.append(run["textRun"]["content"])
    
    return "".join(content)


def create_document(title: str, content: str) -> Dict:
    """Create a new Google Doc."""
    service = get_docs_service()
    
    # Create empty doc
    doc = service.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]
    
    # Insert content
    service.documents().batchUpdate(
        documentId=doc_id,
        body={
            "requests": [{
                "insertText": {
                    "location": {"index": 1},
                    "text": content
                }
            }]
        }
    ).execute()
    
    return {
        "id": doc_id,
        "title": title,
        "url": f"https://docs.google.com/document/d/{doc_id}"
    }


def upload_file(file_path: str, folder_id: Optional[str] = None, mime_type: Optional[str] = None) -> Dict:
    """Upload a file to Google Drive."""
    from googleapiclient.http import MediaFileUpload
    service = get_drive_service()
    
    file_name = os.path.basename(file_path)
    
    file_metadata = {"name": file_name}
    if folder_id:
        file_metadata["parents"] = [folder_id]
    
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
    result = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, name, webViewLink"
    ).execute()
    
    return result


def share_file(file_id: str, email: str, role: str = "reader") -> bool:
    """Share a file with someone."""
    service = get_drive_service()
    
    service.permissions().create(
        fileId=file_id,
        body={
            "type": "user",
            "role": role,
            "emailAddress": email
        },
        sendNotificationEmail=True
    ).execute()
    
    return True


def read_sheet(sheet_id: str, range_name: str = "Sheet1") -> List[List]:
    """Read data from a Google Sheet."""
    service = get_sheets_service()
    
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=range_name
    ).execute()
    
    return result.get("values", [])


def append_to_sheet(sheet_id: str, range_name: str, values: List[List]) -> Dict:
    """Append rows to a Google Sheet."""
    service = get_sheets_service()
    
    result = service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=range_name,
        valueInputOption="USER_ENTERED",
        body={"values": values}
    ).execute()
    
    return result


def main():
    parser = argparse.ArgumentParser(description="Google Drive Client")
    subparsers = parser.add_subparsers(dest="command")

    p_list = subparsers.add_parser("list", help="List files")
    p_list.add_argument("--folder", help="Folder ID to list")
    p_list.add_argument("--count", type=int, default=20)

    p_search = subparsers.add_parser("search", help="Search files")
    p_search.add_argument("--query", required=True)

    p_read = subparsers.add_parser("read-doc", help="Read a Google Doc")
    p_read.add_argument("--id", required=True)

    p_create = subparsers.add_parser("create-doc", help="Create a Google Doc")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--content", required=True)

    p_upload = subparsers.add_parser("upload", help="Upload a file")
    p_upload.add_argument("--file", required=True)
    p_upload.add_argument("--folder", help="Folder ID")

    p_share = subparsers.add_parser("share", help="Share a file")
    p_share.add_argument("--id", required=True)
    p_share.add_argument("--email", required=True)
    p_share.add_argument("--role", default="reader", choices=["reader", "writer", "commenter"])

    p_sheet = subparsers.add_parser("read-sheet", help="Read a Google Sheet")
    p_sheet.add_argument("--id", required=True)
    p_sheet.add_argument("--range", default="Sheet1")

    args = parser.parse_args()

    try:
        if args.command == "list":
            files = list_files(args.folder, args.count)
            for f in files:
                print(f"{f['name']} ({f['mimeType']}) — {f['id']}")
        elif args.command == "search":
            files = search_files(args.query)
            for f in files:
                print(f"{f['name']} — {f['id']}")
        elif args.command == "read-doc":
            content = read_document(args.id)
            print(content)
        elif args.command == "create-doc":
            result = create_document(args.title, args.content)
            print(f"Created: {result['url']}")
        elif args.command == "upload":
            result = upload_file(args.file, args.folder)
            print(f"Uploaded: {result.get('webViewLink', result['id'])}")
        elif args.command == "share":
            share_file(args.id, args.email, args.role)
            print(f"Shared {args.id} with {args.email} as {args.role}")
        elif args.command == "read-sheet":
            data = read_sheet(args.id, args.range)
            print(json.dumps(data, indent=2))
        else:
            parser.print_help()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
