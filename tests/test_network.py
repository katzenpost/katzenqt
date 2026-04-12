from unittest.mock import MagicMock

import pytest

from katzenqt.network import courier_destination_exists


class TestCourierDestinationExists:
    """Tests for courier_destination_exists() using the new get_all_couriers() API."""

    def _mock_connection(self, couriers):
        conn = MagicMock()
        conn.get_all_couriers.return_value = couriers
        return conn

    def test_matching_destination(self):
        dest = b'\x01' * 32
        conn = self._mock_connection([(dest, b'queue1')])
        assert courier_destination_exists(conn, dest) is True

    def test_non_matching_destination(self):
        dest = b'\x01' * 32
        other = b'\x02' * 32
        conn = self._mock_connection([(other, b'queue1')])
        assert courier_destination_exists(conn, dest) is False

    def test_multiple_couriers(self):
        dest = b'\x03' * 32
        conn = self._mock_connection([
            (b'\x01' * 32, b'q1'),
            (b'\x02' * 32, b'q2'),
            (dest, b'q3'),
        ])
        assert courier_destination_exists(conn, dest) is True

    def test_empty_couriers(self):
        conn = self._mock_connection([])
        assert courier_destination_exists(conn, b'\x01' * 32) is False

    def test_exception_returns_false(self):
        conn = MagicMock()
        conn.get_all_couriers.side_effect = Exception("no PKI")
        assert courier_destination_exists(conn, b'\x01' * 32) is False
