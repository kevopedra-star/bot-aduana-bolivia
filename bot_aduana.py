import os
import re
import json
import time
from datetime import datetime
from bs4 import BeautifulSoup
import requests
import gspread
from playwright.sync_api import sync_playwright

ID_GOOGLE_SHEET = "1f73HlRPVJzl1yLcU59fbjAMJwt80venOydQ6rJL6AvU"
NOMBRE_PESTAÑA = "CONSULTA"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GSPREAD_CREDENTIALS_JSON = os.environ.get("GSPREAD_CREDENTIALS_JSON", "")

if not SUPABASE_KEY or not GSPREAD_CREDENTIALS_JSON:
    raise ValueError("Faltan llaves de configuración en GitHub Secrets.")

def conectar_google_sheets():
    credenciales_dict = json.loads(GSPREAD_CREDENTIALS_JSON)
    client = gspread.service_account_from_dict(credenciales_dict)
    spreadsheet = client.open_by_key(ID_GOOGLE_SHEET)
    return spreadsheet.worksheet(NOMBRE_PESTAÑA)

def obtener_dims_terminadas_supabase() -> set:
    """Evita reconsultar trámites que ya tienen canal definitivo en Supabase."""
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    url_rest = f"{SUPABASE_URL}/rest/v1/monitoreo_aduana?select=gestion,aduana,numero_c,canal"
    dims_listas = set()
    try:
        resp = requests.get(url_rest, headers=headers, timeout=15)
        if resp.status_code == 200:
            for item in resp.json():
                if item.get("canal") in ["Verde", "Amarillo", "Rojo"]:
                    clave = f"{item['gestion']}-{item['aduana']}-{item['numero_c']}"
                    dims_listas.add(clave)
    except Exception as e:
        print(f"Aviso leyendo Supabase: {e}")
    return dims_listas

def guardar_en_supabase(gestion: str, aduana: str, numero_c: str, resultado: dict):
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    payload = {
        "gestion": str(gestion).strip(),
        "aduana": str(aduana).strip(),
        "numero_c": str(numero_c).strip(),
        "registro": resultado["registro"],
        "canal": resultado["canal"],
        "ultimo_estado": resultado["ultimo_estado"],
        "actualizado_el": datetime.now().isoformat(),
    }
    url_rest = f"{SUPABASE_URL}/rest/v1/monitoreo_aduana?on_conflict=gestion,aduana,numero_c"
    try:
        resp = requests.post(url_rest, headers=headers, json=payload, timeout=15)
        if resp.status_code in [200, 201]:
            print(f"OK Guardado: {resultado['registro']} -> Canal: {resultado['canal']}")
        else:
            print(f"Error Supabase ({resp.status_code}): {resp.text}")
    except Exception as err:
        print(f"Error de red Supabase: {err}")

def extraer_datos_aduana(page, gestion: str, cod_aduana: str, numero_c: str):
    url_portal = "http://anbsw01.aduana.gob.bo:7601/click/"
    try:
        page.goto(url_portal, timeout=45000)
        page.wait_for_selector("#gestion", timeout=15000)

        page.fill("#gestion", str(gestion).strip())
        page.select_option("#aduana", value=str(cod_aduana).strip())
        page.fill("#numero", str(numero_c).strip())
        page.click("#consulta")

        page.wait_for_selector(".panel-body, table", timeout=20000)
        time.sleep(1.2)

        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        if "DECLARACI" not in html and "OPERACIONES" not in html:
            return None

        num_registro = f"{gestion}/{cod_aduana}/C-{numero_c}"

        canal = "Pendiente"
        html_lower = html.lower()
        if any(w in html_lower for w in ["canal rojo", "examen físico", "examen fisico"]):
            canal = "Rojo"
        elif any(w in html_lower for w in ["canal amarillo", "examen documental"]):
            canal = "Amarillo"
        elif "canal verde" in html_lower:
            canal = "Verde"

        historial_eventos = []
        for fila in soup.find_all("tr"):
            celdas = fila.find_all(["td", "th"])
            if len(celdas) >= 2:
                texto_0 = celdas[0].get_text(separator=" ", strip=True)
                match_fecha = re.search(r"(\d{2}/\d{2}/\d{4})", texto_0)
                if match_fecha:
                    f_fila = match_fecha.group(1)
                    for celda in celdas[2:]:
                        t_ev = re.sub(r"\s+", " ", celda.get_text(separator=" ", strip=True)).strip()
                        if re.search(r"\d{2}:\d{2}:\d{2}", t_ev):
                            historial_eventos.append(
                                re.sub(r"(\d{2}:\d{2}:\d{2})", rf"[{f_fila} \1]", t_ev)
                            )
                        elif t_ev and t_ev not in ["|", "-", ""]:
                            historial_eventos.append(f"[{f_fila}] {t_ev}")

        ultimo_estado = " | ".join(historial_eventos) if historial_eventos else "Aceptación registrada"

        return {
            "registro": num_registro,
            "canal": canal,
            "ultimo_estado": ultimo_estado,
        }
    except Exception as e:
        print(f"Error consultando aduana para {gestion}/{cod_aduana}/C-{numero_c}: {e}")
        return None

def main():
    print(f"Iniciando escaneo: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    completadas = obtener_dims_terminadas_supabase()
    print(f"Trámites ya concluidos en Supabase: {len(completadas)}")

    sheet = conectar_google_sheets()
    filas = sheet.get_all_values()

    if len(filas) < 2:
        print("No hay filas suficientes en Google Sheets.")
        return

    tramites_a_consultar = []
    for fila in filas[1:]:
        g = fila[0].strip() if len(fila) > 0 else ""
        a = fila[1].strip() if len(fila) > 1 else ""
        n = fila[2].strip() if len(fila) > 2 else ""
        if g and a and n:
            clave = f"{g}-{a}-{n}"
            if clave not in completadas:
                tramites_a_consultar.append((g, a, n))

    if not tramites_a_consultar:
        print("Todos los trámites de Google Sheets ya tienen canal definitivo asignado.")
        return

    print(f"Trámites pendientes por consultar: {len(tramites_a_consultar)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()

        for g, a, n in tramites_a_consultar:
            print(f"Consultando {g}/{a}/C-{n}...")
            resultado = extraer_datos_aduana(page, g, a, n)
            if resultado:
                guardar_en_supabase(g, a, n, resultado)
            time.sleep(1.5)

        browser.close()

    print("Ronda finalizada con éxito.")

if __name__ == "__main__":
    main()
