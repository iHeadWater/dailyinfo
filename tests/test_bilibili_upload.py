"""Tests for scripts/bilibili_upload.py."""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import bilibili_upload as bu


class TestCheckCookie:
    def test_cookie_exists(self, tmp_path):
        cookie = tmp_path / "cookies.json"
        cookie.write_text('{"test": 1}')
        result = bu.check_cookie(cookie)
        assert result == cookie.resolve()

    def test_cookie_missing(self, tmp_path):
        cookie = tmp_path / "nonexistent.json"
        with pytest.raises(SystemExit):
            bu.check_cookie(cookie)


class TestGenerateCover:
    def test_generates_1920x1080_png(self, tmp_path):
        out = tmp_path / "cover.png"
        result = bu.generate_cover("Test Title", "2026 W26", output_path=out)
        assert result.exists()
        assert result.suffix == ".png"
        from PIL import Image
        img = Image.open(result)
        assert img.size == (1920, 1080)

    def test_subtitle_optional(self, tmp_path):
        out = tmp_path / "cover_no_sub.png"
        result = bu.generate_cover("Title Only", output_path=out)
        assert result.exists()


class TestAudioToVideo:
    def test_ffmpeg_args(self, tmp_path, monkeypatch):
        audio = tmp_path / "test.mp3"
        cover = tmp_path / "cover.png"
        out = tmp_path / "output.mp4"
        audio.touch()
        cover.touch()

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(bu.subprocess, "run", fake_run)
        result = bu.audio_to_video(audio, cover, out)
        assert result == out
        cmd_str = " ".join(calls[0])
        assert "-loop" in cmd_str
        assert "-c:v" in cmd_str and "libx264" in cmd_str
        assert "-c:a" in cmd_str and "aac" in cmd_str
        assert "-shortest" in cmd_str

    def test_creates_output_dir(self, tmp_path, monkeypatch):
        audio = tmp_path / "test.mp3"
        cover = tmp_path / "cover.png"
        out = tmp_path / "subdir" / "output.mp4"
        audio.touch()
        cover.touch()

        monkeypatch.setattr(
            bu.subprocess, "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0),
        )
        bu.audio_to_video(audio, cover, out)
        assert out.parent.exists()


class TestUploadVideo:
    def test_calls_biliup_single(self, tmp_path, monkeypatch):
        video = tmp_path / "test.mp4"
        cookie = tmp_path / "cookies.json"
        video.touch()
        cookie.write_text("{}")

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(bu.subprocess, "run", fake_run)
        rc = bu.upload_video(
            [video], "Title", 171, "tag1,tag2", "desc",
            cookie_path=cookie,
        )
        assert rc == 0
        cmd_str = " ".join(calls[0])
        assert "biliup" in cmd_str
        assert "upload" in cmd_str
        assert "--title" in cmd_str and "Title" in cmd_str
        assert "--tag" in cmd_str and "tag1,tag2" in cmd_str

    def test_calls_biliup_multi_p(self, tmp_path, monkeypatch):
        v1 = tmp_path / "p1.mp4"
        v2 = tmp_path / "p2.mp4"
        v1.touch()
        v2.touch()

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(bu.subprocess, "run", fake_run)
        rc = bu.upload_video(
            [v1, v2], "Multi-P", 171, "a,b", "desc",
        )
        assert rc == 0
        cmd_str = " ".join(calls[0])
        assert "p1.mp4" in cmd_str
        assert "p2.mp4" in cmd_str


class TestRunBilibiliUpload:
    def test_missing_audio(self, tmp_path):
        rc = bu.run_bilibili_upload(
            [str(tmp_path / "no_such_file.mp3")],
            title="Test",
        )
        assert rc == 1

    def test_dry_run_skips_upload(self, tmp_path, monkeypatch):
        audio = tmp_path / "test.mp3"
        audio.write_bytes(b"\xff\xfb\x90\x00" * 100)
        monkeypatch.setattr(
            bu.subprocess, "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0),
        )
        monkeypatch.setattr(bu.shutil, "which", lambda _: "/usr/bin/ffmpeg")

        rc = bu.run_bilibili_upload(
            [str(audio)],
            title="Dry Test",
            dry_run=True,
        )
        assert rc == 2

    def test_missing_ffmpeg_shows_error(self, tmp_path, monkeypatch):
        audio = tmp_path / "test.mp3"
        audio.touch()
        monkeypatch.setattr(bu.shutil, "which", lambda _: None)

        rc = bu.run_bilibili_upload([str(audio)], title="Test")
        assert rc == 1


class TestCheckPrereqs:
    def test_ffmpeg_missing(self, monkeypatch):
        monkeypatch.setattr(bu.shutil, "which", lambda name: None)
        with pytest.raises(SystemExit):
            bu.check_prereqs()

    def test_all_present(self, monkeypatch):
        monkeypatch.setattr(bu.shutil, "which", lambda name: f"/usr/bin/{name}")
        bu.check_prereqs()
