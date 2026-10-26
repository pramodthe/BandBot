from app.providers.context import selection
from app.providers.connections import resolve
from app.config import settings

def selected_composio():
    binding = selection("integrations")
    if not binding or binding.get("legacy"):
        return None
    if binding.get("provider") != "composio" or binding.get("disabled"):
        return False
    return resolve(binding, "integrations")

def api_key():
    connection = selected_composio()
    if connection is False:
        return ""
    return connection.secret["api_key"] if connection else settings.composio_api_key
