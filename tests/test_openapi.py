import copy
import re
from typing import Annotated, Literal

import httpx
import pytest
from openapi_spec_validator import validate
from pydantic import BaseModel, Field, create_model
from wred import App

from wred_pydantic import OpenAPI, body, operation, path, query, response


def resolve(document, schema):
    if len(schema.get("allOf", [])) == 1:
        schema = schema["allOf"][0]
    while "$ref" in schema:
        target = document
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        schema = target
    return schema


async def test_generated_schema_and_swagger_are_served_from_live_routes():
    class Details(BaseModel):
        price: int = Field(ge=0)

    class Product(BaseModel):
        name: str = Field(alias="label", serialization_alias="displayName")
        details: Details

    class Filters(BaseModel):
        count: int = Field(ge=1, description="Number of copies")
        sort: str = "name"

    class Route(BaseModel):
        item_id: int

    app = App()
    # Mount before registering routes: there must be no stale schema cache.
    docs = OpenAPI(app, title="Products <API>", version="2").mount()

    @app.route("/products/{item_id}", methods=["POST", "PUT"])
    @operation(summary="Save products", tags=["products"])
    @body(Product)
    @query(Filters)
    @path(Route)
    @response(list[Product], status=201)
    async def save(request, item_id, body, query, path):
        return [body] * query.count, [("content-type", "application/json")], 201

    @app.route("/health")
    async def health(request):
        return {"ok": True}

    metadata = copy.deepcopy(save.__wred_openapi__)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.get("/openapi.json")
        assert result.status_code == 200
        assert result.headers["content-type"] == "application/json"
        document = result.json()
        validate(document)
        assert document["info"] == {"title": "Products <API>", "version": "2"}
        assert set(document["paths"]) == {"/products/{item_id}", "/health"}
        operations = document["paths"]["/products/{item_id}"]
        assert set(operations) == {"post", "put"}
        assert operations["post"]["operationId"] != operations["put"]["operationId"]
        op = operations["post"]
        assert op["summary"] == "Save products" and op["tags"] == ["products"]
        parameters = {(p["in"], p["name"]): p for p in op["parameters"]}
        assert parameters["path", "item_id"]["schema"]["type"] == "integer"
        assert parameters["path", "item_id"]["required"] is True
        assert parameters["query", "count"]["schema"]["minimum"] == 1
        assert parameters["query", "count"]["required"] is True
        assert parameters["query", "sort"]["required"] is False
        assert parameters["query", "sort"]["schema"]["default"] == "name"
        assert set(op["responses"]) == {"201", "413", "422"}
        request_schema = op["requestBody"]["content"]["application/json"]["schema"]
        assert op["requestBody"]["required"] is True
        assert set(request_schema["properties"]) == {"label", "details"}
        details = resolve(document, request_schema["properties"]["details"])
        assert details["properties"]["price"]["minimum"] == 0

        payload = {"label": "Book", "details": {"price": 12}}
        saved = await client.post("/products/3?count=1", json=payload)
        assert saved.status_code == 201
        output = resolve(
            document,
            op["responses"]["201"]["content"]["application/json"]["schema"]["items"],
        )
        assert (
            set(output["properties"])
            == set(saved.json()[0])
            == {"displayName", "details"}
        )
        assert (
            await client.post("/products/3?count=0", json=payload)
        ).status_code == 422
        assert (await client.get("/health")).json() == {"ok": True}

        page = await client.get("/docs")
        assert page.status_code == 200
        assert page.headers["content-type"] == "text/html; charset=utf-8"
        assert "Products &lt;API&gt;" in page.text
        assert 'url:"/openapi.json"' in page.text
        assets = re.findall(r'(?:href|src)="([^"]+)"', page.text)
        assert set(assets) == {
            "/_openapi/swagger-ui.css",
            "/_openapi/swagger-ui-bundle.js",
        }
        for url in assets:
            asset = await client.get(url)
            assert asset.status_code == 200
            if url.endswith(".js"):
                assert asset.headers["content-type"] == "text/javascript"
                assert "SwaggerUIBundle" in asset.text
            else:
                assert asset.headers["content-type"] == "text/css"
                assert ".swagger-ui" in asset.text
        assert (await client.get("/openapi.json")).json() == document
    assert save.__wred_openapi__ == metadata
    assert docs.document() == document


