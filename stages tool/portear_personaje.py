#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
portear_personaje.py - Porta un personaje de SSF2 a FMC.

Que hace, en orden:
  1. Lee el .ssf del personaje: id (de sus clips _select/_charselect) y guid si lo tiene.
  2. Lo copia a data/character.
  3. Anade las entradas en assets/config/mappings.json: la seccion character_9b
     (o character), el selector de personajes (pagina a elegir) y los aleatorios.
  4. Si das imagenes (portrait, icono, final smash, costumes), las copia a assets/ y
     anade el <fighter> en assets/config/fighter.xml usando las carpetas que ya usan
     los personajes de tu FMC.
  5. Si no das tu icono, pone uno azul liso por defecto (el icono se cambia despues).
     Si no das tu portrait ni tu triangulo de final smash, pone los de Vegeta (estan
     bien alineados) para que el personaje cargue. Para usar otras por defecto, pon
     default_icon.png, default_portrait.png o default_triangle.png junto a este script.

Hace copia de seguridad (.bak_fecha) de cada archivo de configuracion que modifica.
No cambia nada DENTRO del .ssf del personaje (su id y sus clips no se tocan).

Uso sin argumentos: abre una ventana.
Uso por consola:   python portear_personaje.py --help
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
    _s0 = p
    n, p = u30(b, p)
    a.lay = {'str_cnt': (_s0, p), 'str_n': n}
    a.str = ['']
    a.str_pos = [None]       # (inicio del prefijo de longitud, fin de los datos)
    for _ in range(max(n - 1, 0)):
        st = p
        l, p = u30(b, p)
        a.str.append(bytes(b[p:p + l]).decode('utf8', 'replace'))
        a.str_pos.append((st, p + l))
        p += l
    a.lay['str_end'] = p
    n, p = u30(b, p)
    for _ in range(max(n - 1, 0)):
        p += 1
        _, p = u30(b, p)
    n, p = u30(b, p)
    for _ in range(max(n - 1, 0)):
        c, p = u30(b, p)
        for _ in range(c):
            _, p = u30(b, p)
    _s0 = p
    n, p = u30(b, p)
    a.lay['mn_cnt'] = (_s0, p)
    a.lay['mn_n'] = n
    a.mn = [None]
    a.mn_raw = {}
    for _ in range(max(n - 1, 0)):
        k = b[p]
        p += 1
        if k in (7, 0xd):
            ns, p = u30(b, p)
            nm, p = u30(b, p)
            a.mn_raw[len(a.mn)] = (k, ns, nm)
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
    a.lay['mn_end'] = p
    _s0 = p
    n, p = u30(b, p)
    a.lay['meth_cnt'] = (_s0, p)
    a.lay['meth_n'] = n
    a.meth_span = []
    for _ in range(n):
        _ms = p
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
        a.meth_span.append((_ms, p))
    a.lay['meth_end'] = p
    n, p = u30(b, p)
    for _ in range(n):
        _, p = u30(b, p)
        c, p = u30(b, p)
        for _ in range(c):
            _, p = u30(b, p)
            _, p = u30(b, p)

    a.tr_info = []

    def traits(p):
        _cs = p
        c, p = u30(b, p)
        out = []
        spans = []
        for _ in range(c):
            _ts = p
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
            spans.append((_ts, p, k))
        a.tr_info.append((_cs, _cs + len(u30_enc(c)), c, spans, p))
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
    _s0 = p
    c, p = u30(b, p)
    a.lay['body_cnt'] = (_s0, p)
    a.lay['body_n'] = c
    a.body = {}          # metodo -> (offset absoluto del codigo, longitud)
    a.body_span = {}     # metodo -> (inicio, fin, (max_stack, locales, scope_ini, scope_max))
    for _ in range(c):
        st = p
        m, p = u30(b, p)
        hv = []
        for _ in range(4):
            v_, p = u30(b, p)
            hv.append(v_)
        cl, p = u30(b, p)
        a.body[m] = (p, cl)
        p += cl
        ec, p = u30(b, p)
        for _ in range(ec * 5):
            _, p = u30(b, p)
        _, p = traits(p)
        a.body_span[m] = (st, p, tuple(hv))
    a.lay['body_end'] = p
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

NOOPERAND = set([0x01, 0x02, 0x03, 0x07, 0x09, 0x1C, 0x1D, 0x1E, 0x1F, 0x20, 0x21, 0x23,
                 0x26, 0x27, 0x28, 0x29, 0x2A, 0x2B, 0x30, 0x35, 0x36, 0x37, 0x38,
                 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x47, 0x48, 0x4D, 0x50, 0x51, 0x52,
                 0x57, 0x64, 0x70, 0x71, 0x72, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78,
                 0x82, 0x83, 0x84, 0x85, 0x87, 0x90, 0x91, 0x93, 0x95, 0x96, 0x97,
                 0xB1, 0xB3, 0xB4, 0xC0, 0xC1, 0xC4, 0xC5, 0xC6, 0xC7, 0xF3])
for _o in range(0x0C, 0x1B):
    OPS[_o] = 's'
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

LITERALS = (0x20, 0x21, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E, 0x2F)

CALLS = (0x46, 0x4C, 0x4F)

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

ID_RE = re.compile(r'^[A-Za-z0-9_.\-]+$')

GUID_RE = re.compile(r'^[^\s"\\]+$')


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


def engine_gaps(info, d_syms, eng):
    """Funciones que el personaje llama y que el motor de FMC no tiene (ni el propio archivo define)."""
    a = info.abc
    own = set()
    classes = set()
    for i, (nm, ii, tr) in enumerate(a.inst):
        classes.add(mname(a, nm))
        for x, k, m in tr + a.cls[i][1]:
            own.add(mname(a, x))
    for s in d_syms:
        classes.update(s.split('.'))
    calls = {}
    for m, ins in info.code.items():
        for off, op, v, ln in (ins or []):
            if op in (0x46, 0x4F, 0x4C, 0x4E, 0x4A, 0x45):
                n = mname(a, v[0])
                calls.setdefault(n, set()).add(info.owner.get(m, '?'))
    return dict((n, w) for n, w in calls.items()
                if n not in eng and n not in own and n not in classes and not n.startswith('_')
                and n not in SECURITY_CALLS)


# --------------------------------------------------------------------------
#  Parche del SSF2API interno de los archivos 9B (SSF2API update for 9b)
#  El archivo trae su propio SSF2API con addEventListener(param1..4) que pasa los 4
#  argumentos al motor. El motor de FMC (api/SSF2API) solo acepta 3, asi que cada
#  registro de evento da error. La version nueva usa param1 como destino: si es null llama
#  al motor con 3 argumentos y si no, llama al addEventListener del propio objeto.
# --------------------------------------------------------------------------
API_WRAPPERS = {            # nombre: argumentos (locales) que se pasan a la llamada
    'addEventListener': [2, 3, 4],
    'removeEventListener': [2, 3],
    'createTimer': [2, 3, 4, 5],
    'destroyTimer': [2],
}


def _getlocal(n):
    return bytes([0xD0 + n]) if n <= 3 else b'\x62' + u30_enc(n)


def _s24(v):
    return struct.pack('<i', v)[:3]


def build_api_wrapper(i_find, i_call_ready, i_mapi, i_name, args):
    """Cuerpo de: if(!isReady()) return; if(p1==null) m_api.X(args) else p1.X(args);"""
    pre = (b'\xd0\x30' + b'\x5d' + u30_enc(i_find) + b'\x46' + u30_enc(i_call_ready) + u30_enc(0)
           + b'\x96' + b'\x12' + _s24(1) + b'\x47')
    call_args = b''.join(_getlocal(n) for n in args)
    call = b'\x4f' + u30_enc(i_name) + u30_enc(len(args))
    then_b = b'\x60' + u30_enc(i_mapi) + call_args + call
    else_b = _getlocal(1) + call_args + call
    jump = b'\x10' + _s24(len(else_b))
    test = _getlocal(1) + b'\x20' + b'\x14' + _s24(len(then_b) + len(jump))
    return pre + test + then_b + jump + else_b + b'\x47'


