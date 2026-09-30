"""Agent access: the API keys an agent presents, and the MCP endpoint it calls."""

from __future__ import annotations

import json

import pytest

from apps.api.mcp.tools import TOOLS

# The surface Muse can reach. Adding a name here is the deliberate act of
# widening what an agent may do to this install — the point of the test.
PINNED_TOOLS = {
    "overview",
    "start_upload",
    "list_footage",
    "cut_reels",
    "make_long_video",
    "check_job",
}


async def _mint(api_client, name: str = "Muse on my phone") -> str:
    resp = await api_client.post("/api/v1/api-keys", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _rpc(api_client, token, method, params=None, msg_id=1):
    body: dict = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    return await api_client.post("/mcp", json=body, headers=_bearer(token))


async def _call(api_client, token, name, arguments=None):
    resp = await _rpc(api_client, token, "tools/call", {"name": name, "arguments": arguments or {}})
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    if result["isError"]:
        return result, result["content"][0]["text"]
    return result, json.loads(result["content"][0]["text"])


# --- keys ---------------------------------------------------------------------


async def test_key_is_shown_once_and_stored_hashed(api_client) -> None:
    token = await _mint(api_client)
    assert token.startswith("rf_")

    listed = (await api_client.get("/api/v1/api-keys")).json()["keys"]
    assert len(listed) == 1
    assert "token" not in listed[0]
    assert listed[0]["prefix"] == token[:11]
    assert listed[0]["last_used_at"] is None

    from apps.api.services.api_keys import hash_token

    # Whatever is on disk must not be the token itself.
    assert hash_token(token) != token


async def test_revoked_key_stops_working(api_client) -> None:
    token = await _mint(api_client)
    assert (await _rpc(api_client, token, "ping")).status_code == 200

    key_id = (await api_client.get("/api/v1/api-keys")).json()["keys"][0]["id"]
    revoked = await api_client.post(f"/api/v1/api-keys/{key_id}/revoke")
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]

    assert (await _rpc(api_client, token, "ping")).status_code == 401


async def test_use_stamps_last_used_at(api_client) -> None:
    token = await _mint(api_client)
    await _rpc(api_client, token, "ping")
    assert (await api_client.get("/api/v1/api-keys")).json()["keys"][0]["last_used_at"]


async def test_connection_info_reports_whether_a_phone_could_reach_it(
    api_client, monkeypatch
) -> None:
    from apps.api.settings import settings

    # No tunnel: the URL is honest about being local, which no phone can reach.
    monkeypatch.setattr(settings, "public_media_base", "")
    monkeypatch.setattr(settings, "public_api_base", "http://localhost:8001")
    info = (await api_client.get("/api/v1/api-keys/connection")).json()
    assert info == {"mcp_url": "http://localhost:8001/mcp", "reachable_publicly": False}

    # With the tunnel up, this is the URL to paste into the agent.
    monkeypatch.setattr(settings, "public_media_base", "https://reels.example.com/")
    info = (await api_client.get("/api/v1/api-keys/connection")).json()
    assert info == {"mcp_url": "https://reels.example.com/mcp", "reachable_publicly": True}


# --- transport ------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer rf_not-a-real-key-at-all"}, {"Authorization": "Bearer xx"}],
)
async def test_mcp_requires_a_live_key(api_client, headers) -> None:
    resp = await api_client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers
    )
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == 'Bearer realm="reelforge"'


async def test_no_streams_and_no_sessions(api_client) -> None:
    for verb in ("get", "delete"):
        resp = await getattr(api_client, verb)("/mcp")
        assert resp.status_code == 405
        assert resp.headers["allow"] == "POST"
    assert "stream" in (await api_client.get("/mcp")).json()["error"]
    assert "stateless" in (await api_client.delete("/mcp")).json()["error"]


async def test_initialize_echoes_a_supported_protocol_version(api_client) -> None:
    token = await _mint(api_client)
    result = (
        await _rpc(api_client, token, "initialize", {"protocolVersion": "2025-03-26"})
    ).json()["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "reelforge"
    assert "overview" in result["instructions"]

    # An unknown version falls back to the newest we speak, rather than failing.
    odd = (await _rpc(api_client, token, "initialize", {"protocolVersion": "1999-01-01"})).json()
    assert odd["result"]["protocolVersion"] == "2025-06-18"


async def test_notifications_get_202_and_no_body(api_client) -> None:
    token = await _mint(api_client)
    resp = await api_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_bearer(token),
    )
    assert resp.status_code == 202


async def test_batch_returns_one_response_per_request(api_client) -> None:
    token = await _mint(api_client)
    resp = await api_client.post(
        "/mcp",
        json=[
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ],
        headers=_bearer(token),
    )
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()]
    assert ids == [1, 2]  # the notification contributes nothing


async def test_unknown_method_and_malformed_message(api_client) -> None:
    token = await _mint(api_client)
    unknown = (await _rpc(api_client, token, "tools/nope")).json()
    assert unknown["error"]["code"] == -32601

    bad = await api_client.post("/mcp", json={"id": 1, "method": "ping"}, headers=_bearer(token))
    assert bad.json()["error"]["code"] == -32600


async def test_unknown_tool_is_a_protocol_error(api_client) -> None:
    token = await _mint(api_client)
    resp = await _rpc(api_client, token, "tools/call", {"name": "delete_everything"})
    assert resp.json()["error"]["code"] == -32602


# --- the tool surface -------------------------------------------------------------


async def test_tools_list_is_pinned(api_client) -> None:
    token = await _mint(api_client)
    tools = (await _rpc(api_client, token, "tools/list")).json()["result"]["tools"]
    assert {t["name"] for t in tools} == PINNED_TOOLS == {t.name for t in TOOLS}
    assert all(t["inputSchema"]["type"] == "object" for t in tools)
    assert all(t["description"] and t["title"] for t in tools)


@pytest.mark.parametrize("word", ["publish", "delete", "remove", "destroy", "key"])
def test_no_publishing_or_deleting_tools(word) -> None:
    """Publishing stays in the dashboard, where the video is watched first;
    nothing an agent holds may delete footage or mint itself another key."""
    assert not [t for t in TOOLS if word in t.name]


def test_read_tools_are_flagged_read_only() -> None:
    for tool in TOOLS:
        if not tool.mutates:
            annotations = tool.listing()["annotations"]
            assert annotations["readOnlyHint"] is True
            assert annotations["destructiveHint"] is False


# --- overview ---------------------------------------------------------------------


async def test_overview_reports_projects_and_their_video_count(api_client) -> None:
    token = await _mint(api_client)
    empty = (await _call(api_client, token, "overview"))[1]
    assert empty == {"projects": [], "projectCount": 0, "shown": 0}

    await api_client.post("/api/v1/projects", json={"name": "skimboard"})
    result, data = await _call(api_client, token, "overview")
    assert data["projectCount"] == 1
    assert data["projects"][0]["name"] == "skimboard"
    assert data["projects"][0]["videoClips"] == 0
    # Structured content rides alongside the text, for clients that read it.
    assert result["structuredContent"] == data


async def test_tool_failures_come_back_as_tool_output(api_client, monkeypatch) -> None:
    """A refusal the agent can read and explain, never a transport error."""
    from apps.api.mcp import tools as tools_mod

    async def _boom(ctx, args):
        raise tools_mod.ToolError("nothing to do here")

    monkeypatch.setattr(tools_mod.TOOLS_BY_NAME["overview"], "run", _boom)
    token = await _mint(api_client)
    result, text = await _call(api_client, token, "overview")
    assert result["isError"] is True
    assert text == "ReelForge refused this: nothing to do here"
