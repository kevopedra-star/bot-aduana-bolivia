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

if not SUPABASE_URL or not SUPABASE_KEY or not GSPREAD_CREDENTIALS_JSON:
    raise ValueError("Faltan llaves de configuración (SUPABASE_URL, SUPABASE_KEY o GSPREAD_CREDENTIALS_JSON).")

def conectar_google_sheets():
    credenciales_dict = json.loads(GSPREAD_CREDENTIALS_JSON)
    client = gspread.service_account_from_dict(credenciales_dict)
    spreadsheet = client.open_by_key(ID_GOOGLE_SHEET)
    return spreadsheet.worksheet(NOMBRE_PESTAÑA)

def obtener_dims_terminadas_supabase(gestion_filtro: str = None) -> set:
    """Consulta únicamente los trámites que ya concluyeron para gastar el mínimo de ancho de banda."""
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    
    url_rest = (
        f"{SUPABASE_URL}/rest/v1/monitoreo_aduana"
        f"?select=gestion,aduana,numero_c"
        f"&canal=in.(Verde,Amarillo,Rojo)"
    )
    if gestion_filtro:
        url_rest += f"&gestion=eq.{gestion_filtro}"

    dims_listas = set()
    try:
        resp = requests.get(url_rest, headers=headers, timeout=15)
        if resp.status_code == 200:
            for item in resp.json():
                clave = f"{item['gestion']}-{item['aduana']}-{item['numero_c']}"
                dims_listas.add(clave)
    except Exception as e:
        print(f"Aviso leyendo Supabase: {e}")
    return dims_listas

def guardar_lote_en_supabase(lista_registros: list):
    """Envía todos los trámites en un solo POST con respuesta vacía (0 bytes egress)."""
    if not lista_registros:
        return

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    url_rest = f"{SUPABASE_URL}/rest/v1/monitoreo_aduana?on_conflict=gestion,aduana,numero_c"
    
    tamano_bloque = 100
    for i in range(0, len(lista_registros), tamano_bloque):
        bloque = lista_registros[i:i + tamano_bloque]
        try:
            resp = requests.post(url_rest, headers=headers, json=bloque, timeout=30)
            if resp.status_code in [200, 201, 204]:
                print(f"Éxito: Bloque de {len(bloque)} trámites guardado (Egress: ~0 KB).")
            else:
                print(f"Error Supabase ({resp.status_code}): {resp.text}")
        except Exception as err:
            print(f"Error de red Supabase en lote: {err}")

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
            "gestion": str(gestion).strip(),
            "aduana": str(cod_aduana).strip(),
            "numero_c": str(numero_c).strip(),
            "registro": num_registro,
            "canal": canal,
            "ultimo_estado": ultimo_estado,
            "actualizado_el": datetime.now().isoformat(),
        }
    except Exception as e:
        print(f"Error consultando aduana para {gestion}/{cod_aduana}/C-{numero_c}: {e}")
        return None

def main():
    print(f"Iniciando escaneo: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    sheet = conectar_google_sheets()
    filas = sheet.get_all_values()

    if len(filas) < 2:
        print("No hay filas suficientes en Google Sheets.")
        return

    gestiones_presentes = {f[0].strip() for f in filas[1:] if len(f) > 0 and f[0].strip()}
    gestion_filtro = list(gestiones_presentes)[0] if len(gestiones_presentes) == 1 else None

    completadas = obtener_dims_terminadas_supabase(gestion_filtro)
    print(f"Trámites concluidos detectados en Supabase: {len(completadas)}")

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

    lote_a_guardar = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()

        for g, a, n in tramites_a_consultar:
            print(f"Consultando {g}/{a}/C-{n}...")
            resultado = extraer_datos_aduana(page, g, a, n)
            if resultado:
                lote_a_guardar.append(resultado)
            time.sleep(1.5)

        browser.close()

    if lote_a_guardar:
        guardar_lote_en_supabase(lote_a_guardar)

    print("Ronda finalizada con éxito.")

if __name__ == "__main__":
    main()
