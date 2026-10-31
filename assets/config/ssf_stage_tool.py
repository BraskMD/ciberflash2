#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ssf_stage_tool.py - Registra un stage externo (.ssf convertido a .swf) en tu SSF2 FMC.

Lee del .swf el ID y el GUID del stage y los escribe donde el juego los necesita:
  1. assets/config/mappings.json  -> "stage": { id: {guid, file, file_pub, name} }   (OBLIGATORIO)
  2. assets/config/stage.xml      -> <stage id=".." previewURL=".."/>                (icono del menu)
  3. (opcional, --menu) mappings.json -> stage_select_screen.normal.rows_N           (que aparezca en el menu)

Por defecto SOLO MUESTRA lo que haria (simulacion). Para escribir de verdad, agrega --apply.
Antes de escribir hace copias de seguridad (*.bak_FECHA) y verifica el resultado; si algo
no cuadra, no escribe nada.

Uso:
  Ver que datos tiene el stage:   python ssf_stage_tool.py MiStage.swf --list
  Simular:                        python ssf_stage_tool.py MiStage.swf --config "C:\\juego\\assets\\config" --menu
  Aplicar:                        python ssf_stage_tool.py MiStage.swf --config "C:\\juego\\assets\\config" --menu --apply

Opciones utiles si la herramienta no adivina bien:  --id  --guid  --name  --file  --preview
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patch_swf_lobby as swf   # reutiliza el lector de SWF/ABC (probado)

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
COMMON = {"id", "guid", "name", "stage", "getprop", "getid", "getguid", "main", "null", "true", "false",
          "object", "string", "number", "array", "prototype", "length", "stats", "stageinfo"}
ROW_MAX_ITEMS = 12          # iconos por fila en el menu de stages (segun tu mappings.json)
PAGE_MAX_ROWS = 6           # filas por pagina (segun tu mappings.json)


# ----------------------------------------------------------------------------
# 1) Lectura del SWF
# ----------------------------------------------------------------------------
def read_pool(path):
    try:
        sig, version, body = swf.load_swf(path)
    except SystemExit as e:
        raise SystemExit("ERROR: %s Si es un .ssf cifrado o con otro formato, primero hay que convertirlo "
                         "a un .swf normal." % e)
    except Exception as e:
        raise SystemExit("ERROR: no pude leer el archivo como SWF (%s). Si es un .ssf cifrado, "
                         "primero hay que convertirlo a .swf." % e.__class__.__name__)
    head, tags = swf.split_tags(body)
    pool = []
    for code, _, tbody in tags:
        if code in (swf.TAG_DOABC, swf.TAG_DOABC2):
            a0 = swf.abc_offset(code, tbody)
            abc = tbody[a0:]
            for s, d0, d1 in swf.string_entries(abc):
                pool.append(abc[d0:d1].decode("utf-8", "replace"))
    return pool


def extract(pool, stem):
    """Devuelve dict con id, guid y notas (lista de avisos)."""
    notes = []
    out = {"id": None, "guid": None, "id_how": "", "notes": notes}
    if "getProp" not in pool:
        notes.append("AVISO: el codigo no contiene 'getProp'. El juego exige esa funcion para validar el stage; "
                     "puede que este .swf no sea un .ssf de stage.")
    lower = [s.lower() for s in pool]

    # --- GUID ---
    uu = [(i, s) for i, s in enumerate(pool) if UUID_RE.match(s)]
    gi = pool.index("guid") if "guid" in pool else None
    if len(uu) == 1:
        out["guid"] = uu[0][1].lower()
    elif len(uu) > 1:
        if gi is not None:
            uu.sort(key=lambda t: abs(t[0] - gi))
        out["guid"] = uu[0][1].lower()
        notes.append("AVISO: encontre %d GUID en el archivo: %s. Elegi el mas cercano a la palabra 'guid'. "
                     "Si no es, usa --guid." % (len(uu), ", ".join(s for _, s in uu)))
    else:
        notes.append("ERROR: no hay ningun GUID (formato xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx) en el archivo. "
                     "Usa --guid si lo conoces.")

    # --- ID ---
    st = re.sub(r"(_stage|_ssf)$", "", stem.lower())
    if stem.lower() in lower:
        out["id"], out["id_how"] = pool[lower.index(stem.lower())], "coincide con el nombre del archivo"
    elif st in lower:
        out["id"], out["id_how"] = pool[lower.index(st)], "coincide con el nombre del archivo"
    else:
        ii = pool.index("id") if "id" in pool else None
        cands = []
        if ii is not None:
            for j in range(max(0, ii - 3), min(len(pool), ii + 4)):
                s = pool[j]
                if j != ii and ID_RE.match(s) and s.lower() not in COMMON:
                    cands.append(s)
        if len(cands) == 1:
            out["id"], out["id_how"] = cands[0], "deducido por su posicion junto a 'id' (VERIFICALO)"
        elif cands:
            notes.append("AVISO: no se cual de estos es el ID: %s. Usa --id." % ", ".join(cands))
        else:
            notes.append("AVISO: no pude deducir el ID. Usa --id (suele ser el nombre del archivo, p. ej. 'yoshisstory').")
    return out