def patch_api_wrappers(d, info, log):
    """Pone la version nueva de addEventListener/removeEventListener/createTimer/destroyTimer en
    el SSF2API interno del archivo. Devuelve (bytes, cantidad de metodos cambiados)."""
    a = info.abc
    api = None
    for i, (nm, ii, tr) in enumerate(a.inst):
        if mname(a, nm) == 'SSF2API':
            api = i
    if api is None:
        return d, 0
    edits = []
    done = 0
    for x, k, m in a.cls[api][1]:
        name = mname(a, x)
        if name not in API_WRAPPERS or k != 1 or m not in a.body_span:
            continue
        args = API_WRAPPERS[name]
        ins = info.code.get(m) or []
        i_find = i_ready = i_mapi = i_call = None
        argc = None
        for off, op, v, ln in ins:
            if op == 0x5D and mname(a, v[0]) == 'isReady' and i_find is None:
                i_find = v[0]
            elif op == 0x46 and mname(a, v[0]) == 'isReady' and i_ready is None:
                i_ready = v[0]
            elif op == 0x60 and mname(a, v[0]) == 'm_api' and i_mapi is None:
                i_mapi = v[0]
            elif op == 0x4F and mname(a, v[0]) == name:
                i_call, argc = v[0], v[1]
        if None in (i_find, i_ready, i_mapi, i_call):
            log.warn('SSF2API.%s tiene una forma que no reconozco: no la toco.' % name)
            continue
        if argc == len(args):
            continue                      # ya tiene la version nueva
        code = build_api_wrapper(i_find, i_ready, i_mapi, i_call, args)
        st, en, hv = a.body_span[m]
        body = (u30_enc(m) + u30_enc(max(hv[0], len(args) + 4)) + u30_enc(max(hv[1], len(args) + 2))
                + u30_enc(hv[2]) + u30_enc(max(hv[3], hv[2] + 1)) + u30_enc(len(code)) + code
                + u30_enc(0) + u30_enc(0))
        edits.append((st, en, body))
        done += 1
        log('  SSF2API.%s actualizado (version nueva, con destino)' % name)
    if not edits:
        return d, 0
    d = bytearray(d)
    hp, p, l = find_abc_tag(d)
    edits.sort(reverse=True)
    delta = 0
    for st, en, body in edits:
        d[st:en] = body
        delta += len(body) - (en - st)
    if struct.unpack_from('<H', d, hp)[0] & 63 != 63:
        raise ValueError('El tag DoABC usa cabecera corta; no se puede reescribir con seguridad.')
    struct.pack_into('<I', d, hp + 2, l + delta)
    return d, done


NEW_GETTERS = ['getPlayer', 'getCharacter9B', 'getProjectile9B', 'getItem9B', 'getEnemy9B']


def add_api_getters(d, info, log):
    """Anade al SSF2API interno los metodos getPlayer/getCharacter9B/getProjectile9B/getItem9B/
    getEnemy9B (parte de 'SSF2API update for 9b'). Copia la estructura de getCharacter, que es
    identica salvo el nombre del metodo que llama. Devuelve (bytes, cantidad anadida)."""
    a = info.abc
    n_inst = len(a.inst)
    api = None
    for i, (nm, ii, tr) in enumerate(a.inst):
        if mname(a, nm) == 'SSF2API':
            api = i
    if api is None:
        return d, 0
    cs, ce, cnt, spans, tr_end = a.tr_info[n_inst + api]
    statics = {}
    for j, (x, k, m) in enumerate(a.cls[api][1]):
        statics[mname(a, x)] = (x, k, m, spans[j])
    missing = [g for g in NEW_GETTERS if g not in statics]
    if not missing:
        return d, 0
    if 'getCharacter' not in statics:
        log.warn('No encuentro SSF2API.getCharacter para copiarlo: no anado %s.' % ', '.join(missing))
        return d, 0
    x_t, k_t, m_t, span_t = statics['getCharacter']
    if x_t not in a.mn_raw or k_t != 1 or span_t[2] & 0x40:
        log.warn('SSF2API.getCharacter tiene una forma que no reconozco: no anado %s.' % ', '.join(missing))
        return d, 0
    kmn, ns_t, _nm = a.mn_raw[x_t]
    p = span_t[0]
    _n, p = u30(d, p)
    kind_byte = d[p]
    p += 1
    disp, p = u30(d, p)
    _m, p = u30(d, p)
    if p != span_t[1]:
        log.warn('Rasgo de SSF2API.getCharacter no reconocido: no anado %s.' % ', '.join(missing))
        return d, 0
    meth_raw = bytes(d[a.meth_span[m_t][0]:a.meth_span[m_t][1]])
    st_b, en_b, hv = a.body_span[m_t]
    code_start, cl = a.body[m_t]
    code = bytes(d[code_start:code_start + cl])
    call = None
    for off, op, v, ln in (info.code.get(m_t) or []):
        if op == 0x46 and mname(a, v[0]) == 'getCharacter':
            call = (off, ln, v[1])
    if call is None:
        log.warn('No reconozco el cuerpo de SSF2API.getCharacter: no anado %s.' % ', '.join(missing))
        return d, 0
    rel = call[0] - code_start
    lay = a.lay
    new_str, str_idx = bytearray(), {}
    n_str = lay['str_n']
    for g in missing:
        if g in a.str:
            str_idx[g] = a.str.index(g)
        else:
            nb = g.encode('utf-8')
            str_idx[g] = n_str + len([1 for _ in str_idx.values() if _ >= n_str])
            new_str += u30_enc(len(nb)) + nb
    n_new_str = len(set(v for v in str_idx.values() if v >= n_str))
    new_mn, new_meth, new_body, new_tr = bytearray(), bytearray(), bytearray(), bytearray()
    for j, g in enumerate(missing):
        mn_idx = lay['mn_n'] + j
        m_idx = lay['meth_n'] + j
        new_mn += bytes([kmn]) + u30_enc(ns_t) + u30_enc(str_idx[g])
        new_meth += meth_raw
        code2 = code[:rel] + b'\x46' + u30_enc(mn_idx) + u30_enc(call[2]) + code[rel + call[1]:]
        new_body += (u30_enc(m_idx) + u30_enc(hv[0]) + u30_enc(hv[1]) + u30_enc(hv[2]) + u30_enc(hv[3])
                     + u30_enc(len(code2)) + code2 + u30_enc(0) + u30_enc(0))
        new_tr += u30_enc(mn_idx) + bytes([kind_byte]) + u30_enc(disp) + u30_enc(m_idx)
    k = len(missing)
    edits = [
        (lay['str_cnt'][0], lay['str_cnt'][1], u30_enc(n_str + n_new_str)),
        (lay['str_end'], lay['str_end'], bytes(new_str)),
        (lay['mn_cnt'][0], lay['mn_cnt'][1], u30_enc(lay['mn_n'] + k)),
        (lay['mn_end'], lay['mn_end'], bytes(new_mn)),
        (lay['meth_cnt'][0], lay['meth_cnt'][1], u30_enc(lay['meth_n'] + k)),
        (lay['meth_end'], lay['meth_end'], bytes(new_meth)),
        (cs, ce, u30_enc(cnt + k)),
        (tr_end, tr_end, bytes(new_tr)),
        (lay['body_cnt'][0], lay['body_cnt'][1], u30_enc(lay['body_n'] + k)),
        (lay['body_end'], lay['body_end'], bytes(new_body)),
    ]
    d = bytearray(d)
    hp, p, l = find_abc_tag(d)
    edits.sort(key=lambda e: (e[0], e[1]), reverse=True)
    delta = 0
    for st, en, new in edits:
        d[st:en] = new
        delta += len(new) - (en - st)
    if struct.unpack_from('<H', d, hp)[0] & 63 != 63:
        raise ValueError('El tag DoABC usa cabecera corta; no se puede reescribir con seguridad.')
    struct.pack_into('<I', d, hp + 2, l + delta)
    log('  SSF2API: anadidos %s (iguales a los del parche)' % ', '.join(missing))
    return d, k


# Eventos de la API antigua que el motor de FMC llama de otra forma. El archivo los registra con
# el texto viejo y el motor nunca los dispara. (valor viejo -> valor que usa el motor)
EVENT_RENAMES = {'groundLand': 'groundTouch'}


