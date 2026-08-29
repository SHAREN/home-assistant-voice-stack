from __future__ import annotations

import importlib.util
from pathlib import Path
import socket
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "experimental" / "gemini_live" / "network_safety.py"
spec = importlib.util.spec_from_file_location("gemini_live_network_safety_test", MODULE_PATH)
assert spec is not None and spec.loader is not None
network_safety = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = network_safety
spec.loader.exec_module(network_safety)


class ProxyError(Exception):
    pass


class GeminiNetworkSafetyTests(unittest.TestCase):
    def test_connect_proxy_dns_and_tls_failures_are_network(self) -> None:
        for exc, expected in (
            (ConnectionResetError(), "ConnectionResetError"),
            (ConnectionRefusedError(), "ConnectionRefusedError"),
            (socket.gaierror(), "gaierror"),
            (ProxyError(), "ProxyError"),
            (TimeoutError(), "TimeoutError"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(
                    network_safety.initial_network_error_type(exc),
                    expected,
                )

    def test_cleanup_exception_can_reveal_nested_transport_failure(self) -> None:
        try:
            try:
                raise ConnectionResetError()
            except ConnectionResetError as transport:
                raise AttributeError("cleanup failed") from transport
        except AttributeError as exc:
            self.assertEqual(
                network_safety.initial_network_error_type(exc),
                "ConnectionResetError",
            )

    def test_protocol_auth_and_model_failures_are_not_network(self) -> None:
        for exc in (RuntimeError("bad key"), ValueError("bad config"), EOFError()):
            with self.subTest(exc=type(exc).__name__):
                self.assertIsNone(network_safety.initial_network_error_type(exc))


if __name__ == "__main__":
    unittest.main()
