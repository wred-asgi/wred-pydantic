"""Install the reviewed Swagger UI release, verifying npm's pinned archive digest."""

import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path
from urllib.request import urlopen

VERSION = "5.34.1"
INTEGRITY = "VSr8fXfI93obYaQicLJbLUKGRxDojj/SfRJDDLCioGTUBj4BK/Nj4eE9jCeCAnJxVLi7iS+08PbiZLyrQKBkJQ=="
URL = f"https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-{VERSION}.tgz"
ASSETS = Path(__file__).resolve().parents[1] / "wred_pydantic" / "assets"


def main():
    with urlopen(URL, timeout=30) as response:
        archive = response.read(20 * 1024 * 1024)
    if base64.b64encode(hashlib.sha512(archive).digest()).decode() != INTEGRITY:
        raise RuntimeError("Swagger archive integrity mismatch")
    hashes = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
        for source, destination in (
            ("swagger-ui-bundle.js", "swagger-ui-bundle.js"),
            ("swagger-ui.css", "swagger-ui.css"),
            ("LICENSE", "LICENSE.swagger"),
            ("NOTICE", "NOTICE.swagger"),
        ):
            member = package.extractfile("package/" + source)
            if member is None:
                raise RuntimeError(f"Missing {source}")
            content = member.read()
            (ASSETS / destination).write_bytes(content)
            hashes[destination] = hashlib.sha256(content).hexdigest()
    (ASSETS / "manifest.json").write_text(
        json.dumps(
            {
                "package": "swagger-ui-dist",
                "version": VERSION,
                "source": URL,
                "integrity": "sha512-" + INTEGRITY,
                "sha256": hashes,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
