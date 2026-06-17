"""Tests for src/main.py — helpers du pipeline CLI (sans exécuter le pipeline)."""

import io

from src.main import _confirm_iris_download


class TestConfirmIrisDownload:
    def test_assume_yes_returns_true(self):
        assert _confirm_iris_download(["381850999"], assume_yes=True) is True

    def test_non_interactive_returns_false(self, monkeypatch):
        # stdin non-tty (io.StringIO.isatty() -> False) et pas de --yes -> refus
        monkeypatch.setattr("sys.stdin", io.StringIO())
        assert _confirm_iris_download(["381850999"], assume_yes=False) is False

    def test_interactive_yes(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "o")
        assert _confirm_iris_download(["381850999"], assume_yes=False) is True

    def test_interactive_no(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "")
        assert _confirm_iris_download(["381850999"], assume_yes=False) is False
