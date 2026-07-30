import pytest
from fastapi.testclient import TestClient

from common.model.validation import validate_provider_name


def test_provider_name_rejects_path_segments():
    with pytest.raises(ValueError):
        validate_provider_name("../../wrappers")


def test_registry_provider_query_rejects_path_segments(tmp_path):
    from tests.test_model_registry_service import load_service
    service = load_service(tmp_path)
    response = TestClient(service.app).get("/v1/models", params={"provider": "../../wrappers"})
    assert response.status_code == 400
