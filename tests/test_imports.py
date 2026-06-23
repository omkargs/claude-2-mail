"""Basic tests to verify package imports and structure."""

import importlib


def test_gmail_calendar_imports():
    """Gmail+Calendar MCP module imports successfully."""
    mod = importlib.import_module("gmail_calendar_mcp")
    assert hasattr(mod, "main_sync")
    assert hasattr(mod, "load_config")
    assert hasattr(mod, "build_search_query")


def test_drive_imports():
    """Drive MCP module imports successfully."""
    # easter-egg directory uses hyphen, so import via importlib
    import importlib.util

    spec = importlib.util.spec_from_file_location("drive_mcp", "easter-egg/drive_mcp.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "main_sync")
    assert hasattr(mod, "get_drive_service")


def test_build_search_query():
    """Search query builder produces valid IMAP criteria."""
    from gmail_calendar_mcp import build_search_query

    assert build_search_query(sender="test@example.com") == 'FROM "test@example.com"'
    assert build_search_query(subject="hello") == 'SUBJECT "hello"'
    assert build_search_query(unread_only=True) == "UNSEEN"
    assert build_search_query() == "ALL"
    # Combined
    q = build_search_query(sender="a@b.com", subject="test", unread_only=True)
    assert 'FROM "a@b.com"' in q
    assert 'SUBJECT "test"' in q
    assert "UNSEEN" in q


def test_imap_escape():
    """IMAP injection prevention."""
    from gmail_calendar_mcp import _imap_escape

    assert _imap_escape('say "hello"') == 'say \\"hello\\"'
    assert _imap_escape("normal") == "normal"
