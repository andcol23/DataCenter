"""
Script de autenticación OAuth2 para obtener el refresh_token de Google.

Uso:
    python tools/google_auth.py

Requiere en .env (o env vars):
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET

Scopes configurados: Gmail send + Google Sheets.

El script abre el navegador para autenticación, recibe el callback en localhost:8080,
e imprime el refresh_token para añadir a .env como GOOGLE_REFRESH_TOKEN.
"""
from __future__ import annotations

import json
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from dotenv import load_dotenv

# Añadir el root del proyecto al path
sys.path.insert(0, str(Path(__file__).parent.parent))

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/spreadsheets",  # lectura/escritura en Google Sheets (backend de datos)
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",  # para saber qué cuenta quedó autorizada
]

REDIRECT_URI  = "http://localhost:8080"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

_auth_code: str | None = None


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        global _auth_code
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "code" in params:
            _auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Autenticacion completada.</h2>"
                b"<p>Puedes cerrar esta ventana.</p></body></html>"
            )
        elif "error" in params:
            error = params.get("error", ["desconocido"])[0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                f"<html><body><h2>Error: {error}</h2></body></html>".encode()
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: object) -> None:
        pass  # Silenciar el log del servidor HTTP


def get_authorization_url(client_id: str) -> str:
    params = {
        "client_id":     client_id,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         " ".join(SCOPES),
        "access_type":   "offline",
        "prompt":        "consent",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code_for_tokens(
    code: str,
    client_id: str,
    client_secret: str,
) -> dict[str, str]:
    import urllib.request

    data = urlencode({
        "code":          code,
        "client_id":     client_id,
        "client_secret": client_secret,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
    }).encode()

    import ssl
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(TOKEN_ENDPOINT, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    with urllib.request.urlopen(req, context=ctx) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    client_id     = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        print(
            "ERROR: Define GOOGLE_CLIENT_ID y GOOGLE_CLIENT_SECRET en .env antes de ejecutar.\n"
            "Obtén estas credenciales en: https://console.cloud.google.com/apis/credentials"
        )
        sys.exit(1)

    auth_url = get_authorization_url(client_id)

    print("\n=== Google OAuth2 — Obtener refresh_token ===\n")
    print("Scopes solicitados:")
    for scope in SCOPES:
        print(f"  • {scope}")
    print()
    print("Abriendo el navegador para autorizar acceso...")
    print(f"Si no se abre automáticamente, ve a:\n  {auth_url}\n")

    webbrowser.open(auth_url)

    # Servidor local para capturar el callback
    server = HTTPServer(("localhost", 8080), OAuthCallbackHandler)
    server.timeout = 300
    print("Esperando autorización en http://localhost:8080 (timeout: 5 min)...")
    server.handle_request()

    if not _auth_code:
        print("\nERROR: No se recibió el código de autorización.")
        sys.exit(1)

    print("Código de autorización recibido. Intercambiando por tokens...")
    tokens = exchange_code_for_tokens(_auth_code, client_id, client_secret)

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print(f"\nERROR: No se recibió refresh_token. Respuesta: {tokens}")
        sys.exit(1)

    print("\n✓ AUTENTICACIÓN EXITOSA\n")
    print("Añade esta línea a tu archivo .env:")
    print(f"\n  GOOGLE_REFRESH_TOKEN={refresh_token}\n")
    print("Y como secret en GitHub Actions:")
    print("  Repositorio → Settings → Secrets → GOOGLE_REFRESH_TOKEN")
    print()


if __name__ == "__main__":
    main()
