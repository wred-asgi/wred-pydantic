# wred-pydantic

Pydantic v2 input validation, response conversion, and automatic OpenAPI 3.1.1
documentation with Swagger UI for Wred 0.13–0.15. Python 3.11+.
No dependency injection or annotation inference.

```sh
pip install .
```

```python
from pydantic import BaseModel, Field
from wred import App
from wred_pydantic import OpenAPI, body, query, path, response


class Product(BaseModel):
    name: str = Field(min_length=1)


app = App()


@app.route("/products", methods=["POST"])
@body(Product)
@response(Product, status=201)
async def create(request, body):
    return body, [("content-type", "application/json")], 201


OpenAPI(app, title="Products API", version="1.0.0").mount()
```

Open `/docs` for interactive Swagger UI or `/openapi.json` for the generated
document.

- `body(Model, argument='body', max_bytes=1048576)` validates raw JSON bytes and
  injects a keyword argument. Bodies over the limit return 413. Set `max_bytes=None`
  to disable the limit, or supply a nonnegative byte count.
- `query(Model, argument='query')` validates Wred's query mapping. Duplicate query keys
  retain the last value; array query syntax is not inferred.
- `path(Model, argument='path')` validates route kwargs and injects a model while
  forwarding the original route kwargs. Handlers must accept both.
- `response(Model, status=200, by_alias=True)` validates and serializes successful
  handler returns. Serialization aliases are used in both JSON and schema; set
  `by_alias=False` to use field names in both.
  `Model` may also be a supported type such as `list[Product]`. `status` describes
  OpenAPI metadata; the decorator does not change the returned HTTP status.

Invalid input returns 422 with `detail` and `errors` containing locations and types,
without raw inputs or custom validator messages. Model settings control coercion,
strictness, aliases, and extra fields. Invalid server responses raise Pydantic's
`ValidationError`; they are not converted to client errors. Tuples retain headers
and status. Non-2xx responses, 204/205 responses, and callable ASGI responses pass
through without validation. Successful payloads are validated regardless of the
metadata `status` value.

Use `@app.route` outermost, then input decorators, then response conversion.
Place exception mapping outside response conversion to handle response validation
failures. Injected argument names cannot collide with
route parameters. Do not register duplicate metadata for the same input source.

## OpenAPI and Swagger UI

`OpenAPI(app, title='Wred API', version='1.0.0', security_schemes=None)` reads Wred's
current fixed and dynamic routes. `body`, `query`, `path`, and `response` decorators
provide request and response schemas, constraints, required fields, and aliases
automatically. Nested, recursive, and discriminated model references become reusable
schema components. Input validation adds a 422 response; limited bodies also add
413. Routes without model metadata get string path parameters and a generic 200
response. Handler annotations are not inspected.

Call `.mount(schema_path='/openapi.json', swagger_path='/docs',
assets_path='/_openapi')` once to register the schema, UI, and asset endpoints.
Paths are configurable and existing routes are never overwritten. Set
`swagger_path=None` to serve only the schema. Documentation routes are public and
excluded from the generated document. Routes registered after mounting are included
automatically. `.document()` returns a fresh dictionary for exporting the schema
without mounting any endpoints.

Swagger UI 5.34.1 JavaScript and CSS are bundled locally under
`wred_pydantic/assets` with their Apache-2.0 license. The UI needs no CDN or external
validator. ASGI `root_path` is applied to schema, asset, and API server URLs so that
interactive requests work behind a mounted URL prefix. The page uses an inline
initialization script; deployments with a restrictive CSP can serve just the schema
and provide their own UI.

Use `operation` for optional summaries, descriptions, tags, operation IDs, additional
responses, parameters, or security requirements:

```python
from wred_pydantic import operation


@app.route("/products/{product_id}")
@operation(summary="Read a product", tags=["products"])
@response(Product)
async def read(request, product_id):
    return {"name": "Book"}
```

`operation(responses={404: {'description': 'Product not found'}})` adds explicit
OpenAPI response objects; response values can also be Pydantic-compatible types.
Security requirements use standard OpenAPI objects, with schemes configured through
`OpenAPI(..., security_schemes=...)`. Conflicting metadata, parameters, duplicate
explicit operation IDs, and missing security schemes raise `ValueError`.

Metadata supplied by other Wred extensions is also consumed through the shared
`handler.__wred_openapi__` convention.

Run `pip install -e '.[test]'` and `pytest` inside this repository.

The body limit counts actual streamed bytes, including requests without a
Content-Length header, and stops reading when exceeded. Cached bodies are also
checked, but middleware that reads the body first has already allocated that
memory. Configure body limits and read timeouts at the server or reverse proxy
as well. The decorator cannot prevent the ASGI server from buffering a large
individual chunk before delivering it.

Constrain collection lengths, string lengths, and nesting in your models, and
paginate large responses. Validation and serialization run synchronously in the
async worker; expensive custom validators and large payloads delay other requests
on that worker. The body limit is not a CPU or response-size limit. Model instances
follow Pydantic's revalidation policy; use `revalidate_instances='always'` if your
application returns models that may have been mutated after construction.

Run `tox` for the Python 3.11/3.14 compatibility matrix (Wred 0.13/0.14/0.15 and Pydantic
2.8.0/latest 2.x). Run `python benchmarks/validation.py` after installing this
package to compare plain Wred with request and response validation for 1, 100,
and 10,000 items. It reports in-process ASGI latency, excluding networking and
application I/O. Measure throughput, memory, and tail latency under your expected
concurrency before sizing a production deployment.

## Extension compatibility

Follows the [Wred extension contract v1.1](https://github.com/wred-asgi/.github/blob/main/EXTENSIONS.md).
Keep `@app.route` outermost for
route decorators; register routes on the inner Wred app when using ASGI wrappers.

Input decorators share original route kwargs with pagination, so their injected
values do not enter path validation. Use distinct `argument=` names.

Bundled assets are loaded once at mount time, avoiding filesystem reads on requests.
`wred_pydantic/assets/manifest.json` records the upstream archive integrity and file
SHA-256 digests. Run `python scripts/update_swagger.py` to reproduce the pinned
release; review upstream security changes before changing its version and digest.
Python dependency auditing does not inspect JavaScript bundled inside Swagger UI.
