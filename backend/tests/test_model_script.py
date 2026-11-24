"""scripts/model.sh: switching providers edits only .env, keeps every provider's own key, never prints a key."""
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _root(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "model.sh", tmp_path / "scripts" / "model.sh")
    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=p\nOPENAI_API_KEY=sk-openai-secret\nFORGE_MODEL_NAME=gpt-x\n"
                                   "FORGE_PRICE_INPUT_USD_PER_MILLION=2\nFORGE_PRICE_OUTPUT_USD_PER_MILLION=8\n"
                                   "FORGE_MODEL_BASE_URL=http://stale:1/v1\n")
    return tmp_path


def _run(root: Path, *args: str, stdin: str = ""):
    return subprocess.run(["bash", str(root / "scripts" / "model.sh"), *args], input=stdin, capture_output=True,
                          text=True, timeout=60)


def _env(root: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in (root / ".env").read_text().splitlines() if "=" in line)


def test_switching_to_kimi_asks_its_key_once_keeps_the_openai_key_and_clears_old_prices(tmp_path):
    root = _root(tmp_path)
    done = _run(root, "moonshot", "kimi-k2.6", stdin="mk-moonshot-secret\n")
    assert done.returncode == 0, done.stderr
    env = _env(root)
    assert (env["FORGE_MODEL_PROVIDER"], env["FORGE_MODEL_NAME"]) == ("moonshot", "kimi-k2.6")
    assert env["MOONSHOT_API_KEY"] == "mk-moonshot-secret" and env["OPENAI_API_KEY"] == "sk-openai-secret"
    assert "FORGE_PRICE_INPUT_USD_PER_MILLION" not in env          # a price for gpt-x is wrong for kimi
    assert "FORGE_MODEL_BASE_URL" not in env                       # a stale URL would override the preset
    assert "secret" not in done.stdout + done.stderr               # keys are never echoed

    back = _run(root, "openai", "gpt-x")                           # no stdin: must not ask again
    assert back.returncode == 0 and "already set" in back.stdout
    assert _env(root)["MOONSHOT_API_KEY"] == "mk-moonshot-secret"


def test_ollama_needs_no_key_and_show_lists_keys_without_values(tmp_path):
    root = _root(tmp_path)
    assert _run(root, "ollama", "qwen3:8b").returncode == 0
    shown = _run(root)
    assert "provider: ollama" in shown.stdout and "OPENAI_API_KEY: set" in shown.stdout
    assert "sk-openai-secret" not in shown.stdout


def test_openai_compatible_requires_a_url_and_unknown_providers_are_rejected(tmp_path):
    root = _root(tmp_path)
    assert _run(root, "openai_compatible", "m").returncode == 2
    assert _run(root, "anthropic", "m").returncode == 2
    assert _run(root, "openai_compatible", "m", "http://127.0.0.1:1234/v1").returncode == 0
    assert _env(root)["FORGE_MODEL_BASE_URL"] == "http://127.0.0.1:1234/v1"
