"""
Verifica que el GOOGLE_REFRESH_TOKEN puede LEER y ESCRIBIR en el spreadsheet.

Uso:
    python tools/verify_sheets.py

Comprueba, en este orden:
  1. Qué cuenta de Google está autorizada por el token (email).
  2. Que puede abrir el spreadsheet (GOOGLE_SHEET_ID).
  3. Que puede ESCRIBIR (crea una pestaña temporal, escribe y la borra).

Si algo falla, el error te dice exactamente qué arreglar.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()


def _credentials():
    from google.oauth2.credentials import Credentials
    return Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"].strip(),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"].strip(),
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"].strip(),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/userinfo.email",
            "openid",
        ],
    )


def main() -> None:
    print("\n=== Verificación de acceso a Google Sheets ===\n")

    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        print("✗ Falta GOOGLE_SHEET_ID en .env")
        sys.exit(1)

    creds = _credentials()

    # 1) ¿Qué cuenta es?
    try:
        import google.auth.transport.requests as gar
        creds.refresh(gar.Request())
        import urllib.request, json
        req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"},
        )
        with urllib.request.urlopen(req) as r:
            info = json.loads(r.read().decode())
        email = info.get("email", "(desconocido)")
        print(f"  Cuenta autorizada por el token: {email}")
    except Exception as exc:
        email = "(no se pudo determinar)"
        print(f"  Aviso: no se pudo leer el email de la cuenta ({exc})")
        print("  (Puede que falte el scope userinfo.email; no es bloqueante.)")

    # 2) Abrir el spreadsheet
    try:
        import gspread
        gc = gspread.authorize(creds)
        ss = gc.open_by_key(sheet_id)
        print(f"  ✓ Spreadsheet abierto: '{ss.title}'")
        tabs = [ws.title for ws in ss.worksheets()]
        print(f"    Pestañas actuales ({len(tabs)}): {tabs}")
    except Exception as exc:
        print(f"\n✗ NO se pudo abrir el spreadsheet.")
        print(f"  Motivo: {exc}")
        print(f"\n  → La cuenta '{email}' probablemente NO tiene acceso a este Sheet,")
        print(f"    o el GOOGLE_SHEET_ID es incorrecto.")
        print(f"  → Comparte el Sheet con esa cuenta (permiso Editor), o re-autoriza")
        print(f"    con `python tools/google_auth.py` usando la cuenta correcta.")
        sys.exit(1)

    # 3) Probar ESCRITURA (pestaña temporal)
    try:
        tmp = ss.add_worksheet(title="__verify_tmp__", rows=2, cols=2)
        tmp.update([["ok"]], "A1")
        ss.del_worksheet(tmp)
        print("  ✓ Permiso de ESCRITURA confirmado (creó/escribió/borró una pestaña de prueba)")
    except Exception as exc:
        print(f"\n✗ La cuenta puede LEER pero NO ESCRIBIR.")
        print(f"  Motivo: {exc}")
        print(f"  → Dale permiso de **Editor** (no solo Lector) a '{email}' en el Sheet.")
        sys.exit(1)

    print("\n✓ TODO OK — el token puede leer y escribir. Ya puedes migrar:\n")
    print("    python migrate_supabase_to_sheets.py\n")


if __name__ == "__main__":
    main()
