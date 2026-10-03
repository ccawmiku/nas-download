from __future__ import annotations

import http.client


HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade"}


def stream_proxy(handler, host: str, port: int, path: str, rewrite=None, location_prefix: str = "") -> None:
    handler._proxy_response_started = False
    connection = http.client.HTTPConnection(host, port, timeout=120)
    try:
        length = int(handler.headers.get("Content-Length", "0") or 0)
        if length < 0 or length > 5_000_000:
            handler.send_error(413, "Request body too large")
            return
        body = handler.rfile.read(length) if length else None
        blocked = HOP_HEADERS | {"host", "accept-encoding", "x-nas-download-token"}
        headers = {k: v for k, v in handler.headers.items() if k.lower() not in blocked}
        headers["Accept-Encoding"] = "identity"
        connection.request(handler.command, path, body=body, headers=headers)
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "")
        transformed = response.read() if rewrite and "text/html" in content_type.lower() else None
        if transformed is not None:
            transformed = rewrite(transformed, content_type)
        handler.send_response(response.status)
        handler._proxy_response_started = True
        blocked = HOP_HEADERS | {"content-length"}
        if transformed is not None:
            blocked.add("content-encoding")
        for key, value in response.getheaders():
            if key.lower() not in blocked:
                if key.lower() == "location" and value.startswith("/") and location_prefix:
                    value = location_prefix.rstrip("/") + value
                handler.send_header(key, value)
        size = len(transformed) if transformed is not None else response.getheader("Content-Length")
        if size is not None:
            handler.send_header("Content-Length", str(size))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        if handler.command != "HEAD":
            if transformed is not None:
                handler.wfile.write(transformed)
            else:
                while chunk := response.read(64 * 1024):
                    handler.wfile.write(chunk)
    finally:
        connection.close()
