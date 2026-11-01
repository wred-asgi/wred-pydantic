import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from wred import App

from wred_pydantic import body, path, query, response


class Input(BaseModel):
    name: str = Field(min_length=1, alias="label")


class Query(BaseModel):
    count: int = Field(ge=1)


class Path(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: int


class Output(BaseModel):
    at: datetime
    names: list[str]


async def test_nested_composition_and_errors():
    app = App()

    @app.route("/items/{item_id}", methods=["POST"])
    @body(Input)
    @query(Query)
    @path(Path)
    @response(Output, status=201)
    async def handler(request, item_id, body, query, path):
        assert path.item_id == int(item_id)
        return (
            {
                "at": datetime(2026, 1, 1, tzinfo=UTC),
                "names": [body.name] * query.count,
            },
            [("content-type", "application/json"), ("x-test", "ok")],
            201,
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.post("/items/3?count=2", json={"label": "hello"})
        assert result.status_code == 201 and result.headers["x-test"] == "ok"
        assert result.json() == {
            "at": "2026-01-01T00:00:00Z",
            "names": ["hello", "hello"],
        }
        for url, data in [
            ("/items/3?count=0", {"label": "secret"}),
            ("/items/no?count=1", {"label": "secret"}),
            ("/items/3?count=1", {"label": ""}),
        ]:
            result = await client.post(url, json=data)
            assert result.status_code == 422 and "secret" not in result.text
        assert (await client.post("/items/3?count=1", content=b"{")).status_code == 422


async def test_response_failure_is_not_client_error_and_asgi_untouched():
    @response(Output)
    async def broken(request):
        return {"names": []}

    with pytest.raises(ValidationError):
        await broken(None)

    async def asgi(scope, receive, send):
        pass

    @response(Output)
    async def streaming(request):
        return asgi

    assert await streaming(None) is asgi


@pytest.mark.parametrize("by_alias, key", [(True, "displayName"), (False, "name")])
async def test_response_alias_matches_schema(by_alias, key):
    class Aliased(BaseModel):
        name: str = Field(serialization_alias="displayName")

    app = App()

    @app.route("/")
    @response(Aliased, by_alias=by_alias)
    async def handler(request):
        return {"name": "hello"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.get("/")
    assert result.json() == {key: "hello"}
    schema = handler.__wred_openapi__["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert set(schema["properties"]) == set(result.json())


@pytest.mark.parametrize("status", [302, 400, 404, 422, 500, 204, 205])
async def test_response_preserves_errors_redirects_and_empty_statuses(status):
    app = App()
    payload = b"" if status in (204, 205) else {"detail": "not a success model"}

    @app.route("/")
    @response(Output)
    async def handler(request):
        return payload, [("x-test", "preserved")], status

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.get("/")
    assert result.status_code == status
    assert result.headers["x-test"] == "preserved"
    if status in (204, 205):
        assert result.content == b""
    else:
        assert result.json() == payload


@pytest.mark.parametrize("size, expected", [(16, 200), (17, 413)])
async def test_body_limit_counts_streamed_bytes_and_stops_reading(size, expected):
    app = App()
    consumed = []
    called = []

    @app.route("/", methods=["POST"])
    @body(str, max_bytes=16)
    async def handler(request, body):
        called.append(body)
        return {"value": body}

    async def chunks():
        for chunk in (b'"', b"a" * (size - 2), b'"', b" " * 100):
            consumed.append(chunk)
            yield chunk
            if expected == 200 and len(consumed) == 3:
                return

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.post("/", content=chunks())
    assert result.status_code == expected
    assert len(consumed) == 3
    assert bool(called) == (expected == 200)


async def test_concurrent_requests_keep_original_path_arguments_isolated():
    app = App()
    arrived = 0
    ready = asyncio.Event()

    def pause(handler):
        async def wrapped(request, **kwargs):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=2)
            return await handler(request, **kwargs)

        return wrapped

    @app.route("/{item_id}", methods=["POST"])
    @body(Input)
    @pause
    @path(Path)
    async def handler(request, item_id, body, path):
        return {"id": path.item_id, "name": body.name}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        results = await asyncio.gather(
            *[client.post(f"/{i}", json={"label": str(i)}) for i in (1, 2)]
        )
    assert [r.json() for r in results] == [
        {"id": 1, "name": "1"},
        {"id": 2, "name": "2"},
    ]


@pytest.mark.parametrize("limit, expected", [(3, 413), (None, 200)])
async def test_body_limit_applies_to_cached_body_and_can_be_disabled(limit, expected):
    app = App()

    @body(str, max_bytes=limit)
    async def validated(request, body):
        return {"value": body}

    @app.route("/", methods=["POST"])
    async def handler(request):
        await request.body()
        return await validated(request)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.post("/", json="hello")
    assert result.status_code == expected
