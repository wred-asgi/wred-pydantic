"""Generate OpenAPI 3.1 documents and serve bundled Swagger UI assets."""

import copy
import hashlib
import html
import json
import re
from importlib.resources import files

from pydantic import TypeAdapter


def operation(
    *,
    summary=None,
    description=None,
    operation_id=None,
    tags=None,
    parameters=None,
    body=None,
    responses=None,
    security=None,
):
    """Add optional operation details beyond automatically generated model schemas."""
    values = {
        "summary": summary,
        "description": description,
        "operationId": operation_id,
        "tags": tags,
        "parameters": parameters,
        "body": (
            body
            if isinstance(body, dict) or body is None
            else TypeAdapter(body).json_schema()
        ),
        "security": security,
    }
    if responses is not None:
        values["responses"] = {
            str(status): value
            if isinstance(value, dict)
            else {
                "description": "Response",
                "content": {
                    "application/json": {
                        "schema": TypeAdapter(value).json_schema(mode="serialization")
                    }
                },
            }
            for status, value in responses.items()
        }

    def decorate(handler):
        metadata = copy.deepcopy(getattr(handler, "__wred_openapi__", {}))
        for key, value in values.items():
            if value is None:
                continue
            if key == "responses":
                merged = dict(metadata.get(key, {}))
                for status, definition in value.items():
                    if status in merged and merged[status] != definition:
                        raise ValueError(f"Conflicting response metadata: {status}")
                    merged[status] = definition
                metadata[key] = merged
            elif key == "parameters":
                metadata[key] = list(metadata.get(key, [])) + copy.deepcopy(value)
            else:
                if key in metadata and metadata[key] != value:
                    raise ValueError(f"Conflicting operation metadata: {key}")
                metadata[key] = copy.deepcopy(value)
        handler.__wred_openapi__ = metadata
        return handler

    return decorate


