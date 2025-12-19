import uuid

import hypothesis
import hypothesis.strategies as st
import pydantic
from hypothesis import example, given

from katzenqt import models, persistent

test_please_add_deserialize = given(st.text(), st.binary())
@test_please_add_deserialize
@example(
    # The test always failed when commented parts were varied together.
    orig_display_name="",  # or any other generated value
    orig_read_cap=b"",  # or any other generated value
).via("discovered failure")
@example(
    # The test always failed when commented parts were varied together.
    orig_display_name="hello",  # or any other generated value
    orig_read_cap=b"a"*168,  # or any other generated value
).via("discovered failure")
def test_please_add_deserialize(orig_display_name, orig_read_cap):
    try:
        gcpa = models.GroupChatPleaseAdd(display_name=orig_display_name, read_cap=orig_read_cap)
    except pydantic.ValidationError as v:
        if v.error_count() > 0:
            hypothesis.event("invalid GCPA constructor")
            return
    ser1 = gcpa.to_human_readable()
    gcpa2 = models.GroupChatPleaseAdd.from_human_readable(ser1)
    assert orig_display_name == gcpa2.display_name
    assert orig_read_cap == gcpa2.read_cap

def test_please_add_deserialize_ex1():
    ser1 = "omxkaXNwbGF5X25hbWVjYWJjaHJlYWRfY2FwWIgUrpdmcpaGg7MltRYuCXL+wHekem4erUxfaO4A7PQ6cRZnTCtmr+yGm+nc3IYtYumbLzEh7NBuOXPHPeC3EN7sT8m4mfsP+q+eP6vZGNOi7iOLjqAKrY2L5VsNswy4VtT/ssUQGZW/TIBQM+aok/600tjRX1PeqCjVDGDlg6s0agRjj6JXOscT"
    gcpa2 = models.GroupChatPleaseAdd.from_human_readable(ser1)
    assert gcpa2.display_name == "abc"
    assert gcpa2.read_cap == (
        b'\x14\xae\x97fr\x96\x86\x83\xb3%\xb5\x16.\tr\xfe\xc0w\xa4zn\x1e\xadL'
        b'_h\xee\x00\xec\xf4:q\x16gL+f\xaf\xec\x86\x9b\xe9\xdc\xdc\x86-b\xe9\x9b/1!'
        b'\xec\xd0n9s\xc7=\xe0\xb7\x10\xde\xecO\xc9\xb8\x99\xfb\x0f\xfa\xaf'
        b'\x9e?\xab\xd9\x18\xd3\xa2\xee#\x8b\x8e\xa0\n\xad\x8d\x8b\xe5[\r\xb3'
        b'\x0c\xb8V\xd4\xff\xb2\xc5\x10\x19\x95\xbfL\x80P3\xe6\xa8\x93\xfe\xb4'
        b'\xd2\xd8\xd1_S\xde\xa8(\xd5\x0c`\xe5\x83\xab4j\x04c\x8f\xa2W:\xc7\x13')


def test_send_operation_empty():
    bacap_stream = uuid.uuid4()
    s = models.SendOperation(messages=[],bacap_stream=bacap_stream)
    res = s.serialize(chunk_size=1400, conversation_id=123)
    assert res is not None
    assert type(res) is tuple
    assert res != []

test_send_operation_preserves_1 = given(st.integers(min_value=2), st.text())
@test_send_operation_preserves_1
def test_send_operation_preserves_1(chunk_size, text):
    """Test that SendOperation chunking preserves the CBOR encoding of the input."""
    m = models.GroupChatMessage(version=0,membership_hash=b'a'*32, text=text)
    bacap_stream = uuid.uuid4()
    s = models.SendOperation(messages=[m],bacap_stream=bacap_stream)
    new_bacap, ser = s.serialize(chunk_size=chunk_size, conversation_id=123)
    recon = b''
    if len(ser) > 1:
        assert len(ser) >= 3, "at least one message, one readcapwal for it, and one indirection message"
        for pwal in ser[:-2]:
            assert isinstance(pwal, persistent.PlaintextWAL)
            assert bacap_stream != pwal.bacap_stream
            recon += pwal.bacap_payload[1:]
            assert len(pwal.bacap_payload) >= 2
            assert pwal.indirection is None

        final = ser[-3]
        assert isinstance(final, persistent.PlaintextWAL), "the third-last item should be a PWAL with a b'F' tag"
        assert final.bacap_payload[0:1] == b"F"

        temp_cap = ser[-2]
        assert isinstance(temp_cap, persistent.ReadCapWAL), "second-last should be a read cap for the temp stream"
        assert temp_cap.write_cap_id == final.bacap_stream

        release = ser[-1]
        assert isinstance(release, persistent.PlaintextWAL)
        assert release.bacap_stream == bacap_stream, "final PWAL should write to original BACAP stream"
        assert release.bacap_payload == b""  # Indirect waits for filling from network
        assert release.indirection == temp_cap.id
    else:
        assert isinstance(ser[0], persistent.PlaintextWAL)
        assert ser[0].bacap_payload[0:1] == b"F"  # final
        recon += ser[0].bacap_payload[1:]
        assert ser[-1].bacap_stream == bacap_stream
    assert recon == m.to_cbor()
