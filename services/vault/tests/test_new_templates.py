"""Tests for the Task-135 connection templates: social OAuth quirks,
Amazon Associates (link building + SigV4), WordPress basic auth,
WebinarNinja, and WordPress MCP."""

import base64
import datetime
import json
from urllib.parse import parse_qs, urlsplit

import pytest

import amazon_ops
import connections
import oauth as oauth_mod
from catalog import CATALOG


NEW_SOCIAL = ["meta", "x_twitter", "linkedin", "reddit", "tiktok", "pinterest"]
NEW_ALL = NEW_SOCIAL + ["amazon_associates", "wordpress", "wordpress_mcp",
                        "webinarninja"]


# ---------------------------------------------------------------------------
# Template shape
# ---------------------------------------------------------------------------

def test_new_templates_exist():
    for key in NEW_ALL:
        assert key in CATALOG, f"missing template {key}"
        assert CATALOG[key].get("label")
        assert CATALOG[key].get("setup_help")


def test_social_templates_are_oauth2_with_https_endpoints():
    for key in NEW_SOCIAL:
        tpl = CATALOG[key]
        assert tpl["auth"]["kind"] == "oauth2"
        oauth = tpl["oauth"]
        assert oauth["authorize_url"].startswith("https://")
        assert oauth["token_url"].startswith("https://")
        assert oauth.get("scopes"), f"{key} needs default scopes"
        assert tpl.get("test_probe"), f"{key} needs a test probe"


def test_allowed_hosts_https_only_and_base_host_included():
    for key in NEW_SOCIAL + ["amazon_associates"]:
        tpl = CATALOG[key]
        base = tpl.get("base_url") or ""
        assert base.startswith("https://")
        host = urlsplit(base).hostname
        assert host in tpl.get("allowed_hosts", []), f"{key}: {host} not allowlisted"


def test_no_tool_name_collisions_between_templates():
    # Template keys normalize to tool names the same way connection ids do:
    # lowercase, non-alnum → underscore. Ensure all catalog keys stay unique
    # after that normalization.
    import re
    normalized = [re.sub(r"[^a-z0-9]+", "_", k.lower()).strip("_") for k in CATALOG]
    assert len(normalized) == len(set(normalized))


def test_meta_uses_long_lived_exchange_and_no_refresh():
    oauth = CATALOG["meta"]["oauth"]
    assert oauth.get("long_lived_exchange") == "facebook"
    assert oauth.get("no_refresh") is True


def test_reddit_quirks():
    tpl = CATALOG["reddit"]
    assert tpl["oauth"].get("token_auth") == "basic"
    assert tpl["oauth"].get("extra_authorize_params", {}).get("duration") == "permanent"
    assert "User-Agent" in tpl.get("default_headers", {})


def test_tiktok_client_key_param():
    assert CATALOG["tiktok"]["oauth"].get("client_id_param") == "client_key"


def test_x_twitter_pkce_and_offline_access():
    oauth = CATALOG["x_twitter"]["oauth"]
    assert oauth.get("pkce") is True
    assert "offline.access" in oauth["scopes"]


def test_wordpress_mcp_is_mcp_kind():
    tpl = CATALOG["wordpress_mcp"]
    assert tpl["auth"]["kind"] == "mcp_bearer"
    assert tpl.get("is_mcp") is True


# ---------------------------------------------------------------------------
# OAuth token-request quirks (vault-side, never agent-side)
# ---------------------------------------------------------------------------

def test_token_request_kwargs_basic_auth_strips_body_creds():
    kwargs = oauth_mod._token_request_kwargs(
        {"token_auth": "basic"},
        {"grant_type": "authorization_code", "code": "c",
         "client_id": "id1", "client_secret": "sec1"},
        "id1", "sec1")
    assert "client_secret" not in kwargs["data"]
    assert kwargs["auth"] == ("id1", "sec1")
    assert kwargs["headers"].get("User-Agent")


def test_token_request_kwargs_client_id_param_rename():
    kwargs = oauth_mod._token_request_kwargs(
        {"client_id_param": "client_key"},
        {"grant_type": "authorization_code", "client_id": "abc",
         "client_secret": "s"},
        "abc", "s")
    assert kwargs["data"]["client_key"] == "abc"
    assert "client_id" not in kwargs["data"]


# ---------------------------------------------------------------------------
# build_auth: basic + amazon kinds
# ---------------------------------------------------------------------------

