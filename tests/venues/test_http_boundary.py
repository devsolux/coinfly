import threading
import unittest
from unittest.mock import patch

from coinfly.domain.context import DomainError
from coinfly.venues.coinbase.endpoint_profiles import endpoint_profile
from coinfly.venues.coinbase.http_reader import BoundHttpsReader


class Response:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body or b'{"accounts":[],"has_next":false}'

    def getheader(self, name, default=None):
        return default

    def read(self, limit):
        return self.body[:limit]


class Connection:
    calls = []
    response = Response()

    def __init__(self, host, *, timeout, context):
        self.host = host

    def request(self, method, target, *, headers):
        self.calls.append((self.host, method, target, dict(headers)))

    def getresponse(self):
        return self.response

    def close(self):
        pass


class HttpTests(unittest.TestCase):
    def setUp(self):
        Connection.calls = []
        Connection.response = Response()

    def test_poisoned_environment_cannot_change_host_auth_tls_or_proxy(self):
        poisoned = {
            "HTTPS_PROXY": "https://attacker.invalid",
            "ALL_PROXY": "https://attacker.invalid",
            "NETRC": "/attacker/netrc",
            "SSL_CERT_FILE": "/attacker/ca",
            "SSL_CERT_DIR": "/attacker",
            "COINBASE_API_KEY": "secret-canary",
            "COINBASE_API_SECRET": "secret-canary",
        }
        with (
            patch.dict("os.environ", poisoned),
            patch("coinfly.venues.coinbase.http_reader.HTTPSConnection", Connection),
        ):
            reader = BoundHttpsReader(endpoint_profile("coinbase-static-sandbox"))
            reader.get("/accounts", query={"limit": "250"})
        self.assertEqual(len(Connection.calls), 1)
        host, method, target, headers = Connection.calls[0]
        self.assertEqual(host, "api-sandbox.coinbase.com")
        self.assertEqual(method, "GET")
        self.assertEqual(target, "/api/v3/brokerage/accounts?limit=250")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("secret-canary", repr(Connection.calls))

    def test_redirect_rejected_without_a_second_invocation(self):
        Connection.response = Response(status=307)
        with patch("coinfly.venues.coinbase.http_reader.HTTPSConnection", Connection):
            reader = BoundHttpsReader(endpoint_profile("coinbase-static-sandbox"))
            with self.assertRaisesRegex(DomainError, "HTTP_REDIRECT_FORBIDDEN"):
                reader.get("/accounts")
        self.assertEqual(len(Connection.calls), 1)

    def test_timeout_does_not_retry(self):
        class TimeoutConnection(Connection):
            def getresponse(self):
                raise TimeoutError("canary")

        with patch("coinfly.venues.coinbase.http_reader.HTTPSConnection", TimeoutConnection):
            with self.assertRaisesRegex(DomainError, "HTTP_READ_FAILED") as caught:
                BoundHttpsReader(endpoint_profile("coinbase-static-sandbox")).get("/accounts")
        self.assertEqual(len(Connection.calls), 1)
        self.assertNotIn("canary", str(caught.exception))

    def test_unregistered_or_credentialed_endpoint_cannot_construct_reader(self):
        with self.assertRaisesRegex(DomainError, "HTTP_SIGNER_REALM_MISMATCH"):
            BoundHttpsReader(endpoint_profile("coinbase-advanced-spot"))
        reader = BoundHttpsReader(endpoint_profile("coinbase-static-sandbox"))
        with self.assertRaisesRegex(DomainError, "HTTP_QUERY_FORBIDDEN"):
            reader.get("/accounts", query={"retail_portfolio_id": "different"})
        with self.assertRaisesRegex(DomainError, "ENDPOINT_ROUTE_FORBIDDEN"):
            reader.get("https://attacker.invalid")

    def test_retirement_interrupts_response_socket_and_rejects_late_results(self):
        sent, released, closed = threading.Event(), threading.Event(), threading.Event()
        class PendingSocket:
            def shutdown(self, _how):
                released.set()
        class PendingConnection(Connection):
            sock = PendingSocket()
            def getresponse(self):
                sent.set()
                if not released.wait(2):
                    raise TimeoutError
                return self.response
            def close(self):
                closed.set()
        errors = []
        with patch("coinfly.venues.coinbase.http_reader.HTTPSConnection", PendingConnection):
            reader = BoundHttpsReader(endpoint_profile("coinbase-static-sandbox"))
            def read():
                try:
                    reader.get("/accounts")
                except DomainError as error:
                    errors.append(error.code)
            thread = threading.Thread(target=read)
            thread.start()
            try:
                self.assertTrue(sent.wait(1))
                reader.retire()
                self.assertTrue(closed.is_set())
                with self.assertRaisesRegex(DomainError, "NETWORK_CONTEXT_RETIRED"):
                    reader.get("/accounts")
                thread.join(2)
                self.assertEqual(errors, ["NETWORK_CONTEXT_RETIRED"])
                self.assertEqual(len(Connection.calls), 1)
            finally:
                released.set()
                thread.join(2)


if __name__ == "__main__":
    unittest.main()
