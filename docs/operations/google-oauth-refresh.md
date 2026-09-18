# Google OAuth token refresh

The vault refreshes expiring, `ready` Google OAuth connections in two ways:

- lazily, when a granted request needs an expiring token; and
- proactively, on a periodic sweep when a token is within ten minutes of
  expiry.

The sweep covers the combined `google` connection and every dedicated
`google_*` catalog connection. It does not create connections, copy
credentials, or change grants.

Both paths use the same bounded per-connection Redis lock. After acquiring the
lock, the vault rereads the encrypted secret record before refreshing. The
result is merged into that latest record so a rotated refresh token is retained
and a provider-omitted refresh token does not erase the stored token.

Temporary transport, rate-limit, and provider failures are retried with bounded
backoff and leave the connection `ready`. A missing refresh token or permanent
authorization rejection marks it `needs_reauth`; the owner must complete the
explicit authorization flow. Google authorization continues to request offline
access with `prompt=consent`, which is needed when obtaining a missing refresh
token.

Admin connection views expose only safe state: whether a refresh token exists,
whether Google auto-refresh applies, token expiry, refresh status, a fixed
sanitized reason, and refresh/retry timestamps. Tokens and provider response
bodies are never exposed.

Proactive refresh is not a keepalive guarantee. It cannot prevent revocation,
consent changes, or Google testing-mode token expiry.