def engine_event_values(scripts):
    """Valores de las constantes de api/SSF2Event.as del motor (None si no hay scripts)."""
    if not scripts:
        return None
    rx = re.compile(r'const\s+\w+:String\s*=\s*"([^"]*)"')
    if os.path.isdir(scripts):
        p = os.path.join(scripts, 'scripts', 'com', 'mcleodgaming', 'ssf2', 'api', 'SSF2Event.as')
        if not os.path.isfile(p):
            return None
        return set(rx.findall(open(p, encoding='utf-8', errors='replace').read()))
    z = zipfile.ZipFile(scripts)
    for n in z.namelist():
        if n.endswith('api/SSF2Event.as'):
            return set(rx.findall(z.read(n).decode('utf8', 'replace')))
    return None


def fix_event_names(d, info, scripts, log):
    """Cambia en el SSF2Event interno los valores de eventos que el motor no dispara.
    Devuelve (bytes, cantidad)."""
    a = info.abc
    vals = engine_event_values(scripts)
    mapping = {}
    for old, new in EVENT_RENAMES.items():
        if old not in a.str:
            continue
        if vals is not None and (old in vals or new not in vals):
            continue                       # el motor si lo tiene (o no tiene el reemplazo)
        mapping[old] = new
    if not mapping:
        return d, 0
    d = rename_strings(d, mapping, None, info.code)
    log('  Eventos de la API antigua adaptados al motor: %s' % ', '.join(
        '%s -> %s' % kv for kv in sorted(mapping.items())))
    return d, len(mapping)


# ======================================================================
#  PARTE ESPECIFICA DE PERSONAJES
# ======================================================================
import xml.sax.saxutils as _sx

SECTIONS = ('character_9b', 'character')
DEFAULT_DIRS = {'icon': 'assets/char_icon/', 'portrait': 'assets/char_portrait/',
                'fs_cutin': 'assets/char_triangle/', 'costume_data': 'assets/char_costume/'}
# sufijo del nombre de archivo segun el tipo (asi lo usa el fighter.xml de FMC)
FILE_PATTERN = {'icon': '%s_css.png', 'portrait': '%s.png', 'fs_cutin': '%s_triangle.png'}
_ATTR_RE = re.compile(r'([\w:]+)\s*=\s*"([^"]*)"')
ICON_W, ICON_H = 172, 160          # el selector lo dibuja al 25% (iconos de 43x40)


def read_9b(info):
    """Formato 9B: mira getID(), getGUID() y getChar1..N del MainTimeline del personaje."""
    a = info.abc
    out = {'is_9b': False, 'id': None, 'guid': None, 'guid_kind': 'ausente', 'chars': []}
    getters = {}
    for nm, iinit, tr in a.inst:
        for mn, kind, m in tr:
            fn = mname(a, mn)
            if kind == 1 and (fn in ('getID', 'getGUID') or re.match(r'^getChar\d+$', fn)):
                getters[fn] = m
    if 'getID' not in getters or 'getChar1' not in getters:
        return out
    out['is_9b'] = True

    def first_value(m):
        for off, op, v, ln in (info.code.get(m) or []):
            if op == 0x2C:
                return 'string', a.str[v[0]]
            if op in (0x20, 0x21):
                return 'null', None
        return 'desconocido', None
    kind, val = first_value(getters['getID'])
    out['id'] = val if kind == 'string' else None
    if 'getGUID' in getters:
        out['guid_kind'], out['guid'] = first_value(getters['getGUID'])
    key = a.str.index('statsName') if 'statsName' in a.str else -1
    n = 1
    while 'getChar%d' % n in getters:
        ins = info.code.get(getters['getChar%d' % n]) or []
        nm = None
        for k in range(len(ins) - 1):
            if ins[k][1] == 0x2C and ins[k][2][0] == key and ins[k + 1][1] == 0x2C:
                nm = a.str[ins[k + 1][2][0]]
                break
        out['chars'].append(nm)
        n += 1
    return out


def char_info(path):
    """Lee el .ssf del personaje: id, guid, formato (9B o no) y si trae sus propios clips."""
    d, comp = read_swf(path)
    return char_info_bytes(d, comp, path)


def char_info_bytes(d, comp, path):
    syms = symbol_names(d)
    sel = sorted(set(s[:-len('_select')] for s in syms if s.endswith('_select') and not s.endswith('_charselect')))
    csel = sorted(set(s[:-len('_charselect')] for s in syms if s.endswith('_charselect')))
    res = {'compressed': comp, 'select': sel, 'charselect': csel, 'id': None, 'guid': None,
           'id_from': None, 'is_9b': False, 'guid_kind': 'ausente', 'chars': []}
    info = None
    try:
        info = analyze(d)
    except Exception:
        pass
    nb = read_9b(info) if info else {'is_9b': False}
    if nb['is_9b']:
        res.update(is_9b=True, guid=nb['guid'], guid_kind=nb['guid_kind'], chars=nb['chars'])
        if nb['id']:
            res['id'], res['id_from'] = nb['id'], 'getID() del archivo'
    elif info:
        res['guid'] = info.guid
        if info.id:
            res['id'], res['id_from'] = info.id, 'el codigo del archivo'
    if not res['id']:
        ids = [x for x in sel if x in csel] or sel or csel
        if ids:
            res['id'], res['id_from'] = ids[0], 'sus clips _select/_charselect'
    if not res['id']:
        res['id'] = re.sub(r'[^A-Za-z0-9_]+', '', os.path.splitext(os.path.basename(path))[0].lower())
        res['id_from'] = 'el nombre del archivo (verificalo)'
    res['own_select'] = res['id'] in sel
    res['own_charselect'] = res['id'] in csel
    return res


def engine_literals(scripts):
    """Textos entre comillas que usa el motor (de scripts.zip o una carpeta de .as)."""
    lits = set()
    rx = re.compile(r'"([^"\\\n]{3,})"')
    if not scripts:
        return None
    if os.path.isdir(scripts):
        for r, _, fs in os.walk(scripts):
            for f in fs:
                if f.endswith('.as'):
                    lits |= set(rx.findall(open(os.path.join(r, f), encoding='utf-8', errors='replace').read()))
    else:
        z = zipfile.ZipFile(scripts)
        for n in z.namelist():
            if n.endswith('.as'):
                lits |= set(rx.findall(z.read(n).decode('utf8', 'replace')))
    return lits


def family_mapping(d, old_id, new_id, lits=None, series=None):
    """Todo lo que lleve el id viejo (clases, clips exportados, sonidos...) pasa a llevar el nuevo.
    Asi dos personajes con el mismo id base no se pisan: el motor busca clips y sonidos por
    nombre entre todos los .ssf cargados y se queda con el primero que encuentra.
    No toca los textos que el motor tambien usa (icono de serie, etc.)."""
    hp, p, l = find_abc_tag(d)
    a = parse_abc(d, p, l)
    old_c = old_id[:1].upper() + old_id[1:]
    new_c = new_id[:1].upper() + new_id[1:]

    def conv(x):
        return x.replace(old_id, new_id).replace(old_c, new_c)

    def protected(x):
        return (x == series or re.search(r'_symbol\d+$', x) is not None
                or (lits is not None and x in lits and x != old_id))
    pool, excluded = {}, []
    for x in set(a.str[1:]):
        t = conv(x)
        if t != x:
            if protected(x):
                excluded.append(x)
            else:
                pool[x] = t
    sym = {}
    for x in symbol_names(d):
        t = conv(x)
        if t != x and not protected(x):
            sym[x] = t
    # el id nuevo no puede chocar con nombres que ya existen en el archivo
    taken = set(a.str[1:]) | set(symbol_names(d))
    clash = sorted(set(pool.values()) & (taken - set(pool)))[:5] + sorted(set(sym.values()) & (taken - set(sym)))[:5]
    if clash or len(set(pool.values())) != len(pool):
        raise ValueError('El id nuevo "%s" choca con nombres que YA existen en el archivo (%s). '
                         'Elige otro id.' % (new_id, ', '.join(clash[:4]) or 'nombres repetidos'))
    return pool, sym, sorted(excluded)


def read_fighter_xml(game):
    p = os.path.join(game, 'assets', 'config', 'fighter.xml')
    if not os.path.isfile(p):
        return p, None, False
    text, bom = read_text(p)
    return p, text, bom


