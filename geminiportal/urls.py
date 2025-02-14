from __future__ import annotations

import mimetypes
import os
import os.path
import urllib.parse
from urllib.parse import quote, unquote_to_bytes, urljoin, urlparse, urlunparse

from quart import url_for

# Add custom mimetypes for extensions not defined by the filesystem
mimetypes.add_type("text/gemini", ".gmi")
mimetypes.add_type("text/gemini", ".gemini")
mimetypes.add_type("text/x-rst", ".rst")
mimetypes.add_type("application/gopher-menu", ".goph")


# Patch hardcoded URL schemes to support our niche protocols
def _extend(container: list, schemes: list):
    for scheme in schemes:
        if scheme not in container:
            container.append(scheme)


_extend(
    urllib.parse.uses_relative,
    ["gemini", "gopher", "gophers", "spartan", "text", "finger"],
)
_extend(
    urllib.parse.uses_netloc,
    ["gemini", "gopher", "gophers", "spartan", "text", "finger"],
)


class URLReference:
    """
    Central class for all URL handling and manipulation.

    Contains extra support for schemes like gopher:// and finger:// that
    aren't well-structured according to rfc4266, and schemes like mailto:
    that don't have a netloc.

    This was copied from another project that I'm working on, which is why
    it handles gopher & other URL patterns that the proxy does not support
    (yet).
    """

    DEFAULT_PORTS: dict[str, int] = {
        "http": 80,
        "https": 443,
        "gopher": 70,
        "gophers": 70,
        "spartan": 300,
        "gemini": 1965,
        "text": 1961,
        "finger": 79,
    }

    def __init__(self, url: str, base: str | None = None):
        """
        Deconstruct the URL, so we can inspect and transform it.

        Args:
            url: The target URL, may be either absolute or relative.
            base: The document that the URL was retrieved from,
                this is used to resolve relative URLs.
        """
        self.original = url
        self.base = base
        if base:
            url = urljoin(base, url)

        url_parts = urlparse(url)

        self.scheme = url_parts.scheme
        self.port = url_parts.port or self.DEFAULT_PORTS.get(self.scheme, None)
        self.hostname = url_parts.hostname

        # HTTP/Gemini/Spartan URL components (RFC 3986)
        self.path = url_parts.path
        self.params = url_parts.params
        self.query = url_parts.query
        self.fragment = url_parts.fragment

        # Remove the optional trailing slash
        if self.path == "/":
            self.path = ""

        sections = url.split("/", maxsplit=3)

        # https://datatracker.ietf.org/doc/html/draft-ietf-uri-url-finger
        # Finger URLs parse differently after the authority component
        self.finger_request = ""
        if self.scheme == "finger" and len(sections) == 4:
            self.finger_request = sections[3]

        # https://datatracker.ietf.org/doc/html/rfc4266
        # Gopher URLs parse differently after the authority component
        self.gopher_item_type = ""
        self.gopher_selector = ""
        self.gopher_search = ""
        if self.scheme in ("gopher", "gophers"):
            if len(sections) < 4 or sections[3] == "":
                self.gopher_item_type = "1"
                self.gopher_selector = ""
            else:
                self.gopher_item_type = sections[3][0]
                self.gopher_selector = sections[3][1:]

        if "%09" in self.gopher_selector:
            # Strip the search string & gopher+ data out of the selector and path
            self.gopher_selector, self.gopher_search = self.gopher_selector.split("%09", maxsplit=1)
            self.path = self.path.split("%09", maxsplit=1)[0]

    def __str__(self):
        return self.get_url()

    def __repr__(self):
        return f"URLReference: {self.get_url()}"

    def __eq__(self, other) -> bool:
        if isinstance(other, URLReference):
            return self.get_url() == other.get_url()
        else:
            return False

    def guess_mimetype(self) -> str | None:
        """
        Guess the mimetype of the file/document that the URL is pointed to.
        """
        if self.scheme in ("gopher", "gophers"):
            # Using the path component instead of the selector is a heuristic
            # here, because even though the path has no meaning in gopher, I'm
            # assuming most modern gopher servers are going to use HTTP-style
            # paths for file names.
            return self.guess_gopher_mimetype(self.gopher_item_type, self.path)
        else:
            return mimetypes.guess_type(self.path, strict=False)[0]

    def get_external_indicator(self) -> str | None:
        """
        Return a string that can be displayed next to external links to
        indicate that they will navigate away from the current host.
        """
        if self.base is None:
            return None

        base_parts = urlparse(self.base)
        base_scheme = base_parts.scheme
        base_hostname = base_parts.hostname

        if self.scheme and self.scheme != base_scheme:
            if self.hostname:
                return f"{self.scheme}://{self.hostname}"
            else:
                return f"{self.scheme}://"

        if self.hostname and self.hostname != base_hostname:
            return self.hostname

        return None

    @property
    def netloc(self) -> str:
        """
        Return the normalized netloc value for constructing URLs.
        """
        if self.port and self.port != self.DEFAULT_PORTS.get(self.scheme):
            return f"{self.hostname}:{self.port}"
        elif self.hostname:
            return self.hostname
        else:
            return ""

    @property
    def conn_info(self) -> tuple[str, int]:
        """
        Return the IP connection info if available.
        """
        if not self.port:
            raise ValueError(f"Unable to build connection info, missing port: {self.get_url()}")
        elif not self.hostname:
            raise ValueError(f"Unable to build connection info, missing hostname: {self.get_url()}")
        else:
            return self.hostname, self.port

    def _get_finger_url(self) -> str:
        if self.finger_request:
            return f"{self.scheme}://{self.netloc}/{self.finger_request}"
        else:
            return f"{self.scheme}://{self.netloc}"

    def _get_gopher_url(self) -> str:
        selector = self.gopher_selector
        if self.gopher_search:
            selector = f"{selector}%09{self.gopher_search}"

        if selector:
            return f"{self.scheme}://{self.netloc}/{self.gopher_item_type}{selector}"
        elif self.gopher_item_type != "1":
            return f"{self.scheme}://{self.netloc}/{self.gopher_item_type}"
        else:
            return f"{self.scheme}://{self.netloc}"

    def get_url(self, include_query: bool = True, include_fragment: bool = True) -> str:
        """
        Construct a normalized URL string.
        """
        if self.scheme == "finger":
            return self._get_finger_url()
        elif self.scheme in ("gopher", "gophers"):
            return self._get_gopher_url()

        query = self.query
        if not include_query:
            query = ""

        fragment = self.fragment
        if not include_fragment:
            fragment = ""

        parts = (self.scheme, self.netloc, self.path, self.params, query, fragment)
        return urlunparse(parts)

    def get_gemini_request_url(self) -> str:
        """
        Get the URL formatted to be sent in a gemini request string.
        """
        path = self.path
        if self.scheme in ("gemini", "text") and path == "":
            # Add an optional trailing slash for gemini because of a quirk in
            # many server implementations that will redirect if the slash is
            # missing from the root URL.
            path = "/"

        # Drop the fragment from the request sent to the server
        fragment = ""

        # Convert domain names to punycode for compatibility with URLs that
        # contain encoded IDNs (follows RFC 3490).
        netloc = self.netloc.encode("idna").decode("ascii")
        parts = (self.scheme, netloc, path, self.params, self.query, fragment)
        return urlunparse(parts)

    def get_gopher_request(self) -> bytes:
        """
        Get the URL formatted to be sent to a gopher server.
        """
        if self.gopher_search:
            request_string = f"{self.gopher_selector}%09{self.gopher_search}\r\n"
        else:
            request_string = f"{self.gopher_selector}\r\n"
        return unquote_to_bytes(request_string)

    def get_root(self, include_user_dirs=False) -> URLReference | None:
        """
        Get the base component of the URL that denotes a unique site.
        """
        if self.scheme == "view-source":
            target = self.get_view_source_target()
            return target.get_root(include_user_dirs=include_user_dirs)

        path = ""
        if include_user_dirs:
            if self.scheme == "finger":
                # E.g. finger://mozz.us/michael
                user = self.finger_request
                path = f"/{user}"
            elif self.scheme == "gopher":
                if len(self.path) >= 3 and self.path[2] == "~":
                    # E.g. gopher://example.com/0~user/file.txt
                    user = self.path[2:].split("/")[0]
                    path = f"/1{user}/"
                elif len(self.path) >= 4 and self.path[2:4] == "/~":
                    # E.g. gopher://example.com/0/~user/file.txt
                    user = self.path[3:].split("/")[0]
                    path = f"/1/{user}/"
            elif self.path.startswith("/~"):
                # E.g. gemini://example.com/~user/file.txt
                user = self.path[1:].split("/")[0]
                path = f"/{user}/"

        if self.scheme == "gopher" and len(path) > 1:
            # Force gopher menu type
            path = f"/1{path[2:]}"

        if self.netloc and path:
            return URLReference(f"{self.scheme}://{self.netloc}{path}")
        elif self.netloc:
            return URLReference(f"{self.scheme}://{self.netloc}")
        else:
            return None

    def get_parent(self) -> URLReference | None:
        """
        Get the parent of the URL with the last path component stripped off.
        """
        path = self.path.rstrip("/")
        if not path:
            # We're already at the root URL
            return None

        # Strip off the last path component and add a trailing slash
        path = "/".join(path.split("/")[:-1]) + "/"

        if self.scheme == "gopher" and len(path) > 1:
            # Force gopher menu type
            path = f"/1{path[2:]}"

        if self.netloc and path:
            return URLReference(f"{self.scheme}://{self.netloc}{path}")
        elif self.netloc:
            return URLReference(f"{self.scheme}://{self.netloc}")
        else:
            return None

    def get_view_source(self) -> URLReference:
        """
        Get the URL to view the raw source for the resource.
        """
        if self.scheme == "view-source":
            return self
        else:
            return URLReference(f"view-source:{self.get_url()}")

    def get_view_source_target(self) -> URLReference:
        """
        Get the URL nested inside a view-source scheme.
        """
        if self.scheme == "view-source":
            return URLReference(self.original[12:])
        else:
            return self

    def join(self, url: str) -> URLReference:
        """
        Create a new URL reference using the current object as the base.
        """
        return self.__class__(url, self.get_url())

    @classmethod
    def from_filename(cls, filename: str):
        """
        Generate a file:// link from a local filename.
        """
        filename = os.path.abspath(filename)
        url = f"file://{quote(filename)}"
        return cls(url)

    def get_dir(self) -> URLReference:
        """
        If the URL path is not a directory, go up one level to the nearest directory.
        """
        if not self.path:
            # The root URL is always a directory
            return self
        elif self.path.endswith("/"):
            return self
        else:
            parent = self.get_parent()
            if parent is None:
                raise AssertionError(f"Could not derive directory for url: {self}")
            return parent

    def get_proxy_url(self, external=True, **query_params) -> str:
        """
        Build a https://portal.mozz.us/... proxy link for the given URL.
        """
        if self.scheme not in ("gemini", "spartan", "text", "finger"):
            if external:
                return self.get_url()

            raise ValueError("Unsupported URL scheme")

        path = urlunparse(("", "", self.path, self.params, self.query, ""))
        if path:
            return url_for(
                "proxy-path",
                scheme=self.scheme,
                netloc=self.netloc,
                path=path.lstrip("/"),
                _anchor=self.fragment or None,
                **query_params,
            )
        else:
            return url_for(
                "proxy-netloc",
                scheme=self.scheme,
                netloc=self.netloc,
                _anchor=self.fragment or None,
                **query_params,
            )

    def get_root_proxy_url(self, include_user_dirs=False) -> str | None:
        """
        Get the base component of the URL as a string.
        """
        root = self.get_root(include_user_dirs)
        if root:
            return root.get_proxy_url()
        else:
            return None

    def get_parent_proxy_url(self) -> str | None:
        """
        Get the parent of the URL as a string.
        """
        parent = self.get_parent()
        if parent:
            return parent.get_proxy_url()
        else:
            return None

    @classmethod
    def guess_gopher_mimetype(cls, item_type: str, selector: str) -> str | None:
        """
        Attempt to guess a specific mimetype for a gopher selector based on the
        selector's item type and the extension on the selector.
        """
        mimetype, _ = mimetypes.guess_type(selector)

        if item_type in ("1", "7"):
            mimetype = "application/gopher-menu"
        elif item_type in ("h", "H"):
            mimetype = "text/html"
        elif item_type == "g":
            mimetype = "image/gif"
        elif item_type == "4":
            mimetype = "application/binhex"
        elif item_type == "d":
            mimetype = mimetype or "application/pdf"
        elif item_type in ("5", "9"):
            mimetype = mimetype or "application/octet-stream"
        elif item_type == "s":
            if mimetype and mimetype.startswith("audio/"):
                mimetype = mimetype
            else:
                mimetype = "audio/wave"
        elif item_type == "0":
            if mimetype and mimetype.startswith("text/"):
                mimetype = mimetype
            else:
                mimetype = "text/plain"
        elif item_type in ("2", "8", "3", "i", "+"):
            mimetype = None
        else:
            mimetype = mimetype

        return mimetype
