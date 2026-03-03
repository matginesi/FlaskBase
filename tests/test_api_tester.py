from __future__ import annotations

def test_resolve_api_public_base_url_prefers_explicit_url(app) -> None:
    from app.services.api_runtime import resolve_api_public_base_url

    app.config["BASE_URL"] = "http://127.0.0.1:5000"
    app.config["API"] = {"PUBLIC_BASE_URL": "http://127.0.0.1:8000"}

    assert resolve_api_public_base_url(app) == "http://127.0.0.1:8000"


def test_auth_api_keys_data_returns_workspace_payload(user_client) -> None:
    response = user_client.get("/auth/api-keys/data")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert isinstance(payload["personal_tokens"], list)
    assert isinstance(payload["revealed_tokens"], list)
    assert "pending_token_count" in payload


def test_api_tester_page_uses_real_auth_api_key_routes(user_client) -> None:
    response = user_client.get("/addons/api_tester/")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "auth/api-keys/data" in html
    assert "/auth/api-keys/data.json" not in html
    assert "auth/api-keys/create.json" in html
    assert "api_tester.run_request" not in html
    assert 'style="display:none;"' not in html
    assert "reference-only" in html


def test_api_tester_run_rejects_disallowed_path(user_client) -> None:
    response = user_client.post(
        "/addons/api_tester/api/run",
        json={"path": "/admin/users", "method": "GET", "token": "", "payload": {}},
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is False
    assert payload["proxy_status"] == 400
    assert payload["result"]["error"] == "path_not_allowed"


def test_api_tester_rejects_token_owned_by_another_user(app, user_client) -> None:
    from app.extensions import db
    from app.models import ApiToken, User

    with app.app_context():
        owner = User.query.filter_by(email="admin@test.com").first()
        assert owner is not None
        token_row, raw_token = ApiToken.create(
            user_id=int(owner.id),
            name="foreign-test-token",
            scopes=["profile:read"],
            created_by_user_id=int(owner.id),
        )
        db.session.add(token_row)
        db.session.commit()

    response = user_client.post(
        "/addons/api_tester/api/run",
        json={"path": "/v1/auth/me", "method": "GET", "token": raw_token, "payload": {}},
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is False
    assert payload["proxy_status"] == 403
    assert payload["result"]["error"] == "token_not_owned_by_current_user"


def test_api_tester_mint_token_returns_raw_token(user_client) -> None:
    response = user_client.post("/addons/api_tester/api/mint-token", json={})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["token"]
    assert payload["token_prefix"]
    assert "api_tester:*" in payload["scopes"]
