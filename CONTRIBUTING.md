# Contributing to claude-2-mail

## Development Setup

```bash
git clone https://github.com/omkargs/claude-2-mail
cd claude-2-mail
python -m venv venv
source venv/bin/activate
pip install -e .
```

## Running Tests

```bash
pip install pytest
pytest tests/
```

## Code Style

- 4-space indentation
- Type hints on all functions
- Docstrings on all public functions

## Before Submitting PR

1. Run tests: `pytest tests/`
2. Verify syntax: `python -m py_compile gmail_calendar_mcp.py easter-egg/drive_mcp.py`
3. Ensure no personal data or credentials are committed
4. Update CHANGELOG.md if adding features

## Reporting Issues

Open an issue at https://github.com/omkargs/claude-2-mail/issues
