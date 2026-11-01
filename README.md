# wred-pydantic

Explicit Pydantic v2 input validation and response conversion for Wred 0.13.
Python 3.11+. No dependency injection or annotation inference.

```sh
pip install ./extensions/wred-pydantic
```

```python
from pydantic import BaseModel, Field
from wred import App
from wred_pydantic import body, query, path, response


class Product(BaseModel):
    name: str = Field(min_length=1)


app = App()


@app.route("/products", methods=["POST"])
@body(Product)
@response(Product, status=201)
async def create(request, body):
    return body, [("content-type", "application/json")], 201
```

- `body(Model, argument='body')` validates raw JSON bytes and injects a keyword argument.
- `query(Model, argument='query')` validates Wred's query mapping. Duplicate query keys
  retain the last value; array query syntax is not inferred.
- `path(Model, argument='path')` validates route kwargs and injects a model while
  forwarding the original route kwargs. Handlers must accept both.
- `response(Model, status=200)` validates and serializes successful handler returns.
  `Model` may also be a supported type such as `list[Product]`. `status` describes
  OpenAPI metadata; the decorator does not change the returned HTTP status.

Invalid input returns 422 with `detail` and `errors` containing locations and types,
without raw inputs or custom validator messages. Model settings control coercion,
strictness, aliases, and extra fields. Invalid server responses raise Pydantic's
`ValidationError`; they are not converted to client errors. Tuples retain headers
and status. Callable ASGI responses pass through.

Use `@app.route` outermost, then input decorators, then response conversion.
Place exception mapping outside response conversion so its error responses are not
validated against the success model. Injected argument names cannot collide with
route parameters. Schema metadata is consumed by `wred-openapi` without a dependency
on that package. Do not register duplicate metadata for the same input source.

Run `pip install -e '.[test]'` and `pytest` inside this repository.