def parse_fighters(text):
    """Lista de <fighter> de fighter.xml: [{'id':..., 'txtNumber':..., 'res': {...}}].
    Es tolerante (espacios alrededor del '=', comentarios, un '&' suelto) a proposito:
    el fighter.xml real de FMC no es XML estricto."""
    out = []
    if not text:
        return out
    t = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    for ch in re.split(r'<fighter\b', t)[1:]:
        head = ch.split('>', 1)[0]
        a = dict(_ATTR_RE.findall(head))
        res = {}
        for r in re.findall(r'<resource\b(.*?)/>', ch.split('</fighter>', 1)[0], re.S):
            res.update(dict(_ATTR_RE.findall(r)))
        out.append({'id': a.get('id', ''), 'txtNumber': a.get('txtNumber'), 'attrs': a, 'res': res})
    return out


def detect_fighter_style(text):
    """Carpetas por tipo de imagen y tamano de texto mas usado, mirando los <fighter> existentes."""
    from collections import Counter
    dirs = dict(DEFAULT_DIRS)
    cnt = dict((k, Counter()) for k in dirs)
    tn = Counter()
    for f in parse_fighters(text):
        if f['txtNumber']:
            tn[f['txtNumber']] += 1
        for k in dirs:
            v = f['res'].get(k)
            if v and '/' in v:
                cnt[k][v.rsplit('/', 1)[0] + '/'] += 1
    for k in dirs:
        if cnt[k]:
            dirs[k] = cnt[k].most_common(1)[0][0]
    return dirs, (tn.most_common(1)[0][0] if tn else '12')


def save_default_image(kind, dst):
    """Guarda la imagen por defecto (las de Vegeta, alineadas). Si hay un default_<tipo>.png
    junto al script se usa ese; si no, la incluida dentro de este archivo. No necesita Pillow."""
    import base64
    ext = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'default_%s.png' % kind)
    if os.path.isfile(ext):
        shutil.copyfile(ext, dst)
    else:
        with open(dst, 'wb') as f:
            f.write(base64.b64decode(''.join(_DEFAULTS_B64[kind].split())))


def save_default_if_missing(kind, dst, log):
    """Pone la imagen por defecto solo si no hay ya un archivo con ese nombre (no pisa el tuyo)."""
    if os.path.exists(dst):
        log('  (ya existe %s: no lo sobrescribo)' % os.path.basename(dst))
        return False
    save_default_image(kind, dst)
    return True


def prepare_image(src, dst, size=None, log=None):
    """Copia una imagen como PNG. Con size=(w,h) la recorta/escala para llenar ese tamano."""
    try:
        from PIL import Image
    except ImportError:
        if size:
            raise ImportError('Falta Pillow (pip install pillow) para ajustar el tamano de la imagen.')
        shutil.copyfile(src, dst)
        return
    im = Image.open(src).convert('RGBA')
    if size:
        w, h = size
        s = max(w / im.width, h / im.height)
        im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.LANCZOS)
        l, t = (im.width - w) // 2, (im.height - h) // 2
        im = im.crop((l, t, l + w, t + h))
    im.save(dst)


def make_icon_from_portrait(src, dst):
    """Icono 172x160 a partir de un portrait (560x246, personaje a la izquierda)."""
    from PIL import Image
    im = Image.open(src).convert('RGBA')
    side = min(im.height, int(im.width * 0.45))
    box = im.crop((0, 0, int(side * ICON_W / ICON_H), side)) if side * ICON_W / ICON_H <= im.width else im
    s = max(ICON_W / box.width, ICON_H / box.height)
    box = box.resize((max(1, round(box.width * s)), max(1, round(box.height * s))), Image.LANCZOS)
    l, t = (box.width - ICON_W) // 2, 0
    box.crop((l, t, l + ICON_W, t + ICON_H)).save(dst)


def edit_char_mappings(path, char_id, section, entry, page, add_random, dry, log):
    text, bom = read_text(path)
    nl = '\r\n' if '\r\n' in text else '\n'
    data = json.loads(text)
    changed = False
    # --- entrada del personaje
    if char_id in data.get('character', {}) or char_id in data.get('character_9b', {}):
        log.warn('"%s" ya estaba en mappings.json (character/character_9b); no toco su entrada.' % char_id)
    else:
        keys = ['file', 'file_pub', 'guid', 'name', 'seriesIcon']
        keys = [k for k in keys if k in entry]
        lines = ['\t\t"%s": {' % char_id]
        for i, k in enumerate(keys):
            lines.append('\t\t\t"%s": %s%s' % (k, json.dumps(entry[k], ensure_ascii=False),
                                              ',' if i < len(keys) - 1 else ''))
        lines.append('\t\t}' + (',' if data.get(section) else ''))
        text = insert_first_in_object(text, section, nl.join(lines) + nl, nl)
        changed = True
        log('  mappings.json: anadida la entrada %s "%s"' % (section, char_id))
    # --- selector de personajes
    scr = data['character_select_screen']
    flat = [x for k, v in scr.items() if re.match(r'^rows_\d+$', k) and isinstance(v, list)
            for r in v for x in r]
    if char_id in flat:
        log.warn('"%s" ya esta en el selector de personajes; no lo toco.' % char_id)
    else:
        pages = int(scr.get('rows_max', 1))
        pg = page or pages
        key = 'rows_%d' % pg
        if key not in scr:
            raise ValueError('No existe la pagina %d del selector (hay %d).' % (pg, pages))
        rows = scr[key]
        allrows = [r for k, v in scr.items() if re.match(r'^rows_\d+$', k) and isinstance(v, list) for r in v]
        cap = max(len(r) for r in allrows)          # ancho real del selector (todas las paginas)
        maxrows = max(len(v) for k, v in scr.items() if re.match(r'^rows_\d+$', k) and isinstance(v, list))
        last = rows[-1]
        tail = [last[-1]] if last and last[-1] == 'random' else []
        body = last[:len(last) - len(tail)]

        def row_txt(r):
            return '[' + ', '.join(json.dumps(x) for x in r) + ']'
        start = text.index('"%s": [' % key, text.index('"character_select_screen"'))
        old = row_txt(last)
        at = text.index(old, start)
        if len(last) + 1 <= cap:
            new = row_txt(body + [char_id] + tail)
            where = 'fila %d' % len(rows)
        else:
            new = row_txt(body) + ',' + nl + '\t\t\t' + row_txt([char_id] + tail)
            where = 'fila nueva %d' % (len(rows) + 1)
            if len(rows) + 1 > maxrows:
                log.warn('La pagina %d tendra %d filas y tus otras paginas tienen como maximo %d; si el '
                         'selector no las muestra, elige otra pagina.' % (pg, len(rows) + 1, maxrows))
        text = text[:at] + new + text[at + len(old):]
        changed = True
        log('  mappings.json: "%s" puesto en la pagina %d (%s) del selector (%s)' % (
            char_id, pg, where, scr.get('rows_%d_name' % pg) or 'sin nombre'))
    # --- aleatorios
    if add_random:
        if char_id not in data['random_characters']['characters']:
            i = text.index('"random_characters"')
            j = text.index('"characters": [', i)
            k = text.index(']', j)
            text = text[:k] + ', ' + json.dumps(char_id) + text[k:]
            changed = True
            log('  mappings.json: anadido a los personajes aleatorios')
    json.loads(text)
    if changed and not dry:
        backup(path)
        write_text(path, text, bom)


