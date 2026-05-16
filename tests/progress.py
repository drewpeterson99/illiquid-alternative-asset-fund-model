"""Test-run progress output (use with pytest -s)."""


def progress(message: str) -> None:
    print(message, flush=True)
