#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
portear_stage.py - Porta un stage de SSF2 (version nueva) a FMC.

Que hace, en orden:
  1. Lee el .ssf del stage: id, guid, musicas que usa.
  2. Detecta las llamadas a la API del motor que tu FMC no tiene y las anula
     dentro del .ssf (p. ej. setStarKOEnabled).
  3. Copia el stage a data/stage y la musica a data/sound (con su guid).
  4. Anade las entradas en assets/config/mappings.json (stage, music,
     selector de stages y stages aleatorios) y en assets/config/stage.xml.
  5. Genera la vista previa de 640x360 en assets/stage_preview: tu imagen, o la
     que se arma con el fondo del propio stage, o (si no hay ninguna de las dos)
     la imagen por defecto (la de Terraria, o la que pongas como preview_default.png
     junto a este script).
  6. Permite CAMBIAR el id y el guid del stage (y el id/guid de sus musicas) desde
     la ventana: se reescriben dentro del .ssf y en mappings.json, asi coinciden.

Uso sin argumentos: abre una ventana.
Uso por consola:   python portear_stage.py --help
"""
import argparse
import io
import json
import os
import re
import shutil
import struct
import sys
import time
import zipfile
import zlib

UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
PREVIEW_W, PREVIEW_H = 640, 360
# Respaldo si no se indica la carpeta de scripts de FMC.
KNOWN_MISSING = {'setStarKOEnabled', 'getStarKOEnabled', 'setScreenKOEnabled',
                 'getScreenKOEnabled', 'generateCharacterItem'}


class Log:
    def __init__(self, fn=None):
        self.fn = fn or print
        self.warnings = []

    def __call__(self, msg=''):
        self.fn(msg)

    def warn(self, msg):
        self.warnings.append(msg)
        self.fn('  [AVISO] ' + msg)


# ----------------------------------------------------------------- SWF ----
def read_swf(path):
    d = open(path, 'rb').read()
    sig = d[:3]
    if sig == b'FWS':
        return bytearray(d), False
    if sig == b'CWS':
        return bytearray(b'FWS' + d[3:8] + zlib.decompress(d[8:])), True
    raise ValueError('Formato no soportado (cabecera %r). Se espera un SWF '
                     'plano (FWS) o comprimido con zlib (CWS).' % sig)


def write_swf(path, data, compressed):
    data = bytearray(data)
    data[4:8] = struct.pack('<I', len(data))
    if compressed:
        out = b'CWS' + bytes(data[3:8]) + zlib.compress(bytes(data[8:]), 9)
    else:
        out = bytes(data)
    with open(path, 'wb') as f:
        f.write(out)


def swf_tags(d):
    nbits = d[8] >> 3
    pos = 8 + (5 + 4 * nbits + 7) // 8 + 4
    out = []
    while pos + 2 <= len(d):
        hp = pos
        h = struct.unpack_from('<H', d, pos)[0]
        pos += 2
        t, l = h >> 6, h & 63
        if l == 63:
            l = struct.unpack_from('<I', d, pos)[0]
            pos += 4
        out.append((t, pos, l, hp))
        pos += l
        if t == 0:
            break
    return out


def u30(b, p):
    r = s = 0
    while True:
        x = b[p]
        p += 1
        r |= (x & 0x7f) << s
        s += 7
        if not x & 0x80:
            return r, p


# ----------------------------------------------------------------- ABC ----
class ABC:
    pass


def parse_abc(d, base, length):
    """d: buffer completo del SWF; base: inicio del cuerpo del tag DoABC."""
    b = d
    p = base + 4
    while b[p] != 0:
        p += 1
    p += 1 + 4
    a = ABC()
    n, p = u30(b, p)
    for _ in range(max(n - 1, 0)):
        _, p = u30(b, p)
    n, p = u30(b, p)
    for _ in range(max(n - 1, 0)):
        _, p = u30(b, p)
    n, p = u30(b, p)
    p += 8 * max(n - 1, 0)
    n, p = u30(b, p)
    a.str = ['']
    a.str_pos = [None]       # (inicio del prefijo de longitud, fin de los datos)
    for _ in range(max(n - 1, 0)):
        st = p
        l, p = u30(b, p)
        a.str.append(bytes(b[p:p + l]).decode('utf8', 'replace'))
        a.str_pos.append((st, p + l))
        p += l
    n, p = u30(b, p)
    for _ in range(max(n - 1, 0)):
        p += 1
        _, p = u30(b, p)
    n, p = u30(b, p)
    for _ in range(max(n - 1, 0)):
        c, p = u30(b, p)
        for _ in range(c):
            _, p = u30(b, p)
    n, p = u30(b, p)
    a.mn = [None]
    for _ in range(max(n - 1, 0)):
        k = b[p]
        p += 1
        if k in (7, 0xd):
            ns, p = u30(b, p)
            nm, p = u30(b, p)
            a.mn.append(nm)
        elif k in (0xf, 0x10):
            nm, p = u30(b, p)
            a.mn.append(nm)
        elif k in (0x11, 0x12):
            a.mn.append(0)
        elif k in (9, 0xe):
            nm, p = u30(b, p)
            _, p = u30(b, p)
            a.mn.append(nm)
        elif k in (0x1b, 0x1c):
            _, p = u30(b, p)
            a.mn.append(0)
        elif k == 0x1d:
            _, p = u30(b, p)
            c, p = u30(b, p)
            for _ in range(c):
                _, p = u30(b, p)
            a.mn.append(0)
        else:
            raise ValueError('multiname desconocido %x' % k)
    n, p = u30(b, p)
    for _ in range(n):
        pc, p = u30(b, p)
        _, p = u30(b, p)
        for _ in range(pc):
            _, p = u30(b, p)
        _, p = u30(b, p)
        fl = b[p]
        p += 1
        if fl & 8:
            oc, p = u30(b, p)
            for _ in range(oc):
                _, p = u30(b, p)
                p += 1
        if fl & 0x80:
            for _ in range(pc):
                _, p = u30(b, p)
    n, p = u30(b, p)
    for _ in range(n):
        _, p = u30(b, p)
        c, p = u30(b, p)
        for _ in range(c):
            _, p = u30(b, p)
            _, p = u30(b, p)

    def traits(p):
        c, p = u30(b, p)
        out = []
        for _ in range(c):
            nm, p = u30(b, p)
            k = b[p]
            p += 1
            kind = k & 15
            d_ = None
            if kind in (0, 6):
                _, p = u30(b, p)
                _, p = u30(b, p)
                vi, p = u30(b, p)
                if vi:
                    p += 1
            else:
                _, p = u30(b, p)
                d_, p = u30(b, p)
            if k & 0x40:
                c2, p = u30(b, p)
                for _ in range(c2):
                    _, p = u30(b, p)
            out.append((nm, kind, d_))
        return out, p

    n, p = u30(b, p)
    a.inst = []
    for _ in range(n):
        nm, p = u30(b, p)
        _, p = u30(b, p)
        fl = b[p]
        p += 1
        if fl & 8:
            _, p = u30(b, p)
        c, p = u30(b, p)
        for _ in range(c):
            _, p = u30(b, p)
        iinit, p = u30(b, p)
        tr, p = traits(p)
        a.inst.append((nm, iinit, tr))
    a.cls = []
    for _ in range(n):
        ci, p = u30(b, p)
        tr, p = traits(p)
        a.cls.append((ci, tr))
    c, p = u30(b, p)
    for _ in range(c):
        _, p = u30(b, p)
        _, p = traits(p)
    c, p = u30(b, p)
    a.body = {}          # metodo -> (offset absoluto del codigo, longitud)
    for _ in range(c):
        m, p = u30(b, p)
        for _ in range(4):
            _, p = u30(b, p)
        cl, p = u30(b, p)
        a.body[m] = (p, cl)
        p += cl
        ec, p = u30(b, p)
        for _ in range(ec * 5):
            _, p = u30(b, p)
        _, p = traits(p)
    a.d = d
    a.mn_names = set(x for x in a.mn if x)
    return a


def mname(a, i):
    if i <= 0 or i >= len(a.mn):
        return '*'
    nm = a.mn[i]
    return a.str[nm] if nm else '*'


def u30_enc(v):
    out = bytearray()
    while True:
        x = v & 0x7f
        v >>= 7
        if v:
            out.append(x | 0x80)
        else:
            out.append(x)
            return bytes(out)


def find_abc_tag(d):
    for t, p, l, hp in swf_tags(d):
        if t == 82:
            return hp, p, l
    raise ValueError('El archivo no contiene codigo ActionScript 3 (DoABC).')


def rename_strings(d, mapping, log=None, code=None):
    """Cambia cadenas del pool de constantes del ABC: mapping = {texto_viejo: texto_nuevo}.
    Cambia el largo del tag, asi que hay que llamarla AL FINAL (despues de cualquier
    parche que use desplazamientos). Devuelve un bytearray nuevo."""
    d = bytearray(d)
    mapping = dict((o, n) for o, n in mapping.items() if o != n)
    if not mapping:
        return d
    hp, p, l = find_abc_tag(d)
    a = parse_abc(d, p, l)
    edits = []
    for old, new in mapping.items():
        idx = [i for i, x in enumerate(a.str) if i > 0 and x == old]
        if not idx:
            raise ValueError('No encuentro el texto "%s" dentro del archivo para cambiarlo.' % old)
        uses = 0
        if code:
            for ins in code.values():
                for off, op, v, ln in (ins or []):
                    if op == 0x2C and v[0] in idx:
                        uses += 1
        uses += sum(1 for i in idx if i in a.mn_names)
        if uses > 1 and log is not None:
            log.warn('El texto "%s" se usa en %d sitios dentro del archivo; al cambiarlo '
                     'cambian todos. Si algo falla al cargar, prueba con otro valor.' % (old, uses))
        nb = new.encode('utf-8')
        for i in idx:                      # el pool puede tener la misma cadena repetida
            pre, end = a.str_pos[i]
            edits.append((pre, end, u30_enc(len(nb)) + nb))
    edits.sort(reverse=True)
    delta = 0
    for pre, end, repl in edits:
        d[pre:end] = repl
        delta += len(repl) - (end - pre)
    h = struct.unpack_from('<H', d, hp)[0]
    if h & 63 != 63:
        raise ValueError('El tag DoABC usa cabecera corta; no se puede reescribir con seguridad.')
    struct.pack_into('<I', d, hp + 2, l + delta)
    return d


def symbol_names(d):
    """Nombres exportados en el tag SymbolClass (tipo 76)."""
    out = []
    for t, p, l, hp in swf_tags(d):
        if t == 76:
            n = struct.unpack_from('<H', d, p)[0]
            q = p + 2
            for _ in range(n):
                q += 2
                e = d.index(0, q)
                out.append(bytes(d[q:e]).decode('utf8', 'replace'))
                q = e + 1
            break
    return out


def rename_symbolclass(d, mapping):
    """Renombra simbolos exportados en SymbolClass. Cambia el largo del tag."""
    d = bytearray(d)
    for t, p, l, hp in swf_tags(d):
        if t == 76:
            n = struct.unpack_from('<H', d, p)[0]
            q = p + 2
            out = bytearray(struct.pack('<H', n))
            for _ in range(n):
                cid = bytes(d[q:q + 2])
                q += 2
                e = d.index(0, q)
                name = bytes(d[q:e]).decode('utf8', 'replace')
                q = e + 1
                out += cid + mapping.get(name, name).encode('utf8') + b'\0'
            d[hp:p + l] = struct.pack('<HI', (76 << 6) | 63, len(out)) + bytes(out)
            return d
    return d


# Operandos por opcode: u = u30, b = byte, s = s24
OPS = {
    0x04: 'u', 0x05: 'u', 0x06: 'u', 0x08: 'u', 0x24: 'b', 0x25: 'u', 0x2C: 'u',
    0x2D: 'u', 0x2E: 'u', 0x2F: 'u', 0x31: 'u', 0x32: 'uu', 0x40: 'u', 0x41: 'u',
    0x42: 'u', 0x43: 'uu', 0x44: 'uu', 0x45: 'uu', 0x46: 'uu', 0x49: 'u',
    0x4A: 'uu', 0x4C: 'uu', 0x4E: 'uu', 0x4F: 'uu', 0x53: 'u', 0x55: 'u',
    0x56: 'u', 0x58: 'u', 0x59: 'u', 0x5A: 'u', 0x5D: 'u', 0x5E: 'u', 0x5F: 'u',
    0x60: 'u', 0x61: 'u', 0x62: 'u', 0x63: 'u', 0x65: 'b', 0x66: 'u', 0x68: 'u',
    0x6A: 'u', 0x6C: 'u', 0x6D: 'u', 0x6E: 'u', 0x6F: 'u', 0x80: 'u', 0x86: 'u',
    0x92: 'u', 0x94: 'u', 0xB2: 'u', 0xC2: 'u', 0xC3: 'u', 0xF0: 'u', 0xF1: 'u',
    0xF2: 'u',
}
for _o in range(0x0C, 0x1B):
    OPS[_o] = 's'
NOOPERAND = set([0x01, 0x02, 0x03, 0x07, 0x09, 0x1C, 0x1D, 0x1E, 0x1F, 0x20, 0x21, 0x23,
                 0x26, 0x27, 0x28, 0x29, 0x2A, 0x2B, 0x30, 0x35, 0x36, 0x37, 0x38,
                 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x47, 0x48, 0x4D, 0x50, 0x51, 0x52,
                 0x57, 0x64, 0x70, 0x71, 0x72, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78,
                 0x82, 0x83, 0x84, 0x85, 0x87, 0x90, 0x91, 0x93, 0x95, 0x96, 0x97,
                 0xB1, 0xB3, 0xB4, 0xC0, 0xC1, 0xC4, 0xC5, 0xC6, 0xC7, 0xF3])
NOOPERAND.update(range(0xA0, 0xB1))
NOOPERAND.update(range(0xD0, 0xD8))


def decode(code, start):
    """Devuelve [(offset_abs, op, [operandos], longitud)]."""
    out = []
    p = 0
    n = len(code)
    while p < n:
        st = p
        op = code[p]
        p += 1
        vals = []
        if op == 0x1B:
            v, p = s24(code, p)
            vals.append(v)
            c, p = u30(code, p)
            for _ in range(c + 1):
                v, p = s24(code, p)
                vals.append(v)
        elif op == 0xEF:
            vals.append(code[p])
            p += 1
            v, p = u30(code, p)
            vals.append(v)
            p += 1
            v, p = u30(code, p)
            vals.append(v)
        elif op in OPS:
            for ch in OPS[op]:
                if ch == 'u':
                    v, p = u30(code, p)
                elif ch == 'b':
                    v = code[p]
                    p += 1
                else:
                    v, p = s24(code, p)
                vals.append(v)
        elif op in NOOPERAND:
            pass
        else:
            raise ValueError('opcode desconocido 0x%02x' % op)
        out.append((start + st, op, vals, p - st))
    return out


def s24(b, p):
    v = b[p] | (b[p + 1] << 8) | (b[p + 2] << 16)
    if v & 0x800000:
        v -= 0x1000000
    return v, p + 3


class StageInfo:
    pass


def analyze(d):
    """Extrae id, guid, musicas, API faltante del SWF del stage."""
    info = StageInfo()
    info.abc = None
    for t, p, l, hp in swf_tags(d):
        if t in (82, 72):
            if t == 72:
                continue
            info.abc = parse_abc(d, p, l)
            break
    a = info.abc
    if a is None:
        raise ValueError('El archivo no contiene codigo ActionScript 3 (DoABC).')
    # metodos -> dueno
    owner = {}
    for i, (nm, iinit, tr) in enumerate(a.inst):
        cn = mname(a, nm)
        owner[iinit] = cn
        owner[a.cls[i][0]] = cn
        for _n, kind, dd in tr + a.cls[i][1]:
            if kind in (1, 2, 3) and dd is not None:
                owner[dd] = cn
    info.owner = owner
    # instrucciones decodificadas de cada cuerpo
    info.code = {}
    for m, (off, ln) in a.body.items():
        try:
            info.code[m] = decode(bytes(d[off:off + ln]), off)
        except Exception:
            info.code[m] = None
    # id / guid: pares pushstring consecutivos 'id' -> valor, 'guid' -> valor
    info.id = info.guid = None
    free_guid = None
    for m, ins in info.code.items():
        if not ins:
            continue
        for k in range(len(ins) - 1):
            if ins[k][1] == 0x2C and ins[k + 1][1] == 0x2C:
                key = a.str[ins[k][2][0]]
                val = a.str[ins[k + 1][2][0]]
                if key == 'id' and info.id is None and val and ' ' not in val:
                    info.id = val
                if key == 'guid' and info.guid is None and UUID_RE.match(val):
                    info.guid = val
                elif key == 'guid' and free_guid is None and val and not re.search(r'\s', val):
                    free_guid = val          # guid de texto libre (p. ej. uno puesto a mano)
    # formato 9b: getID()/getGUID() devuelven una cadena
    for nm, iinit, tr in a.inst:
        for n_, kind, dd in tr:
            fn = mname(a, n_)
            if kind == 1 and fn in ('getID', 'getGUID') and info.code.get(dd):
                ins = info.code[dd]
                for k in range(len(ins) - 1):
                    if ins[k][1] == 0x2C and ins[k + 1][1] == 0x48:
                        val = a.str[ins[k][2][0]]
                        if fn == 'getID' and info.id is None:
                            info.id = val
                        if fn == 'getGUID' and info.guid is None and val:
                            info.guid = val
    if info.guid is None and free_guid:
        info.guid = free_guid
    if info.guid is None:
        for s in a.str:
            if UUID_RE.match(s):
                info.guid = s
                break
    info.music = sorted(set(s for s in a.str if s.startswith('bgm_')))
    info.embedded_music = []
    if 'stageMusic' in a.str or 'stageMusicArray' in a.str:
        # nombres de las clases de sonido integradas: aparecen como "MainTimeline_<nombre>"
        # y como cadena sola (la que se asigna a stageMusic)
        for s in a.str:
            if s.startswith('MainTimeline_') and s[len('MainTimeline_'):] in a.str:
                info.embedded_music.append(s[len('MainTimeline_'):])
        info.embedded_music = sorted(set(info.embedded_music))
    # nombres declarados por las clases de API empotradas (SSF2*)
    names = set()
    for i, (nm, iinit, tr) in enumerate(a.inst):
        if mname(a, nm).startswith('SSF2'):
            for n_, kind, dd in tr + a.cls[i][1]:      # metodos de instancia y estaticos
                if kind in (1, 2, 3):
                    names.add(mname(a, n_))
    info.api_names = names
    return info


def engine_identifiers(path):
    """Conjunto de identificadores presentes en los scripts de FMC (carpeta o zip)."""
    ids = set()
    tok = re.compile(r'[A-Za-z_][A-Za-z_0-9]*')
    if path.lower().endswith('.zip'):
        z = zipfile.ZipFile(path)
        for n in z.namelist():
            if n.endswith('.as'):
                ids.update(tok.findall(z.read(n).decode('utf-8', 'replace')))
    else:
        for root, _, files in os.walk(path):
            for f in files:
                if f.endswith('.as'):
                    with open(os.path.join(root, f), encoding='utf-8', errors='replace') as fh:
                        ids.update(tok.findall(fh.read()))
    return ids


LITERALS = (0x20, 0x21, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E, 0x2F)
CALLS = (0x46, 0x4C, 0x4F)


def patch_missing_api(d, info, missing, log):
    """Anula llamadas  this.nombre(literales...)  a funciones que FMC no tiene."""
    a = info.abc
    patched = 0
    for m, ins in info.code.items():
        if not ins:
            continue
        own = info.owner.get(m, '')
        if own.startswith('SSF2'):
            continue                      # el propio envoltorio de la API: no se ejecuta
        targets = set()
        for off, op, v, ln in ins:
            if op in (0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A,
                      0x0C, 0x0D, 0x0E, 0x0F):
                targets.add(off + ln + v[0])
        for k, (off, op, v, ln) in enumerate(ins):
            if op not in CALLS or mname(a, v[0]) not in missing:
                continue
            name = mname(a, v[0])
            argc = v[1]
            where = '%s.%s' % (own or '?', name)
            ok = False
            if op == 0x4F and k - argc - 1 >= 0:
                recv = ins[k - argc - 1]
                args = ins[k - argc:k]
                recv_ok = (recv[1] in (0x5D, 0x5E) and recv[2][0] == v[0]) or recv[1] == 0xD0
                if recv_ok and all(x[1] in LITERALS for x in args):
                    start = recv[0]
                    end = off + ln
                    if not any(start < t < end for t in targets):
                        for i in range(start, end):
                            d[i] = 0x02            # nop
                        patched += 1
                        ok = True
                        log('  Anulada la llamada a %s(...)' % where)
            if not ok:
                log.warn('Llamada a "%s" que FMC no tiene, no se pudo anular sola '
                         '(en %s). El stage podria fallar al cargarse.' % (name, where))
    return patched


SECURITY_CALLS = ('allowDomain', 'allowInsecureDomain')


def patch_security(d, info, log):
    """Anula Security.allowDomain(...): en una aplicacion de escritorio lanza el
    error #3207 y aborta el resto del codigo (frame1 del stage: camara, fondo, musica)."""
    a = info.abc
    patched = 0
    for m, ins in info.code.items():
        if not ins:
            continue
        targets = set()
        for off, op, v, ln in ins:
            if 0x0C <= op <= 0x1A:
                targets.add(off + ln + v[0])
        for k, (off, op, v, ln) in enumerate(ins):
            if op != 0x4F or mname(a, v[0]) not in SECURITY_CALLS:
                continue
            argc = v[1]
            if k - argc - 1 < 0:
                continue
            recv = ins[k - argc - 1]
            args = ins[k - argc:k]
            if recv[1] == 0x60 and mname(a, recv[2][0]) == 'Security' and \
                    all(x[1] in LITERALS for x in args):
                start, end = recv[0], off + ln
                if any(start < t_ < end for t_ in targets):
                    continue
                for i in range(start, end):
                    d[i] = 0x02
                patched += 1
                log('  Anulada la llamada a Security.%s(...) en %s (en FMC da el error #3207 y '
                    'cortaba el stage)' % (mname(a, v[0]), info.owner.get(m, '?')))
    return patched


