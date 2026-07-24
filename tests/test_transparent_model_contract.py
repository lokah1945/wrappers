from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_nvidia_has_no_inference_model_fallback_path():
    source = (ROOT / "nvidia-python" / "src" / "main.py").read_text()
    forbidden = (
        "_build_fallback_candidates",
        "MODEL_FALLBACK_ENABLED",
        "MODEL_FALLBACK_MAX_HOPS",
        "No candidate models available",
    )
    for marker in forbidden:
        assert marker not in source, marker


def test_transparency_contract_is_explicit_in_docs():
    contract = (ROOT / "WRAPPER_CONTRACT.md").read_text()
    assert "must never select another model" in contract
    assert "model substitution" in contract.lower()


def test_all_wrappers_have_no_alternative_model_execution_markers():
    sources = [
        ROOT / "nvidia-python" / "src" / "main.py",
        ROOT / "nous" / "wrapper_nous.py",
        ROOT / "opencode" / "src" / "main.py",
        ROOT / "blackbox" / "src" / "main.py",
    ]
    forbidden = ("select_fallback_model", "select_alternative_model")
    for path in sources:
        source = path.read_text()
        for marker in forbidden:
            assert marker not in source, f"{marker} found in {path}"


def test_concrete_requests_do_not_mutate_dynamic_alias_state():
    for path in (
        ROOT / "nvidia-python" / "src" / "main.py",
        ROOT / "nous" / "wrapper_nous.py",
        ROOT / "opencode" / "src" / "main.py",
        ROOT / "blackbox" / "src" / "main.py",
    ):
        source = path.read_text()
        assert "set_dynamic_alias_target(m)" not in source