# ----------------------------------------------------------------------------
# 2) Edicion de texto de mappings.json (sin reformatear el resto del archivo)
# ----------------------------------------------------------------------------
def skip_ws(t, p):
    while p < len(t) and t[p] in " \t\r\n":
        p += 1
    return p


def scan_string(t, p):
    assert t[p] == '"'
    p += 1
    while t[p] != '"':
        p += 2 if t[p] == "\\" else 1
    return p + 1


def scan_value(t, p):
    p = skip_ws(t, p)
    c = t[p]
    if c == '"':
        return scan_string(t, p)
    if c in "{[":
        close = "}" if c == "{" else "]"
        p += 1
        while True:
            p = skip_ws(t, p)
            if t[p] == close:
                return p + 1
            if t[p] == ",":
                p += 1
                continue
            if c == "{":
                p = scan_string(t, p)
                p = skip_ws(t, p)
                assert t[p] == ":"
                p += 1
            p = scan_value(t, p)
    q = p
    while t[q] not in ",}] \t\r\n":
        q += 1
    return q


def members(t, p):
    """Para un objeto que empieza en t[p]=='{': lista de (clave, inicio_clave, inicio_valor, fin_valor)."""
    assert t[p] == "{"
    res = []
    p += 1
    while True:
        p = skip_ws(t, p)
        if t[p] == "}":
            return res
        if t[p] == ",":
            p += 1
            continue
        ks = p
        p = scan_string(t, p)
        key = json.loads(t[ks:p])
        p = skip_ws(t, p)
        p += 1
        p = skip_ws(t, p)
        vs = p
        ve = scan_value(t, p)
        res.append((key, ks, vs, ve))
        p = ve


def items(t, p):
    """Para un array que empieza en t[p]=='[': lista de (inicio, fin) de cada elemento."""
    assert t[p] == "["
    res = []
    p += 1
    while True:
        p = skip_ws(t, p)
        if t[p] == "]":
            return res
        if t[p] == ",":
            p += 1
            continue
        e = scan_value(t, p)
        res.append((p, e))
        p = e


def child(t, vs, key):
    for k, ks, a, b in members(t, vs):
        if k == key:
            return ks, a, b
    return None


def line_indent(t, pos):
    ls = t.rfind("\n", 0, pos) + 1
    m = re.match(r"[ \t]*", t[ls:pos])
    return m.group(0)


def insert_stage_entry(t, sid, entry):
    root = skip_ws(t, 0)
    st = child(t, root, "stage")
    if st is None:
        raise ValueError('mappings.json no tiene la seccion "stage".')
    ks, vs, ve = st
    mem = members(t, vs)
    nl = "\r\n" if "\r\n" in t else "\n"
    if mem:
        last_ks = mem[-1][1]
        ind = line_indent(t, last_ks)
        pos = mem[-1][3]
    else:
        ind = line_indent(t, ks) + "\t"
        pos = vs + 1
    inner = ind + "\t"
    lines = ['"%s": {' % sid]
    keys = list(entry.keys())
    for i, k in enumerate(keys):
        lines.append('%s"%s": %s%s' % (inner, k, json.dumps(entry[k], ensure_ascii=False), "," if i < len(keys) - 1 else ""))
    lines.append(ind + "}")
    block = nl.join(lines)
    ins = ("," if mem else "") + nl + ind + block
    return t[:pos] + ins + t[pos:]


