"""Explicit Pydantic v2 validation and serialization for Wred."""

from contextvars import ContextVar
from functools import wraps

from pydantic import TypeAdapter, ValidationError

__version__ = "0.1.0"

# Task-local: nested input decorators share only the original route arguments.
_route_context = ContextVar("wred_pydantic_route", default=None)


class _BodyTooLarge(Exception):
    pass


async def _read_body(request, max_bytes):
    if max_bytes is None:
        return await request.body()
    receive = request.receive
    size = 0

    async def limited_receive():
        nonlocal size
        message = await receive()
        size += len(message.get("body", b""))
        if size > max_bytes:
            raise _BodyTooLarge
        return message

    request.receive = limited_receive
    try:
        data = await request.body()
    finally:
        request.receive = receive
    # Also enforce the limit if middleware already cached the body.
    if len(data) > max_bytes:
        raise _BodyTooLarge
    return data


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


def _validate(model, source, argument, max_bytes=None):
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
            context = _route_context.get()
            route_kwargs = (
                context[1] if context is not None and context[0] is request else kwargs
            )
            try:
                if source == "body":
                    value = adapter.validate_json(await _read_body(request, max_bytes))
                else:
                    value = adapter.validate_python(
                        request.query if source == "query" else route_kwargs
                    )
            except _BodyTooLarge:
                return (
                    {"detail": "Request body too large"},
                    [("content-type", "application/json")],
                    413,
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
            token = _route_context.set((request, route_kwargs))
            try:
                return await handler(request, *args, **kwargs, **{argument: value})
            finally:
                _route_context.reset(token)

        _metadata(wrapped, **{source: adapter.json_schema()})
        return wrapped

    return decorate


def body(model, *, argument="body", max_bytes=1024 * 1024):
    """Validate raw JSON bytes and inject a model as a keyword argument."""
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise ValueError("max_bytes must be a nonnegative integer or None")
    return _validate(model, "body", argument, max_bytes)


def query(model, *, argument="query"):
    """Validate Wred's query mapping (repeated keys retain their last value)."""
    return _validate(model, "query", argument)


def path(model, *, argument="path"):
    """Validate route kwargs, preserving the original kwargs too."""
    return _validate(model, "path", argument)


def response(model, *, status=200, by_alias=True):
    """Validate and JSON-normalize a response body, preserving tuples and ASGI."""
    adapter = TypeAdapter(model)

    def decorate(handler):
        @wraps(handler)
        async def wrapped(request, *args, **kwargs):
            result = await handler(request, *args, **kwargs)
            if callable(result):
                return result
            if isinstance(result, tuple):
                payload, _headers, actual_status = result
                if not 200 <= actual_status < 300 or actual_status in (204, 205):
                    return result
            else:
                payload = result
            normalized = adapter.dump_python(
                adapter.validate_python(payload), mode="json", by_alias=by_alias
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
                            "schema": adapter.json_schema(
                                mode="serialization", by_alias=by_alias
                            )
                        }
                    },
                }
            },
        )
        return wrapped

    return decorate