async def test_custom_paths_and_root_path_keep_try_it_out_on_the_mounted_app():
    app = App()
    OpenAPI(app).mount(
        schema_path="/spec/", swagger_path="/reference/", assets_path="/static/docs/"
    )

    @app.route("/{item_id}")
    async def item(request, item_id):
        return {"id": item_id}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, root_path="/api/v2"),
        base_url="http://test",
    ) as client:
        page = await client.get("/api/v2/reference")
        assert page.status_code == 200
        assert 'url:"/api/v2/spec"' in page.text
        urls = re.findall(r'(?:href|src)="([^"]+)"', page.text)
        assert all(url.startswith("/api/v2/static/docs/") for url in urls)
        for url in urls:
            assert (await client.get(url)).status_code == 200
        document = (await client.get("/api/v2/spec")).json()
        validate(document)
        assert document["servers"] == [{"url": "/api/v2"}]
        assert document["paths"]["/{item_id}"]["get"]["parameters"] == [
            {
                "name": "item_id",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        ]


def test_recursive_discriminated_and_same_named_models_have_resolvable_references():
    class Tree(BaseModel):
        children: list["Tree"] = []

    class Cat(BaseModel):
        kind: Literal["cat"]
        lives: int

    class Dog(BaseModel):
        kind: Literal["dog"]
        bark: bool

    Pet = Annotated[Cat | Dog, Field(discriminator="kind")]
    string_value = create_model("Value", value=(str, ...))
    int_value = create_model("Value", value=(int, ...))
    app = App()

    for url, model in [
        ("/tree", Tree),
        ("/pet", Pet),
        ("/text", list[string_value]),
        ("/number", list[int_value]),
    ]:

        @app.route(url, methods=["POST"])
        @body(model, max_bytes=None)
        @response(model)
        async def handler(request, body):
            return body

    document = OpenAPI(app).document()
    validate(document)

    def request_schema(url):
        op = document["paths"][url]["post"]
        assert set(op["responses"]) == {"200", "422"}
        return op["requestBody"]["content"]["application/json"]["schema"]

    tree = resolve(document, request_schema("/tree"))
    assert resolve(document, tree["properties"]["children"]["items"]) == tree
    pet = request_schema("/pet")
    for kind, ref in pet["discriminator"]["mapping"].items():
        assert resolve(document, {"$ref": ref})["properties"]["kind"]["const"] == kind
    for url, expected in [("/text", "string"), ("/number", "integer")]:
        value = resolve(document, request_schema(url)["items"])
        assert value["properties"]["value"]["type"] == expected


def test_recursive_query_model_exposes_parameters_across_pydantic_versions():
    class Filter(BaseModel):
        name: str
        children: list["Filter"] = []

    app = App()

    @app.route("/search")
    @query(Filter)
    async def search(request, query):
        return query.model_dump()

    document = OpenAPI(app).document()
    validate(document)
    parameters = {
        p["name"]: p for p in document["paths"]["/search"]["get"]["parameters"]
    }
    assert parameters["name"]["required"] is True
    assert parameters["children"]["required"] is False
    child = resolve(document, parameters["children"]["schema"]["items"])
    assert child["properties"]["name"]["type"] == "string"


@pytest.mark.parametrize(
    "paths, existing",
    [
        ({}, "/docs"),
        ({"swagger_path": "/openapi.json/"}, None),
        ({"schema_path": "/_openapi/swagger-ui.css"}, None),
        ({"swagger_path": "relative"}, None),
        ({"assets_path": "/{filename}"}, None),
    ],
)
def test_mount_rejects_conflicts_and_invalid_paths_before_registering_routes(
    paths, existing
):
    app = App()
    if existing:

        @app.route(existing)
        async def original(request):
            return {"original": True}

    before = copy.deepcopy(app.routes)
    with pytest.raises(ValueError):
        OpenAPI(app).mount(**paths)
    assert app.routes == before


async def test_schema_only_mount_leaves_ui_and_asset_paths_available():
    app = App()
    docs = OpenAPI(app).mount(swagger_path=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get("/openapi.json")).json() == docs.document()
        for url in (
            "/docs",
            "/_openapi/swagger-ui.css",
            "/_openapi/swagger-ui-bundle.js",
        ):
            assert (await client.get(url)).status_code == 404


def test_explicit_metadata_and_security_are_preserved_and_conflicts_fail():
    app = App()

    @app.route("/private")
    @operation(
        operation_id="read_private",
        security=[{"bearerAuth": []}],
        parameters=[{"name": "q", "in": "query", "schema": {"type": "string"}}],
        responses={401: {"description": "Unauthorized"}, 200: str},
    )
    async def private(request):
        return "secret"

    with pytest.raises(ValueError, match="Missing security scheme"):
        OpenAPI(app).document()
    docs = OpenAPI(
        app, security_schemes={"bearerAuth": {"type": "http", "scheme": "bearer"}}
    )
    document = docs.document()
    validate(document)
    op = document["paths"]["/private"]["get"]
    assert op["operationId"] == "read_private"
    assert op["responses"]["401"]["description"] == "Unauthorized"
    assert op["security"] == [{"bearerAuth": []}]
    with pytest.raises(ValueError, match="Conflicting"):
        operation(operation_id="other")(private)

    app.route("/duplicate")(private)
    with pytest.raises(ValueError, match="Duplicate operationId"):
        docs.document()
