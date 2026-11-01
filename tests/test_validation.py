from datetime import datetime, timezone
import httpx
import pytest
from pydantic import BaseModel, Field, ValidationError
from wred import App
from wred_pydantic import body, query, path, response


class Input(BaseModel):
    name: str = Field(min_length=1, alias="label")


class Query(BaseModel):
    count: int = Field(ge=1)


class Path(BaseModel):
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
                "at": datetime(2026, 1, 1, tzinfo=timezone.utc),
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
