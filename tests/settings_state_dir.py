"""The settings module of the kill test subprocess: the state directory the
test names through WINSLOW_TEST_STATE_DIR (see test_kill_restore)."""

import os

STATE_DIR = os.environ["WINSLOW_TEST_STATE_DIR"]