def edit_fighter_xml(path, text, bom, char_id, attrs, res, dry, log):
    """Anade <fighter .../> a fighter.xml (o lo crea si no existe)."""
    def q(v):
        return _sx.quoteattr(v if v is not None else '')
    if text is not None and any(f['id'] == char_id for f in parse_fighters(text)):
        log.warn('"%s" ya esta en fighter.xml; no lo toco.' % char_id)
        return
    nl = '\r\n' if text and '\r\n' in text else '\n'
    ind = '\t'
    lines = [ind + '<fighter id=%s alternateID=%s charName=%s cssName=%s info=%s txtNumber=%s>' % (
        q(char_id), q(attrs.get('alternateID', '')), q(attrs['charName']), q(attrs['cssName']),
        q(attrs.get('info', '')), q(attrs['txtNumber'])),
        ind * 2 + '<resource portrait=%s icon=%s fs_cutin=%s costume_data=%s/>' % (
            q(res.get('portrait', '')), q(res.get('icon', '')), q(res.get('fs_cutin', '')),
            q(res.get('costume_data', ''))),
        ind + '</fighter>']
    block = nl.join(lines) + nl
    if text is None:
        text = '<?xml version="1.0" encoding="utf-8"?>' + nl + '<fighters_config>' + nl + block + '</fighters_config>' + nl
        bom = True
        log.warn('No existia assets/config/fighter.xml: lo cree nuevo.')
    else:
        m = list(re.finditer(r'</[A-Za-z_][\w\-\.]*>\s*$', text))
        if not m:
            raise ValueError('No pude encontrar el cierre de fighter.xml.')
        pos = m[-1].start()
        text = text[:pos] + block + text[pos:]
    import xml.etree.ElementTree as ET
    ET.fromstring(('<x>' + block + '</x>').encode('utf-8'))      # valida SOLO lo que anadimos
    log('  fighter.xml: anadida la entrada de "%s"' % char_id)
    if not dry:
        if os.path.isfile(path):
            backup(path)
        write_text(path, text, bom)


