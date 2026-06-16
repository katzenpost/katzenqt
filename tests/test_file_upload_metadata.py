from pathlib import Path

from katzenqt.models import GroupChatFileUpload


def test_group_chat_file_upload_marks_opus_paths_as_audio(tmp_path: Path):
    clip_path = tmp_path / "voice-note.opus"
    clip_path.write_bytes(b"OggSfake")

    upload = GroupChatFileUpload.from_path(clip_path)

    assert upload.basename == "voice-note.opus"
    assert upload.filetype == "audio/opus"
    assert upload.payload == b"OggSfake"


def test_group_chat_file_upload_leaves_other_suffixes_arbitrary(tmp_path: Path):
    file_path = tmp_path / "notes.txt"
    file_path.write_bytes(b"hello")

    upload = GroupChatFileUpload.from_path(file_path)

    assert upload.basename == "notes.txt"
    assert upload.filetype == "arbitrary"
    assert upload.payload == b"hello"