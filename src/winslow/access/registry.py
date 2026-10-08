"""The backends of the two stores, selected by WINSLOW_POLICY_BACKEND and
WINSLOW_TOKEN_BACKEND (default file), in the shape of the state store. A
package registers another backend at import time, before a serve process
builds its stores (see django-winslow)."""

from winslow.access.file import FilePolicyStore, FileTokenStore
from winslow.backends import BackendRegistry

POLICY_BACKENDS = BackendRegistry(
    "POLICY_BACKENDS", "WINSLOW_POLICY_BACKEND", file=FilePolicyStore
)
TOKEN_BACKENDS = BackendRegistry(
    "TOKEN_BACKENDS", "WINSLOW_TOKEN_BACKEND", file=FileTokenStore
)