def run(a, log):
    game = a['game']
    cfg = os.path.join(game, 'assets', 'config', 'mappings.json')
    if not os.path.isfile(cfg):
        raise ValueError('No encuentro assets/config/mappings.json dentro de "%s". '
                         'Elige la carpeta raiz del juego.' % game)
    dry = a.get('dry', False)
    section = a.get('section') or 'auto'
    if section not in SECTIONS + ('auto',):
        raise ValueError('La seccion debe ser auto, character_9b o character.')
    existing_map = json.loads(read_text(cfg)[0])
    log('Leyendo el personaje: %s' % a['character'])
    ci = char_info(a['character'])
    char_id = (a.get('id') or ci['id']).strip()
    if not ID_RE.match(char_id):
        raise ValueError('El id "%s" no es valido: usa solo letras, numeros, _ - .' % char_id)
    log('  id: %s   (leido de %s)' % (char_id, ci['id_from']) if not a.get('id') else '  id: %s   (indicado por ti)' % char_id)
    if a.get('id') and ci['id'] and a['id'] != ci['id'] and ci['id_from'].startswith('sus clips'):
        log.warn('El archivo trae los clips de "%s", pero usas el id "%s". Si no coinciden, el '
                 'personaje no se vera bien en los menus.' % (ci['id'], a['id']))
    log('  clips propios en el archivo: icono del selector (%s), imagen grande (%s)' % (
        'si' if ci['own_select'] else 'no', 'si' if ci['own_charselect'] else 'no'))
    if section == 'auto':
        section = 'character_9b' if ci['is_9b'] or not ci['guid'] else 'character'
    log('  formato: %s  ->  seccion %s' % (
        '9B (tiene getID y getChar1)' if ci['is_9b'] else 'no tiene getID/getChar1 (no parece 9B)', section))
    if ci['is_9b']:
        log('  personajes dentro del archivo: %s' % ', '.join(str(x) for x in ci['chars']))
    syms = set(symbol_names(read_swf(a['character'])[0]))
    guid = a.get('guid') or None
    if guid and not GUID_RE.match(guid):
        raise ValueError('El guid "%s" no es valido.' % guid)
    rmap = {}                       # textos que se cambian DENTRO del .ssf
    # --- id
    old_id = ci['id'] if ci['id_from'] in ('getID() del archivo', 'el codigo del archivo') else None
    if old_id and char_id != old_id:
        rmap[old_id] = char_id
        for suf in ('_select', '_charselect'):
            if old_id + suf in syms:
                rmap[old_id + suf] = char_id + suf
    elif char_id != ci['id']:
        log.warn('No pude leer el id dentro del archivo, asi que el id nuevo solo cambia en mappings.json '
                 'y el motor probablemente lo rechace.')
    # --- guid (el motor compara el del archivo con el de mappings.json: tienen que ser iguales)
    if ci['is_9b'] and ci['guid_kind'] != 'ausente':
        if ci['guid_kind'] == 'null':
            if guid:
                log.warn('getGUID() de este archivo no devuelve nada y no hay donde poner un guid dentro: '
                         'se queda en null (con otro valor el motor lo rechazaria).')
            guid = None
            log('  guid: null   (getGUID() del archivo no devuelve nada)')
        else:
            if guid is None:
                guid = ci['guid']
            elif guid != ci['guid']:
                rmap[ci['guid']] = guid
            log('  guid: %s' % guid)
    elif section == 'character' or guid:
        if not guid:
            guid = ci['guid']
            if not guid:
                raise ValueError('La seccion "character" necesita guid y no pude leerlo del archivo: indicalo.')
        elif ci['guid'] and guid != ci['guid']:
            rmap[ci['guid']] = guid
        elif not ci['guid']:
            log.warn('No pude leer el guid dentro del archivo: el nuevo solo se pone en mappings.json.')
        log('  guid: %s' % guid)
    else:
        log('  guid: ninguno (null)')
    if rmap:
        log('  Se cambiara DENTRO del archivo: %s' % ', '.join('%s -> %s' % kv for kv in sorted(rmap.items())))
    rename_family = bool(a.get('rename_all', True)) and bool(old_id) and char_id != old_id
    if guid:
        for sec in SECTIONS:
            for k, v in existing_map.get(sec, {}).items():
                if k != char_id and v.get('guid') == guid:
                    log.warn('El guid "%s" ya lo usa el personaje "%s" de tu mappings.json.' % (guid, k))
    in_map = char_id in existing_map.get('character', {}) or char_id in existing_map.get('character_9b', {})
    if in_map and not a.get('force'):
        raise ValueError('"%s" ya existe en tu mappings.json (personaje de FMC o ya portado). '
                         'Usa --force para reemplazar solo su archivo .ssf.' % char_id)
    display = a.get('name') or ' '.join(w.capitalize() for w in re.split(r'[_\-\s]+', char_id))
    ssf_name = a.get('file') or (os.path.splitext(os.path.basename(a['character']))[0] + '.ssf')
    if not re.match(r'^[A-Za-z0-9_.\-]+\.ssf$', ssf_name):
        raise ValueError('Nombre de archivo no valido: %s' % ssf_name)
    def _clash(name):
        return [k for sec in SECTIONS for k, v in existing_map.get(sec, {}).items()
                if v.get('file') == name and k != char_id]
    clash = _clash(ssf_name)
    if clash and not a.get('file') and not _clash(char_id + '.ssf'):
        log('  El archivo %s ya lo usa "%s": lo guardo como %s.ssf' % (ssf_name, clash[0], char_id))
        ssf_name = char_id + '.ssf'
        clash = []
    if clash:
        raise ValueError('El archivo %s ya lo usa el personaje "%s". Usa otro nombre (--file).' % (ssf_name, clash[0]))
    dst = os.path.join(game, 'data', 'character', ssf_name)
    if not dry:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isfile(dst) and not in_map:
            backup(dst)
        d0, comp0 = read_swf(a['character'])
        info0 = analyze(d0)
        n_sec = patch_security(d0, info0, log) if not in_map else 0
        n_api = 0
        if ci['is_9b'] and not in_map and not a.get('no_api_patch'):
            d0, n_api = patch_api_wrappers(d0, info0, log)
            if n_api:
                info0 = analyze(d0)
            d0, n_get = add_api_getters(d0, info0, log)
            if n_get:
                info0 = analyze(d0)
            n_sec += n_api + n_get
        if ci['is_9b'] and not in_map and not a.get('no_api_patch'):
            d0, n_ev = fix_event_names(d0, info0, a.get('scripts'), log)
            if n_ev:
                info0 = analyze(d0)
            n_sec += n_ev
        if a.get('scripts') and not in_map:
            try:
                gaps = engine_gaps(info0, symbol_names(d0), engine_identifiers(a['scripts']))
            except Exception as e:
                gaps = {}
                log.warn('No pude comparar con el motor: %s' % e)
            if gaps:
                names = sorted(gaps)
                log.warn('El personaje llama a %d funcion(es) que tu FMC no tiene: %s. Cada vez que su '
                         'codigo las ejecute dara error y el ataque se quedara a medias. Hay que anadirlas al '
                         'motor (Character.as) o anularlas.' % (len(names), ', '.join(names[:14])
                                                                 + (' ...' if len(names) > 14 else '')))
            else:
                log('  API: el personaje no llama a ninguna funcion que falte en tu FMC.')
        elif not a.get('scripts') and not in_map:
            log('  (pasa tu scripts.zip para comprobar si llama a funciones que FMC no tiene)')
        if (rmap or n_sec) and not in_map:
            n_sites = 0
            for ins in info0.code.values():
                for off, op, v, ln in (ins or []):
                    if op == 0x2C and info0.abc.str[v[0]] in rmap:
                        n_sites += 1
            mapping = dict(rmap)
            smap = dict((k, v) for k, v in rmap.items() if k in syms)
            if rename_family:
                lits = engine_literals(a.get('scripts'))
                fpool, fsym, excl = family_mapping(d0, old_id, char_id, lits, a.get('series'))
                for k, v in fpool.items():
                    mapping.setdefault(k, v)
                for k, v in fsym.items():
                    smap.setdefault(k, v)
                log('  Todo lo que lleva "%s" pasa a "%s": %d textos y %d simbolos exportados '
                    '(para poder usar los dos personajes a la vez).' % (old_id, char_id, len(fpool), len(fsym)))
                if excl:
                    log('  Sin tocar (los usa el motor): %s' % ', '.join(excl[:8]))
                if lits is None:
                    log('  (sin scripts.zip no puedo saber que nombres usa el motor; deje fuera los '
                        'terminados en _symbolN, que son iconos de serie)')
            d2 = rename_strings(d0, mapping, None, info0.code)
            if smap:
                d2 = rename_symbolclass(d2, smap)
            left = [x for x in symbol_names(d2) if old_id in x.replace(char_id, '')]
            if rename_family and left:
                log('  Quedan %d simbolos con "%s" sin cambiar (ej. %s).' % (len(left), old_id, ', '.join(left[:3])))
            _cnt = __import__('collections').Counter
            before = set(x for x, n in _cnt(symbol_names(d0)).items() if n > 1)
            dup = [x for x, n in _cnt(symbol_names(d2)).items() if n > 1 and x not in before
                   and not any(smap.get(o) == x for o in before)]
            if dup:
                log.warn('Despues del cambio hay simbolos repetidos: %s' % ', '.join(dup[:5]))
            chk = char_info_bytes(d2, comp0, a['character'])
            write_swf(dst, d2, comp0)
            log('  Cambiado dentro del archivo (%d usos del id en el codigo, %d textos, %d simbolos exportados).' % (
                n_sites, len(mapping), len(smap)))
            if chk['id'] != char_id:
                log.warn('Despues del cambio el archivo dice id "%s" (esperaba "%s").' % (chk['id'], char_id))
            if chk['is_9b'] and chk['chars'] and chk['chars'][0] != char_id:
                log.warn('getChar1() define a "%s", no a "%s": el motor registra las estadisticas con ese '
                         'nombre y no coincide con el de mappings.json.' % (chk['chars'][0], char_id))
            if chk['is_9b'] and chk['guid_kind'] == 'string' and chk['guid'] != guid:
                log.warn('getGUID() devuelve "%s" y mappings.json tendra "%s".' % (chk['guid'], guid))
        else:
            shutil.copyfile(a['character'], dst)
    log('  Archivo guardado en data/character/%s' % ssf_name)
    if in_map:
        log('Modo --force: solo se reemplazo el archivo del personaje.')
        log('')
        log('LISTO (archivo reemplazado).')
        return
    entry = {'file': ssf_name, 'file_pub': ssf_name}
    entry['guid'] = guid
    entry['name'] = display
    if a.get('series'):
        entry['seriesIcon'] = a['series']
    # --- imagenes y fighter.xml
    fpath, ftext, fbom = read_fighter_xml(game)
    dirs, txtn = detect_fighter_style(ftext)
    res = {}
    needs_icon = not ci['own_select']
    needs_portrait = not ci['own_charselect']
    res = {}
    if a.get('portrait') or needs_portrait:
        res['portrait'] = dirs['portrait'] + FILE_PATTERN['portrait'] % char_id
    if a.get('icon') or a.get('portrait') or needs_icon:
        res['icon'] = dirs['icon'] + FILE_PATTERN['icon'] % char_id
    res['fs_cutin'] = dirs['fs_cutin'] + FILE_PATTERN['fs_cutin'] % char_id
    if a.get('costume'):
        res['costume_data'] = dirs['costume_data'] + os.path.basename(a['costume'])
    if any(f['id'] == char_id for f in parse_fighters(ftext)):
        if res:
            log('  fighter.xml ya tiene a "%s": no toco ni sus entradas ni sus imagenes.' % char_id)
        res = {}
    try:
        if 'portrait' in res:
            out = os.path.join(game, *res['portrait'].split('/'))
            if not dry:
                os.makedirs(os.path.dirname(out), exist_ok=True)
                if a.get('portrait'):
                    prepare_image(a['portrait'], out)
                else:
                    save_default_if_missing('portrait', out, log)
            log('  Imagen portrait: %s%s' % (res['portrait'], '' if a.get('portrait') else
                                            '  (imagen por defecto de Vegeta: cambiala por la tuya)'))
        if 'icon' in res:
            out = os.path.join(game, *res['icon'].split('/'))
            if not dry:
                os.makedirs(os.path.dirname(out), exist_ok=True)
                if a.get('icon'):
                    prepare_image(a['icon'], out, (ICON_W, ICON_H))
                elif a.get('portrait'):
                    make_icon_from_portrait(a['portrait'], out)
                else:
                    save_default_if_missing('icon', out, log)
            log('  Imagen icon: %s%s' % (res['icon'], '' if a.get('icon') else (
                '  (generada a partir del portrait)' if a.get('portrait') else
                '  (icono por defecto azul: cambialo por el tuyo)')))
        if 'fs_cutin' in res:
            out = os.path.join(game, *res['fs_cutin'].split('/'))
            if not dry:
                os.makedirs(os.path.dirname(out), exist_ok=True)
                if a.get('fs_cutin'):
                    prepare_image(a['fs_cutin'], out)
                else:
                    save_default_if_missing('triangle', out, log)
            log('  Imagen fs_cutin: %s%s' % (res['fs_cutin'], '' if a.get('fs_cutin') else
                                             '  (triangulo por defecto de Vegeta)'))
    except ImportError as e:
        raise ValueError('%s (hace falta para ajustar tus imagenes; sin tus imagenes usa las de Vegeta por defecto)' % e)
    if res.get('costume_data'):
        out = os.path.join(game, *res['costume_data'].split('/'))
        if not dry:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            shutil.copyfile(a['costume'], out)
        log('  Costumes: %s' % res['costume_data'])
    want_x = bool(res) or bool(a.get('info') or a.get('css_name'))
    log('Editando la configuracion:')
    edit_char_mappings(cfg, char_id, section, entry, a.get('page'), not a.get('no_random'), dry, log)
    if want_x:
        attrs = {'charName': display, 'cssName': a.get('css_name') or display,
                 'info': a.get('info') or '', 'txtNumber': a.get('txt_size') or txtn}
        edit_fighter_xml(fpath, ftext, fbom, char_id, attrs, res, dry, log)
    else:
        log('  fighter.xml: sin imagenes ni textos externos, no hace falta tocarlo.')
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
    root.title('Portear personaje a FMC')
    keys = ('game', 'character', 'id', 'name', 'series', 'guid', 'page', 'icon', 'portrait',
            'fs_cutin', 'costume', 'css_name', 'info', 'scripts')
    vars_ = dict((k, tk.StringVar()) for k in keys)
    section = tk.StringVar()
    section.set('auto')
    row = [0]

    def say(msg):
        box.insert('end', msg + '\n')
        box.see('end')
        root.update()

    def load_char():
        p = vars_['character'].get().strip()
        if not p:
            say('Elige primero el archivo del personaje.')
            return
        try:
            ci = char_info(p)
        except Exception as e:
            say('ERROR al leer el personaje: %s' % e)
            return
        vars_['id'].set(ci['id'])
        if not vars_['name'].get().strip():
            vars_['name'].set(' '.join(w.capitalize() for w in re.split(r'[_\-\s]+', ci['id'])))
        say('Personaje leido: id=%s (de %s)' % (ci['id'], ci['id_from']))
        say('  clips propios: icono %s, imagen grande %s' % (
            'si' if ci['own_select'] else 'NO', 'si' if ci['own_charselect'] else 'NO'))
        say('  formato: %s' % ('9B (getID/getChar1)' if ci['is_9b'] else 'no es 9B'))
        if ci['is_9b']:
            say('  personajes dentro del archivo: %s' % ', '.join(str(x) for x in ci['chars']))
            say('  getGUID(): %s' % (ci['guid'] if ci['guid_kind'] == 'string' else 'devuelve null (guid null en mappings)'))
        elif ci['guid']:
            say('  guid en el archivo: %s' % ci['guid'])

    def field(label, key, kind, extra=None):
        tk.Label(root, text=label, anchor='w').grid(row=row[0], column=0, sticky='w', padx=6, pady=3)
        tk.Entry(root, textvariable=vars_[key], width=60).grid(row=row[0], column=1, padx=4)
        if kind:
            def pick():
                if kind == 'dir':
                    v = filedialog.askdirectory()
                elif kind == 'img':
                    v = filedialog.askopenfilename(filetypes=[('Imagen', '*.png *.jpg *.jpeg')])
                elif kind == 'zip':
                    v = filedialog.askopenfilename(filetypes=[('Scripts (zip)', '*.zip')]) or filedialog.askdirectory()
                elif kind == 'json':
                    v = filedialog.askopenfilename(filetypes=[('Costumes', '*.as *.json *.txt'), ('Todos', '*.*')])
                else:
                    v = filedialog.askopenfilename(filetypes=[('Personaje SSF', '*.ssf'), ('Todos', '*.*')])
                if v:
                    vars_[key].set(v)
                    if key == 'character':
                        load_char()
            tk.Button(root, text='...', command=pick).grid(row=row[0], column=2, padx=4)
        if extra:
            extra(row[0])
        row[0] += 1

    field('Carpeta del juego (FMC):', 'game', 'dir')
    field('Personaje (.ssf) a portar:', 'character', 'file',
          lambda r: tk.Button(root, text='Leer personaje', command=load_char).grid(row=r, column=3, padx=4))
    field('ID (editable: tambien se cambia dentro del .ssf):', 'id', None)
    field('Nombre visible:', 'name', None)
    field('Icono de serie (opcional, ej. DragonBallsign):', 'series', None)
    field('Guid (editable; vacio = el del archivo):', 'guid', None,
          lambda r: tk.Button(root, text='GUID nuevo',
                              command=lambda: vars_['guid'].set(str(uuid.uuid4()))).grid(row=r, column=2, columnspan=2, padx=4))
    tk.Label(root, text='Seccion de mappings.json:').grid(row=row[0], column=0, sticky='w', padx=6)
    fr = tk.Frame(root)
    fr.grid(row=row[0], column=1, sticky='w')
    tk.Radiobutton(fr, text='automatica', variable=section, value='auto').pack(side='left')
    tk.Radiobutton(fr, text='character_9b', variable=section, value='character_9b').pack(side='left')
    tk.Radiobutton(fr, text='character', variable=section, value='character').pack(side='left')
    row[0] += 1
    renall = tk.IntVar()
    renall.set(1)
    tk.Checkbutton(root, text='Si cambio el id: renombrar TODO lo que lo lleve (poder usar los dos a la vez)',
                   variable=renall).grid(row=row[0], column=1, sticky='w')
    row[0] += 1
    field('scripts.zip de FMC (opcional):', 'scripts', 'zip')
    field('Pagina del selector (vacio = ultima):', 'page', None)
    field('Portrait 560x246 (vacio = el de Vegeta):', 'portrait', 'img')
    field('Icono 172x160 (vacio = el azul por defecto):', 'icon', 'img')
    field('Triangulo final smash 491x132 (vacio = el de Vegeta):', 'fs_cutin', 'img')
    field('Archivo de costumes (opcional):', 'costume', 'json')
    field('Texto del selector (opcional):', 'css_name', None)
    field('Descripcion (opcional):', 'info', None)
    dry = tk.IntVar()
    norand = tk.IntVar()
    tk.Checkbutton(root, text='No anadir a los personajes aleatorios', variable=norand).grid(row=row[0], column=1, sticky='w')
    row[0] += 1
    tk.Checkbutton(root, text='Solo simular (no escribe nada)', variable=dry).grid(row=row[0], column=1, sticky='w')
    row[0] += 1
    box = scrolledtext.ScrolledText(root, width=95, height=18)
    box.grid(row=row[0] + 1, column=0, columnspan=4, padx=6, pady=6)

    def go():
        box.delete('1.0', 'end')
        args = dict((k, v.get().strip() or None) for k, v in vars_.items())
        args['section'] = section.get()
        args['dry'] = bool(dry.get())
        args['no_random'] = bool(norand.get())
        args['rename_all'] = bool(renall.get())
        try:
            args['page'] = int(args['page']) if args['page'] else None
        except ValueError:
            say('La pagina del selector debe ser un numero.')
            return
        if not args['game'] or not args['character']:
            say('Falta la carpeta del juego o el personaje.')
            return
        try:
            run(args, Log(say))
        except Exception as e:
            say('ERROR: %s' % e)
    tk.Button(root, text='PORTEAR', command=go, width=20).grid(row=row[0], column=2, columnspan=2, pady=4)
    root.mainloop()