def remove_stage_entry(t, sid):
    ks, vs, ve = child(t, skip_ws(t, 0), "stage")
    mem = members(t, vs)
    for i, (k, k_s, v_s, v_e) in enumerate(mem):
        if k != sid:
            continue
        if i > 0:                                   # quitar desde el fin del anterior hasta el fin de esta
            return t[:mem[i - 1][3]] + t[v_e:]
        if len(mem) > 1:                            # era la primera: quitar hasta el inicio de la siguiente
            return t[:k_s] + t[mem[1][1]:]
        return t[:k_s].rstrip() + t[v_e:]
    return t


def model_add(normal, sid):
    """Modelo (sobre los datos ya leidos) de donde debe quedar el stage. 'random' siempre queda de ultimo."""
    pages = sorted(int(k[5:]) for k in normal if re.fullmatch(r"rows_\d+", k))
    rows = normal["rows_%d" % pages[-1]]
    last = rows[-1]
    rnd = bool(last) and last[-1] == "random"
    if len(last) < ROW_MAX_ITEMS:
        last.insert(len(last) - 1 if rnd else len(last), sid)
    elif len(rows) < PAGE_MAX_ROWS:
        if rnd:
            last.pop()
        rows.append([sid, "random"] if rnd else [sid])
    else:
        if rnd:
            last.pop()
        normal["rows_%d" % (pages[-1] + 1)] = [[sid, "random"] if rnd else [sid]]
        normal["rows_max"] = pages[-1] + 1


def add_to_menu(t, sid):
    """Agrega el id al menu respetando que 'random' quede ultimo; crea fila/pagina si hace falta."""
    root = skip_ws(t, 0)
    ss = child(t, root, "stage_select_screen")
    nm = child(t, ss[1], "normal")
    normal_vs = nm[1]
    mem = members(t, normal_vs)
    pages = sorted([int(k[5:]) for k, *_ in mem if re.fullmatch(r"rows_\d+", k)])
    last = pages[-1]
    page_ks, page_vs, page_ve = child(t, normal_vs, "rows_%d" % last)
    rows = items(t, page_vs)
    nl = "\r\n" if "\r\n" in t else "\n"
    rs, re_ = rows[-1]
    it = items(t, rs)
    sep = t[it[-2][1]:it[-1][0]] if len(it) >= 2 else ", "          # separador entre elementos (p. ej. ", ")
    last_is_random = json.loads(t[it[-1][0]:it[-1][1]]) == "random"
    if len(it) < ROW_MAX_ITEMS:
        if last_is_random:                                           # entrar ANTES de random
            return t[:it[-1][0]] + json.dumps(sid) + sep + t[it[-1][0]:], "fila %d de la pagina %d (antes de 'random')" % (len(rows), last)
        return t[:it[-1][1]] + sep + json.dumps(sid) + t[it[-1][1]:], "fila %d de la pagina %d" % (len(rows), last)
    # fila llena: se quita 'random' de ella (si estaba) y se pone en la fila/pagina nueva junto al stage
    new_items = json.dumps(sid) + ((sep + json.dumps("random")) if last_is_random else "")
    if last_is_random:
        cut_from = it[-2][1]
        t = t[:cut_from] + t[it[-1][1]:]
        rows = items(t, child(t, normal_vs, "rows_%d" % last)[1])
        rs, re_ = rows[-1]
        it = items(t, rs)
    if len(rows) < PAGE_MAX_ROWS:
        row_text = t[rs:re_]
        f, l = it[0][0] - rs, it[-1][1] - rs
        new_row = row_text[:f] + new_items + row_text[l:]
        rsep = t[rows[-2][1]:rows[-1][0]] if len(rows) >= 2 else "," + nl
        return t[:re_] + rsep.rstrip(" \t").rstrip() + rsep[len(rsep.rstrip()):] + new_row + t[re_:], "fila nueva (%d) de la pagina %d" % (len(rows) + 1, last)
    ind = line_indent(t, page_ks)
    page_ve = child(t, normal_vs, "rows_%d" % last)[2]
    block = ('"rows_%d": [' + nl + ind + "\t[" + nl + ind + "\t\t" + new_items + nl + ind + "\t]" + nl + ind + "]") % (last + 1)
    t2 = t[:page_ve] + "," + nl + ind + block + t[page_ve:]
    ss2 = child(t2, skip_ws(t2, 0), "stage_select_screen")
    nm2 = child(t2, ss2[1], "normal")
    mx = child(t2, nm2[1], "rows_max")
    if mx is not None:
        t2 = t2[:mx[1]] + str(last + 1) + t2[mx[2]:]
    return t2, "pagina NUEVA %d (rows_max ahora %d)" % (last + 1, last + 1)


