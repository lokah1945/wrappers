import pytest

from common.model.validation import validate_catalog_entries, validate_observation


def test_catalog_validation_rejects_control_chars_and_oversized_metadata():
    with pytest.raises(ValueError):
        validate_catalog_entries([{"id": "provider/\nmodel"}])
    with pytest.raises(ValueError):
        validate_catalog_entries([{"id": "provider/model", "metadata": "x" * 70000}])


def test_observation_validation_rejects_unknown_state_and_invalid_model():
    with pytest.raises(ValueError):
        validate_observation("../model", "account", "available", "OK", "", "")
    with pytest.raises(ValueError):
        validate_observation("provider/model", "account", "invented", "", "", "")
