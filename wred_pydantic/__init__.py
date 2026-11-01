"""Explicit Pydantic v2 validation and serialization for Wred."""

from functools import wraps
from pydantic import TypeAdapter, ValidationError

__version__ = "0.1.0"


def _metadata(handler, **entries):
    metadata = dict(getattr(handler, "__wred_openapi__", {}))
    for key, value in entries.items():
        if key == "responses":
            merged = dict(metadata.get(key, {}))
            for status, definition in value.items():
                if status in merged and merged[status] != definition:
                    raise ValueError(f"Conflicting response metadata: {status}")
                merged[status] = definition
            metadata[key] = merged
        else:
            if key in metadata and metadata[key] != value:
                raise ValueError(f"Conflicting {key} metadata")
            metadata[key] = value
    handler.__wred_openapi__ = metadata


def _validate(model, source, argument):
    adapter = TypeAdapter(model)
    if not argument or not argument.isidentifier() or argument == "request":
        raise ValueError("Use a named argument other than request")

    def decorate(handler):
        @wraps(handler)
        async def wrapped(request, *args, **kwargs):
            if argument in kwargs:
                raise TypeError(
                    f"Validated argument conflicts with path parameter: {argument}"
                )
            try:
                if source == "body":
                    value = adapter.validate_json(await request.body())
                else:
                    value = adapter.validate_python(
                        request.query if source == "query" else kwargs
                    )
            except ValidationError as exc:
                errors = [
                    {"loc": list(e["loc"]), "type": e["type"]}
                    for e in exc.errors(
                        include_input=False, include_context=False, include_url=False
                    )
                ]
                return (
                    {"detail": "Invalid request", "errors": errors},
                    [("content-type", "application/json")],
                    422,
                )
            except (UnicodeError, ValueError):
                if source != "query":
                    raise
                return (
                    {"detail": "Invalid query encoding"},
                    [("content-type", "application/json")],
                    422,
                )
            return await handler(request, *args, **kwargs, **{argument: value})

        _metadata(wrapped, **{source: adapter.json_schema()})
        return wrapped

    return decorate


def body(model, *, argument="body"):
    """Validate raw JSON bytes and inject a model as a keyword argument."""
    return _validate(model, "body", argument)


def query(model, *, argument="query"):
    """Validate Wred's query mapping (repeated keys retain their last value)."""
    return _validate(model, "query", argument)


def path(model, *, argument="path"):
    """Validate route kwargs, preserving the original kwargs too."""
    return _validate(model, "path", argument)


def response(model, *, status=200):
    """Validate and JSON-normalize a response body, preserving tuples and ASGI."""
    adapter = TypeAdapter(model)

    def decorate(handler):
        @wraps(handler)
        async def wrapped(request, *args, **kwargs):
            result = await handler(request, *args, **kwargs)
            if callable(result):
                return result
            payload = result[0] if isinstance(result, tuple) else result
            normalized = adapter.dump_python(
                adapter.validate_python(payload), mode="json"
            )
            return (
                (normalized, result[1], result[2])
                if isinstance(result, tuple)
                else normalized
            )

        _metadata(
            wrapped,
            responses={
                str(status): {
                    "description": "Successful response",
                    "content": {
                        "application/json": {
                            "schema": adapter.json_schema(mode="serialization")
                        }
                    },
                }
            },
        )
        return wrapped

    return decorate