# ----------------------------------------------------------------------------
# 3) Edicion de stage.xml
# ----------------------------------------------------------------------------
def xml_attr(v):
    return v.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def update_stage_xml(text, sid, preview):
    nl = "\r\n" if "\r\n" in text else "\n"
    tag_re = re.compile(r'<stage\b[^>]*\bid\s*=\s*"%s"[^>]*?/?>' % re.escape(sid))
    m = tag_re.search(text)
    new_tag = '<stage id="%s" previewURL="%s"/>' % (xml_attr(sid), xml_attr(preview))
    if m:
        return text[:m.start()] + new_tag + text[m.end():], "se ACTUALIZO la entrada existente"
    existing = list(re.finditer(r"^([ \t]*)<stage\b", text, re.M))
    ind = existing[-1].group(1) if existing else "\t"
    closes = list(re.finditer(r"</[A-Za-z_][\w.-]*>\s*$", text))
    if not closes:
        raise ValueError("no encuentro la etiqueta de cierre del XML en stage.xml")
    pos = closes[-1].start()
    before = text[:pos].rstrip(" \t")
    return before + ind + new_tag + nl + text[pos:], "se AGREGO una entrada nueva"


def common_preview_dir(text):
    urls = re.findall(r'previewURL\s*=\s*"([^"]+)"', text)
    if urls:
        d = os.path.dirname(urls[0].replace("\\", "/"))
        if d:
            return d
    return "assets/stage_icon"


