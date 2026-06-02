from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse
import requests
from urllib.parse import urljoin

app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/proxy")
def proxy(url: str):

    r = requests.get(url)

    headers = {
        "Access-Control-Allow-Origin": "*"
    }

    if ".m3u8" in url:

        lines = []

        for line in r.text.splitlines():

            if line.startswith("#") or not line.strip():
                lines.append(line)
                continue

            absolute = urljoin(url, line)

            lines.append(
                f"/proxy?url={absolute}"
            )

        return Response(
            "\n".join(lines),
            media_type="application/vnd.apple.mpegurl",
            headers=headers
        )

    return StreamingResponse(
        iter([r.content]),
        media_type=r.headers.get(
            "content-type",
            "application/octet-stream"
        ),
        headers=headers
    )
