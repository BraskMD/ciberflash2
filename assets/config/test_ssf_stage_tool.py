#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pruebas de ssf_stage_tool.py con SWF sinteticos y un mappings.json de muestra (mismo formato que el real).
Si existe la variable de entorno SSF2_REAL_MAPPINGS (ruta a tu mappings.json) tambien prueba contra una COPIA del real."""
import difflib, json, os, shutil, struct, subprocess, sys, tempfile, zlib
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import patch_swf_lobby as P
TOOL = os.path.join(HERE, "ssf_stage_tool.py")
fails = 0
def check(c, m):
    global fails
    print(("  [OK]   " if c else "  [FAIL] ") + m)
    if not c: fails += 1

def make_abc(strings):
    b = struct.pack("<HH", 16, 46) + P.write_u30(1) + P.write_u30(1) + P.write_u30(1)
    b += P.write_u30(len(strings) + 1)
    for s in strings:
        e = s.encode(); b += P.write_u30(len(e)) + e
    return b + bytes(range(1, 50))
def tag(code, body):
    if len(body) < 63: return struct.pack("<H", (code << 6) | len(body)) + body
    return struct.pack("<HI", (code << 6) | 63, len(body)) + body
def make_swf(path, strings, compressed=False):
    body = b"\x00" + struct.pack("<HH", 30 * 256, 1) + tag(P.TAG_DOABC, make_abc(strings)) + tag(0, b"")
    hdr = (b"CWS" if compressed else b"FWS") + bytes([26]) + struct.pack("<I", 8 + len(body))
    open(path, "wb").write(hdr + (zlib.compress(body) if compressed else body))

def run(*args):
    r = subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr

def sample_mappings():
    def row(prefix, n): return [prefix + str(i) for i in range(n)]
    d = {"music": {"bgm_a": {"guid": "34ca34f7-caa9-41fd-888f-323aa861fd43", "file": "a.ssf", "file_pub": "DAT1.ssf"}},
         "stage": {"battlefield": {"guid": "11111111-1111-4111-8111-111111111111", "file": "battlefield.ssf", "file_pub": "DAT2.ssf", "name": "Battlefield"},
                   "yoshisstory": {"guid": "78ec5f26-dcf3-4699-beaf-68f43f4e5c37", "file": "yoshisstory.ssf", "file_pub": "DAT176.ssf", "name": "Yoshi's Story"}},
         "stage_select_screen": {"hand_start": {"x": 0, "y": 0},
             "normal": {"position": {"x": -287, "y": 8}, "icon_size": {"width": 52, "height": 30}, "rows_max": 2,
                        "rows_1": [row("a", 12), row("b", 12)], "rows_2": [row("c", 12), row("d", 12), row("e", 5) + ["random"]]}}}
    return json.dumps(d, indent="\t", ensure_ascii=False)

def _subseq(r, a):
    it = iter(a)
    return all(ch in it for ch in r)

def only_additions(old, new):
    """True si NADA existente se perdio: cada linea quitada reaparece completa (solo con texto insertado, p. ej. una coma o un id) en una linea nueva."""
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0))
    added = [l[1:] for l in diff if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in diff if l.startswith("-") and not l.startswith("---")]
    return all(any(_subseq(r, a) for a in added) for r in removed)

d = tempfile.mkdtemp()
UU1, UU2 = "aaaaaaaa-1111-4222-8333-bbbbbbbbbbbb", "cccccccc-1111-4222-8333-dddddddddddd"

print("== 1. Lectura de id y guid ==")
make_swf(os.path.join(d, "mistage.swf"), ["x", "getProp", "id", "mistage", "guid", UU1, "name"], compressed=True)
rc, out = run(os.path.join(d, "mistage.swf"), "--list")
check(rc == 0 and UU1 in out and "mistage" in out and "coincide con el nombre" in out, "FWS/CWS: GUID unico e id por nombre de archivo")
make_swf(os.path.join(d, "raro.swf"), ["getProp", UU2, "id", "otro_stage", "guid", UU1])
rc, out = run(os.path.join(d, "raro.swf"), "--list")
check(UU1 in out.split("GUID :")[1].split("\n")[0] and "2 GUID" in out, "con 2 GUID elige el cercano a 'guid' y avisa")
check("otro_stage" in out and "VERIFICALO" in out, "id deducido por vecindad con 'id' (y pide verificarlo)")
make_swf(os.path.join(d, "sinuuid.swf"), ["getProp", "id", "guid", "algo"])
rc, out = run(os.path.join(d, "sinuuid.swf"), "--list")
check("ningun GUID" in out, "sin GUID: lo dice claramente")
open(os.path.join(d, "cifrado.swf"), "wb").write(os.urandom(300))
rc, out = run(os.path.join(d, "cifrado.swf"), "--list")
check(rc != 0 and "cifrado" in out.lower(), "archivo que no es SWF: error claro")
make_swf(os.path.join(d, "noprop.swf"), ["id", UU1])
rc, out = run(os.path.join(d, "noprop.swf"), "--list")
check("getProp" in out, "avisa si falta 'getProp'")

print("\n== 2. Simulacion y aplicacion ==")
cfg = os.path.join(d, "config"); os.makedirs(cfg)
open(os.path.join(cfg, "mappings.json"), "w", encoding="utf-8").write(sample_mappings())
open(os.path.join(cfg, "stage.xml"), "w", encoding="utf-8-sig", newline="").write(
    '<stage_config>\r\n\t<stage id="battlefield" previewURL="assets/stage_icon/battlefield.png"/>\r\n</stage_config>\r\n')
m0 = open(os.path.join(cfg, "mappings.json"), encoding="utf-8").read(); x0 = open(os.path.join(cfg, "stage.xml"), "rb").read()
rc, out = run(os.path.join(d, "mistage.swf"), "--config", cfg, "--menu")
check(rc == 0 and "SIMULACION" in out and open(os.path.join(cfg, "mappings.json"), encoding="utf-8").read() == m0 and open(os.path.join(cfg, "stage.xml"), "rb").read() == x0,
      "por defecto SIMULA y no toca ningun archivo")
rc, out = run(os.path.join(d, "mistage.swf"), "--config", cfg, "--menu", "--name", "Mi Stage", "--apply")
m1 = open(os.path.join(cfg, "mappings.json"), encoding="utf-8").read(); j = json.loads(m1)
check(rc == 0 and j["stage"]["mistage"] == {"guid": UU1, "file": "mistage.ssf", "file_pub": "mistage.ssf", "name": "Mi Stage"}, "--apply escribe la entrada en mappings.json")
check(only_additions(m0, m1), "mappings.json: SOLO se agregaron lineas (nada existente cambio) y conserva tabuladores")
lr = j["stage_select_screen"]["normal"]["rows_2"][2]
check(lr[-1] == "random" and lr[-2] == "mistage" and len(lr) == 7, "--menu lo agrega ANTES de 'random' (que sigue siendo el ultimo)")
xb = open(os.path.join(cfg, "stage.xml"), "rb").read()
check(xb.startswith(b"\xef\xbb\xbf") and b"\r\n" in xb and b"\n" not in xb.replace(b"\r\n", b""), "stage.xml conserva BOM y saltos CRLF")
xt = xb.decode("utf-8-sig")
check('<stage id="mistage" previewURL="assets/stage_icon/mistage.png"/>' in xt and "battlefield.png" in xt and xt.rstrip().endswith("</stage_config>"),
      "stage.xml: entrada nueva, la carpeta del icono se toma de las existentes, lo anterior intacto")
check(len([f for f in os.listdir(cfg) if ".bak_" in f]) == 2, "se crearon copias de seguridad de ambos archivos")
rc, out = run(os.path.join(d, "mistage.swf"), "--config", cfg)
check(rc == 1 and "YA existe" in out, "un id repetido se rechaza sin --overwrite")
rc, out = run(os.path.join(d, "mistage.swf"), "--config", cfg, "--overwrite", "--guid", UU2, "--name", "Otro", "--menu", "--apply")
j2 = json.load(open(os.path.join(cfg, "mappings.json"), encoding="utf-8"))
check(rc == 0 and j2["stage"]["mistage"]["guid"] == UU2 and j2["stage"]["mistage"]["name"] == "Otro" and list(j2["stage"]).count("mistage") == 1
      and sum(r.count("mistage") for r in j2["stage_select_screen"]["normal"]["rows_2"]) == 1, "--overwrite actualiza sin duplicar (ni en el menu)")
xt = open(os.path.join(cfg, "stage.xml"), "rb").read().decode("utf-8-sig")
check(xt.count('id="mistage"') == 1, "stage.xml: sin entradas duplicadas")
rc, out = run(os.path.join(d, "mistage.swf"), "--config", cfg, "--id", "otro", "--guid", UU2)
check("mismo GUID" in out or "ya lo usa" in out, "avisa si el GUID ya lo usa otro stage")
cfg2 = os.path.join(d, "config2"); os.makedirs(cfg2)
open(os.path.join(cfg2, "mappings.json"), "w", encoding="utf-8").write(sample_mappings())
rc, out = run(os.path.join(d, "mistage.swf"), "--config", cfg2, "--apply")
check(rc == 0 and open(os.path.join(cfg2, "stage.xml"), encoding="utf-8").read().startswith("<stage_config>"), "si no existe stage.xml, lo crea")

print("\n== 3. Menu: llenar filas y paginas ==")
cfg3 = os.path.join(d, "config3"); os.makedirs(cfg3)
open(os.path.join(cfg3, "mappings.json"), "w", encoding="utf-8").write(sample_mappings())
ok = True
for n in range(48):
    rc, out = run(os.path.join(d, "mistage.swf"), "--config", cfg3, "--id", "s%02d" % n, "--guid", "%08x-1111-4222-8333-bbbbbbbbbbbb" % n, "--menu", "--apply")
    if rc != 0: ok = False; print(out); break
nm = json.load(open(os.path.join(cfg3, "mappings.json"), encoding="utf-8"))["stage_select_screen"]["normal"]
allids = [x for k, v in nm.items() if k.startswith("rows_") and k != "rows_max" for r in v for x in r]
check(ok and all(("s%02d" % n) in allids for n in range(48)) and len(allids) == len(set(allids)), "48 stages seguidos: todos en el menu, sin repetidos")
check(max(len(r) for k, v in nm.items() if k.startswith("rows_") and k != "rows_max" for r in v) <= 12 and max(len(v) for k, v in nm.items() if k.startswith("rows_") and k != "rows_max") <= 6,
      "ninguna fila pasa de 12 iconos ni pagina de 6 filas")
check("rows_3" in nm and nm["rows_max"] == 3, "al llenar, crea la pagina 3 y sube rows_max a 3")
lastpage = nm["rows_%d" % nm["rows_max"]]
check(lastpage[-1][-1] == "random" and allids.count("random") == 1, "tras llenar todo, 'random' sigue siendo el ultimo icono y no se duplico")

real = os.environ.get("SSF2_REAL_MAPPINGS")
if real and os.path.isfile(real):
    print("\n== 4. Contra una COPIA de tu mappings.json real ==")
    cfg4 = os.path.join(d, "real"); os.makedirs(cfg4); shutil.copy(real, os.path.join(cfg4, "mappings.json"))
    r0 = open(os.path.join(cfg4, "mappings.json"), encoding="utf-8").read()
    for n in range(3):
        rc, out = run(os.path.join(d, "mistage.swf"), "--config", cfg4, "--id", "prueba%d" % n, "--guid", "%08x-2222-4222-8333-bbbbbbbbbbbb" % n, "--menu", "--apply")
        check(rc == 0, "stage de prueba %d aplicado a la copia real (%s)" % (n, [l for l in out.splitlines() if "menu:" in l][:1]))
    r1 = open(os.path.join(cfg4, "mappings.json"), encoding="utf-8").read()
    jr = json.loads(r1)
lr = jr["stage_select_screen"]["normal"]["rows_%d" % jr["stage_select_screen"]["normal"]["rows_max"]][-1]
check(only_additions(r0, r1) and jr["stage"]["prueba2"]["name"] == "Prueba2" and lr[-1] == "random" and lr[-4:-1] == ["prueba0", "prueba1", "prueba2"], "el archivo real solo recibio lineas nuevas, sigue siendo JSON valido y 'random' queda ultimo")
print("\nFALLOS:", fails); sys.exit(1 if fails else 0)