def test_build_auth_basic():
    auth = connections.build_auth(
        {"auth": {"kind": "basic"}}, {"username": "wp_admin", "api_key": "abcd efgh"})
    header = auth["headers"]["Authorization"]
    assert header.startswith("Basic ")
    assert base64.b64decode(header[6:]).decode() == "wp_admin:abcd efgh"


def test_build_auth_basic_missing_creds_raises():
    with pytest.raises(connections.AuthInjectionError):
        connections.build_auth({"auth": {"kind": "basic"}}, {"username": "x"})


def test_build_auth_amazon_directs_to_special_endpoint():
    with pytest.raises(connections.AuthInjectionError) as exc:
        connections.build_auth({"auth": {"kind": "amazon"}}, {"associate_tag": "t-20"})
    assert "/api/vault/amazon/" in str(exc.value)


# ---------------------------------------------------------------------------
# Amazon ops: tagged links + SigV4
# ---------------------------------------------------------------------------

def test_build_link_from_asin():
    out = amazon_ops.build_tagged_link("mytag-20", {"asin": "B01ABCDEFG"})
    assert out["link"] == "https://www.amazon.com/dp/B01ABCDEFG?tag=mytag-20"
    assert out["asin"] == "B01ABCDEFG"


def test_build_link_from_url_canonicalizes():
    out = amazon_ops.build_tagged_link(
        "mytag-20",
        {"url": "https://www.amazon.com/Some-Product/dp/b09xyzw123/ref=sr_1_1?keywords=x&tag=someoneelse-20"})
    assert out["link"] == "https://www.amazon.com/dp/B09XYZW123?tag=mytag-20"


def test_build_link_replaces_existing_tag_on_non_product_url():
    out = amazon_ops.build_tagged_link(
        "mytag-20", {"url": "https://www.amazon.com/s?k=laptops&tag=other-20"})
    q = parse_qs(urlsplit(out["link"]).query)
    assert q["tag"] == ["mytag-20"]
    assert q["k"] == ["laptops"]


def test_build_link_rejects_non_amazon_host():
    with pytest.raises(amazon_ops.AmazonOpsError):
        amazon_ops.build_tagged_link("t-20", {"url": "https://evil.example.com/dp/B000000000"})


def test_build_link_rejects_bad_asin():
    with pytest.raises(amazon_ops.AmazonOpsError):
        amazon_ops.build_tagged_link("t-20", {"asin": "notanasin!"})


def test_build_link_requires_tag():
    with pytest.raises(amazon_ops.AmazonOpsError):
        amazon_ops.build_tagged_link("", {"asin": "B01ABCDEFG"})


def test_normalize_marketplace():
    assert amazon_ops.normalize_marketplace("") == "www.amazon.com"
    assert amazon_ops.normalize_marketplace("amazon.co.uk") == "www.amazon.co.uk"
    with pytest.raises(amazon_ops.AmazonOpsError):
        amazon_ops.normalize_marketplace("www.amazon.fake")


def test_sigv4_headers_deterministic():
    now = datetime.datetime(2026, 8, 14, 12, 0, 0, tzinfo=datetime.timezone.utc)
    target = "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.SearchItems"
    payload = json.dumps({"Keywords": "book", "PartnerTag": "t-20",
                          "PartnerType": "Associates",
                          "Marketplace": "www.amazon.com"},
                         separators=(",", ":"))
    h1 = amazon_ops.sigv4_headers("AKIAEXAMPLE", "secretkey",
                                  "webservices.amazon.com", "us-east-1",
                                  target, payload, now=now)
    h2 = amazon_ops.sigv4_headers("AKIAEXAMPLE", "secretkey",
                                  "webservices.amazon.com", "us-east-1",
                                  target, payload, now=now)
    assert h1 == h2
    assert h1["X-Amz-Date"] == "20260814T120000Z"
    assert h1["X-Amz-Target"] == target
    assert h1["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/20260814/us-east-1/ProductAdvertisingAPI/aws4_request")
    assert "Signature=" in h1["Authorization"]
    # Secret never appears in any header.
    assert "secretkey" not in json.dumps(h1)


@pytest.mark.asyncio
async def test_paapi_call_without_keys_raises():
    with pytest.raises(amazon_ops.AmazonOpsError) as exc:
        await amazon_ops.paapi_call({"associate_tag": "t-20"}, "search_items",
                                    {"keywords": "book"})
    assert "build_link" in str(exc.value)


@pytest.mark.asyncio
async def test_paapi_call_unknown_operation():
    with pytest.raises(amazon_ops.AmazonOpsError):
        await amazon_ops.paapi_call(
            {"paapi_access_key": "a", "paapi_secret_key": "s",
             "associate_tag": "t"}, "delete_everything", {})