def main():
    if len(sys.argv) == 1:
        return gui()
    ap = argparse.ArgumentParser(description='Porta un personaje de SSF2 a FMC.')
    ap.add_argument('character', help='archivo .ssf del personaje')
    ap.add_argument('--game', required=True, help='carpeta raiz del juego (la que tiene data/ y assets/)')
    ap.add_argument('--id', help='id del personaje (por defecto se lee del archivo)')
    ap.add_argument('--name', help='nombre visible')
    ap.add_argument('--series', help='icono de serie (seriesIcon), opcional')
    ap.add_argument('--guid', help='guid a guardar en mappings.json (por defecto ninguno)')
    ap.add_argument('--section', choices=SECTIONS + ('auto',), default='auto',
                    help='seccion de mappings.json (auto: la detecta del archivo)')
    ap.add_argument('--file', help='nombre del .ssf dentro de data/character (por defecto el mismo)')
    ap.add_argument('--page', type=int, help='pagina del selector (por defecto la ultima)')
    ap.add_argument('--portrait', help='imagen grande (se usa tal cual; 560x246, personaje a la izquierda)')
    ap.add_argument('--icon', help='icono del selector (se recorta a 172x160)')
    ap.add_argument('--fs-cutin', dest='fs_cutin', help='imagen del final smash')
    ap.add_argument('--default-triangle', action='store_true', dest='default_triangle',
                    help=argparse.SUPPRESS)  # ya no hace falta: sin --fs-cutin siempre se pone el de Vegeta
    ap.add_argument('--costume', help='archivo de costumes (JSON) para assets/char_costume')
    ap.add_argument('--css-name', dest='css_name', help='texto bajo el icono del selector')
    ap.add_argument('--info', help='descripcion del personaje')
    ap.add_argument('--txt-size', dest='txt_size', help='tamano de texto del selector (txtNumber)')
    ap.add_argument('--scripts', help='scripts.zip (o carpeta) de FMC: evita tocar nombres que usa el motor')
    ap.add_argument('--no-api-patch', action='store_true', dest='no_api_patch',
                    help='no tocar el SSF2API interno del archivo (por defecto se actualiza si es 9B)')
    ap.add_argument('--keep-names', action='store_true', dest='keep_names',
                    help='al cambiar el id, cambiar SOLO el id (no el resto de nombres que lo llevan)')
    ap.add_argument('--no-random', action='store_true', dest='no_random')
    ap.add_argument('--force', action='store_true', help='reemplazar el .ssf de un personaje que ya existe')
    ap.add_argument('--dry', action='store_true', help='simular sin escribir nada')
    ns = ap.parse_args()
    try:
        args = vars(ns)
        args['rename_all'] = not ns.keep_names
        run(args, Log())
    except Exception as e:
        print('ERROR: %s' % e)
        sys.exit(1)


