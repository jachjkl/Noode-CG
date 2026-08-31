from __future__ import annotations

import argparse
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROUTES = {
    "/api/nodes": "api.json",
    "/api/health": "health.json",
    "/nodes.txt": "nodes.txt",
    "/nodes.json": "nodes.json",
    "/nodes.csv": "nodes.csv",
    "/ip.zip": "ip.zip",
}


def make_handler(output_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NoodeCG/8.0.0"

        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "public, max-age=60")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()

        def do_GET(self) -> None:
            route = urlsplit(self.path).path
            if route in {"/", "/healthz"}:
                payload = json.dumps(
                    {
                        "service": "Noode-CG V8-OnePassForeign",
                        "status": "ok",
                        "routes": sorted(ROUTES),
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self._headers(HTTPStatus.OK, "application/json; charset=utf-8", len(payload))
                self.wfile.write(payload)
                return
            filename = ROUTES.get(route)
            path = output_dir / filename if filename else None
            if not path or not path.is_file():
                payload = b'{"error":"not found"}'
                self._headers(HTTPStatus.NOT_FOUND, "application/json", len(payload))
                self.wfile.write(payload)
                return
            payload = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if path.suffix == ".json":
                content_type = "application/json; charset=utf-8"
            elif path.suffix in {".txt", ".csv"}:
                content_type += "; charset=utf-8"
            self._headers(HTTPStatus.OK, content_type, len(payload))
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"{self.address_string()} - {fmt % args}")

    return Handler


def serve(host: str, port: int, output: str | Path) -> None:
    output_dir = Path(output).resolve()
    server = ThreadingHTTPServer((host, port), make_handler(output_dir))
    print(f"Noode-CG API: http://{host}:{port} (output={output_dir})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve Noode-CG generated outputs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--output", default="output")
    args = parser.parse_args()
    serve(args.host, args.port, args.output)


if __name__ == "__main__":
    main()
