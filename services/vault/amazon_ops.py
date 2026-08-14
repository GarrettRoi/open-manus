"""Amazon Associates operations for the Open Manus Key Vault.

Two capabilities:

  1. build_link — build a tagged affiliate link from an ASIN or an Amazon
     product URL. Works with just the stored associate tag; no PA-API keys
     needed. Never touches the network.

  2. PA-API 5 proxying (search_items / get_items / get_variations /
     get_browse_nodes) — the vault signs requests with AWS Signature V4
     server-side using the stored PA-API access key/secret. Agents never see
     the keys; the signature happens entirely inside the vault.

Marketplace → PA-API host/region mapping follows the official PA-API 5 docs.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit, urlencode, parse_qsl, urlunsplit

import httpx


class AmazonOpsError(Exception):
    pass


# Marketplace domain -> (PA-API host, AWS region)
MARKETPLACES: Dict[str, Tuple[str, str]] = {
    "www.amazon.com":    ("webservices.amazon.com",    "us-east-1"),
    "www.amazon.ca":     ("webservices.amazon.ca",     "us-east-1"),
    "www.amazon.com.mx": ("webservices.amazon.com.mx", "us-east-1"),
    "www.amazon.com.br": ("webservices.amazon.com.br", "us-east-1"),
    "www.amazon.co.uk":  ("webservices.amazon.co.uk",  "eu-west-1"),
    "www.amazon.de":     ("webservices.amazon.de",     "eu-west-1"),
    "www.amazon.fr":     ("webservices.amazon.fr",     "eu-west-1"),
    "www.amazon.it":     ("webservices.amazon.it",     "eu-west-1"),
    "www.amazon.es":     ("webservices.amazon.es",     "eu-west-1"),
    "www.amazon.nl":     ("webservices.amazon.nl",     "eu-west-1"),
    "www.amazon.in":     ("webservices.amazon.in",     "eu-west-1"),
    "www.amazon.co.jp":  ("webservices.amazon.co.jp",  "us-west-2"),
    "www.amazon.com.au": ("webservices.amazon.com.au", "us-west-2"),
    "www.amazon.sg":     ("webservices.amazon.sg",     "us-west-2"),
}

_PAAPI_OPERATIONS = {
    "search_items": "SearchItems",
    "get_items": "GetItems",
    "get_variations": "GetVariations",
    "get_browse_nodes": "GetBrowseNodes",
}

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$", re.IGNORECASE)
_URL_ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", re.IGNORECASE)


def normalize_marketplace(marketplace: str) -> str:
    m = (marketplace or "").strip().lower() or "www.amazon.com"
    if not m.startswith("www.") and m.startswith("amazon."):
        m = "www." + m
    if m not in MARKETPLACES:
        raise AmazonOpsError(
            f"Unknown Amazon marketplace '{marketplace}'. Supported: "
            + ", ".join(sorted(MARKETPLACES)))
    return m


def build_tagged_link(associate_tag: str, args: Dict[str, Any],
                      marketplace: str = "www.amazon.com") -> Dict[str, Any]:
    """Build an affiliate link from an ASIN or an existing Amazon URL."""
    if not associate_tag:
        raise AmazonOpsError("No associate tag stored for this connection")
    asin = str(args.get("asin") or "").strip()
    url = str(args.get("url") or "").strip()
    mkt = normalize_marketplace(str(args.get("marketplace") or marketplace))

    if asin:
        if not _ASIN_RE.match(asin):
            raise AmazonOpsError(f"'{asin}' does not look like a valid ASIN")
        link = f"https://{mkt}/dp/{asin.upper()}?tag={associate_tag}"
        return {"link": link, "asin": asin.upper(), "tag": associate_tag,
                "marketplace": mkt}

    if url:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if not (host == "amzn.to" or host.endswith("amazon.com")
                or ".amazon." in f".{host}" or host.startswith("amazon.")):
            raise AmazonOpsError(
                f"'{host}' is not an Amazon host — only Amazon product URLs "
                "can be tagged")
        m = _URL_ASIN_RE.search(parts.path)
        if m:
            # Canonicalize to a clean /dp/ASIN link (drops tracking params).
            link = f"https://{host}/dp/{m.group(1).upper()}?tag={associate_tag}"
            return {"link": link, "asin": m.group(1).upper(),
                    "tag": associate_tag, "marketplace": host}
        # No ASIN found (search/category page): replace/append the tag param.
        q = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() != "tag"]
        q.append(("tag", associate_tag))
        link = urlunsplit((parts.scheme or "https", parts.netloc, parts.path,
                           urlencode(q), ""))
        return {"link": link, "tag": associate_tag, "marketplace": host}

    raise AmazonOpsError("build_link needs an 'asin' or an Amazon 'url'")


# ---------------------------------------------------------------------------
# PA-API 5 — AWS Signature Version 4 (signed entirely inside the vault)
# ---------------------------------------------------------------------------

def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(access_key: str, secret_key: str, host: str, region: str,
                  target: str, payload: str,
                  now: Optional[datetime.datetime] = None) -> Dict[str, str]:
    """Compute AWS SigV4 headers for a PA-API 5 POST request."""
    service = "ProductAdvertisingAPI"
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    canonical_uri = "/paapi5/" + target.rsplit(".", 1)[-1].lower()
    canonical_headers = (
        f"content-encoding:amz-1.0\n"
        f"host:{host}\n"
        f"x-amz-date:{amz_date}\n"
        f"x-amz-target:{target}\n"
    )
    signed_headers = "content-encoding;host;x-amz-date;x-amz-target"
    payload_hash = hashlib.sha256(payload.encode()).hexdigest()
    canonical_request = "\n".join([
        "POST", canonical_uri, "", canonical_headers, signed_headers,
        payload_hash,
    ])

    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    k = _hmac_sha256(f"AWS4{secret_key}".encode(), date_stamp)
    k = _hmac_sha256(k, region)
    k = _hmac_sha256(k, service)
    k = _hmac_sha256(k, "aws4_request")
    signature = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()

    return {
        "Content-Encoding": "amz-1.0",
        "Content-Type": "application/json; charset=utf-8",
        "Host": host,
        "X-Amz-Date": amz_date,
        "X-Amz-Target": target,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


async def paapi_call(secrets: Dict[str, Any], operation: str,
                     args: Dict[str, Any],
                     marketplace: str = "www.amazon.com",
                     timeout: float = 30.0) -> Dict[str, Any]:
    """Run a signed PA-API 5 call. Returns {status, json}."""
    op = _PAAPI_OPERATIONS.get(operation)
    if not op:
        raise AmazonOpsError(
            f"Unknown PA-API operation '{operation}'. Supported: "
            + ", ".join(sorted(_PAAPI_OPERATIONS)) + ", build_link")
    access_key = secrets.get("paapi_access_key") or ""
    secret_key = secrets.get("paapi_secret_key") or ""
    tag = secrets.get("associate_tag") or ""
    if not access_key or not secret_key:
        raise AmazonOpsError(
            "No PA-API credentials stored — this connection can only build "
            "tagged links (operation='build_link'). Add a Product Advertising "
            "API access key and secret in the vault dashboard to enable "
            "product search.")
    mkt = normalize_marketplace(
        str(args.get("marketplace") or marketplace))
    host, region = MARKETPLACES[mkt]

    body: Dict[str, Any] = {k: v for k, v in args.items()
                            if k not in ("marketplace",)}
    # Server-side injected identity — agent-supplied values never win.
    body["PartnerTag"] = tag
    body["PartnerType"] = "Associates"
    body["Marketplace"] = mkt
    # Friendly aliases for the common cases.
    if op == "SearchItems" and "Keywords" not in body and body.get("keywords"):
        body["Keywords"] = body.pop("keywords")
    if op == "GetItems" and "ItemIds" not in body:
        if body.get("asin"):
            body["ItemIds"] = [str(body.pop("asin"))]
        elif body.get("item_ids"):
            body["ItemIds"] = list(body.pop("item_ids"))
    if "Resources" not in body:
        body["Resources"] = [
            "ItemInfo.Title", "Offers.Listings.Price", "Images.Primary.Medium",
        ]

    payload = json.dumps(body, separators=(",", ":"))
    target = f"com.amazon.paapi5.v1.ProductAdvertisingAPIv1.{op}"
    headers = sigv4_headers(access_key, secret_key, host, region, target, payload)
    url = f"https://{host}/paapi5/{op.lower()}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, content=payload.encode(), headers=headers)
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text[:2000]}
    return {"status": resp.status_code, "json": data}