# --------------------------------------------------------------- previews --
def decode_bitmaps(d):
    from PIL import Image
    res = []
    for t, p, l, hp in swf_tags(d):
        try:
            if t in (20, 36):
                cid, fmt, w, h = struct.unpack_from('<HBHH', d, p)
                if fmt != 5:
                    continue
                raw = zlib.decompress(bytes(d[p + 7:p + l]))
                if len(raw) < w * h * 4:
                    continue
                out = bytearray(w * h * 4)
                if t == 36:
                    for i in range(0, w * h * 4, 4):
                        A = raw[i]
                        if A:
                            out[i] = min(255, raw[i + 1] * 255 // A)
                            out[i + 1] = min(255, raw[i + 2] * 255 // A)
                            out[i + 2] = min(255, raw[i + 3] * 255 // A)
                        out[i + 3] = A
                else:
                    for i in range(0, w * h * 4, 4):
                        out[i], out[i + 1], out[i + 2], out[i + 3] = raw[i + 1], raw[i + 2], raw[i + 3], 255
                res.append((cid, Image.frombytes('RGBA', (w, h), bytes(out))))
            elif t == 21:
                cid = struct.unpack_from('<H', d, p)[0]
                res.append((cid, Image.open(io.BytesIO(bytes(d[p + 2:p + l]))).convert('RGBA')))
        except Exception:
            continue
    return res


def auto_preview(d, log):
    from PIL import Image
    bms = [(c, im) for c, im in decode_bitmaps(d) if im.width * im.height >= 300000]
    if not bms:
        return None
    bg = fg = None
    for c, im in bms:
        asp = im.width / float(im.height)
        opaque = im.getchannel('A').getextrema()[0] == 255
        if bg is None and opaque and 1.5 <= asp <= 2.0:
            bg = im
        elif fg is None and not opaque:
            fg = im
    if bg is None:
        return None          # sin fondo reconocible: el que llama usa la imagen por defecto
    S = 2
    W, H = PREVIEW_W * S, PREVIEW_H * S
    canvas = bg.convert('RGBA').resize((W, H), Image.LANCZOS)
    if fg is not None:
        th = int(H * 0.97)
        tw = int(fg.width * th / float(fg.height))
        fgr = fg.resize((tw, th), Image.LANCZOS)
        canvas.alpha_composite(fgr, ((W - tw) // 2, H - th + int(H * 0.04)))
    return canvas.resize((PREVIEW_W, PREVIEW_H), Image.LANCZOS).convert('RGB')


def make_preview_from_image(path):
    from PIL import Image
    im = Image.open(path).convert('RGB')
    sc = max(PREVIEW_W / float(im.width), PREVIEW_H / float(im.height))
    im = im.resize((max(PREVIEW_W, int(round(im.width * sc))), max(PREVIEW_H, int(round(im.height * sc)))), Image.LANCZOS)
    x, y = (im.width - PREVIEW_W) // 2, (im.height - PREVIEW_H) // 2
    return im.crop((x, y, x + PREVIEW_W, y + PREVIEW_H))


# ------------------------------------------------------------- config -----
def read_text(path):
    raw = open(path, 'rb').read()
    bom = raw.startswith(b'\xef\xbb\xbf')
    return raw.decode('utf-8-sig'), bom


def write_text(path, text, bom):
    with open(path, 'wb') as f:
        f.write((b'\xef\xbb\xbf' if bom else b'') + text.encode('utf-8'))


def backup(path):
    bak = path + '.bak_' + time.strftime('%Y%m%d_%H%M%S')
    shutil.copy2(path, bak)
    return bak


def insert_first_in_object(text, key, entry_lines, nl):
    """Inserta entry_lines justo despues de  \t"key": {  (objeto de nivel 1)."""
    m = re.search(r'(\r?\n)\t"%s": \{(\r?\n)' % re.escape(key), text)
    if not m:
        raise ValueError('No encuentro la seccion "%s" en mappings.json' % key)
    pos = m.end()
    return text[:pos] + entry_lines + text[pos:]


def edit_mappings(path, stage_id, guid, file_name, display, creator, music_entries, page, dry, log):
    text, bom = read_text(path)
    nl = '\r\n' if '\r\n' in text else '\n'
    data = json.loads(text)
    changed = False
    # --- stage
    if stage_id in data.get('stage', {}):
        log.warn('"%s" ya estaba en mappings.json (seccion stage); no la toco.' % stage_id)
    else:
        lines = ['\t\t"%s": {' % stage_id,
                 '\t\t\t"guid": %s,' % json.dumps(guid),
                 '\t\t\t"file": %s,' % json.dumps(file_name),
                 '\t\t\t"file_pub": %s,' % json.dumps(file_name),
                 '\t\t\t"name": %s%s' % (json.dumps(display, ensure_ascii=False), ',' if creator else '')]
        if creator:
            lines.append('\t\t\t"creator": %s' % json.dumps(creator, ensure_ascii=False))
        lines.append('\t\t},')
        text = insert_first_in_object(text, 'stage', nl.join(lines) + nl, nl)
        changed = True
        log('  mappings.json: anadida la entrada stage "%s"' % stage_id)
    # --- musica
    for mid, mguid, mfile in music_entries:
        if mid in data.get('music', {}):
            continue
        lines = ['\t\t"%s": {' % mid,
                 '\t\t\t"guid": %s,' % json.dumps(mguid),
                 '\t\t\t"file": %s,' % json.dumps(mfile),
                 '\t\t\t"file_pub": %s' % json.dumps(mfile),
                 '\t\t},']
        text = insert_first_in_object(text, 'music', nl.join(lines) + nl, nl)
        changed = True
        log('  mappings.json: anadida la musica "%s"' % mid)
    # --- selector de stages
    normal = data['stage_select_screen']['normal']
    flat = [x for k, v in normal.items() if k.startswith('rows_') and isinstance(v, list) and k.count('_') == 1
            for r in v for x in r]
    if stage_id in flat:
        log.warn('"%s" ya esta en el selector de stages; no lo toco.' % stage_id)
    else:
        pages = int(normal.get('rows_max', 1))
        pg = page or pages
        key = 'rows_%d' % pg
        if key not in normal:
            raise ValueError('No existe la pagina %d del selector (hay %d).' % (pg, pages))
        rows = normal[key]
        cap = max(len(r) for r in rows)
        last = rows[-1]
        tail = [last[-1]] if last and last[-1] == 'random' else []
        body = last[:len(last) - len(tail)]

        def row_txt(r):
            return '[' + ', '.join(json.dumps(x) for x in r) + ']'
        start = text.index('"%s": [' % key, text.index('"stage_select_screen"'))
        old = row_txt(last)
        at = text.index(old, start)
        if len(last) + 1 <= cap:
            new = row_txt(body + [stage_id] + tail)
            where = 'fila %d' % len(rows)
        else:
            new = row_txt(body) + ',' + nl + '\t\t\t' + row_txt([stage_id] + tail)
            where = 'fila nueva %d' % (len(rows) + 1)
        text = text[:at] + new + text[at + len(old):]
        changed = True
        log('  mappings.json: "%s" puesto en la pagina %d (%s) del selector' % (stage_id, pg, where))
    # --- aleatorios
    if stage_id not in data['random_stages']['stages']:
        i = text.index('"random_stages"')
        j = text.index('"stages": [', i)
        k = text.index(']', j)
        text = text[:k] + ', ' + json.dumps(stage_id) + text[k:]
        changed = True
        log('  mappings.json: anadido a los stages aleatorios')
    json.loads(text)                       # validacion final
    if changed and not dry:
        b = backup(path)
        write_text(path, text, bom)
        log('  (copia de seguridad: %s)' % os.path.basename(b))
    return changed


def edit_stage_xml(path, stage_id, preview_url, dry, log):
    line = '<stage id="%s" previewURL="%s"/>' % (stage_id, preview_url)
    if os.path.exists(path):
        text, bom = read_text(path)
    else:
        text, bom = '<stages_config>\r\n\t<!-- External Stage Assets -->\r\n</stages_config>', True
    nl = '\r\n' if '\r\n' in text else '\n'
    pat = re.compile(r'[ \t]*<stage\s+id="%s"[^>]*/>' % re.escape(stage_id))
    if pat.search(text):
        text = pat.sub('\t' + line, text)
        log('  stage.xml: actualizada la entrada de "%s"' % stage_id)
    else:
        marker = '<!-- External Stage Assets -->'
        if marker in text:
            text = text.replace(marker, marker + nl + '\t' + line, 1)
        else:
            text = text.replace('</stages_config>', '\t' + line + nl + '</stages_config>', 1)
        log('  stage.xml: anadida la entrada de "%s"' % stage_id)
    if not dry:
        if os.path.exists(path):
            backup(path)
        write_text(path, text, bom)


# -------------------------------------------------------------- music -----
def read_music_info(path):
    d, _ = read_swf(path)
    guid = mid = None
    for t, p, l, hp in swf_tags(d):
        if t == 82:
            a = parse_abc(d, p, l)
            for s in a.str:
                if guid is None and UUID_RE.match(s):
                    guid = s
                if mid is None and s.startswith('bgm_'):
                    mid = s
            if guid is None:                 # guid de texto libre (no UUID)
                try:
                    guid = analyze(d).guid
                except Exception:
                    guid = None
            break
    return mid, guid


def find_file(folder, name):
    want = (name + '.ssf').lower()
    for root, _, files in os.walk(folder):
        for f in files:
            if f.lower() == want:
                return os.path.join(root, f)
    return None


# --------------------------------------------------------------- main -----
ID_RE = re.compile(r'^[A-Za-z0-9_.\-]+$')
GUID_RE = re.compile(r'^[^\s"\\]+$')


def default_preview_bytes():
    import base64
    return base64.b64decode(''.join(_DEFAULT_PREVIEW_B64.split()))


def save_default_preview(dst):
    """Guarda la vista previa por defecto (640x360). Si hay un preview_default.png junto
    al script se usa ese; si no, la imagen incluida dentro de este archivo."""
    ext = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'preview_default.png')
    try:
        from PIL import Image
        if os.path.isfile(ext):
            make_preview_from_image(ext).save(dst)
        else:
            Image.open(io.BytesIO(default_preview_bytes())).convert('RGB').save(dst)
    except ImportError:
        with open(dst, 'wb') as f:
            f.write(open(ext, 'rb').read() if os.path.isfile(ext) else default_preview_bytes())


def music_in_fmc(game, existing_map, mid):
    """True si la musica ya esta en mappings.json y su archivo esta en data/sound."""
    em = (existing_map.get('music') or {}).get(mid)
    if not em:
        return None                       # no esta en mappings.json
    f = em.get('file') or (mid + '.ssf')
    return os.path.isfile(os.path.join(game, 'data', 'sound', f))


def inspect_stage(path, music_dir=None, game=None):
    """Lee el stage y devuelve lo que se puede editar: id, guid y musicas."""
    d, _ = read_swf(path)
    info = analyze(d)
    res = {'id': info.id or os.path.splitext(os.path.basename(path))[0].lower(),
           'id_found': bool(info.id), 'guid': info.guid or '',
           'embedded': info.embedded_music, 'music': []}
    for mid in info.music:
        guid, found = '', False
        src = find_file(music_dir, mid) if music_dir else None
        if src:
            found = True
            guid = read_music_info(src)[1] or ''
        in_fmc = False
        if game:
            try:
                in_fmc = bool(music_in_fmc(game, json.loads(read_text(
                    os.path.join(game, 'assets', 'config', 'mappings.json'))[0]), mid))
            except Exception:
                in_fmc = False
        res['music'].append({'id': mid, 'guid': guid, 'found': found, 'in_fmc': in_fmc})
    return res


def run(a, log):
    game = a['game']
    cfg = os.path.join(game, 'assets', 'config', 'mappings.json')
    if not os.path.isfile(cfg):
        raise ValueError('No encuentro assets/config/mappings.json dentro de "%s". '
                         'Elige la carpeta raiz del juego.' % game)
    dry = a.get('dry', False)
    existing_map = json.loads(read_text(cfg)[0])
    log('Leyendo el stage: %s' % a['stage'])
    d, compressed = read_swf(a['stage'])
    info = analyze(d)
    if not info.guid:
        raise ValueError('No pude leer el guid del stage.')
    stage_id = (a.get('id') or info.id or os.path.splitext(os.path.basename(a['stage']))[0].lower()).strip()
    new_guid = (a.get('guid') or info.guid).strip()
    if not info.id:
        log.warn('No pude leer el id dentro del stage; uso "%s" (revisalo).' % stage_id)
    if not ID_RE.match(stage_id):
        raise ValueError('El id "%s" no es valido: usa solo letras, numeros, _ - .' % stage_id)
    if not GUID_RE.match(new_guid):
        raise ValueError('El guid "%s" no es valido: sin espacios ni comillas.' % new_guid)
    log('  id:   %s%s' % (stage_id, '' if stage_id == info.id else '   (en el archivo: %s)' % info.id))
    log('  guid: %s%s' % (new_guid, '' if new_guid == info.guid else '   (en el archivo: %s)' % info.guid))
    log('  musicas externas que usa: %s' % (', '.join(info.music) or '(ninguna)'))
    if info.embedded_music:
        log('  musica INTEGRADA en el stage: %s' % ', '.join(info.embedded_music))
        log('  (formato 9B: el juego la toma del propio archivo, no hace falta copiar musica)')
    elif not info.music:
        log.warn('El stage no declara musica externa ni integrada reconocible; puede sonar sin musica.')
    for k, v in existing_map.get('stage', {}).items():
        if k != stage_id and v.get('guid') == new_guid:
            log.warn('El guid "%s" ya lo usa el stage "%s" de tu mappings.json.' % (new_guid, k))
    existing = stage_id in existing_map.get('stage', {})
    if existing and not a.get('force'):
        raise ValueError('"%s" ya existe en tu mappings.json (es un stage que FMC ya tiene o ya '
                         'lo portaste). Cambia el id para portarlo como otro stage, o usa --force '
                         'para reemplazar solo su archivo .ssf.' % stage_id)
    display = a.get('name') or ' '.join(w.capitalize() for w in re.split(r'[_\-\s]+', stage_id))
    file_name = stage_id + '.ssf'

    # --- API faltante
    if a.get('scripts'):
        eng = engine_identifiers(a['scripts'])
        missing = set(n for n in info.api_names if n not in eng)
        log('API: %d funciones declaradas por el stage, %d no existen en tu FMC' % (len(info.api_names), len(missing)))
    else:
        missing = set(n for n in info.api_names if n in KNOWN_MISSING)
        log('API: sin carpeta de scripts, uso la lista conocida de funciones ausentes')
    used = set()
    for m, ins in info.code.items():
        if ins and not info.owner.get(m, '').startswith('SSF2'):
            for off, op, v, ln in ins:
                if op in (0x46, 0x4C, 0x4F, 0x60, 0x66) and mname(info.abc, v[0]) in missing:
                    used.add(mname(info.abc, v[0]))
    n_patched = patch_security(d, info, log)
    if used:
        log('  El stage usa funciones que FMC no tiene: %s' % ', '.join(sorted(used)))
        n_patched += patch_missing_api(d, info, used, log)
    elif not n_patched:
        log('  El stage no usa ninguna funcion ausente. No hace falta parchearlo.')

    # --- cambios de id / guid / musica DENTRO del stage (al final: cambian el largo del archivo)
    music_map = a.get('music_map') or {}
    rmap = {}
    if info.id and stage_id != info.id:
        rmap[info.id] = stage_id
    elif not info.id and a.get('id'):
        log.warn('No pude leer el id dentro del archivo: el id nuevo solo se aplica a mappings.json.')
    if new_guid != info.guid:
        rmap[info.guid] = new_guid
    for mid in info.music:
        nm = (music_map.get(mid) or (None, None))[0]
        if nm and nm != mid:
            if not ID_RE.match(nm):
                raise ValueError('El id de musica "%s" no es valido.' % nm)
            rmap[mid] = nm
    if rmap:
        d = rename_strings(d, rmap, log, info.code)
        log('  Cambiados dentro del stage: %s' % ', '.join('%s -> %s' % kv for kv in sorted(rmap.items())))
    # el juego busca el clip del stage como "stage_<id>": el simbolo exportado tiene que llamarse asi
    sym_fixed = False
    want = 'stage_' + stage_id
    syms = [x for x in symbol_names(d) if re.match(r'^stage_[A-Za-z0-9_]+$', x)]
    if want not in syms:
        if len(syms) == 1:
            try:
                d = rename_strings(d, {syms[0]: want}, log)
                d = rename_symbolclass(d, {syms[0]: want})
                sym_fixed = True
                log('  Simbolo del stage renombrado: %s -> %s (el juego busca "stage_<id>"; sin esto da '
                    'el error #1009 al empezar la partida)' % (syms[0], want))
            except ValueError as e:
                log.warn('No pude renombrar el simbolo %s a %s: %s' % (syms[0], want, e))
        else:
            log.warn('No encuentro el simbolo "%s" dentro del stage (exporta: %s). El juego dara el '
                     'error #1009 al empezar la partida; hay que renombrarlo a mano.'
                     % (want, ', '.join(syms) or 'ninguno'))

    # --- archivos
    out_stage = os.path.join(game, 'data', 'stage', file_name)
    if not dry:
        os.makedirs(os.path.dirname(out_stage), exist_ok=True)
        if n_patched or rmap or sym_fixed:
            write_swf(out_stage, d, compressed)
        else:
            shutil.copyfile(a['stage'], out_stage)      # sin cambios: copia identica
    log('  Stage guardado en data/stage/%s' % file_name)

    music_entries = []
    for mid in info.music:
        nm, ng = music_map.get(mid) or (None, None)
        new_mid = (nm or mid).strip()
        src = find_file(a['music'], mid) if a.get('music') else None
        if not src:
            fin = music_in_fmc(game, existing_map, new_mid)
            if fin:
                log('  Musica "%s": ya esta en tu FMC (mappings.json y data/sound), no hace falta copiarla.' % new_mid)
            elif fin is False:
                log.warn('La musica "%s" esta en mappings.json pero falta su archivo en data/sound: '
                         'el stage sonara sin ella.' % new_mid)
            else:
                log.warn('Falta la musica "%s": no esta en tu FMC ni en la carpeta de musicas, el stage '
                         'sonara sin ella. Pasa la carpeta con sus .ssf.' % new_mid)
            continue
        fid, fguid = read_music_info(src)
        if not fguid:
            log.warn('No pude leer el guid de %s' % os.path.basename(src))
            continue
        if fid and fid != mid:
            log.warn('%s trae el id "%s", no "%s"' % (os.path.basename(src), fid, mid))
        final_guid = (ng or fguid).strip()
        if not GUID_RE.match(final_guid):
            raise ValueError('El guid de musica "%s" no es valido.' % final_guid)
        if new_mid != mid and new_mid in existing_map.get('music', {}):
            log.warn('La musica "%s" ya existe en tu mappings.json: no copio ningun archivo, '
                     'el stage usara esa musica.' % new_mid)
            continue
        dst = os.path.join(game, 'data', 'sound', new_mid + '.ssf')
        rm = {}
        if new_mid != mid and fid and fid != new_mid:
            rm[fid] = new_mid
        elif new_mid != mid and not fid:
            log.warn('No pude leer el id dentro de %s: el id nuevo solo se aplica a mappings.json.'
                     % os.path.basename(src))
        if final_guid != fguid:
            rm[fguid] = final_guid
        if not dry:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if rm:
                md, mcomp = read_swf(src)
                write_swf(dst, rename_strings(md, rm, log), mcomp)
            else:
                shutil.copy2(src, dst)
        music_entries.append((new_mid, final_guid, new_mid + '.ssf'))
        log('  Musica copiada: data/sound/%s.ssf%s' % (new_mid, '  (id/guid cambiados: %s)' % ', '.join(
            '%s -> %s' % kv for kv in sorted(rm.items())) if rm else ''))

    if existing:
        log('Modo --force: solo se reemplazo el archivo del stage y se copio la musica que faltaba.')
        log('No se toca el selector, stage.xml ni la vista previa de un stage que ya existia.')
        for mid, mguid, mfile in music_entries:
            if mid not in existing_map.get('music', {}):
                log.warn('La musica "%s" no esta en mappings.json; anadela a mano (guid %s).' % (mid, mguid))
        log('')
        log('LISTO (archivo reemplazado).')
        return
    # --- vista previa: tu imagen > fondo del stage > imagen por defecto
    prev_rel = 'assets/stage_preview/%s.png' % stage_id
    prev_abs = os.path.join(game, *prev_rel.split('/'))
    im = None
    origin = ''
    try:
        if a.get('preview'):
            im = make_preview_from_image(a['preview'])
            origin = 'a partir de tu imagen'
        elif not a.get('default_preview'):
            im = auto_preview(d, log)
            if im is not None:
                origin = 'automatica con el fondo del stage (revisala)'
            else:
                log('  No encontre un fondo utilizable dentro del stage.')
    except ImportError:
        log.warn('Falta Pillow (pip install pillow): no se pudo armar la vista previa desde el stage.')
    if not dry:
        os.makedirs(os.path.dirname(prev_abs), exist_ok=True)
    if im is not None:
        if not dry:
            im.save(prev_abs)
    else:
        origin = 'por defecto'
        if not dry:
            save_default_preview(prev_abs)
    log('  Vista previa 640x360 %s: %s' % (origin, prev_rel))

    # --- configuracion
    log('Editando la configuracion:')
    edit_mappings(cfg, stage_id, new_guid, file_name, display, a.get('creator'),
                  music_entries, a.get('page'), dry, log)
    edit_stage_xml(os.path.join(game, 'assets', 'config', 'stage.xml'), stage_id, prev_rel, dry, log)

    log('')
    log('LISTO: "%s" portado%s.' % (display, ' (simulacion, no se escribio nada)' if dry else ''))
    if log.warnings:
        log('Hay %d aviso(s) arriba. Revisalos.' % len(log.warnings))
    log('Recuerda: todos los jugadores online deben tener los mismos archivos.')


def gui():
    import tkinter as tk
    import uuid
    from tkinter import filedialog, scrolledtext
    root = tk.Tk()
    root.title('Portear stage a FMC')
    keys = ('game', 'stage', 'music', 'scripts', 'preview', 'name', 'creator', 'page', 'id', 'guid')
    vars_ = {k: tk.StringVar() for k in keys}
    row = [0]
    music_rows = []            # (id original, var id nuevo, var guid nuevo, guid detectado)
    state = {}

    def say(msg):
        box.insert('end', msg + '\n')
        box.see('end')
        root.update()

    def refresh_music(keep=True):
        """Reconstruye las filas de musica a partir del stage y la carpeta de musicas."""
        old = dict((o, (iv.get(), gv.get())) for o, iv, gv, _g in music_rows) if keep else {}
        for w in music_frame.winfo_children():
            w.destroy()
        del music_rows[:]
        res = state.get('res')
        if not res:
            return
        found = {}
        for m in res['music']:
            found[m['id']] = m
        if vars_['music'].get().strip():
            try:
                res2 = inspect_stage(vars_['stage'].get().strip(), vars_['music'].get().strip(),
                                     vars_['game'].get().strip() or None)
                found = dict((m['id'], m) for m in res2['music'])
            except Exception as e:
                say('ERROR al leer las musicas: %s' % e)
        if not res['music']:
            txt = 'Este stage no usa musica externa.'
            if res['embedded']:
                txt = 'Musica integrada en el stage (%s): no hay nada que cambiar.' % ', '.join(res['embedded'])
            tk.Label(music_frame, text=txt, anchor='w').grid(row=0, column=0, sticky='w', padx=6)
            return
        tk.Label(music_frame, text='Musica (original)').grid(row=0, column=0, padx=4, sticky='w')
        tk.Label(music_frame, text='ID nuevo').grid(row=0, column=1, padx=4, sticky='w')
        tk.Label(music_frame, text='GUID nuevo').grid(row=0, column=2, padx=4, sticky='w')
        for i, m in enumerate(res['music']):
            mid = m['id']
            det = found.get(mid, m)
            iv, gv = tk.StringVar(), tk.StringVar()
            iv.set(old.get(mid, (mid, ''))[0] or mid)
            gv.set(old.get(mid, ('', det['guid']))[1] or det['guid'])
            tk.Label(music_frame, text=mid + ('' if det['found'] else (
                '  (ya esta en FMC)' if det.get('in_fmc') else '  (falta el archivo)'))).grid(
                row=i + 1, column=0, sticky='w', padx=4)
            tk.Entry(music_frame, textvariable=iv, width=26).grid(row=i + 1, column=1, padx=4)
            tk.Entry(music_frame, textvariable=gv, width=40).grid(row=i + 1, column=2, padx=4)
            music_rows.append((mid, iv, gv, det['guid']))

    def load_stage():
        p = vars_['stage'].get().strip()
        if not p:
            say('Elige primero el archivo del stage.')
            return
        try:
            res = inspect_stage(p, vars_['music'].get().strip() or None, vars_['game'].get().strip() or None)
        except Exception as e:
            say('ERROR al leer el stage: %s' % e)
            return
        state['res'] = res
        vars_['id'].set(res['id'])
        vars_['guid'].set(res['guid'])
        refresh_music(keep=False)
        say('Stage leido: id=%s  guid=%s' % (res['id'], res['guid']))
        if not res['id_found']:
            say('  (no pude leer el id dentro del archivo: pongo el nombre del archivo)')

    def field(label, key, kind, extra=None):
        tk.Label(root, text=label, anchor='w').grid(row=row[0], column=0, sticky='w', padx=6, pady=3)
        tk.Entry(root, textvariable=vars_[key], width=60).grid(row=row[0], column=1, padx=4)
        if kind:
            def pick():
                if kind == 'dir':
                    v = filedialog.askdirectory()
                elif kind == 'zipdir':
                    v = filedialog.askopenfilename(filetypes=[('Scripts (zip)', '*.zip')]) or filedialog.askdirectory()
                elif kind == 'img':
                    v = filedialog.askopenfilename(filetypes=[('Imagen', '*.png *.jpg *.jpeg')])
                else:
                    v = filedialog.askopenfilename(filetypes=[('Stage SSF', '*.ssf'), ('Todos', '*.*')])
                if v:
                    vars_[key].set(v)
                    if key == 'stage':
                        load_stage()
                    elif key == 'music':
                        refresh_music()
            tk.Button(root, text='...', command=pick).grid(row=row[0], column=2, padx=4)
        if extra:
            extra(row[0])
        row[0] += 1

    field('Carpeta del juego (FMC):', 'game', 'dir')
    field('Stage (.ssf) a portar:', 'stage', 'file',
          lambda r: tk.Button(root, text='Leer stage', command=load_stage).grid(row=r, column=3, padx=4))
    field('ID del stage (editable):', 'id', None)
    field('GUID del stage (editable):', 'guid', None,
          lambda r: tk.Button(root, text='GUID nuevo',
                              command=lambda: vars_['guid'].set(str(uuid.uuid4()))).grid(row=r, column=2, columnspan=2, padx=4))
    field('Carpeta con las musicas (.ssf):', 'music', 'dir')
    music_frame = tk.LabelFrame(root, text='Musicas del stage (version beta): id y guid editables')
    music_frame.grid(row=row[0], column=0, columnspan=4, sticky='we', padx=6, pady=3)
    row[0] += 1
    field('scripts.zip de FMC (opcional):', 'scripts', 'zipdir')
    field('Imagen de vista previa (opcional):', 'preview', 'img')
    field('Nombre visible (opcional):', 'name', None)
    field('Creador (opcional):', 'creator', None)
    field('Pagina del selector (vacio = ultima):', 'page', None)
    dry = tk.IntVar()
    defprev = tk.IntVar()
    tk.Checkbutton(root, text='Solo simular (no escribe nada)', variable=dry).grid(row=row[0], column=1, sticky='w')
    row[0] += 1
    tk.Checkbutton(root, text='Vista previa: usar siempre la imagen por defecto (sin buscarla en el stage)',
                   variable=defprev).grid(row=row[0], column=1, sticky='w')
    row[0] += 1
    box = scrolledtext.ScrolledText(root, width=95, height=18)
    box.grid(row=row[0] + 1, column=0, columnspan=4, padx=6, pady=6)

    def go():
        box.delete('1.0', 'end')
        args = {k: v.get().strip() or None for k, v in vars_.items()}
        args['dry'] = bool(dry.get())
        args['default_preview'] = bool(defprev.get())
        try:
            args['page'] = int(args['page']) if args['page'] else None
        except ValueError:
            say('La pagina del selector debe ser un numero.')
            return
        mm = {}
        for orig, iv, gv, det in music_rows:
            ni, ng = iv.get().strip(), gv.get().strip()
            mm[orig] = (ni if ni and ni != orig else None, ng if ng and ng != det else None)
        args['music_map'] = mm
        if not args['game'] or not args['stage']:
            say('Falta la carpeta del juego o el stage.')
            return
        try:
            run(args, Log(say))
        except Exception as e:
            say('ERROR: %s' % e)
    tk.Button(root, text='PORTEAR', command=go, width=20).grid(row=row[0], column=2, columnspan=2, pady=4)
    root.mainloop()


def _pairs(items, what):
    out = {}
    for it in items or []:
        if '=' not in it:
            raise ValueError('%s debe ser VIEJO=NUEVO (recibi "%s")' % (what, it))
        k, v = it.split('=', 1)
        out[k.strip()] = v.strip()
    return out


def main():
    if len(sys.argv) == 1:
        return gui()
    ap = argparse.ArgumentParser(description='Porta un stage de SSF2 a FMC.')
    ap.add_argument('stage', help='archivo .ssf del stage')
    ap.add_argument('--game', required=True, help='carpeta raiz del juego (la que tiene data/ y assets/)')
    ap.add_argument('--music', help='carpeta con los .ssf de musica que usa el stage')
    ap.add_argument('--scripts', help='scripts.zip (o carpeta) de FMC para detectar la API que falta')
    ap.add_argument('--preview', help='imagen para la vista previa (se recorta a 640x360)')
    ap.add_argument('--default-preview', action='store_true', dest='default_preview',
                    help='usar la imagen por defecto como vista previa (sin buscarla en el stage)')
    ap.add_argument('--name', help='nombre visible en el juego')
    ap.add_argument('--id', help='id nuevo del stage (se reescribe tambien dentro del .ssf)')
    ap.add_argument('--guid', help='guid nuevo del stage (se reescribe tambien dentro del .ssf)')
    ap.add_argument('--music-id', action='append', metavar='VIEJO=NUEVO',
                    help='cambiar el id de una musica (repetible)')
    ap.add_argument('--music-guid', action='append', metavar='ID=GUID',
                    help='cambiar el guid de una musica (repetible; ID es el id original)')
    ap.add_argument('--creator', help='creador (opcional)')
    ap.add_argument('--page', type=int, help='pagina del selector (por defecto la ultima)')
    ap.add_argument('--force', action='store_true', help='reemplazar el .ssf de un stage que ya existe')
    ap.add_argument('--dry', action='store_true', help='simular sin escribir nada')
    ns = ap.parse_args()
    try:
        args = vars(ns)
        ids, guids = _pairs(ns.music_id, '--music-id'), _pairs(ns.music_guid, '--music-guid')
        args['music_map'] = dict((k, (ids.get(k), guids.get(k))) for k in set(ids) | set(guids))
        run(args, Log())
    except Exception as e:
        print('ERROR: %s' % e)
        sys.exit(1)


# Vista previa por defecto (640x360, PNG en base64). Para usar otra, pon un preview_default.png junto al script.
_DEFAULT_PREVIEW_B64 = (
    'iVBORw0KGgoAAAANSUhEUgAAAoAAAAFoCAMAAADw7LpjAAADAFBMVEWF5Olx1N1sxM1ztMJds71VsLpUrLhMq7hVp7dMp7kqvsxL'
    'qLdKqLVKqKcYqLZJprVJpbNKpLZJpLBEo7BblqJUjZtRlaZMobtNlbJKoLhKl7dIobFHoK1HoKxInbNIk7RGnq1Gn6tGnqtGnqpG'
    'napGmq5Gkq9DlqlCjKg/i6IYkJ5Xd5ZNcpJOa4pMao1MaYtMaYpMaYlLapA/gp5Gd4pEboNCa3xJZoVDYYBAaHtAZ3tAZ3pAZ3hA'
    'X30+Zng9YXM9XnNCXHM7W3Y7XHI7W3I7XHE7XHA7W3E7W2s5fZ04eZggeIk5XnA5W24mZ3paTF5KVGk/WXJDU2g7WnA6WW46Wms5'
    'V207S2I3WGw2VWo2VmQ1UmUzUV81TmE0TV4zTF8zTFwxV2oxUWIxTF4yTFgxTFgxTFcyS2AyS1wxS1wxS1gxSlsxSVsxSVcwU2Yw'
    'UWUvUWUvUWQwUV0wTF8wSVovSFkvR1cuT2IuSFofTl0nSFstRmIrRlwuRlcnRlq3CxZhICgyQlA4KDAtRVUtQ1QtQk0uO0ctND4s'
    'RF4sRVcsRFcrRFcsQ1csRFYrQ1UrQU8rN0EqRGAqQlUqQVYpQVMoQFQpP04pO0koO0MpOkMoOkMpOUMoOkIpNT4qL0EqLzcoLzco'
    'LzYpHiInQFMmPlEmPlAmPVAnO0YnO0UmO0UmO0QmOk0nOkUmOkUnOUUnOkQmOkQnOUQmOUQnOUEnN0MmMDclPlElPU4kPFMlPE8k'
    'PE8jPE8kPEwkO1IkO04jO04jO0skOUklNUEkMkMjL0kkMDcjLEAiPVMiOlciOk0hOk0iOkwiOUwhOUshOUYhOEshOEoaOkkhN0kg'
    'N0ogN0kgNkogNkghNkYdNkchMkQiL0QiLkkiLkggLkkiLjkeNUcdMkUcMkMbMkQaMEIaMD8bL0EKMjgiLUgiLUchLUkhLUghLUci'
    'LEchLEghLEchLEMgLEcgLEUZLUEhK0YgK0YgK0UgKkUgKj8fKkMcKD8ZIzkUGyYSExQIExcIEhYNCQn0M/p6AAEAAElEQVR42tT9'
    'fXAb57ngiRIYzr2s1Fb+mA343/6xdbdq6sTn/GWAW7RUgDM+UTxj0vbVhDGJL0GJFCECKFpmN0jbbNVRKMgCIrRSzW7R+VAlTjfb'
    '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'
)


if __name__ == '__main__':
    main()
