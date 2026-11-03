"""Task 10b B6: setup.py's default model and its Ollama version check."""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import setup as setup_script

WARNING = "bonsai-2:4b needs Ollama 0.40.6 or newer; you have {version}. Update Ollama from https://ollama.com."


def test_the_default_model_is_bonsai():
    assert setup_script.MODEL == "bonsai-2:4b"


@pytest.mark.parametrize("version", ["0.40.5", "0.39.9", "0.9.99", "0.40.0", "0.1.0"])
def test_an_older_ollama_gets_the_warning(version):
    assert setup_script.ollama_version_warning(version, "bonsai-2:4b") == WARNING.format(version=version)


@pytest.mark.parametrize("version", ["0.40.6", "0.40.6-rc1"])
def test_exactly_the_minimum_is_fine(version):
    assert setup_script.ollama_version_warning(version, "bonsai-2:4b") is None


@pytest.mark.parametrize("version", ["0.40.7", "0.40.13", "0.41.0", "0.100.0", "1.0.0", "v0.40.13"])
def test_a_newer_ollama_is_fine(version):
    """0.40.13 > 0.40.6 numerically (a string comparison would get this wrong)."""
    assert setup_script.ollama_version_warning(version, "bonsai-2:4b") is None


@pytest.mark.parametrize("version", [None, "", "   ", "garbage", "0.40", "latest", 12, {"version": "0.40.5"}])
def test_an_unreadable_version_prints_nothing(version):
    assert setup_script.ollama_version_warning(version, "bonsai-2:4b") is None


def test_only_bonsai_needs_the_new_ollama():
    assert setup_script.ollama_version_warning("0.30.0", "granite4.1:3b") is None
    assert setup_script.ollama_version_warning("0.30.0", "qwen3:4b") is None


class FakeRequests(types.SimpleNamespace):
    """Just enough of `requests` for check_ollama: GET /api/tags and /api/version."""

    class exceptions:
        class RequestException(Exception):
            pass

    def __init__(self, version=None, version_fails=False, models=("bonsai-2:4b",)):
        super().__init__()
        self._version, self._version_fails, self._models = version, version_fails, models

    def get(self, url, timeout=None):
        class Reply:
            def __init__(inner, payload):
                inner._payload = payload

            def raise_for_status(inner):
                pass

            def json(inner):
                if isinstance(inner._payload, Exception):
                    raise inner._payload
                return inner._payload

        if url.endswith("/api/tags"):
            return Reply({"models": [{"name": m} for m in self._models]})
        if url.endswith("/api/version"):
            if self._version_fails:
                raise FakeRequests.exceptions.RequestException("no version endpoint")
            return Reply({"version": self._version} if self._version is not None else ValueError("not json"))
        raise AssertionError(url)


def run_check(monkeypatch, capsys, **kwargs):
    monkeypatch.setitem(sys.modules, "requests", FakeRequests(**kwargs))
    ok = setup_script.check_ollama()
    return ok, capsys.readouterr().out


def test_check_ollama_warns_about_an_old_ollama_and_carries_on(monkeypatch, capsys):
    ok, out = run_check(monkeypatch, capsys, version="0.40.5")
    assert ok is True                                              # not aborted: the model is pulled
    assert WARNING.format(version="0.40.5") in out
    assert "Ollama is running and bonsai-2:4b is pulled." in out


def test_check_ollama_is_quiet_about_a_new_enough_ollama(monkeypatch, capsys):
    ok, out = run_check(monkeypatch, capsys, version="0.40.13")
    assert ok is True and "needs Ollama" not in out


def test_check_ollama_is_quiet_when_the_version_cannot_be_read(monkeypatch, capsys):
    for kwargs in ({"version_fails": True}, {"version": None}):
        ok, out = run_check(monkeypatch, capsys, **kwargs)
        assert ok is True and "needs Ollama" not in out


def test_the_warning_is_only_for_the_default_model(monkeypatch, capsys):
    monkeypatch.setattr(setup_script, "MODEL", "granite4.1:3b")
    ok, out = run_check(monkeypatch, capsys, version="0.30.0", models=("granite4.1:3b",))
    assert ok is True and "needs Ollama" not in out