# ----------------------------------------------------------------------------
def backup(path):
    b = "%s.bak_%s" % (path, datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(path, b)
    return b


def read_text(path):
    raw = open(path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    return raw[3:].decode("utf-8") if bom else raw.decode("utf-8"), bom


def write_text(path, text, bom):
    open(path, "wb").write((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="Registra un stage (.ssf convertido a .swf) en mappings.json / stage.xml")
    ap.add_argument("swf", help="el stage convertido a .swf (o el .ssf si no esta cifrado)")
    ap.add_argument("--config", help="carpeta assets/config del juego (con mappings.json y stage.xml)")
    ap.add_argument("--list", action="store_true", help="solo mostrar los datos que se leen del archivo")
    ap.add_argument("--id"), ap.add_argument("--guid"), ap.add_argument("--name"), ap.add_argument("--file")
    ap.add_argument("--preview", help="ruta de la imagen de vista previa (def.: <carpeta de las demas>/<id>.png)")
    ap.add_argument("--menu", action="store_true", help="tambien agregarlo al menu de seleccion de stages")
    ap.add_argument("--overwrite", action="store_true", help="permitir actualizar un stage que ya existe")
    ap.add_argument("--apply", action="store_true", help="ESCRIBIR los cambios (sin esto solo simula)")
    a = ap.parse_args()

    stem = os.path.splitext(os.path.basename(a.swf))[0]
    pool = read_pool(a.swf)
    info = extract(pool, stem)
    sid = a.id or info["id"]
    guid = (a.guid or info["guid"] or "").lower() or None

    print("Archivo: %s  (%d textos en su codigo)" % (a.swf, len(pool)))
    print("  ID   : %s%s" % (sid, ("   <- " + info["id_how"]) if sid == info["id"] and info["id_how"] else ""))
    print("  GUID : %s" % guid)
    for n in info["notes"]:
        print("  " + n)
    if a.list:
        return 0
    if not sid or not guid:
        print("\nFalta el ID o el GUID: no se puede continuar. Pasalos con --id / --guid.")
        return 1
    if not ID_RE.match(sid):
        print("\nEl ID debe ser minusculas, numeros y _ (3-40 caracteres): %r" % sid)
        return 1
    if not UUID_RE.match(guid):
        print("\nEl GUID no tiene formato valido: %r" % guid)
        return 1
    if not a.config:
        print("\nAgrega --config con la carpeta assets/config del juego para registrar el stage.")
        return 1

    mp = os.path.join(a.config, "mappings.json")
    sp = os.path.join(a.config, "stage.xml")
    if not os.path.isfile(mp):
        print("\nNo encuentro %s" % mp)
        return 1
    mtext, mbom = read_text(mp)
    mj = json.loads(mtext)
    name = a.name or " ".join(w.capitalize() for w in sid.split("_"))
    fname = a.file or (sid + ".ssf")
    entry = {"guid": guid, "file": fname, "file_pub": fname, "name": name}

    if sid in mj.get("stage", {}) and not a.overwrite:
        print("\nEl stage %r YA existe en mappings.json. Usa --overwrite para actualizarlo." % sid)
        return 1
    dup = [k for k, v in mj.get("stage", {}).items() if isinstance(v, dict) and v.get("guid") == guid and k != sid]
    if dup:
        print("\nAVISO: ese GUID ya lo usa el stage %s. Dos stages con el mismo GUID se pisan." % dup)
    expected = json.loads(mtext)
    expected.setdefault("stage", {})[sid] = entry
    base = remove_stage_entry(mtext, sid) if sid in mj.get("stage", {}) else mtext
    if base is not mtext:
        print("\n(--overwrite) se reemplazara la entrada existente de %r" % sid)
    new_m = insert_stage_entry(base, sid, entry)
    menu_note = None
    if a.menu:
        in_menu = any(sid in r for k, v in mj["stage_select_screen"]["normal"].items()
                      if re.fullmatch(r"rows_\d+", k) for r in v)
        if in_menu:
            menu_note = "ya estaba en el menu"
        else:
            new_m, menu_note = add_to_menu(new_m, sid)
            model_add(expected["stage_select_screen"]["normal"], sid)
    try:
        if json.loads(new_m) != expected:
            raise ValueError("el resultado no coincide con lo esperado")
    except Exception as e:
        print("\nERROR interno verificando mappings.json (%s). NO se escribio nada." % e)
        return 2

    # stage.xml
    if os.path.isfile(sp):
        xtext, xbom = read_text(sp)
        pdir = common_preview_dir(xtext)
    else:
        xtext, xbom, pdir = None, False, "assets/stage_icon"
    preview = a.preview or ("%s/%s.png" % (pdir, sid))
    if xtext is None:
        xnew = '<stage_config>\n\t<stage id="%s" previewURL="%s"/>\n</stage_config>\n' % (xml_attr(sid), xml_attr(preview))
        xnote = "stage.xml NO existia: se CREARIA"
    else:
        xnew, xnote = update_stage_xml(xtext, sid, preview)

    print("\n=== Cambios %s ===" % ("A APLICAR" if a.apply else "(SIMULACION - no se escribe nada)"))
    print("mappings.json  -> stage[%r] = %s" % (sid, json.dumps(entry, ensure_ascii=False)))
    if a.menu:
        print("mappings.json  -> menu: %s" % menu_note)
    print("stage.xml      -> %s: previewURL=%r" % (xnote, preview))
    print("\nNO OLVIDES:")
    print("  1. Copiar el stage como   data/stage/%s   (el .ssf original, no el .swf convertido)" % fname)
    print("  2. Crear la imagen de vista previa:  %s   (PNG de unos 256x144 px)" % preview)
    if not a.apply:
        print("\nPara aplicar de verdad, repite el comando agregando  --apply")
        return 0
    b1 = backup(mp)
    write_text(mp, new_m, mbom)
    if os.path.isfile(sp):
        b2 = backup(sp)
    else:
        b2 = None
    write_text(sp, xnew, xbom)
    print("\nHECHO. Copias de seguridad: %s%s" % (os.path.basename(b1), (", " + os.path.basename(b2)) if b2 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
