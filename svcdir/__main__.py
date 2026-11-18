"""WSGI entry point used by Docker Compose and for local runs."""

from __future__ import annotations

import os

from .web import create_app

app = create_app()


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
