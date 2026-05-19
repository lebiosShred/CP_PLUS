import logging

import requests
from flask import Flask, request, Response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
)
log = logging.getLogger("cp-plus-gateway")

PRODUCT_URL = "http://127.0.0.1:8000/product/run"
RFP_URL = "http://127.0.0.1:8001/run"
MATCHER_URL = "http://127.0.0.1:8002/matcher/run"

app = Flask(__name__)
log.info("Gateway Flask app created")

def log_request():
    log.info("Incoming %s %s", request.method, request.path)


def _forward(target_url: str) -> Response:
    """Forward the incoming request body and headers to target_url."""
    try:
        log.info("Forwarding request to %s", target_url)

    
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() != "host"
        }

        resp = requests.post(
                target_url,
                headers=headers,
                data=request.data,
                timeout=120,
            )

        log.info(
            "Upstream %s returned status %s",
            target_url,
            resp.status_code,
        )

        return Response(
            resp.content,
            status=resp.status_code,
            content_type=resp.headers.get("Content-Type", "application/json"),
        )
    except Exception as e:
        log.exception("Error forwarding to %s", target_url)
        return Response(
            f'{{"error": "Gateway error: {e}"}}',
            status=500,
            mimetype="application/json",
        )


@app.post("/product/run")
def product_run():
    log.info("Gateway /product/run called")
    return _forward(PRODUCT_URL)


@app.post("/rfp/run")
def rfp_run():
    log.info("Gateway /rfp/run called")
    return _forward(RFP_URL)

@app.post("/matcher/run")
def matcher_run():
    log.info("Gateway /matcher/run called")
    return _forward(MATCHER_URL)

if __name__ == "__main__":
    log.info("Starting gateway on 0.0.0.0:8100")
    app.run(host="0.0.0.0", port=8100, debug=True)
