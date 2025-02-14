import logging
from datetime import datetime
from urllib.parse import quote

from quart import Quart, Response, g, render_template, request
from quart.logging import default_handler
from werkzeug.wrappers.response import Response as WerkzeugResponse

from geminiportal.favicons import favicon_cache
from geminiportal.handlers import handle_proxy_response
from geminiportal.protocols import build_proxy_request
from geminiportal.protocols.base import ProxyError, ProxyResponseSizeError
from geminiportal.protocols.gemini import GeminiResponse
from geminiportal.urls import URLReference
from geminiportal.utils import describe_tls_cert

logger = logging.getLogger("geminiportal")
logger.setLevel(logging.INFO)
logger.addHandler(default_handler)

app = Quart(__name__)
app.config.from_prefixed_env()


@app.errorhandler(ProxyResponseSizeError)
async def handle_proxy_size_error(e):
    content = await render_template("proxy/errors/size-limit.html", error=e)
    return Response(content, status=500)


@app.errorhandler(ValueError)
async def handle_value_error(e) -> Response:
    content = await render_template("proxy/errors/gateway.html", error=e)
    return Response(content, status=400)


@app.errorhandler(ProxyError)
async def handle_proxy_error(e):
    content = await render_template("proxy/errors/gateway.html", error=e)
    return Response(content, status=500)


@app.context_processor
def inject_context():
    kwargs = {}
    if "response" in g:
        kwargs["response"] = g.response
        kwargs["status"] = g.response.status_string
        kwargs["meta"] = g.response.meta
        if hasattr(g.response, "tls_cert"):
            kwargs["cert_url"] = g.response.url.get_proxy_url(crt=1)
    if "url" in g:
        kwargs["url"] = g.url.get_url()
        kwargs["root_url"] = g.url.get_root_proxy_url()
        kwargs["parent_url"] = g.url.get_parent_proxy_url() or kwargs["root_url"]
        kwargs["raw_url"] = g.url.get_proxy_url(raw=1)
        kwargs["inline_url"] = g.url.get_proxy_url(inline=1)
    if "favicon" in g and g.favicon:
        kwargs["favicon"] = g.favicon
    return kwargs


@app.route("/robots.txt")
async def robots() -> Response:
    return await app.send_static_file("robots.txt")


@app.route("/about")
async def about() -> Response:
    now = datetime.utcnow()
    content = await render_template("about.html", year=now.year)
    return Response(content)


@app.route("/changes")
async def changes() -> Response:
    content = await render_template("changes.html")
    return Response(content)


@app.route("/")
async def home() -> Response | WerkzeugResponse:
    address = request.args.get("url")
    if address:
        # URL was provided via the address bar, redirect to the canonical endpoint
        proxy_url = URLReference(address).get_proxy_url(external=False)
        return app.redirect(proxy_url)

    content = await render_template("home.html", url="gemini://gemini.circumlunar.space")
    return Response(content)


@app.route("/<scheme>/<netloc>/", endpoint="proxy-netloc")
@app.route("/<scheme>/<netloc>/<path:path>", endpoint="proxy-path")
async def proxy(
    scheme: str = "gemini", netloc: str | None = None, path: str | None = None
) -> Response | WerkzeugResponse:
    """
    The main entrypoint for the web proxy.
    """
    address = request.args.get("url")
    if address:
        # URL was provided via the address bar, redirect to the canonical endpoint
        proxy_url = URLReference(address).get_proxy_url(external=False)
        return app.redirect(proxy_url)

    g.url = URLReference(f"{scheme}://{netloc}{'' if path is None else '/' + path}")

    query = request.args.get("q")
    if query:
        # Query was provided via the input box, redirect to the canonical endpoint
        g.url.query = quote(query)
        proxy_url = g.url.get_proxy_url(external=False)
        return app.redirect(proxy_url)

    proxy_request = build_proxy_request(g.url)
    response = await proxy_request.get_response()
    g.response = response

    g.favicon = favicon_cache.check(g.url)

    if request.args.get("raw_crt"):
        if not isinstance(response, GeminiResponse):
            raise ValueError("Cannot download certificate for non-TLS schemes")

        return Response(
            response.tls_cert,
            content_type="application/x-x509-ca-cert",
            headers={
                "Content-Disposition": f"attachment; filename={request.host}.cer",
            },
        )

    if request.args.get("crt"):
        if not hasattr(response, "tls_cert"):
            raise ValueError("Cannot download certificate for non-TLS schemes")

        # Consume the request, so we can check for the close_notify signal
        await response.get_body()

        cert_description = await describe_tls_cert(response.tls_cert)

        content = await render_template(
            "proxy/tls-context.html",
            cert_description=cert_description,
            raw_cert_url=g.url.get_proxy_url(raw_crt=1),
            tls_close_notify_received=response.tls_close_notify_received,
            tls_version=response.tls_version,
            tls_cipher=response.tls_cipher,
        )
        return Response(content)

    if response.is_input():
        is_secret = response.status == "11"
        content = await render_template("proxy/query.html", is_secret=is_secret)
        return Response(content)

    if response.is_redirect():
        location = g.url.join(response.meta).get_proxy_url()
        return app.redirect(location, 307)

    if response.is_success():
        return await handle_proxy_response(
            response=response,
            raw_data=bool(request.args.get("raw")),
            inline_images=bool(request.args.get("inline")),
        )

    content = await render_template("proxy/response.html")
    return Response(content)


if __name__ == "__main__":
    app.config["DEBUG"] = True
    app.config["SERVER_NAME"] = None
    app.run()
