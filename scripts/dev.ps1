# Windows PowerShell helper scripts for common uv commands.
# Usage: ./scripts/dev.ps1 <command>
# Commands: install | run | dev | test | lint | format | check | clean

param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "run", "dev", "test", "lint", "format", "check", "clean")]
    [string]$Command = "dev"
)

switch ($Command) {
    "install" { uv sync }
    "run"     { uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 }
    "dev"     { uv run uvicorn app.main:app --reload }
    "test"    { uv run pytest }
    "lint"    { uv run ruff check . }
    "format"  { uv run ruff format . }
    "check"   { uv run ruff check .; uv run pytest }
    "clean"   {
        Get-ChildItem -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue .pytest_cache, .ruff_cache, .coverage, htmlcov
    }
}
