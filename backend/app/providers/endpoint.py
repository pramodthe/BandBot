"""Pin DNS results before sending secrets to compatible endpoints."""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit
import httpx
from app.config import settings

def origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Enter a provider base URL without credentials, query parameters or fragments")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    return f"{parsed.scheme}://{host}:{port}"

def approved(url: str) -> bool:
    entries = settings.provider_endpoint_allowlist.replace(",", " ").split()
    return origin(url) in {origin(entry) for entry in entries}

def network_address(value):
    address = ipaddress.ip_address(value)
    return address.ipv4_mapped or address if isinstance(address, ipaddress.IPv6Address) else address

def validate_base(url: str) -> str:
    origin(url)  # Raises ValueError for credentials, queries and unsupported schemes.
    host = urlsplit(url).hostname
    try:
        address = network_address(host)
    except ValueError:
        address = None
    if address and (address.is_link_local or address.is_multicast or address.is_unspecified):
        raise ValueError("Infrastructure addresses are not provider endpoints")
    if (urlsplit(url).scheme != "https" or host == "localhost" or address and not address.is_global) and not approved(url):
        raise ValueError("Local/private endpoints require PROVIDER_ENDPOINT_ALLOWLIST on the server")
    return url.rstrip("/")

class GuardedTransport(httpx.AsyncBaseTransport):
    def __init__(self, base_url: str, inner=None):
        self.base_url = validate_base(base_url)
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        if origin(str(request.url)) != origin(self.base_url):
            raise ValueError("Provider requests cannot change origins")
        host = request.url.host
        records = await asyncio.get_running_loop().getaddrinfo(host, request.url.port or (443 if request.url.scheme == "https" else 80), type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        if not addresses:
            raise ValueError("Provider hostname did not resolve")
        for value in addresses:
            address = network_address(value)
            if address.is_link_local or address.is_multicast or address.is_unspecified or (not address.is_global and not approved(self.base_url)):
                raise ValueError("Provider hostname resolved to a disallowed address")
        # Pin the checked address; httpcore cannot perform a second, unchecked lookup.
        request.extensions["sni_hostname"] = host
        request.headers["Host"] = request.url.netloc.decode()
        request.url = request.url.copy_with(host=addresses[0])
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()

def http_client(base_url: str):
    return httpx.AsyncClient(transport=GuardedTransport(base_url), follow_redirects=False, trust_env=False, timeout=90)
