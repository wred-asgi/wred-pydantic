"""In-process ASGI cost comparison; no network or production throughput claims.

Run: python benchmarks/validation.py
"""

import asyncio
import json
import statistics
import time

import pydantic
import wred
from pydantic import BaseModel

from wred_pydantic import body, response


class Item(BaseModel):
    id: int
    name: str
    price: float


def application(validated):
    app = wred.App()
    if validated:

        @body(list[Item])
        @response(list[Item])
        async def handler(request, body):
            return body
    else:

        async def handler(request):
            return await request.json()

    app.route("/", methods=["POST"])(handler)
    return app


async def measure(app, payload, iterations):
    scope = {"type": "http", "method": "POST", "path": "/"}

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            assert message["status"] == 200

    for _ in range(20):
        await app(scope, receive, send)
    samples = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        await app(scope, receive, send)
        samples.append((time.perf_counter_ns() - start) / 1000)
    return statistics.median(samples), sorted(samples)[int(len(samples) * 0.95)]


async def main():
    print(f"Wred {wred.__version__}; Pydantic {pydantic.__version__}")
    print("items bytes mode median_us p95_us")
    for count, iterations in [(1, 5000), (100, 1000), (10000, 100)]:
        payload = json.dumps(
            [{"id": i, "name": "example product", "price": 12.50} for i in range(count)]
        ).encode()
        for validated in (False, True):
            median, p95 = await measure(application(validated), payload, iterations)
            print(
                count,
                len(payload),
                "validated" if validated else "plain",
                f"{median:.1f}",
                f"{p95:.1f}",
            )


if __name__ == "__main__":
    asyncio.run(main())
