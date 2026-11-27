#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Herramienta de paletas para SSF2
================================

Toma la plantilla vanilla y la plantilla recoloreada de un personaje y añade
(o reemplaza) la paleta resultante en el archivo .as de "costumes" del personaje.

Cómo funciona (verificado con kyo.as, Kyo_Recolor_Sheet.png y Orochi_Kyo.png):
  * La plantilla tiene dos tiras de píxeles (1 píxel de alto) arriba a la izquierda:
      - la tira corta  -> "paletteSwapPA"  (retrato y HUD)
      - la tira larga  -> "paletteSwap"    (sprites dentro del juego)
  * Los colores de la tira VANILLA son la lista "colors" (iguales para todas las paletas).
  * Los colores de la tira RECOLOREADA, en el mismo orden, son la lista "replacements",
    guardados como enteros ARGB con signo (alfa 255).

Uso:
  Interfaz:   python paletas_tool.py
  Consola:    python paletas_tool.py --cli VANILLA.png RECOLOREADA.png archivo.as "Nombre" [--reemplazar] [--forzar]

Solo usa la biblioteca estándar de Python (tkinter para la interfaz).
"""
import json
import os
import shutil
import struct
import sys
import time
import zlib

TITULO = "Herramienta de paletas SSF2"
CLAVES = ("paletteSwap", "paletteSwapPA")
ETIQUETAS = {"paletteSwap": "sprites del juego", "paletteSwapPA": "retrato / HUD"}
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DIR_RESPALDOS = os.path.join(APP_DIR, "respaldos")
ARCHIVO_CONFIG = os.path.join(APP_DIR, "paletas_config.json")


class ToolError(Exception):
    """Error con un mensaje pensado para mostrarse tal cual al usuario."""


# ----------------------------------------------------------------------------
# Lectura de PNG (sin Pillow): exacta, sin gestión de color
# ----------------------------------------------------------------------------
class Imagen(object):
    def __init__(self, ancho, alto, filas):
        self.ancho = ancho
        self.alto = alto
        self.filas = filas  # filas[y][x] = (r, g, b, a)


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _quitar_filtro(tipo, linea, previa, bpp):
    n = len(linea)
    if tipo == 0:
        return linea
    if tipo == 1:
        for i in range(bpp, n):
            linea[i] = (linea[i] + linea[i - bpp]) & 255
    elif tipo == 2:
        for i in range(n):
            linea[i] = (linea[i] + previa[i]) & 255
    elif tipo == 3:
        for i in range(n):
            izq = linea[i - bpp] if i >= bpp else 0
            linea[i] = (linea[i] + ((izq + previa[i]) >> 1)) & 255
    elif tipo == 4:
        for i in range(n):
            izq = linea[i - bpp] if i >= bpp else 0
            arr = previa[i]
            diag = previa[i - bpp] if i >= bpp else 0
            linea[i] = (linea[i] + _paeth(izq, arr, diag)) & 255
    else:
        raise ToolError("El PNG tiene un filtro desconocido (%d)." % tipo)
    return linea


def _pixeles_de_linea(linea, ancho, prof, tipo, paleta, trns):
    fila = []
    if tipo == 3:  # indexado
        if prof == 8:
            idx = list(linea[:ancho])
        else:
            idx = []
            mascara = (1 << prof) - 1
            for byte in linea:
                for s in range(8 - prof, -1, -prof):
                    idx.append((byte >> s) & mascara)
            idx = idx[:ancho]
        for i in idx:
            if i >= len(paleta):
                raise ToolError("El PNG hace referencia a un color que no existe en su paleta.")
            r, g, b = paleta[i]
            a = trns[i] if i < len(trns) else 255
            fila.append((r, g, b, a))
        return fila
    canales = {0: 1, 2: 3, 4: 2, 6: 4}[tipo]
    if prof < 8:  # solo escala de grises
        mascara = (1 << prof) - 1
        vals = []
        for byte in linea:
            for s in range(8 - prof, -1, -prof):
                vals.append(((byte >> s) & mascara) * 255 // mascara)
        for v in vals[:ancho]:
            fila.append((v, v, v, 255))
        return fila
    paso = prof // 8  # 1 o 2 bytes por muestra (de 16 bits nos quedamos con el byte alto)
    for x in range(ancho):
        base = x * canales * paso
        m = [linea[base + k * paso] for k in range(canales)]
        if tipo == 0:
            fila.append((m[0], m[0], m[0], 255))
        elif tipo == 2:
            fila.append((m[0], m[1], m[2], 255))
        elif tipo == 4:
            fila.append((m[0], m[0], m[0], m[1]))
        else:
            fila.append((m[0], m[1], m[2], m[3]))
    return fila


def leer_png(ruta, max_filas=96):
    """Lee las primeras `max_filas` filas de un PNG (las tiras están arriba)."""
    nombre = os.path.basename(ruta)
    try:
        with open(ruta, "rb") as f:
            datos = f.read()
    except OSError as e:
        raise ToolError("No pude abrir «%s»: %s" % (nombre, e))
    if datos[:8] != b"\x89PNG\r\n\x1a\n":
        raise ToolError("«%s» no es un archivo PNG." % nombre)
    pos = 8
    ihdr = None
    plte = None
    trns = b""
    idat = []
    while pos + 8 <= len(datos):
        largo = struct.unpack(">I", datos[pos:pos + 4])[0]
        tipo_chunk = datos[pos + 4:pos + 8]
        cuerpo = datos[pos + 8:pos + 8 + largo]
        pos += 12 + largo
        if tipo_chunk == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", cuerpo)
        elif tipo_chunk == b"PLTE":
            plte = cuerpo
        elif tipo_chunk == b"tRNS":
            trns = cuerpo
        elif tipo_chunk == b"IDAT":
            idat.append(cuerpo)
        elif tipo_chunk == b"IEND":
            break
    if ihdr is None or not idat:
        raise ToolError("«%s» está dañado (le faltan datos)." % nombre)
    ancho, alto, prof, tipo, _comp, _filtro, entrelazado = ihdr
    if entrelazado:
        raise ToolError("«%s» es un PNG entrelazado. Guárdalo de nuevo sin entrelazado." % nombre)
    if tipo not in (0, 2, 3, 4, 6) or prof not in (1, 2, 4, 8, 16):
        raise ToolError("«%s» usa un formato de PNG no soportado." % nombre)
    paleta = []
    if tipo == 3:
        if plte is None:
            raise ToolError("«%s» es indexado pero no tiene paleta." % nombre)
        paleta = [tuple(bytearray(plte[i:i + 3])) for i in range(0, len(plte) - 2, 3)]
    canales = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[tipo]
    bits_px = canales * prof
    stride = (ancho * bits_px + 7) // 8
    bpp = max(1, bits_px // 8)
    try:
        crudo = zlib.decompress(b"".join(idat))
    except zlib.error:
        raise ToolError("«%s» está dañado (no se pudo descomprimir)." % nombre)
    filas = []
    previa = bytearray(stride)
    p = 0
    for _y in range(min(alto, max_filas)):
        if p + 1 + stride > len(crudo):
            raise ToolError("«%s» está incompleto." % nombre)
        ft = crudo[p]
        linea = bytearray(crudo[p + 1:p + 1 + stride])
        p += 1 + stride
        linea = _quitar_filtro(ft, linea, previa, bpp)
        previa = linea
        filas.append(_pixeles_de_linea(linea, ancho, prof, tipo, paleta, trns))
    return Imagen(ancho, alto, filas)


# ----------------------------------------------------------------------------
# Colores
# ----------------------------------------------------------------------------
def int_a_rgb(v):
    u = v & 0xFFFFFFFF
    return ((u >> 16) & 255, (u >> 8) & 255, u & 255)


def rgb_a_int(rgb):
    v = (255 << 24) | (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
    return v - (1 << 32) if v & 0x80000000 else v


# ----------------------------------------------------------------------------
# Tiras de la plantilla
# ----------------------------------------------------------------------------
def buscar_tiras(img, max_y=64, largo_min=8):
    """Devuelve [(y, largo)] de las filas que parecen una tira de paleta:
    empiezan en x=1, el píxel x=0 es el fondo y hay varios colores distintos seguidos."""
    tiras = []
    for y in range(min(len(img.filas), max_y)):
        fila = img.filas[y]
        fondo = fila[0]
        n = 0
        x = 1
        while x < img.ancho and fila[x] != fondo:
            n += 1
            x += 1
        if n >= largo_min and len(set(fila[1:1 + n])) >= 4:
            tiras.append((y, n))
    return tiras


def leer_tira(img, y, n):
    return [img.filas[y][1 + i] for i in range(n)]


# ----------------------------------------------------------------------------
# Archivo .as (JSON con el estilo propio del juego)
# ----------------------------------------------------------------------------
def leer_texto(ruta):
    try:
        with open(ruta, "rb") as f:
            crudo = f.read()
    except OSError as e:
        raise ToolError("No pude abrir «%s»: %s" % (os.path.basename(ruta), e))
    bom = crudo.startswith(b"\xef\xbb\xbf")
    if bom:
        crudo = crudo[3:]
    try:
        return crudo.decode("utf-8"), bom
    except UnicodeDecodeError:
        raise ToolError("«%s» no está en UTF-8." % os.path.basename(ruta))


def cargar_entradas(texto, nombre_archivo=""):
    try:
        entradas = json.loads(texto)
    except ValueError as e:
        raise ToolError("«%s» no es un JSON válido: %s" % (nombre_archivo, e))
    if not isinstance(entradas, list) or not entradas or not all(isinstance(e, dict) for e in entradas):
        raise ToolError("«%s» no tiene la forma esperada (una lista de paletas)." % nombre_archivo)
    return entradas


def buscar_entradas(texto):
    """[(inicio, fin)] de cada objeto {...} de primer nivel dentro de la lista raíz."""
    i = texto.index("[")
    prof = 0
    en_cadena = False
    escape = False
    inicio = None
    spans = []
    for j in range(i + 1, len(texto)):
        c = texto[j]
        if en_cadena:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                en_cadena = False
            continue
        if c == '"':
            en_cadena = True
        elif c == "{":
            if prof == 0:
                inicio = j
            prof += 1
        elif c == "}":
            prof -= 1
            if prof == 0:
                spans.append((inicio, j + 1))
        elif c == "]" and prof == 0:
            break
    return spans


def _sangria_cierre(texto, fin):
    ini = texto.rfind("\n", 0, fin - 1) + 1
    pref = texto[ini:fin - 1]
    return pref if pref.strip() == "" else ""


def construir_objeto(nombre, bloques, nl, cierre):
    """Mismo formato (tabuladores/espacios) que usan las entradas existentes."""
    lineas = ["{", '\t\t\t"info": %s,' % json.dumps(nombre, ensure_ascii=False)]
    for i, (clave, colores, reemplazos) in enumerate(bloques):
        ultimo = i == len(bloques) - 1
        lineas.append('            "%s": {' % clave)
        lineas.append('\t\t\t\t"colors": [%s],' % ",".join(str(c) for c in colores))
        lineas.append('\t\t\t\t"replacements": [%s]' % ",".join(str(c) for c in reemplazos))
        lineas.append("            }" if ultimo else "\t\t\t\t},")
    lineas.append(cierre + "}")
    return nl.join(lineas)


def _saltos_de_linea(texto):
    crlf = texto.count("\r\n")
    lf = texto.count("\n") - crlf
    return "\r\n" if crlf >= lf and crlf > 0 else "\n"


# ----------------------------------------------------------------------------
# Análisis y aplicación
# ----------------------------------------------------------------------------
def _hex(c):
    return "#%02X%02X%02X" % c


def _unicos(lista):
    vistos = set()
    salida = []
    for x in lista:
        if x not in vistos:
            vistos.add(x)
            salida.append(x)
    return salida


def _alinear(colores_as, colores_tira):
    """Para cada color del .as devuelve el índice de la tira que tiene ese color
    (respetando repetidos). None si algún color del .as no está en la tira."""
    libres = {}
    for i, c in enumerate(colores_tira):
        libres.setdefault(c, []).append(i)
    orden = []
    for c in colores_as:
        lst = libres.get(c)
        if not lst:
            return None
        orden.append(lst.pop(0))
    return orden


def _explicar(clave, cols, tiras, tiras_rgb, usadas):
    et = ETIQUETAS[clave]
    cand = [t for t in tiras if t not in usadas]
    if not cand:
        return "No queda ninguna tira de la plantilla para «%s»." % et
    mejor = min(cand, key=lambda t: (abs(t[1] - len(cols)), -sum(1 for a, b in zip(tiras_rgb[t], cols) if a == b)))
    tira = tiras_rgb[mejor]
    iguales = sum(1 for a, b in zip(tira, cols) if a == b)
    solo_as = _unicos([c for c in cols if c not in set(tira)])
    solo_tira = _unicos([c for c in tira if c not in set(cols)])
    txt = "«%s»: el .as tiene %d colores y la tira más parecida de la plantilla tiene %d (coinciden %d posiciones)." % (
        et, len(cols), len(tira), iguales)
    if solo_as:
        txt += " Colores del .as que no están en la plantilla: %s%s." % (
            ", ".join(_hex(c) for c in solo_as[:6]), "…" if len(solo_as) > 6 else "")
    if solo_tira:
        txt += " Colores de la plantilla que no están en el .as: %s%s." % (
            ", ".join(_hex(c) for c in solo_tira[:6]), "…" if len(solo_tira) > 6 else "")
    txt += " Si estás seguro de que el orden es el mismo, marca «Emparejar por posición» y vuelve a intentarlo."
    return txt


def analizar(ruta_vanilla, ruta_recoloreada, ruta_as, forzar=False):
    """Valida las tres entradas y calcula la paleta nueva. No escribe nada.
    forzar=True empareja por posición aunque los colores no coincidan del todo."""
    texto, _bom = leer_texto(ruta_as)
    entradas = cargar_entradas(texto, os.path.basename(ruta_as))
    base = next((e for e in entradas if e.get("base")), entradas[0])
    claves = [k for k in CLAVES if k in base and isinstance(base[k], dict) and "colors" in base[k]]
    if not claves:
        raise ToolError("La paleta base del .as no tiene «paletteSwap» ni «paletteSwapPA».")

    van = leer_png(ruta_vanilla)
    rec = leer_png(ruta_recoloreada)
    if (van.ancho, van.alto) != (rec.ancho, rec.alto):
        raise ToolError("Las dos plantillas no tienen el mismo tamaño (%dx%d y %dx%d). "
                        "¿Son de la misma plantilla?" % (van.ancho, van.alto, rec.ancho, rec.alto))
    tiras = buscar_tiras(van)
    if not tiras:
        raise ToolError("No encontré las tiras de colores en la plantilla vanilla.")
    tiras_rgb = {t: [p[:3] for p in leer_tira(van, t[0], t[1])] for t in tiras}

    avisos = []
    emparejadas = {}  # clave -> (y, n, orden)
    usadas = set()
    problemas = []
    for clave in claves:
        cols = [int_a_rgb(c) for c in base[clave]["colors"]]
        elegido = None
        # 1) coincidencia exacta, en el mismo orden
        for t in tiras:
            if t not in usadas and tiras_rgb[t] == cols:
                elegido = (t, list(range(t[1])), None)
                break
        # 2) mismos colores en otro orden (o la plantilla tiene colores de más)
        if elegido is None:
            for t in tiras:
                if t in usadas or t[1] < len(cols):
                    continue
                orden = _alinear(cols, tiras_rgb[t])
                if orden is not None:
                    msg = ("El orden de los colores de «%s» en el .as es distinto al de la plantilla; "
                           "los emparejé por color." % ETIQUETAS[clave])
                    if t[1] > len(cols):
                        msg += " La plantilla tiene %d colores que el .as no usa (se ignoran)." % (t[1] - len(cols))
                    elegido = (t, orden, msg)
                    break
        # 2b) casi igual: misma longitud y solo unos pocos colores distintos -> por posición, con aviso
        if elegido is None:
            cand = [t for t in tiras if t[1] == len(cols) and t not in usadas]
            if cand:
                t = max(cand, key=lambda t: sum(1 for a, b in zip(tiras_rgb[t], cols) if a == b))
                difs = [i for i, (a, b) in enumerate(zip(tiras_rgb[t], cols)) if a != b]
                if len(difs) <= max(2, len(cols) // 10):
                    detalle = "; ".join("color nº %d (plantilla %s, .as %s)" % (i + 1, _hex(tiras_rgb[t][i]), _hex(cols[i]))
                                        for i in difs[:5])
                    elegido = (t, list(range(t[1])),
                               "En «%s» %d de %d colores difieren entre la plantilla y el .as: %s. "
                               "Emparejé por posición." % (ETIQUETAS[clave], len(difs), len(cols), detalle))
        # 3) por posición, si el usuario lo pidió
        if elegido is None and forzar:
            cand = [t for t in tiras if t[1] == len(cols) and t not in usadas]
            if cand:
                t = max(cand, key=lambda t: sum(1 for a, b in zip(tiras_rgb[t], cols) if a == b))
                iguales = sum(1 for a, b in zip(tiras_rgb[t], cols) if a == b)
                elegido = (t, list(range(t[1])),
                           "Emparejé «%s» por posición aunque solo %d de %d colores coinciden con los del .as."
                           % (ETIQUETAS[clave], iguales, len(cols)))
        if elegido is None:
            problemas.append(_explicar(clave, cols, tiras, tiras_rgb, usadas))
            continue
        t, orden, msg = elegido
        usadas.add(t)
        emparejadas[clave] = (t[0], t[1], orden)
        if msg:
            avisos.append(msg)
    if problemas:
        raise ToolError("La plantilla vanilla no coincide con la base de este .as. " + " ".join(problemas))
    for t in tiras:
        if t not in usadas:
            avisos.append("La plantilla tiene una tira de %d colores que este .as no usa; se ignora." % t[1])

    nuevas = {}
    for clave in claves:
        y, n, orden = emparejadas[clave]
        pix = leer_tira(rec, y, n)
        sel = [pix[i] for i in orden]
        if any(p[3] < 255 for p in sel):
            avisos.append("La tira de «%s» tiene píxeles transparentes; se guardarán opacos." % ETIQUETAS[clave])
        valores = [rgb_a_int(p[:3]) for p in sel]
        colores_as = list(base[clave]["colors"])
        if valores == colores_as:
            avisos.append("La tira de «%s» es idéntica a la vanilla (¿olvidaste recolorearla?)." % ETIQUETAS[clave])
        nuevas[clave] = (colores_as, valores)

    for e in entradas:
        if e is base or "info" not in e:
            continue
        if all(clave in e and e[clave].get("replacements") == nuevas[clave][1] for clave in claves):
            avisos.append("Ya existe una paleta con exactamente estos colores: «%s»." % e["info"])
            break

    return {
        "entradas": entradas,
        "nombres": [e.get("info") for e in entradas if "info" in e],
        "claves": claves,
        "tiras": emparejadas,
        "vanilla": van,
        "recoloreada": rec,
        "nuevas": nuevas,
        "avisos": avisos,
    }


def aplicar(ruta_as, nombre, resultado, reemplazar=False, dir_respaldos=DIR_RESPALDOS):
    """Escribe la paleta en el .as conservando el formato. Devuelve (accion, indice, ruta_respaldo)."""
    nombre = (nombre or "").strip()
    if not nombre:
        raise ToolError("Escribe un nombre para la paleta.")
    texto, bom = leer_texto(ruta_as)
    entradas = cargar_entradas(texto, os.path.basename(ruta_as))
    spans = buscar_entradas(texto)
    if len(spans) != len(entradas):
        raise ToolError("No pude localizar las entradas del .as con seguridad. No toco el archivo.")
    nl = _saltos_de_linea(texto)
    bloques = [(c,) + tuple(resultado["nuevas"][c]) for c in resultado["claves"]]

    existente = next((i for i, e in enumerate(entradas) if e.get("info") == nombre), None)
    if existente is not None and not reemplazar:
        raise ToolError("Ya existe una paleta llamada «%s»." % nombre)
    if existente is not None:
        ini, fin = spans[existente]
        obj = construir_objeto(nombre, bloques, nl, _sangria_cierre(texto, fin))
        nuevo = texto[:ini] + obj + texto[fin:]
        accion, indice = "reemplazada", existente
    else:
        ini, fin = spans[-1]
        obj = construir_objeto(nombre, bloques, nl, _sangria_cierre(texto, fin))
        nuevo = texto[:fin] + "," + nl + "\t" + obj + texto[fin:]
        accion, indice = "añadida", len(entradas)

    # Comprobación antes de escribir: JSON válido y el resto de paletas intactas.
    despues = cargar_entradas(nuevo, os.path.basename(ruta_as))
    esperado = len(entradas) + (0 if existente is not None else 1)
    if len(despues) != esperado:
        raise ToolError("Algo salió mal al preparar el archivo; no lo toco.")
    for i, e in enumerate(entradas):
        if i != existente and despues[i] != e:
            raise ToolError("Algo salió mal al preparar el archivo; no lo toco.")
    nueva_entrada = despues[indice]
    for clave in resultado["claves"]:
        if nueva_entrada[clave]["replacements"] != resultado["nuevas"][clave][1]:
            raise ToolError("Algo salió mal al preparar el archivo; no lo toco.")

    os.makedirs(dir_respaldos, exist_ok=True)
    base_nombre, ext = os.path.splitext(os.path.basename(ruta_as))
    respaldo = os.path.join(dir_respaldos, "%s_%s%s" % (base_nombre, time.strftime("%Y%m%d-%H%M%S"), ext or ".as"))
    try:
        shutil.copy2(ruta_as, respaldo)
        with open(ruta_as, "wb") as f:
            f.write((b"\xef\xbb\xbf" if bom else b"") + nuevo.encode("utf-8"))
    except OSError as e:
        raise ToolError("No pude escribir el archivo: %s" % e)
    return accion, indice, respaldo


# ----------------------------------------------------------------------------
# Configuración recordada
# ----------------------------------------------------------------------------
def cargar_config():
    try:
        with open(ARCHIVO_CONFIG, "r", encoding="utf-8") as f:
            c = json.load(f)
            return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def guardar_config(config):
    try:
        with open(ARCHIVO_CONFIG, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


# ----------------------------------------------------------------------------
# Interfaz
# ----------------------------------------------------------------------------
def main_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox

    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    class App(object):
        def __init__(self, root):
            self.root = root
            self.config = cargar_config()
            self.resultado = None
            root.title(TITULO)
            root.minsize(900, 640)
            self.v_vanilla = tk.StringVar(value=self.config.get("vanilla", ""))
            self.v_recolor = tk.StringVar(value="")
            self.v_as = tk.StringVar(value=self.config.get("as", ""))
            self.v_nombre = tk.StringVar(value="")
            self.v_forzar = tk.BooleanVar(value=False)

            marco = tk.Frame(root, padx=12, pady=10)
            marco.pack(fill="both", expand=True)
            tk.Label(marco, text="1) Elige la plantilla original, la recoloreada y el .as del personaje. "
                                 "2) Escribe el nombre de la paleta. 3) Pulsa «Aplicar».",
                     anchor="w", justify="left", wraplength=860).pack(fill="x", pady=(0, 8))

            self._fila(marco, "Plantilla vanilla (PNG):", self.v_vanilla, self.elegir_vanilla)
            self._fila(marco, "Plantilla recoloreada (PNG):", self.v_recolor, self.elegir_recolor)
            self._fila(marco, "Archivo de paletas (.as):", self.v_as, self.elegir_as)

            fila = tk.Frame(marco)
            fila.pack(fill="x", pady=(8, 4))
            tk.Label(fila, text="Nombre de la paleta:", width=26, anchor="w").pack(side="left")
            self.e_nombre = tk.Entry(fila, textvariable=self.v_nombre, width=40)
            self.e_nombre.pack(side="left")
            cambio = lambda *a: self.actualizar_boton()
            if hasattr(self.v_nombre, "trace_add"):
                self.v_nombre.trace_add("write", cambio)
            else:
                self.v_nombre.trace("w", cambio)

            tk.Checkbutton(marco, text="Emparejar por posición (ignorar diferencias entre los colores de la plantilla y los del .as)",
                           variable=self.v_forzar, command=self.refrescar, anchor="w").pack(fill="x")
            self.l_estado = tk.Label(marco, text="", anchor="w", justify="left", wraplength=860)
            self.l_estado.pack(fill="x", pady=(8, 2))
            self.canvas = tk.Canvas(marco, width=860, height=170, bg="#2b2b2b", highlightthickness=0)
            self.canvas.pack(fill="x", pady=4)

            self.b_aplicar = tk.Button(marco, text="Aplicar paleta al .as", command=self.aplicar, state="disabled")
            self.b_aplicar.pack(pady=6)
            self.t_log = tk.Text(marco, height=9, state="disabled", wrap="word")
            self.t_log.pack(fill="both", expand=True)
            self.refrescar()

        def _fila(self, padre, etiqueta, variable, comando):
            fila = tk.Frame(padre)
            fila.pack(fill="x", pady=2)
            tk.Label(fila, text=etiqueta, width=26, anchor="w").pack(side="left")
            tk.Entry(fila, textvariable=variable, state="readonly", width=70).pack(side="left", padx=4)
            tk.Button(fila, text="Elegir…", command=comando).pack(side="left")

        def log(self, texto):
            self.t_log.configure(state="normal")
            self.t_log.insert("end", texto + "\n")
            self.t_log.see("end")
            self.t_log.configure(state="disabled")

        def _elegir(self, titulo, tipos, variable, clave_config):
            inicial = os.path.dirname(variable.get()) or self.config.get("dir_" + clave_config, "") or None
            ruta = filedialog.askopenfilename(title=titulo, filetypes=tipos, initialdir=inicial)
            if ruta:
                variable.set(ruta)
                self.config["dir_" + clave_config] = os.path.dirname(ruta)
                guardar_config(self.config)
                self.refrescar()

        def elegir_vanilla(self):
            self._elegir("Plantilla vanilla", [("PNG", "*.png")], self.v_vanilla, "png")
            self.config["vanilla"] = self.v_vanilla.get()
            guardar_config(self.config)

        def elegir_recolor(self):
            self._elegir("Plantilla recoloreada", [("PNG", "*.png")], self.v_recolor, "png")

        def elegir_as(self):
            self._elegir("Archivo de paletas del personaje", [("Archivos .as", "*.as"), ("Todos", "*.*")],
                         self.v_as, "as")
            self.config["as"] = self.v_as.get()
            guardar_config(self.config)

        def refrescar(self):
            self.resultado = None
            self.canvas.delete("all")
            if not (self.v_vanilla.get() and self.v_recolor.get() and self.v_as.get()):
                self.l_estado.configure(text="Faltan archivos por elegir.", fg="#444444")
                self.actualizar_boton()
                return
            try:
                self.resultado = analizar(self.v_vanilla.get(), self.v_recolor.get(), self.v_as.get(),
                                          self.v_forzar.get())
            except ToolError as e:
                self.l_estado.configure(text="✗ " + str(e), fg="#b00020")
                self.actualizar_boton()
                return
            r = self.resultado
            partes = ["%s: %d colores" % (ETIQUETAS[c], len(r["nuevas"][c][1])) for c in r["claves"]]
            texto = "✓ Plantilla leída correctamente (%s). Paletas existentes: %s." % (
                ", ".join(partes), ", ".join("«%s»" % n for n in r["nombres"]) or "ninguna")
            if r["avisos"]:
                texto += "\n⚠ " + "\n⚠ ".join(r["avisos"])
            self.l_estado.configure(text=texto, fg="#1b5e20" if not r["avisos"] else "#8a5a00")
            self.dibujar(r)
            self.actualizar_boton()

        def dibujar(self, r):
            y0 = 6
            for clave in r["claves"]:
                colores, reemplazos = r["nuevas"][clave]
                n = len(colores)
                ancho = max(2, min(7, 840 // n))
                self.canvas.create_text(4, y0, anchor="nw", fill="#dddddd", text="%s (%d)" % (ETIQUETAS[clave], n))
                for fila, lista in enumerate((colores, reemplazos)):
                    for i, v in enumerate(lista):
                        rr, gg, bb = int_a_rgb(v)
                        self.canvas.create_rectangle(4 + i * ancho, y0 + 18 + fila * 18,
                                                     4 + (i + 1) * ancho, y0 + 34 + fila * 18,
                                                     fill="#%02x%02x%02x" % (rr, gg, bb), outline="")
                y0 += 82

        def actualizar_boton(self):
            ok = self.resultado is not None and self.v_nombre.get().strip() != ""
            self.b_aplicar.configure(state="normal" if ok else "disabled")

        def aplicar(self):
            if self.resultado is None:
                return
            nombre = self.v_nombre.get().strip()
            reemplazar = False
            if nombre in self.resultado["nombres"]:
                if not messagebox.askyesno(TITULO, "Ya existe una paleta llamada «%s».\n¿Quieres reemplazarla?" % nombre):
                    return
                reemplazar = True
            try:
                accion, indice, respaldo = aplicar(self.v_as.get(), nombre, self.resultado, reemplazar)
            except ToolError as e:
                messagebox.showerror(TITULO, str(e))
                self.log("✗ " + str(e))
                return
            self.log("✓ Paleta «%s» %s (posición %d) en %s" % (nombre, accion, indice, os.path.basename(self.v_as.get())))
            self.log("  Copia de seguridad: %s" % respaldo)
            self.v_recolor.set("")
            self.v_nombre.set("")
            self.refrescar()

    raiz = tk.Tk()
    App(raiz)
    raiz.mainloop()


# ----------------------------------------------------------------------------
# Consola
# ----------------------------------------------------------------------------
def main_cli(argv):
    args = [a for a in argv if a not in ("--reemplazar", "--forzar")]
    reemplazar = "--reemplazar" in argv
    forzar = "--forzar" in argv
    if len(args) != 4:
        print('Uso: python paletas_tool.py --cli VANILLA.png RECOLOREADA.png archivo.as "Nombre" [--reemplazar] [--forzar]')
        return 2
    try:
        r = analizar(args[0], args[1], args[2], forzar)
        for a in r["avisos"]:
            print("Aviso:", a)
        accion, indice, respaldo = aplicar(args[2], args[3], r, reemplazar)
    except ToolError as e:
        print("Error:", e)
        return 1
    print("Paleta «%s» %s (posición %d). Copia de seguridad: %s" % (args[3], accion, indice, respaldo))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        sys.exit(main_cli(sys.argv[2:]))
    main_gui()