# Imagenes por defecto: icono 172x160 (azul liso OPNormal), portrait 560x246 y triangulo 491x132 (las de Vegeta).
_DEFAULTS_B64 = {
    'icon': (
        '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'
        'Gorc3PwGZw4UJxQ+Cc4AAAAASUVORK5CYII='
    ),
    'portrait': (
        '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'
    ),
    'triangle': (
        'iVBORw0KGgoAAAANSUhEUgAAAesAAACECAMAAACH+POoAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAABRUExURQAAABkA'
        'BkQMFAcHB/nMp9SMXJtcNDUfHjktL6qqqtfX1/j4+AAAFkQ3EwAAMoFoI72nShEXcjlB0CYmJn9/f6+vt/f390dHRzUvP2hoaAAA'
        'AGN2LCYAAAAbdFJOU///////////////////////////////////ACc0CzUAAAAJcEhZcwAAEOcAABDnAYvjZwsAAAAYdEVYdFNv'
        'ZnR3YXJlAFBhaW50Lk5FVCA1LjEuMvu8A7YAAAC2ZVhJZklJKgAIAAAABQAaAQUAAQAAAEoAAAAbAQUAAQAAAFIAAAAoAQMAAQAA'
        'AAIAAAAxAQIAEAAAAFoAAABphwQAAQAAAGoAAAAAAAAAa60BAOgDAABrrQEA6AMAAFBhaW50Lk5FVCA1LjEuMgADAACQBwAEAAAA'
        'MDIzMAGgAwABAAAAAQAAAAWgBAABAAAAlAAAAAAAAAACAAEAAgAEAAAAUjk4AAIABwAEAAAAMDEwMAAAAAD8MVT0vL3p6AAACAxJ'
        'REFUeF7t2+F200YURWGHEJyASUiBpvD+D1ptnbnMyLKxLI3z5569VokjyVLWfFq3iml3v9e0a962293dffhwd3d/r1e1u6H7e7bN'
        '9xH7d7v7+/Ltn/Q+XYP98dpty9Z5WmtdVz8UcZtbS/uU6N+t2aNz27pXts7Te1ifnuBhfbxnfNMQ1+CrrXtl6zytta7rf8ma7acm'
        'uPafm+Bxf9i6X7bO03prDPX6svV0e8S+09YhzW9rlu6VrfO0xVoKVZJteqVQo92ubJikfaet6/1j637ZOk9brPXZ5yXr461R7C3f'
        'lrQt7g6kbd0rW+dp7TKGMBLhhWrryndzzej0XrZ5gt8mW+dpvbWUkAkvbJZaa9/8uY3jY1u1tnePbJ2nrdYfPvCqztzrrKur0rbj'
        'Cc7zX7ms25Ct87R+EdunsxCeao+csymtYt/0TmjfH9YcY+se2TpP261bMVnFdnWtdbwKa77auke2ztMW65i39ZWE+LN2ylp7MIx3'
        'kt5bX7OfV5buk63ztG4ZtfrtFK9GMlR8d85aRx1bxyvt1zZb98nWeVprzfqfm+K73cexhwe059Yhqeo7q7ulb5Gt87TeWtq4YaJX'
        'tNs9PMha2uyRYCTBTyVpn5O2db9snae11hiQnBDGZSpNaM8/L0H6Yahqt9IcYen+2TpPa5cytMM6tPVc1nrrGDlKkvshCuuptP7U'
        '+coF3eZsnact1uGLDGEv5U+fRuax6RTHsLUObVlzFkvfKlvnaf1iSlDa4XPKWlN8hB7SBGdrof5jrXQ2Zem+2TpP26yZvKFNp62Z'
        '4jGjOSa2Klm3k5v494Eql3Obs3WetixlaFcjWfM5iTwjjpR13A3VGu32fiH/vcctsnWetlpr9oZ2WEuzxhQPvbLpL9aWvk22ztO2'
        '5aza+zGssSucTRzZTnBZx9+A8AzG+y19y2ydp60LKu39XoaYoTdiTtLvXXWCz615hTZ3jqVvka3ztH1J0d7v+UQET5nxtbpqpnNc'
        'ay1t4ljEP37UFLf0bbJ1nnosKs9VmsdS0yxXMsWRvTHdpV+oh+3cK+yLp7NyYtc1W+epz7Lu9xJFV56PjyP1EN/JNia4XMOad2Et'
        'ad5RTuo6Z+s89VnYmOLyw/Hx8WloxB6EtU+FtO4M/uthvRtrO98yW+ep1+JqirfWn4eqtvaGdDjr//Sx9Ptk6zz1Wt6Y4q31lyE+'
        'F0UUX7ZTSD89PT7Kmix962ydp34LzBSPedxaS5u/15hObyZ8WNv5PbJ1nnouMtr6VETaT0/S/vKFKa7PQeXMtrC29Htl6zz1XWae'
        'sfRbVWhrkn/+PD5+Dao8kWlLneDlze7G2TpP/cdnPJ3R4xCflT498buYtut7S79/ts7TLazrFFehXLeP8A+2ft9snaf+1tMp3sZd'
        'wFdJY23p98zWeeptfRjit6v6Nx01TfcI6f2e4w+H8mZ302ydp+3WX/+EmpxxnM9xthfoUVrHPT+P3GPlRF/LqV3XbJ2nLdZyeXnR'
        '169fUZOmFI/nOHvYGtI6krvjcHgewvplrJxwqFzKdcjWeVpvXWfut7Fj6/AMZ0xDOL4SRx4Or69ox7m+fZM41yiXc5uzdZ7WWCOg'
        'qcvMLThD0q6CU+34DKXOb8VxWKNdrdHmfJyRyqXdhmydp2utWXcEsHl9ba1lE1N8rh1bQ7o9rlqTzqnzjZcZ9th7e7bO0zXWcn59'
        '/ackaz1FRdIOTapPZO1rns/0NaTrnVNONcTWuJa9t2brPC21PnamOnURIqxl0jrrK/FawmHNlvNPZvPrce7yI7krs3WellnLebry'
        'hE99Ovs+KcTDmEbkMbaFNPdHedOQznXKmuRdfix3VbbO0xLrc9Kt9cvL9+8/fvws/RiSXHjzZ2EeCmnuBx0X79N3et47ZW3t9dk6'
        'T5etz0uz7ljjgrScatWuOmOuwrk1Jn3HE99pa2uvzdZ5WmKN9HlrQkgz/N8Sgq3d6eq+eKfexZ+27p2t83TJOib4uVXXfzekCV6l'
        'oyquOyHi2e1YOfr58+1N2uetrb0mW+eplzWWBetE+OqYdqLP7w0lbVv3ztZ5WmbN+paFniRrTdy/WSuO0j9lw5l0P3CkrXtm6zxt'
        'sWbN9YnIEkG63vr0dfl01tbXZus8LbeerzqTlDW/jTX/6HOccrmSttj6+mydp+XW+hqx4mzRU5JmbqH6S8ut9VsX/4aYWz8/23pN'
        'ts7TUmtWF9labOf5rKc10rLWteM6Sle29ZpsnadL1mifsm4FWPcln6VctuazVKTDmmu016rXtvT12TpPS6y11lpjxe9aWn3i+8Ph'
        '7e3SFMdZ057+m8QWKau3N66r89frKluvy9Z5umw91Y5iqippYzQyHplrGxXYsV+/ENZ+Xo/Ef4prxtkL8/gTWHpdts7TUmutNl9R'
        'nUqTpniBKhXUpl9DmGMr4/FmmGkzwdtr2LpHts7TEuvQJn1qObdGQVO8Gofs/JmNac5X/UlTba5XTjwWdxnxE5Qfy12VrfO0zBpt'
        'xVqft2aKYyxhKpSzOKa1brWPJzgxt2vlh3JXZes8XWMdM3RurRk7MozFb1Pn0xRvj+POQFufosyvgHZcpfxQ7qpsnafrrDWpWfdq'
        'oW1Ke15fmeOF8EyyRje0ZY22rtRqjycdrOOeKj+Uuypb52mpNdqarFWb5Ny60DJrdOuTHM4k61Z7vNDQ4RB3VvmR3FXZOk/LrdHW'
        '6lfteDWSNDHFLz2dyRjrSJ/AcO8oeUdI23pLts7S79//AxdQJ6+wIfL3AAAAAElFTkSuQmCC'
    ),
}


if __name__ == '__main__':
    main()
