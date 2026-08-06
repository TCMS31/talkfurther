"""Tests for the test suite's own guarantees.

A suite that cannot reach the network is only useful if the block is real. These
assert the guard fires, so "no key, no network" is a checked property rather than
a claim in a README.
"""

from __future__ import annotations

import socket

import pytest

from tests.conftest import NetworkAccessBlocked


def test_opening_a_socket_is_blocked() -> None:
    with pytest.raises(NetworkAccessBlocked):
        socket.create_connection(("api.openai.com", 443), timeout=1)


def test_dns_resolution_is_blocked() -> None:
    with pytest.raises(NetworkAccessBlocked):
        socket.getaddrinfo("api.openai.com", 443)


def test_a_real_openai_call_fails_loudly_rather_than_silently_billing() -> None:
    """If a test ever constructs the real client by accident, it stops here."""
    from openai import OpenAI

    client = OpenAI(api_key="sk-not-a-real-key", timeout=1, max_retries=0)
    with pytest.raises(Exception) as caught:
        client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )
    assert "NetworkAccessBlocked" in repr(caught.value) or isinstance(
        caught.value, NetworkAccessBlocked
    )


def test_no_api_key_is_visible_to_tests() -> None:
    import os

    assert not os.getenv("OPENAI_API_KEY")
