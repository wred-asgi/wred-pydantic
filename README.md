# wred-pydantic

Explicit Pydantic v2 input validation and response conversion for Wred 0.13–0.14.
Python 3.11+. No dependency injection or annotation inference.

```sh
pip install .
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
route parameters. Schema metadata is consumed by `wred-openapi` without a dependency
on that package. Do not register duplicate metadata for the same input source.

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

Run `tox` for the Python 3.12 compatibility matrix (Wred 0.13/0.14 and Pydantic
2.8.0/latest 2.x). Run `python benchmarks/validation.py` after installing this
package to compare plain Wred with request and response validation for 1, 100,
and 10,000 items. It reports in-process ASGI latency, excluding networking and
application I/O. Measure throughput, memory, and tail latency under your expected
concurrency before sizing a production deployment.
