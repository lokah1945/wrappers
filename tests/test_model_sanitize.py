from common.model.sanitize import sanitize_error_detail


def test_sanitizer_redacts_credentials_and_request_content():
    text = sanitize_error_detail({
        "status": 404,
        "detail": "Function not found for account acct",
        "authorization": "Bearer super-secret-token",
        "api_key": "nvapi-secret",
        "messages": [{"role": "user", "content": "private prompt"}],
    })
    assert "super-secret-token" not in text
    assert "nvapi-secret" not in text
    assert "private prompt" not in text
    assert "Function not found for account acct" in text


def test_sanitizer_is_bounded():
    text = sanitize_error_detail({"detail": "x" * 10000}, max_chars=128)
    assert len(text) <= 128
