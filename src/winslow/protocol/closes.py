"""The close codes of the websocket, shared by the server that sends them and
the clients that read them. The 44xx codes are the refusals of the handshake;
the reason also travels as a hello_error frame before the close, so a browser
client can read it. 4402 is the one planned close: the credential of a live
socket expired, and the client reconnects with a fresh one."""

MALFORMED_HELLO = 4400
CREDENTIAL_REFUSED = 4401
CREDENTIAL_EXPIRED = 4402
ORIGIN_REFUSED = 4403
HELLO_TIMEOUT = 4408
# The close of a client that stays behind a full frame window.
CLIENT_TOO_SLOW = 1013

EXPIRED_REASON = "credential expired - reconnect with a fresh one"
TOO_SLOW_REASON = (
    "the client stays behind a full frame window - "
    "reconnect and subscribe for a fresh snapshot"
)
