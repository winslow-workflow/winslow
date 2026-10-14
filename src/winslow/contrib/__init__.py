"""Optional telemetry backends for the hook in winslow.telemetry. The core
never imports them: the TELEMETRY setting names one by path, and the module
raises at import when its extra is not installed."""
