"""Production Index process. The catalog stays in memory until the cloud adapters."""

from __future__ import annotations

from cap.core.http_ports import Plane, listen
from fastapi import FastAPI

from city_index.app import create_app
from city_index.service import IndexService


def build_app() -> FastAPI:
    return create_app(IndexService())


def main() -> None:
    import uvicorn

    host, port = listen(Plane.INDEX)
    uvicorn.run(build_app(), host=host, port=port)


if __name__ == "__main__":
    main()
