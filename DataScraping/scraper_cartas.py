import json
import time
import requests
import urllib.parse
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator
import re

# Configuración
ARCHIVO_ORIGINAL = "CAH-es-set.json"
ARCHIVO_NUEVO = "CAH-es-set-actualizado.json"
LIMITE_CARTAS_NUEVAS = 50 # Límite para evitar bloqueos de traducción

def cargar_json(ruta):
    try:
        with open(ruta, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error al cargar {ruta}: {e}")
        return None

def guardar_json(datos, ruta):
    with open(ruta, 'w', encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    print(f"\n[+] Archivo guardado con éxito en: {ruta}")

def es_texto_basura(texto):
    basura = ['cookie', 'privacy', 'policy', 'sign in', 'log in', 'javascript', 'copyright', 'all rights reserved', 'search', 'menu', 'download', 'subscribe']
    texto_lower = texto.lower()
    for b in basura:
        if b in texto_lower:
            return True
    # Filtro extra: si tiene demasiados símbolos raros
    if re.search(r'[{}[\]<>|^~]', texto):
        return True
    return False

def extraer_urls_busqueda():
    print("[*] Buscando fuentes automáticamente en DuckDuckGo...")
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    url_busqueda = "https://html.duckduckgo.com/html/?q=cards+against+humanity+list+of+cards"
    
    try:
        res = requests.get(url_busqueda, headers=headers, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, 'html.parser')
        enlaces = []
        for a in soup.find_all('a', class_='result__url'):
            href = a.get('href')
            if href and 'uddg=' in href:
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                real_url = parsed.get('uddg', [None])[0]
                if real_url and not 'reddit.com' in real_url: # reddit suele bloquear bots sin API
                    enlaces.append(real_url)
        return enlaces[:5] # Devolver los primeros 5
    except Exception as e:
        print(f"[!] Error buscando URLs: {e}")
        # URL de fallback
        return ["https://en.wikipedia.org/wiki/Cards_Against_Humanity"]

def extraer_cartas_url(url):
    print(f"[*] Extrayendo datos de: {url}")
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'}
    
    cartas_blancas = set()
    cartas_negras = set()
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # Eliminar scripts y estilos
        for script in soup(["script", "style", "nav", "footer", "header"]):
            script.extract()
            
        texto_crudo = soup.get_text(separator='\n')
        lineas = texto_crudo.split('\n')
        
        for linea in lineas:
            linea = linea.strip()
            # Heurística para encontrar cartas:
            # 1. Longitud entre 15 y 150 caracteres
            # 2. No es basura de la web
            # 3. No es una URL
            if 15 < len(linea) < 150 and not es_texto_basura(linea) and not linea.startswith('http'):
                
                # Para ser considerada carta blanca, normalmente empieza en mayúscula
                # Para ser carta negra, tiene ____ o termina en ?
                if '____' in linea or linea.endswith('?'):
                    cartas_negras.add(linea)
                elif linea[0].isupper() and (linea.endswith('.') or linea.endswith('!') or len(linea.split()) > 2):
                    cartas_blancas.add(linea)
                    
    except Exception as e:
        print(f"[!] No se pudo extraer de {url}: {e}")
        
    return list(cartas_blancas), list(cartas_negras)

def traducir_texto(translator, texto):
    try:
        return translator.translate(texto)
    except Exception as e:
        print(f"Error traduciendo '{texto}': {e}")
        return texto

def main():
    print("=== Scraper Autónomo de Cartas contra la Humanidad ===")
    
    datos_actuales = cargar_json(ARCHIVO_ORIGINAL)
    if not datos_actuales:
        return
        
    blancas_actuales = set([c.strip().lower() for c in datos_actuales.get('whiteCards', [])])
    negras_actuales = set([c['text'].strip().lower() for c in datos_actuales.get('blackCards', [])])
    
    urls_fuente = extraer_urls_busqueda()
    
    todas_blancas = []
    todas_negras = []
    
    for url in urls_fuente:
        b, n = extraer_cartas_url(url)
        todas_blancas.extend(b)
        todas_negras.extend(n)
        
    print(f"\n[*] Total de posibles cartas encontradas: {len(todas_blancas)} blancas, {len(todas_negras)} negras.")
    
    translator = GoogleTranslator(source='auto', target='es')
    nuevas_blancas = []
    nuevas_negras = []
    
    procesadas = 0
    print("\n[*] Procesando y traduciendo cartas blancas...")
    for cb in set(todas_blancas): # set para eliminar duplicados de la misma extracción
        if procesadas >= LIMITE_CARTAS_NUEVAS:
            break
        
        traducida = traducir_texto(translator, cb)
        if traducida and traducida.strip().lower() not in blancas_actuales:
            nuevas_blancas.append(traducida)
            blancas_actuales.add(traducida.strip().lower())
            print(f"  + Blanca: {traducida}")
            procesadas += 1
            time.sleep(0.5)
            
    print("\n[*] Procesando y traduciendo cartas negras...")
    for cn in set(todas_negras):
        if procesadas >= LIMITE_CARTAS_NUEVAS * 2: # Límite global ampliado
            break
            
        traducida = traducir_texto(translator, cn)
        if traducida:
            traducida = traducida.replace("_ _ _ _", "____").replace("____", "_")
            
            if traducida.strip().lower() not in negras_actuales:
                pick_count = traducida.count("_")
                if pick_count == 0 and traducida.endswith("?"):
                    pick_count = 1
                elif pick_count == 0:
                    pick_count = 1
                    
                nueva_carta_negra = {
                    "text": traducida,
                    "pick": pick_count
                }
                nuevas_negras.append(nueva_carta_negra)
                negras_actuales.add(traducida.strip().lower())
                print(f"  + Negra: {traducida}")
                procesadas += 1
                time.sleep(0.5)
            
    print(f"\n[*] Nuevas cartas añadidas: {len(nuevas_blancas)} blancas, {len(nuevas_negras)} negras.")
    
    datos_nuevos = datos_actuales.copy()
    datos_nuevos['whiteCards'].extend(nuevas_blancas)
    datos_nuevos['blackCards'].extend(nuevas_negras)
    
    guardar_json(datos_nuevos, ARCHIVO_NUEVO)

if __name__ == "__main__":
    main()
