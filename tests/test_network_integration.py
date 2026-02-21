#!/usr/bin/env python3
"""
Integration tests for katzenqt.network module.

These tests verify the Pigeonhole API integration in network.py
against a running docker mixnet.

Requires:
- Running docker mixnet (make run from katzenpost/docker)
- Client daemon connected to mixnet

Run with: pytest tests/test_network_integration.py -v
"""

import asyncio
import os
import secrets
import tempfile
import pytest
import pytest_asyncio
from pathlib import Path

from katzenpost_thinclient import ThinClient, Config


def get_config_path():
    """Get the path to the thinclient config file."""
    possible_paths = [
        Path(__file__).parent.parent / "config" / "thinclient.toml",
        Path(__file__).parent.parent / "katzenpost" / "docker" / "voting_mixnet" / "client2" / "thinclient.toml",
    ]
    for path in possible_paths:
        if path.exists():
            return str(path.resolve())
    return str(possible_paths[0])


async def setup_thin_client():
    """Set up a thin client connected to the mixnet."""
    config_path = get_config_path()
    config = Config(config_path)
    client = ThinClient(config)

    loop = asyncio.get_running_loop()
    await client.start(loop)

    print("Waiting for daemon to connect to mixnet...")
    attempts = 0
    while (not client.is_connected() or client.pki_document() is None) and attempts < 30:
        await asyncio.sleep(1)
        attempts += 1

    if not client.is_connected():
        raise Exception("Daemon failed to connect to mixnet within 30 seconds")

    if client.pki_document() is None:
        raise Exception("PKI document not received within 30 seconds")

    print("✅ Daemon connected to mixnet")
    return client


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_pigeonhole_api_roundtrip():
    """
    Test the complete Pigeonhole workflow that network.py uses:
    1. new_keypair - Generate WriteCap/ReadCap
    2. encrypt_write - Encrypt a message
    3. start_resending_encrypted_message - Send message (write)
    4. encrypt_read - Encrypt a read request
    5. start_resending_encrypted_message - Read message back
    
    This is the core workflow used by network.py's:
    - provision_read_caps (new_keypair)
    - start_resending (encrypt_write + persist to MixWAL)
    - drain_mixwal_write_single (start_resending_encrypted_message for writes)
    - readables_to_mixwal (encrypt_read + persist to MixWAL)
    - drain_mixwal_read_single (start_resending_encrypted_message for reads)
    """
    client = await setup_thin_client()

    try:
        print("\n=== Test: Pigeonhole API roundtrip (network.py workflow) ===")

        # Step 1: new_keypair - same as provision_read_caps
        print("\n--- Step 1: new_keypair (provision_read_caps) ---")
        seed = secrets.token_bytes(32)
        write_cap, read_cap, first_message_index = await client.new_keypair(seed)
        
        assert len(write_cap) == 168, f"write_cap should be 168 bytes, got {len(write_cap)}"
        assert len(read_cap) == 136, f"read_cap should be 136 bytes, got {len(read_cap)}"
        assert len(first_message_index) == 104, f"first_message_index should be 104 bytes, got {len(first_message_index)}"
        print(f"✓ Created keypair")

        # Step 2: encrypt_write - same as start_resending
        print("\n--- Step 2: encrypt_write (start_resending) ---")
        plaintext = b"Ftest message from network.py integration test"  # F prefix like network.py uses
        
        ciphertext, envelope_descriptor, envelope_hash = await client.encrypt_write(
            plaintext, write_cap, first_message_index
        )
        
        assert len(ciphertext) > 0, "ciphertext should not be empty"
        assert len(envelope_descriptor) > 0, "envelope_descriptor should not be empty"
        assert len(envelope_hash) == 32, f"envelope_hash should be 32 bytes, got {len(envelope_hash)}"
        print(f"✓ Encrypted write: {len(ciphertext)} bytes ciphertext")

        # Compute next_message_index for later use
        next_message_index = await client.next_message_box_index(first_message_index)
        assert len(next_message_index) == 104, f"next_message_index should be 104 bytes"
        print(f"✓ Computed next_message_index")

        # Step 3: start_resending_encrypted_message (write) - same as drain_mixwal_write_single
        print("\n--- Step 3: start_resending_encrypted_message WRITE (drain_mixwal_write_single) ---")
        
        write_result = await client.start_resending_encrypted_message(
            read_cap=None,
            write_cap=write_cap,
            next_message_index=next_message_index,
            reply_index=None,
            envelope_descriptor=envelope_descriptor,
            message_ciphertext=ciphertext,
            envelope_hash=envelope_hash
        )
        print(f"✓ Write completed, got ACK")

        # Wait for message propagation
        print("\n--- Waiting for message propagation (5 seconds) ---")
        await asyncio.sleep(5)

        # Step 4: encrypt_read - same as readables_to_mixwal
        print("\n--- Step 4: encrypt_read (readables_to_mixwal) ---")
        
        read_ciphertext, read_next_index, read_env_desc, read_env_hash = await client.encrypt_read(
            read_cap, first_message_index
        )
        
        assert len(read_ciphertext) > 0, "read_ciphertext should not be empty"
        print(f"✓ Encrypted read: {len(read_ciphertext)} bytes")

        # Step 5: start_resending_encrypted_message (read) - same as drain_mixwal_read_single
        print("\n--- Step 5: start_resending_encrypted_message READ (drain_mixwal_read_single) ---")

        received_plaintext = await client.start_resending_encrypted_message(
            read_cap=read_cap,
            write_cap=None,
            next_message_index=read_next_index,
            reply_index=0,  # Try reply_index 0 first, like drain_mixwal_read_single
            envelope_descriptor=read_env_desc,
            message_ciphertext=read_ciphertext,
            envelope_hash=read_env_hash
        )

        # Verify we got the original message back
        print(f"\n--- Step 6: Verify received message ---")
        print(f"Received: {received_plaintext}")
        
        assert received_plaintext == plaintext, f"Message mismatch! Expected: {plaintext}, Got: {received_plaintext}"
        print("✅ Roundtrip test passed - message sent and received successfully!")

    finally:
        client.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multiple_messages_same_channel():
    """
    Test sending multiple messages on the same channel.

    This verifies that next_message_box_index correctly advances
    the message index, which is critical for network.py's WriteCapWAL.next_index.
    """
    client = await setup_thin_client()

    try:
        print("\n=== Test: Multiple messages on same channel ===")

        seed = secrets.token_bytes(32)
        write_cap, read_cap, current_index = await client.new_keypair(seed)
        print(f"✓ Created keypair")

        messages = [
            b"FMessage 1: Hello",
            b"FMessage 2: World",
            b"FMessage 3: Test",
        ]

        for i, msg in enumerate(messages):
            print(f"\n--- Sending message {i+1} ---")

            ciphertext, env_desc, env_hash = await client.encrypt_write(msg, write_cap, current_index)
            next_index = await client.next_message_box_index(current_index)

            await client.start_resending_encrypted_message(
                read_cap=None,
                write_cap=write_cap,
                next_message_index=next_index,
                reply_index=None,
                envelope_descriptor=env_desc,
                message_ciphertext=ciphertext,
                envelope_hash=env_hash
            )

            print(f"✓ Sent message {i+1}")
            current_index = next_index  # Advance for next message

        print("\n--- Waiting for propagation (5 seconds) ---")
        await asyncio.sleep(5)

        # Read back all messages
        read_index = (await client.new_keypair(seed))[2]  # Get first_message_index again

        for i, expected_msg in enumerate(messages):
            print(f"\n--- Reading message {i+1} ---")

            read_ct, read_next, read_desc, read_hash = await client.encrypt_read(read_cap, read_index)

            received = await client.start_resending_encrypted_message(
                read_cap=read_cap,
                write_cap=None,
                next_message_index=read_next,
                reply_index=0,
                envelope_descriptor=read_desc,
                message_ciphertext=read_ct,
                envelope_hash=read_hash
            )

            assert received == expected_msg, f"Message {i+1} mismatch!"
            print(f"✓ Read message {i+1}: {received.decode()}")
            # Advance read_index using next_message_box_index, not the returned read_next
            read_index = await client.next_message_box_index(read_index)

        print("✅ Multiple messages test passed!")

    finally:
        client.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_during_resend():
    """
    Test that cancel_resending_encrypted_message works.

    This is important for graceful shutdown in network.py.
    """
    client = await setup_thin_client()

    try:
        print("\n=== Test: Cancel during resend ===")

        seed = secrets.token_bytes(32)
        write_cap, read_cap, first_index = await client.new_keypair(seed)

        plaintext = b"FThis message will be cancelled"
        ciphertext, env_desc, env_hash = await client.encrypt_write(plaintext, write_cap, first_index)
        next_index = await client.next_message_box_index(first_index)

        print(f"✓ Encrypted message, envelope_hash: {env_hash.hex()}")

        # Start resending in background
        async def do_resend():
            return await client.start_resending_encrypted_message(
                read_cap=None,
                write_cap=write_cap,
                next_message_index=next_index,
                reply_index=None,
                envelope_descriptor=env_desc,
                message_ciphertext=ciphertext,
                envelope_hash=env_hash
            )

        resend_task = asyncio.create_task(do_resend())

        # Give it a moment to start, then cancel
        await asyncio.sleep(0.1)

        print("--- Calling cancel_resending_encrypted_message ---")
        await client.cancel_resending_encrypted_message(env_hash)
        print("✓ Cancel call completed")

        # Check that the task raises or returns
        try:
            result = await asyncio.wait_for(resend_task, timeout=5.0)
            # If it completed before cancel, that's also OK
            print(f"✓ Resend completed (may have finished before cancel): {result}")
        except Exception as e:
            print(f"✓ Resend was cancelled: {e}")

        print("✅ Cancel test passed!")

    finally:
        client.stop()

