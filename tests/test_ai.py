"""Tests for the AI fallback classifier (HTTP layer mocked; no network)."""
import json
from pathlib import Path

import pytest

from fs_organizer.ai import (
    _build_prompt,
    _extract_json_object,
    _read_preview,
    classify_with_ai,
)
from fs_organizer.config import AIConfig


def ai_config(**overrides) -> AIConfig:
    cfg = AIConfig(
        enabled=True,
        provider="ollama",
        model="llama3.2",
        allowed_subfolders=["Documents", "Images", "Music"],
        extensions=[".xyz", ".abc"],
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def fake_response(payload: dict, status: int = 200):
    """Build a fake urllib response supporting the context-manager protocol."""
    import io

    body = io.BytesIO(json.dumps(payload).encode("utf-8"))

    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()
            return False

        def getcode(self):
            return status

    return FakeResp(body.read())


def make_file(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


class TestExtractJsonObject:
    def test_plain_json(self):
        assert _extract_json_object('{"category": "Music"}') == {"category": "Music"}

    def test_json_with_prose(self):
        text = 'Sure! Here is my answer:\n{"category": "Music"}\nHope that helps.'
        assert _extract_json_object(text) == {"category": "Music"}

    def test_json_in_code_fence(self):
        text = '```json\n{"category": "Images"}\n```'
        assert _extract_json_object(text) == {"category": "Images"}

    def test_nested_object_takes_outer(self):
        text = '{"category": "Music", "meta": {"x": 1}}'
        assert _extract_json_object(text)["category"] == "Music"

    def test_no_json_raises_valueerror(self):
        with pytest.raises(ValueError):
            _extract_json_object("I have no idea what this file is.")

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError):
            _extract_json_object('{"category": "Music"')  # truncated


class TestReadPreview:
    def test_reads_text(self, tmp_path):
        p = make_file(tmp_path, "a.txt", "hello world")
        assert _read_preview(p, 1024) == "hello world"

    def test_truncates_to_max_bytes(self, tmp_path):
        p = make_file(tmp_path, "a.txt", "x" * 5000)
        assert len(_read_preview(p, 100)) == 100

    def test_binary_replaced_lossily(self, tmp_path):
        p = tmp_path / "b.bin"
        p.write_bytes(b"\xff\xfe\x00binary")
        preview = _read_preview(p, 1024)
        assert isinstance(preview, str)  # decode with errors="replace", never raises

    def test_missing_file_returns_empty(self, tmp_path):
        assert _read_preview(tmp_path / "ghost.txt", 1024) == ""


class TestBuildPrompt:
    def test_contains_categories_and_filename(self, tmp_path):
        p = make_file(tmp_path, "report.xyz", "data")
        prompt = _build_prompt(p, ai_config())
        assert "report.xyz" in prompt
        assert "Documents, Images, Music" in prompt
        assert ".xyz" in prompt
        assert "data" in prompt

    def test_default_categories_when_none_allowed(self, tmp_path):
        p = make_file(tmp_path, "report.xyz", "data")
        cfg = ai_config(allowed_subfolders=[])
        prompt = _build_prompt(p, cfg)
        assert "Documents, Other" in prompt


class TestClassifyWithAI:
    def _patch_urlopen(self, monkeypatch, payload):
        from fs_organizer import ai

        calls = {}

        def fake_urlopen(req, timeout=None):
            calls["url"] = req.full_url
            calls["headers"] = dict(req.header_items())
            calls["payload"] = json.loads(req.data.decode("utf-8"))
            return fake_response(payload)

        monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
        return calls

    def test_disabled_returns_none(self, tmp_path, monkeypatch):
        from fs_organizer import ai

        def fail(*a, **k):
            raise AssertionError("network must not be touched when disabled")

        monkeypatch.setattr(ai.urllib.request, "urlopen", fail)
        p = make_file(tmp_path, "f.xyz", "content")
        assert classify_with_ai(p, ai_config(enabled=False)) is None

    def test_ollama_success(self, tmp_path, monkeypatch):
        calls = self._patch_urlopen(
            monkeypatch, {"message": {"content": '{"category": "Music"}'}}
        )
        p = make_file(tmp_path, "f.xyz", "content")
        result = classify_with_ai(p, ai_config())
        assert result == "Music"
        assert calls["url"] == "http://localhost:11434/api/chat"
        assert calls["payload"]["stream"] is False
        assert calls["payload"]["format"] == "json"

    def test_openai_success(self, tmp_path, monkeypatch):
        cfg = ai_config(provider="openai", api_key="sk-test")
        calls = self._patch_urlopen(
            monkeypatch, {"choices": [{"message": {"content": '{"category": "Images"}'}}]}
        )
        p = make_file(tmp_path, "f.xyz", "content")
        result = classify_with_ai(p, cfg)
        assert result == "Images"
        assert calls["url"] == "https://api.openai.com/v1/chat/completions"
        headers = {k.lower(): v for k, v in calls["headers"].items()}
        assert headers["authorization"] == "Bearer sk-test"

    def test_openai_key_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        calls = self._patch_urlopen(
            monkeypatch, {"choices": [{"message": {"content": '{"category": "Music"}'}}]}
        )
        p = make_file(tmp_path, "f.xyz", "content")
        cfg = ai_config(provider="openai", api_key=None)
        assert classify_with_ai(p, cfg) == "Music"
        headers = {k.lower(): v for k, v in calls["headers"].items()}
        assert headers["authorization"] == "Bearer sk-env"

    def test_category_stripped_of_quotes(self, tmp_path, monkeypatch):
        self._patch_urlopen(
            monkeypatch, {"message": {"content": '{"category": "\\"Music\\""}'}}
        )
        p = make_file(tmp_path, "f.xyz", "content")
        assert classify_with_ai(p, ai_config()) == "Music"

    def test_disallowed_category_returns_none(self, tmp_path, monkeypatch):
        self._patch_urlopen(
            monkeypatch, {"message": {"content": '{"category": "NuclearLaunchCodes"}'}}
        )
        p = make_file(tmp_path, "f.xyz", "content")
        assert classify_with_ai(p, ai_config()) is None

    def test_empty_category_returns_none(self, tmp_path, monkeypatch):
        self._patch_urlopen(monkeypatch, {"message": {"content": "{}"}})
        p = make_file(tmp_path, "f.xyz", "content")
        assert classify_with_ai(p, ai_config()) is None

    def test_garbage_reply_returns_none(self, tmp_path, monkeypatch):
        self._patch_urlopen(monkeypatch, {"message": {"content": "not json at all"}})
        p = make_file(tmp_path, "f.xyz", "content")
        assert classify_with_ai(p, ai_config()) is None

    def test_network_error_returns_none(self, tmp_path, monkeypatch):
        import urllib.error

        from fs_organizer import ai

        def fail(*a, **k):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(ai.urllib.request, "urlopen", fail)
        p = make_file(tmp_path, "f.xyz", "content")
        assert classify_with_ai(p, ai_config()) is None

    def test_missing_message_key_returns_none(self, tmp_path, monkeypatch):
        self._patch_urlopen(monkeypatch, {"nope": True})
        p = make_file(tmp_path, "f.xyz", "content")
        assert classify_with_ai(p, ai_config()) is None

    def test_unknown_provider_returns_none(self, tmp_path):
        p = make_file(tmp_path, "f.xyz", "content")
        assert classify_with_ai(p, ai_config(provider="claude")) is None