class OpenAPI:
    """Read current Wred routes and their decorators without changing registration."""

    def __init__(
        self, app, *, title="Wred API", version="1.0.0", security_schemes=None
    ):
        self.app = app
        self.title = title
        self.version = version
        self.security_schemes = copy.deepcopy(security_schemes or {})
        self._excluded = set()

    def document(self):
        """Return a fresh document, including routes registered since the last call."""
        components, paths, ids = {}, {}, set()

        def lift(schema):
            schema = copy.deepcopy(schema)
            if not isinstance(schema, dict):
                return schema
            definitions = schema.pop("$defs", {})
            suffix = hashlib.sha256(
                json.dumps(definitions, sort_keys=True).encode()
            ).hexdigest()[:12]
            names = {name: f"{name}_{suffix}" for name in definitions}
            refs = {
                "#/$defs/"
                + name.replace("~", "~0").replace("/", "~1"): "#/components/schemas/"
                + target
                for name, target in names.items()
            }

            def rewrite(value):
                if isinstance(value, dict):
                    result = {key: rewrite(item) for key, item in value.items()}
                    for key in ("$ref", "$dynamicRef"):
                        if isinstance(value.get(key), str):
                            result[key] = refs.get(value[key], value[key])
                    discriminator = result.get("discriminator")
                    if isinstance(discriminator, dict) and "mapping" in discriminator:
                        discriminator["mapping"] = {
                            key: refs.get(ref, ref)
                            for key, ref in discriminator["mapping"].items()
                        }
                    return result
                if isinstance(value, list):
                    return [rewrite(item) for item in value]
                return value

            for name, definition in definitions.items():
                components[names[name]] = rewrite(definition)
            return rewrite(schema)

        def schemas(value):
            if isinstance(value, dict):
                return {
                    key: lift(item) if key == "schema" else schemas(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [schemas(item) for item in value]
            return value

        routes = list(self.app.routes.items()) + [
            ("/".join(path), handlers)
            for path, handlers in self.app.pattern_routes.items()
        ]
        for path, handlers in routes:
            for method, handler in handlers.items():
                method = method.lower()
                if handler in self._excluded or method not in {
                    "get",
                    "put",
                    "post",
                    "delete",
                    "options",
                    "head",
                    "patch",
                    "trace",
                }:
                    continue
                meta = copy.deepcopy(getattr(handler, "__wred_openapi__", {}))
                suffix = hashlib.sha256(path.encode()).hexdigest()[:8]
                op_id = meta.setdefault(
                    "operationId",
                    method + "_" + re.sub(r"\W+", "_", path).strip("_") + "_" + suffix,
                )
                if op_id in ids:
                    raise ValueError(f"Duplicate operationId: {op_id}")
                ids.add(op_id)
                parameters = meta.setdefault("parameters", [])
                validated = any(source in meta for source in ("body", "query", "path"))
                for source in ("query", "path"):
                    model = meta.pop(source, None)
                    if model is not None:
                        model = lift(model)
                        # Pydantic 2.8 wraps recursive roots in allOf; newer
                        # versions emit a direct reference instead.
                        while True:
                            if "$ref" in model:
                                model = components[model["$ref"].rsplit("/", 1)[-1]]
                            elif len(model.get("allOf", [])) == 1:
                                model = model["allOf"][0]
                            else:
                                break
                        for name, schema in model.get("properties", {}).items():
                            parameter = {
                                "name": name,
                                "in": source,
                                "schema": schema,
                                "required": source == "path"
                                or name in model.get("required", []),
                            }
                            if "description" in schema:
                                parameter["description"] = schema["description"]
                            parameters.append(parameter)
                seen = {}
                for parameter in parameters:
                    key = parameter["in"], parameter["name"]
                    if key in seen and seen[key] != parameter:
                        raise ValueError(f"Conflicting parameter {key}")
                    seen[key] = parameter
                for name in re.findall(r"\{([^}]+)\}", path):
                    seen.setdefault(
                        ("path", name),
                        {
                            "name": name,
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    )
                meta["parameters"] = list(seen.values())
                body = meta.pop("body", None)
                if body is not None:
                    meta["requestBody"] = {
                        "required": True,
                        "content": {"application/json": {"schema": body}},
                    }
                responses = meta.setdefault(
                    "responses", {"200": {"description": "Successful response"}}
                )
                if validated:
                    responses.setdefault("422", {"description": "Invalid request"})
                if getattr(handler, "__wred_pydantic_body_limit__", None) is not None:
                    responses.setdefault(
                        "413", {"description": "Request body too large"}
                    )
                for requirement in meta.get("security", []):
                    for scheme in requirement:
                        if scheme not in self.security_schemes:
                            raise ValueError(f"Missing security scheme: {scheme}")
                paths.setdefault(path, {})[method] = schemas(meta)
        return {
            "openapi": "3.1.1",
            "info": {"title": self.title, "version": self.version},
            "paths": paths,
            "components": {
                "schemas": components,
                "securitySchemes": copy.deepcopy(self.security_schemes),
            },
        }

    def mount(
        self,
        *,
        schema_path="/openapi.json",
        swagger_path="/docs",
        assets_path="/_openapi",
    ):
        """Serve schema and local Swagger UI; set swagger_path=None for schema only."""

        def normalize(path):
            if (
                not isinstance(path, str)
                or not path.startswith("/")
                or path.startswith("//")
                or any(char in path for char in "?#{}\\")
                or any(ord(char) < 33 for char in path)
            ):
                raise ValueError(f"Expected an absolute literal route path: {path!r}")
            return path.rstrip("/") or "/"

        schema_path = normalize(schema_path)

        async def schema(request):
            document = self.document()
            root = request.scope.get("root_path", "").rstrip("/")
            if root:
                document["servers"] = [{"url": root}]
            return document

        routes = [(schema_path, schema)]
        if swagger_path is not None:
            swagger_path = normalize(swagger_path)
            assets_path = normalize(assets_path).rstrip("/")

            async def swagger(request):
                root = request.scope.get("root_path", "").rstrip("/")
                spec = json.dumps(root + schema_path).replace("<", "\\u003c")
                asset = html.escape(root + assets_path, quote=True)
                page = (
                    '<!doctype html><html><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    f"<title>{html.escape(self.title)}</title>"
                    f'<link rel="stylesheet" href="{asset}/swagger-ui.css">'
                    '</head><body><div id="swagger-ui"></div>'
                    f'<script src="{asset}/swagger-ui-bundle.js"></script>'
                    "<script>SwaggerUIBundle({"
                    f'url:{spec},dom_id:"#swagger-ui",deepLinking:true,validatorUrl:null'
                    "})</script></body></html>"
                )
                return (
                    page.encode(),
                    [("content-type", "text/html; charset=utf-8")],
                    200,
                )

            routes.append((swagger_path, swagger))
            for name in ("swagger-ui.css", "swagger-ui-bundle.js"):

                def asset_handler(filename):
                    # Read once during configuration, never block the event loop
                    # on filesystem reads for each documentation request.
                    content = (
                        files("wred_pydantic").joinpath("assets", filename).read_bytes()
                    )

                    async def serve(request):
                        mime = (
                            "text/css"
                            if filename.endswith(".css")
                            else "text/javascript"
                        )
                        return (
                            content,
                            [("content-type", mime)],
                            200,
                        )

                    return serve

                routes.append((assets_path + "/" + name, asset_handler(name)))
        seen = set()
        for path, _handler in routes:
            if path in seen or path in self.app.routes:
                raise ValueError(f"Route already exists: {path}")
            seen.add(path)
        for path, handler in routes:
            self._excluded.add(handler)
            self.app.route(path)(handler)
        return self
