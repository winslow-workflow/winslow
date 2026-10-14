"""Access control: the permission model (permissions), the check at the call
(authz), the policy of who holds which scopes, the tokens people and service
accounts present, the credential file of a person and the `winslow auth` CLI
(see docs/serve.md). The package imports nothing, so the core modules reach
permissions and authz without the serve extra. Import a submodule by name.
"""
