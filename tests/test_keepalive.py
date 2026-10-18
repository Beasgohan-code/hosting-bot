import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from bot_plugins.builtin.keepalive import _probe_url, _resolve_public_addresses, validate_monitor_url


def dns_record(address: str, family: int = 2):
    return (family, 1, 6, "", (address, 0))


class MonitorUrlValidationTests(unittest.TestCase):
    def test_rejects_private_reserved_and_local_addresses(self):
        for address in (
            "127.0.0.1",
            "10.0.0.1",
            "172.20.0.1",
            "192.168.1.4",
            "169.254.169.254",
            "100.64.0.1",
            "192.0.2.4",
            "224.0.0.1",
            "::1",
            "fc00::1",
            "fe80::1",
            "2001:db8::1",
            "::ffff:10.0.0.1",
        ):
            with self.subTest(address=address):
                with self.assertRaises(ValueError):
                    validate_monitor_url(f"https://[{address}]/" if ":" in address else f"https://{address}/")

    def test_rejects_unsafe_url_features(self):
        for url in (
            "http://localhost/health",
            "http://admin.local/health",
            "http://user:pass@example.org/health",
            "https://example.org/health?token=secret",
            "https://example.org/health#fragment",
            "http://[fe80::1%25eth0]/",
            "ftp://example.org/file",
            "https://example.org:0/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_monitor_url(url)

    def test_canonicalizes_default_ports_host_and_empty_path(self):
        self.assertEqual(validate_monitor_url("HTTPS://Example.ORG:443"), "https://example.org/")
        self.assertEqual(validate_monitor_url("http://8.8.8.8:8080/health"), "http://8.8.8.8:8080/health")

    def test_rejects_mixed_public_and_private_dns_answers(self):
        with patch("bot_plugins.builtin.keepalive.socket.getaddrinfo", return_value=[
            dns_record("93.184.216.34"),
            dns_record("10.0.0.8"),
        ]):
            with self.assertRaisesRegex(ValueError, "Private and reserved"):
                _resolve_public_addresses(urlsplit("https://status.example.org/health"))

    def test_times_out_dns_lookups(self):
        with (
            patch("bot_plugins.builtin.keepalive.DNS_LOOKUP_TIMEOUT_SECONDS", 0.01),
            patch("bot_plugins.builtin.keepalive.socket.getaddrinfo", side_effect=lambda *args: time.sleep(0.05)),
        ):
            with self.assertRaisesRegex(ValueError, "timed out"):
                _resolve_public_addresses(urlsplit("https://slow.example.org/health"))


class PinnedProbeTests(unittest.TestCase):
    def test_pins_http_request_to_validated_public_dns_result(self):
        with (
            patch("bot_plugins.builtin.keepalive.socket.getaddrinfo", return_value=[dns_record("93.184.216.34")]),
            patch("bot_plugins.builtin.keepalive._request_pinned", return_value=204) as request,
        ):
            result = _probe_url("https://status.example.org/health", 5)
        self.assertEqual(result[0:2], (True, 204))
        self.assertEqual(request.call_args.args[1], "93.184.216.34")

    def test_rejects_redirects_as_unhealthy_without_following_them(self):
        with (
            patch("bot_plugins.builtin.keepalive.socket.getaddrinfo", return_value=[dns_record("93.184.216.34")]),
            patch("bot_plugins.builtin.keepalive._request_pinned", return_value=302) as request,
        ):
            result = _probe_url("https://status.example.org/health", 5)
        self.assertFalse(result[0])
        self.assertEqual(result[1], 302)
        self.assertIn("Redirect response is not followed", result[3])
        request.assert_called_once()

    def test_does_not_request_when_dns_contains_a_private_address(self):
        with (
            patch("bot_plugins.builtin.keepalive.socket.getaddrinfo", return_value=[dns_record("93.184.216.34"), dns_record("127.0.0.1")]),
            patch("bot_plugins.builtin.keepalive._request_pinned") as request,
        ):
            result = _probe_url("https://status.example.org/health", 5)
        self.assertFalse(result[0])
        self.assertIsNone(result[1])
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
