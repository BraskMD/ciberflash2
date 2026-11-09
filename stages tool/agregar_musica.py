#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agregar_musica.py - Anade una o varias canciones a un stage de SSF2/FMC.

Uso sin argumentos: abre una ventana. Te pide el stage (.ssf) y te deja anadir
las canciones que quieras (archivo de audio, titulo, artista e inicio del bucle).

Funciona con los dos formatos de stage:
  - Stage con lista de musica externa (propiedad "music"): por cada cancion crea
    data/sound/bgm_<nombre>.ssf, la anade a assets/config/mappings.json (seccion music)
    y la mete en la lista del stage.
  - Stage de formato 9B (musica integrada): mete cada cancion dentro del propio stage
    (como sonido incrustado) y la anade a sus listas stageMusicArray / loopLocArray /
    stageMusicNameArray. No toca mappings.json ni necesita la carpeta de FMC.
En los dos casos el juego elige una cancion al azar. Hace copia de seguridad (.bak_fecha)
de lo que modifica.

Audio: cualquier formato que ffmpeg lea. Si el archivo no es un MP3 que Flash
admita, se convierte solo (ffmpeg junto a este script o en el PATH).

Uso por consola:  python agregar_musica.py --stage stage.ssf --song "cancion.mp3;Titulo;Artista;12.5"
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


# ============================================================================
#  PARTE ESPECIFICA DE LA HERRAMIENTA DE MUSICA
# ============================================================================
import base64
import subprocess
import unicodedata
import uuid

VERSION = '1.0'
CFG_NAME = 'agregar_musica.cfg'
AUDIO_EXTS = ('.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac', '.opus', '.wma', '.mp4', '.webm')
SWF_RATES = {44100: 3, 22050: 2, 11025: 1}

_BR_V1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
_BR_V2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
_SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _here():
    return os.path.dirname(os.path.abspath(sys.argv[0] if sys.argv and sys.argv[0] else __file__))


# ------------------------------------------------------------------ audio ----
def _mp3_header(b, p):
    """Cabecera MPEG capa III en b[p:p+4] -> (largo, samplerate, canales, muestras) o None."""
    if p + 4 > len(b) or b[p] != 0xFF or (b[p + 1] & 0xE0) != 0xE0:
        return None
    ver = (b[p + 1] >> 3) & 3
    layer = (b[p + 1] >> 1) & 3
    if ver == 1 or layer != 1:
        return None
    bri, sri = (b[p + 2] >> 4) & 15, (b[p + 2] >> 2) & 3
    pad = (b[p + 2] >> 1) & 1
    if bri in (0, 15) or sri == 3:
        return None
    sr = _SR[ver][sri]
    if ver == 3:
        br = _BR_V1[bri] * 1000
        ln = 144 * br // sr + pad
        spf = 1152
    else:
        br = _BR_V2[bri] * 1000
        ln = 72 * br // sr + pad
        spf = 576
    ch = 1 if ((b[p + 3] >> 6) & 3) == 3 else 2
    return ln, sr, ch, spf


def parse_mp3(raw):
    """Quita las etiquetas ID3 y devuelve (frames, n_frames, samplerate, canales, muestras_por_frame).
    Lanza ValueError si no es un MP3 que Flash pueda usar tal cual."""
    b = raw
    p = 0
    if b[:3] == b'ID3' and len(b) > 10:
        p = 10 + ((b[6] << 21) | (b[7] << 14) | (b[8] << 7) | b[9])
    end = len(b)
    if b[-128:-125] == b'TAG':
        end -= 128
    # primer frame valido (con el siguiente tambien valido)
    start = None
    q = p
    while q < end - 4:
        h = _mp3_header(b, q)
        if h and (q + h[0] == end or _mp3_header(b, q + h[0])):
            start = q
            break
        q += 1
    if start is None:
        raise ValueError('no es un MP3 valido')
    first = _mp3_header(b, start)
    sr, ch, spf = first[1], first[2], first[3]
    if sr not in SWF_RATES:
        raise ValueError('frecuencia no valida para Flash')
    pos, n = start, 0
    while pos < end:
        h = _mp3_header(b, pos)
        if not h or h[1] != sr or pos + h[0] > end:
            break
        pos += h[0]
        n += 1
    if n < 2:
        raise ValueError('MP3 demasiado corto')
    return bytes(b[start:pos]), n, sr, ch, spf


def find_ffmpeg():
    names = ('ffmpeg.exe', 'ffmpeg') if os.name == 'nt' else ('ffmpeg',)
    base = _here()
    for n in names:
        for sub in ('', 'ffmpeg', os.path.join('ffmpeg', 'bin'), 'bin'):
            c = os.path.join(base, sub, n)
            if os.path.isfile(c):
                return c
    return shutil.which('ffmpeg')


def _run_ffmpeg(src, dst):
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError('Esta cancion hay que convertirla y no encuentro ffmpeg. '
                           'Ponlo junto a este script (ffmpeg.exe) o anadelo al PATH.')
    cmd = [ff, '-y', '-v', 'error', '-i', src, '-vn', '-map_metadata', '-1', '-ar', '44100', '-ac', '2',
           '-c:a', 'libmp3lame', '-b:a', '192k', '-write_xing', '0', '-id3v2_version', '0', dst]
    kw = {}
    if os.name == 'nt':
        kw['creationflags'] = 0x08000000      # CREATE_NO_WINDOW
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    if r.returncode != 0 or not os.path.isfile(dst):
        raise RuntimeError('ffmpeg no pudo convertir "%s": %s' % (
            os.path.basename(src), r.stderr.decode('utf8', 'replace').strip()[-300:]))


def load_audio(path):
    """Devuelve los datos MP3 listos para meter en el .ssf (convierte con ffmpeg si hace falta)."""
    if path.lower().endswith('.mp3'):
        try:
            return parse_mp3(open(path, 'rb').read())
        except ValueError:
            pass
    import tempfile
    tmp = tempfile.mkdtemp(prefix='agregar_musica_')
    try:
        out = os.path.join(tmp, 'x.mp3')
        _run_ffmpeg(path, out)
        return parse_mp3(open(out, 'rb').read())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------- edicion de ABC ----
def _branch_spans(info, m):
    """Intervalos (a, b) que cruza cada salto del metodo. Lanza si hay un lookupswitch."""
    spans = []
    for off, op, v, ln in (info.code.get(m) or []):
        if op == 0x1B:
            raise ValueError('el codigo usa un lookupswitch')
        if 0x0C <= op <= 0x1A or op == 0x10:
            tgt = off + ln + v[0]
            spans.append((min(off + ln, tgt), max(off + ln, tgt)))
    return spans


def rewrite_body(d, info, m, code_edits, pool_edits=(), extra_stack=12):
    """Cambia el codigo del metodo m con code_edits [(inicio_abs, fin_abs, bytes_nuevos)] y aplica pool_edits
    (cambios en el pool de constantes). Devuelve el SWF nuevo. Aborta si un salto cruza un punto de insercion."""
    a = info.abc
    st, en, hv = a.body_span[m]
    cs, cl = a.body[m]
    tail = bytes(d[cs + cl:en])
    if tail != b'\x00\x00':
        raise ValueError('el metodo tiene excepciones o rasgos propios')
    spans = _branch_spans(info, m)
    for s, e, new in code_edits:
        for a0, b0 in spans:
            if a0 < s < b0 or a0 < e < b0:
                raise ValueError('un salto del codigo cruza el punto donde hay que insertar')
    code = bytearray(d[cs:cs + cl])
    for s, e, new in sorted(code_edits, reverse=True):
        code[s - cs:e - cs] = new
    body = (u30_enc(m) + u30_enc(hv[0] + extra_stack) + u30_enc(hv[1]) + u30_enc(hv[2]) + u30_enc(hv[3])
            + u30_enc(len(code)) + bytes(code) + tail)
    d = bytearray(d)
    hp, p, l = find_abc_tag(d)
    if struct.unpack_from('<H', d, hp)[0] & 63 != 63:
        raise ValueError('el tag DoABC usa cabecera corta')
    edits = list(pool_edits) + [(st, en, body)]
    edits.sort(key=lambda e: (e[0], e[1]), reverse=True)
    delta = 0
    for s, e, new in edits:
        d[s:e] = new
        delta += len(new) - (e - s)
    struct.pack_into('<I', d, hp + 2, l + delta)
    return d


def _op_bytes_pushstring(idx):
    return b'\x2c' + u30_enc(idx)


# ------------------------------------------------- .ssf de musica (bgm_*) ----
def _skeleton():
    prefix_b64, suffix_b64 = SKELETON.strip().split('\n')
    return zlib.decompress(base64.b64decode(prefix_b64)), zlib.decompress(base64.b64decode(suffix_b64))


TEMPLATE_ID = 'bgm_theraisingfightingspirit'
TEMPLATE_GUID = 'cc0f422d-d095-40e0-b842-0429ce5bdde1'
TEMPLATE_TITLE = 'The Raising Fighting Spirit'
TEMPLATE_ARTIST = 'Auraka'
TEMPLATE_LOOP = 6904


def _loop_code(ms):
    ms = int(ms)
    if 0 <= ms <= 32767:
        return b'\x25' + u30_enc(ms)
    hi, lo = divmod(ms, 1000)
    if hi > 32767:
        raise ValueError('el punto de bucle es demasiado grande')
    return (b'\x25' + u30_enc(hi) + b'\x25' + u30_enc(1000) + b'\xa2' + b'\x25' + u30_enc(lo) + b'\xa0')


def build_bgm(frames, n_frames, rate, channels, spf, song_id, guid, title, artist, loop_ms):
    """Construye el .ssf de una cancion. Devuelve los bytes del SWF (sin comprimir)."""
    prefix, suffix = _skeleton()
    flags = (2 << 4) | (SWF_RATES[rate] << 2) | (1 << 1) | (1 if channels == 2 else 0)
    body = struct.pack('<HBI', 1, flags, n_frames * spf) + b'\x00\x00' + frames
    tag = struct.pack('<HI', (14 << 6) | 63, len(body)) + body
    d = bytearray(prefix + tag + suffix)
    d[4:8] = struct.pack('<I', len(d))
    info = analyze(bytes(d))
    a = info.abc
    # punto de bucle (musicLoop) en el constructor de Main
    target = None
    for m, ins in info.code.items():
        for k, (off, op, v, ln) in enumerate(ins or []):
            if op == 0x2C and a.str[v[0]] == 'musicLoop' and ins[k + 1][1] == 0x25:
                target = (m, ins[k + 1])
    if target is None:
        raise ValueError('plantilla de musica: no encuentro musicLoop')
    m, (off, op, v, ln) = target
    d = rewrite_body(d, info, m, [(off, off + ln, _loop_code(loop_ms))], extra_stack=0)
    mapping = {TEMPLATE_ID: song_id, TEMPLATE_GUID: guid, TEMPLATE_TITLE: title or '', TEMPLATE_ARTIST: artist or ''}
    # las cadenas vacias dan problemas de pool: si no hay titulo/artista, se deja un espacio
    for k2 in (TEMPLATE_TITLE, TEMPLATE_ARTIST):
        if mapping[k2] == '':
            mapping[k2] = ' '
    d = rename_symbolclass(d, {'Main_' + TEMPLATE_ID: 'Main_' + song_id})
    mapping['Main_' + TEMPLATE_ID] = 'Main_' + song_id
    d = rename_strings(d, mapping, None, None)
    d[4:8] = struct.pack('<I', len(d))
    return bytes(d)


# ---------------------------------------------------- lectura / edicion stage ----
def _register_calls(info, a):
    """Para cada metodo, lista de (indice_instr, clave, instruccion_valor) de llamadas register('clave', ...)."""
    found = {}
    for m, ins in info.code.items():
        if not ins:
            continue
        for k, (off, op, v, ln) in enumerate(ins):
            if op == 0x4F and mname(a, v[0]) == 'register' and v[1] == 2:
                # busca hacia atras el findpropstrict register y la clave
                j = k - 1
                depth = 0
                while j >= 0:
                    if ins[j][1] == 0x5D and mname(a, ins[j][2][0]) == 'register':
                        break
                    j -= 1
                if j >= 0 and ins[j + 1][1] == 0x2C:
                    found.setdefault(m, []).append((j, k, a.str[ins[j + 1][2][0]]))
    return found


def read_stage(path):
    """Devuelve dict con id, guid, canciones declaradas y el metodo del constructor."""
    d, comp = read_swf(path)
    info = analyze(bytes(d))
    a = info.abc
    reg = _register_calls(info, a)
    main = None
    for m, items in reg.items():
        keys = [k for _, _, k in items]
        if 'id' in keys and 'guid' in keys:
            main = m
    if main is None:
        has9b = False
        for i, (nm, ii, tr) in enumerate(a.inst):
            names = set(mname(a, x) for x, k, m in tr)
            if 'getID' in names and 'getGUID' in names:
                has9b = True
        if has9b:
            return _read_9b(d, comp, info)
        raise ValueError('No reconozco este archivo como un stage de SSF2/FMC (no encuentro su id y guid).')
    ins = info.code[main]
    res = {'kind': 'ext', 'd': d, 'comp': comp, 'info': info, 'main': main, 'songs': [], 'has_music': False,
           'embedded': ('stageMusicArray' in a.str), 'id': None, 'guid': None}
    for j, k, key in reg[main]:
        val = ins[j + 2]
        if key == 'id' and val[1] == 0x2C:
            res['id'] = a.str[val[2][0]]
        if key == 'guid' and val[1] == 0x2C:
            res['guid'] = a.str[val[2][0]]
        if key == 'music':
            res['has_music'] = True
            res['songs'] = [a.str[ins[q + 1][2][0]] for q in range(j + 2, k)
                            if ins[q][1] == 0x2C and a.str[ins[q][2][0]] == 'id' and ins[q + 1][1] == 0x2C]
    return res


def stage_add_songs(st, new_ids):
    """Anade canciones a la lista 'music' del stage. Devuelve el SWF nuevo (bytearray)."""
    d, info, main = st['d'], st['info'], st['main']
    a = info.abc
    ins = info.code[main]
    reg = _register_calls(info, a)[main]
    new_ids = [i for i in new_ids if i not in st['songs']]
    if not new_ids:
        return d
    # cadenas nuevas al final del pool
    lay = a.lay
    n_str = lay['str_n']
    new_pool, idx_of = bytearray(), {}

    def sidx(s):
        if s in idx_of:
            return idx_of[s]
        if s in a.str:
            idx_of[s] = a.str.index(s)
        else:
            nb = s.encode('utf-8')
            idx_of[s] = n_str + len(set(v for v in idx_of.values() if v >= n_str))
            new_pool.extend(u30_enc(len(nb)) + nb)
        return idx_of[s]

    entries = b''.join(_op_bytes_pushstring(sidx('id')) + _op_bytes_pushstring(sidx(i)) + b'\x55\x01' for i in new_ids)
    k_new = len(new_ids)
    code_edits = []
    mus = [(j, k) for j, k, key in reg if key == 'music']
    if mus:
        j, k = mus[0]
        # entre la clave y la llamada solo debe haber cadenas y objetos, y al final un newarray
        body = ins[j + 2:k]
        if not body or body[-1][1] != 0x56:
            raise ValueError('la lista de musica del stage tiene una forma que no reconozco')
        for o, op, v, ln in body[:-1]:
            if op not in (0x2C, 0x55):
                raise ValueError('la lista de musica del stage tiene una forma que no reconozco')
        arr = body[-1]
        n_old = arr[2][0]
        code_edits.append((arr[0], arr[0] + arr[3], entries + b'\x56' + u30_enc(n_old + k_new)))
    else:
        guid_calls = [(j, k) for j, k, key in reg if key == 'guid']
        j, k = guid_calls[0]
        find_mn = ins[j][2][0]
        call_mn = ins[k][2][0]
        add = (b'\x5d' + u30_enc(find_mn) + _op_bytes_pushstring(sidx('music')) + entries +
               b'\x56' + u30_enc(k_new) + b'\x4f' + u30_enc(call_mn) + b'\x02')
        end = ins[k][0] + ins[k][3]
        code_edits.append((end, end, add))
    pool_edits = []
    if new_pool:
        added = len(set(v for v in idx_of.values() if v >= n_str))
        pool_edits = [(lay['str_cnt'][0], lay['str_cnt'][1], u30_enc(n_str + added)),
                      (lay['str_end'], lay['str_end'], bytes(new_pool))]
    return rewrite_body(d, info, main, code_edits, pool_edits, extra_stack=8 + 4 * k_new)


# ------------------------------------------------- stages de formato 9B ----
_CHAR_TAGS = (2, 6, 7, 10, 11, 14, 20, 21, 22, 32, 33, 34, 35, 36, 37, 39, 46, 48, 60, 62, 75, 83, 84, 87, 90, 91)


def _find_9b_methods(info, a):
    """(clase, frame1, getID) de la clase principal del stage 9B."""
    for i, (nm, ii, tr) in enumerate(a.inst):
        meths = dict((mname(a, x), m) for x, k, m in tr)
        if 'getID' in meths and 'getGUID' in meths:
            return mname(a, nm), meths.get('frame1'), meths['getID']
    return None, None, None


def _set_props(a, ins):
    out = {}
    for k, (o, op, v, l) in enumerate(ins):
        if op == 0x61:
            out.setdefault(mname(a, v[0]), []).append(k)
    return out


def _read_9b(d, comp, info):
    a = info.abc
    cls, f1, gid = _find_9b_methods(info, a)
    if f1 is None:
        raise ValueError('Stage 9B sin script de arranque (frame1): no se donde declara su musica.')
    ins = info.code[f1]
    sets = _set_props(a, ins)
    sid = None
    for o, op, v, l in info.code[gid]:
        if op == 0x2C:
            sid = a.str[v[0]]
    songs = []
    if 'stageMusicArray' in sets:
        k = sets['stageMusicArray'][0]
        if ins[k - 1][1] == 0x56:
            n = ins[k - 1][2][0]
            songs = [a.str[x[2][0]] for x in ins[k - 1 - n:k - 1] if x[1] == 0x2C]
    elif 'stageMusic' in sets:
        k = sets['stageMusic'][0]
        if ins[k - 1][1] == 0x2C:
            songs = [a.str[ins[k - 1][2][0]]]
    return {'kind': '9b', 'd': d, 'comp': comp, 'info': info, 'id': sid, 'guid': None, 'songs': songs,
            'has_music': bool(songs), 'embedded': True, 'f1': f1}


def _define_sound_tag(char_id, frames, n_frames, rate, channels, spf):
    flags = (2 << 4) | (SWF_RATES[rate] << 2) | (1 << 1) | (1 if channels == 2 else 0)
    body = struct.pack('<HBI', char_id, flags, n_frames * spf) + b'\x00\x00' + frames
    return struct.pack('<HI', (14 << 6) | 63, len(body)) + body


def stage9b_add_songs(st, songs):
    """songs: lista de dict {cls, display, loop_ms, mp3:(frames,n,rate,ch,spf)}. Devuelve el SWF nuevo."""
    d, info = st['d'], st['info']
    a = info.abc
    m = st['f1']
    ins = info.code[m]
    sets = _set_props(a, ins)
    lay = a.lay
    n_str, n_mn = lay['str_n'], lay['mn_n']
    new_pool, new_mn = bytearray(), bytearray()
    sidx_map, midx_map = {}, {}

    def sidx(s):
        if s in sidx_map:
            return sidx_map[s]
        if s in a.str:
            sidx_map[s] = a.str.index(s)
        else:
            nb = s.encode('utf-8')
            sidx_map[s] = n_str + len([1 for v in sidx_map.values() if v >= n_str])
            new_pool.extend(u30_enc(len(nb)) + nb)
        return sidx_map[s]

    pub_ns = None
    for i, raw in a.mn_raw.items():
        if raw[0] == 7 and mname(a, i) in ('stageMusic', 'visible', 'loopLoc'):
            pub_ns = raw[1]
            break
    if pub_ns is None:
        raise ValueError('no encuentro el espacio de nombres del stage')

    def midx(name):
        if name in midx_map:
            return midx_map[name]
        for i, raw in a.mn_raw.items():
            if raw[0] == 7 and raw[1] == pub_ns and mname(a, i) == name:
                midx_map[name] = i
                return i
        midx_map[name] = n_mn + len(midx_map_new)
        midx_map_new.append(name)
        new_mn.extend(b'\x07' + u30_enc(pub_ns) + u30_enc(sidx(name)))
        return midx_map[name]
    midx_map_new = []

    PS = lambda s: b'\x2c' + u30_enc(sidx(s))
    k_new = len(songs)
    edits = []
    raw = lambda q: bytes(d[ins[q][0]:ins[q][0] + ins[q][3]])
    new_cls = b''.join(PS(s['cls']) for s in songs)
    new_loop = b''.join(_loop_code(s['loop_ms']) for s in songs)
    new_names = b''.join(PS(s['display']) for s in songs)
    end_pos = ins[-1][0]
    if ins[-1][1] != 0x47:
        raise ValueError('el script de arranque del stage termina de forma inesperada')
    extra = 0
    tail_add = b''

    def append_literal(name, new_bytes, only_kind):
        """Anade elementos a un array literal ya existente. Devuelve el numero de elementos que tenia."""
        k = sets[name][0]
        if ins[k - 1][1] != 0x56:
            raise ValueError('la lista %s del stage tiene una forma que no reconozco' % name)
        n_old = ins[k - 1][2][0]
        for x in ins[k - 1 - n_old:k - 1]:
            if x[1] not in only_kind:
                raise ValueError('la lista %s del stage tiene una forma que no reconozco' % name)
        o = ins[k - 1][0]
        edits.append((o, o + ins[k - 1][3], new_bytes + b'\x56' + u30_enc(n_old + k_new)))
        return n_old, [a.str[x[2][0]] if x[1] == 0x2C else raw(q) for q, x in
                       zip(range(k - 1 - n_old, k - 1), ins[k - 1 - n_old:k - 1])]

    old_cls_names = []
    if 'stageMusicArray' in sets:
        n_old, old_cls_names = append_literal('stageMusicArray', new_cls, (0x2C,))
        # bucle y nombres
        if 'loopLocArray' in sets:
            append_literal('loopLocArray', new_loop, (0x24, 0x25, 0x2D, 0x2F, 0x2E, 0x27, 0x26, 0x20, 0xA2, 0xA0))
        else:
            base = raw(sets['loopLoc'][0] - 1) if 'loopLoc' in sets else _loop_code(0)
            tail_add += (b'\xd0' + base * 0)  # placeholder, se reconstruye abajo
            tail_add = b'\xd0' + base * n_old + new_loop + b'\x56' + u30_enc(n_old + k_new) + b'\x61' + u30_enc(midx('loopLocArray'))
        if 'stageMusicNameArray' in sets:
            append_literal('stageMusicNameArray', new_names, (0x2C,))
        else:
            tail_add += (b'\xd0' + b''.join(PS(s) for s in old_cls_names) + new_names + b'\x56' + u30_enc(n_old + k_new)
                         + b'\x61' + u30_enc(midx('stageMusicNameArray')))
        extra = 10 + 2 * (n_old + k_new)
    else:
        old_name, old_loop = None, None
        if 'stageMusic' in sets and ins[sets['stageMusic'][0] - 1][1] == 0x2C:
            old_name = a.str[ins[sets['stageMusic'][0] - 1][2][0]]
            old_loop = raw(sets['loopLoc'][0] - 1) if 'loopLoc' in sets else _loop_code(0)
        olds = [old_name] if old_name else []
        n_all = len(olds) + k_new
        tail_add = (b'\xd0' + b''.join(PS(s) for s in olds) + new_cls + b'\x56' + u30_enc(n_all) + b'\x61' + u30_enc(midx('stageMusicArray'))
                    + b'\xd0' + (old_loop if old_name else b'') + new_loop + b'\x56' + u30_enc(n_all) + b'\x61' + u30_enc(midx('loopLocArray'))
                    + b'\xd0' + b''.join(PS(s) for s in olds) + new_names + b'\x56' + u30_enc(n_all) + b'\x61' + u30_enc(midx('stageMusicNameArray')))
        extra = 10 + 2 * n_all
    if tail_add:
        edits.append((end_pos, end_pos, tail_add))
    pool_edits = []
    n_new_str = len([1 for v in sidx_map.values() if v >= n_str])
    if n_new_str:
        pool_edits += [(lay['str_cnt'][0], lay['str_cnt'][1], u30_enc(n_str + n_new_str)),
                       (lay['str_end'], lay['str_end'], bytes(new_pool))]
    if midx_map_new:
        pool_edits += [(lay['mn_cnt'][0], lay['mn_cnt'][1], u30_enc(n_mn + len(midx_map_new))),
                       (lay['mn_end'], lay['mn_end'], bytes(new_mn))]
    d2 = rewrite_body(d, info, m, edits, pool_edits, extra_stack=extra)
    # --- nivel SWF: DefineSound nuevos + entradas en SymbolClass
    tags = swf_tags(bytes(d2))
    used = set()
    sc = None
    for t, p, l, hp in tags:
        if t in _CHAR_TAGS and l >= 2:
            used.add(struct.unpack_from('<H', d2, p)[0])
        if t == 76:
            sc = (t, p, l, hp)
    if sc is None:
        raise ValueError('el stage no tiene SymbolClass')
    ids = []
    nid = max(used | set([100])) + 1
    for _ in songs:
        while nid in used:
            nid += 1
        if nid > 65535:
            raise ValueError('el stage ya no admite mas sonidos')
        ids.append(nid)
        used.add(nid)
        nid += 1
    t, p, l, hp = sc
    nsym = struct.unpack_from('<H', d2, p)[0]
    entries = b''.join(struct.pack('<H', cid) + s['cls'].encode('utf-8') + b'\x00' for cid, s in zip(ids, songs))
    new_sc_body = struct.pack('<H', nsym + k_new) + bytes(d2[p + 2:p + l]) + entries
    new_sc = struct.pack('<HI', (76 << 6) | 63, len(new_sc_body)) + new_sc_body
    sounds = b''.join(_define_sound_tag(cid, *s['mp3']) for cid, s in zip(ids, songs))
    out = bytearray(d2[:hp]) + sounds + new_sc + bytes(d2[p + l:])
    out[4:8] = struct.pack('<I', len(out))
    return out


# ------------------------------------------------------------- mappings ----
def add_music_mappings(path, entries, log):
    text, bom = read_text(path)
    nl = '\r\n' if '\r\n' in text else '\n'
    data = json.loads(text)
    changed = False
    for mid, mguid, mfile in entries:
        if mid in data.get('music', {}):
            continue
        lines = ['\t\t"%s": {' % mid,
                 '\t\t\t"guid": %s,' % json.dumps(mguid),
                 '\t\t\t"file": %s,' % json.dumps(mfile),
                 '\t\t\t"file_pub": %s' % json.dumps(mfile),
                 '\t\t},']
        text = insert_first_in_object(text, 'music', nl.join(lines) + nl, nl)
        changed = True
    if changed:
        json.loads(text)           # que siga siendo JSON valido
        backup(path)
        write_text(path, text, bom)
        log('  mappings.json: anadidas %d canciones' % len(entries))


# ------------------------------------------------------------ utilidades ----
def make_slug(text):
    t = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode('ascii').lower()
    t = re.sub(r'[^a-z0-9]+', '', t)
    return t[:36]


def unique_id(base, taken):
    i = base
    n = 2
    while i in taken:
        i = '%s%d' % (base, n)
        n += 1
    return i


def guess_game_dir(stage_path):
    p = os.path.dirname(os.path.abspath(stage_path))
    if os.path.basename(p).lower() == 'stage' and os.path.basename(os.path.dirname(p)).lower() == 'data':
        return os.path.dirname(os.path.dirname(p))
    return None


def load_cfg():
    try:
        return json.load(open(os.path.join(_here(), CFG_NAME), encoding='utf-8'))
    except Exception:
        return {}


def save_cfg(cfg):
    try:
        json.dump(cfg, open(os.path.join(_here(), CFG_NAME), 'w', encoding='utf-8'))
    except Exception:
        pass


def apply_songs(stage_path, game, songs, log, dry=False):
    """songs: lista de dict {file, title, artist, loop (segundos)}.  Devuelve la lista de ids creados."""
    st = read_stage(stage_path)
    log('Stage: %s%s   canciones que ya tiene: %s' % (st['id'], ' (formato 9B)' if st['kind'] == '9b' else '',
                                                    ', '.join(st['songs']) or '(ninguna)'))
    if not songs:
        raise ValueError('No hay canciones para anadir.')
    if st['kind'] == '9b':
        return apply_songs_9b(st, stage_path, songs, log, dry)
    game = game or guess_game_dir(stage_path)
    if not game or not os.path.isdir(game):
        raise ValueError('Indica la carpeta de tu FMC (la que tiene "data" y "assets").')
    mpath = os.path.join(game, 'assets', 'config', 'mappings.json')
    if not os.path.isfile(mpath):
        raise ValueError('No encuentro %s' % mpath)
    existing = json.loads(read_text(mpath)[0]).get('music', {})
    taken = set(existing) | set(st['songs'])
    sound_dir = os.path.join(game, 'data', 'sound')
    prepared = []
    for s in songs:
        src = s.get('file')
        if not src or not os.path.isfile(src):
            raise ValueError('No encuentro el archivo de audio: %s' % src)
        title = (s.get('title') or '').strip() or os.path.splitext(os.path.basename(src))[0]
        artist = (s.get('artist') or '').strip()
        try:
            loop = float(str(s.get('loop') or 0).replace(',', '.'))
        except ValueError:
            raise ValueError('El inicio del bucle de "%s" no es un numero.' % title)
        loop_ms = int(round(loop * 1000))
        if loop_ms <= 0:
            loop_ms = 1       # con 0 el juego usaria la musica integrada del stage, si la tiene
        frames, n, rate, ch, spf = load_audio(src)
        sid = unique_id('bgm_' + (make_slug(title) or make_slug(os.path.basename(src)) or 'cancion'), taken)
        taken.add(sid)
        guid = str(uuid.uuid4())
        data = build_bgm(frames, n, rate, ch, spf, sid, guid, title, artist, loop_ms)
        prepared.append((sid, guid, data, title))
    if dry:
        for sid, guid, data, title in prepared:
            log('  [simulacion] %s  (%d KB)' % (sid, len(data) // 1024))
        return [p[0] for p in prepared]
    os.makedirs(sound_dir, exist_ok=True)
    for sid, guid, data, title in prepared:
        with open(os.path.join(sound_dir, sid + '.ssf'), 'wb') as f:
            f.write(data)
        log('  Cancion "%s" -> data/sound/%s.ssf' % (title, sid))
    new_swf = stage_add_songs(st, [p[0] for p in prepared])
    backup(stage_path)
    write_swf(stage_path, new_swf, st['comp'])
    log('  Stage actualizado: %s' % os.path.basename(stage_path))
    add_music_mappings(mpath, [(p[0], p[1], p[0] + '.ssf') for p in prepared], log)
    if st['embedded']:
        log.warn('Este stage ademas trae musica integrada propia; el juego elegira al azar entre las de la lista, '
                 'asi que tus canciones tienen que traer su punto de bucle (por defecto lo hacen).')
    return [p[0] for p in prepared]


def apply_songs_9b(st, stage_path, songs, log, dry=False):
    a = st['info'].abc
    taken = set(a.str)
    prepared = []
    for s in songs:
        src = s.get('file')
        if not src or not os.path.isfile(src):
            raise ValueError('No encuentro el archivo de audio: %s' % src)
        title = (s.get('title') or '').strip() or os.path.splitext(os.path.basename(src))[0]
        artist = (s.get('artist') or '').strip()
        try:
            loop = float(str(s.get('loop') or 0).replace(',', '.'))
        except ValueError:
            raise ValueError('El inicio del bucle de "%s" no es un numero.' % title)
        loop_ms = int(round(loop * 1000))
        mp3 = load_audio(src)
        base = '%s_bgm_%s' % (st['id'] or 'stage', make_slug(title) or 'cancion')
        cls = unique_id(base, taken)
        taken.add(cls)
        prepared.append({'cls': cls, 'display': title + (' - ' + artist if artist else ''), 'loop_ms': loop_ms, 'mp3': mp3})
    if dry:
        for s in prepared:
            log('  [simulacion] %s' % s['cls'])
        return [s['cls'] for s in prepared]
    new_swf = stage9b_add_songs(st, prepared)
    backup(stage_path)
    write_swf(stage_path, new_swf, st['comp'])
    for s in prepared:
        log('  Cancion "%s" metida dentro del stage como %s' % (s['display'], s['cls']))
    log('  Stage actualizado: %s' % os.path.basename(stage_path))
    return [s['cls'] for s in prepared]

SKELETON = """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
"""


# ============================================================================
#  VENTANA Y CONSOLA
# ============================================================================
def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    cfg = load_cfg()
    root = tk.Tk()
    root.withdraw()
    stage_path = filedialog.askopenfilename(
        title='Elige el stage (.ssf) al que quieres anadir musica',
        initialdir=cfg.get('stage_dir') or None,
        filetypes=[('Stage SSF2', '*.ssf'), ('Todos', '*.*')])
    if not stage_path:
        return
    cfg['stage_dir'] = os.path.dirname(stage_path)
    try:
        st = read_stage(stage_path)
    except Exception as e:
        messagebox.showerror('Anadir musica', str(e))
        return
    root.deiconify()
    root.title('Anadir musica a un stage  -  %s' % st['id'])
    root.geometry('860x640')
    game_var = tk.StringVar(value=guess_game_dir(stage_path) or cfg.get('game', ''))
    rows = []

    top = tk.Frame(root)
    top.pack(fill='x', padx=10, pady=(10, 4))
    tk.Label(top, text='Stage:', font=('', 10, 'bold')).grid(row=0, column=0, sticky='w')
    tk.Label(top, text='%s    (%s)' % (st['id'], os.path.basename(stage_path))).grid(row=0, column=1, sticky='w')
    tk.Label(top, text='Ya tiene:').grid(row=1, column=0, sticky='nw')
    tk.Label(top, text=', '.join(st['songs']) or '(ninguna cancion)', wraplength=700, justify='left').grid(
        row=1, column=1, sticky='w')
    if st['kind'] == '9b':
        tk.Label(top, text='Formato:').grid(row=2, column=0, sticky='w', pady=(6, 0))
        tk.Label(top, text='Stage 9B: las canciones se meten dentro del propio stage (no hace falta carpeta de FMC).',
                 fg='#3a6').grid(row=2, column=1, sticky='w', pady=(6, 0))
        ent = tk.Entry(top, textvariable=game_var, width=70)
    else:
        tk.Label(top, text='Carpeta de tu FMC:').grid(row=2, column=0, sticky='w', pady=(6, 0))
        ent = tk.Entry(top, textvariable=game_var, width=70)
        ent.grid(row=2, column=1, sticky='w', pady=(6, 0))

    def pick_game():
        p = filedialog.askdirectory(title='Carpeta de FMC (la que tiene data y assets)')
        if p:
            game_var.set(p)
    if st['kind'] != '9b':
        tk.Button(top, text='Examinar...', command=pick_game).grid(row=2, column=2, padx=6, pady=(6, 0))

    tk.Label(root, text='Canciones a anadir', font=('', 10, 'bold')).pack(anchor='w', padx=10, pady=(8, 0))
    box = tk.Frame(root, bd=1, relief='sunken')
    box.pack(fill='both', expand=True, padx=10, pady=4)
    canvas = tk.Canvas(box, highlightthickness=0)
    sb = ttk.Scrollbar(box, orient='vertical', command=canvas.yview)
    inner = tk.Frame(canvas)
    inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.create_window((0, 0), window=inner, anchor='nw')
    canvas.configure(yscrollcommand=sb.set)
    canvas.pack(side='left', fill='both', expand=True)
    sb.pack(side='right', fill='y')

    def add_row(path=''):
        fr = tk.LabelFrame(inner, text='Cancion %d' % (len(rows) + 1), padx=6, pady=4)
        fr.pack(fill='x', padx=4, pady=4)
        v = {'file': tk.StringVar(value=path), 'title': tk.StringVar(), 'artist': tk.StringVar(),
             'loop': tk.StringVar(value='0'), 'frame': fr}

        def pick():
            p = filedialog.askopenfilename(
                title='Elige la cancion', initialdir=cfg.get('audio_dir') or None,
                filetypes=[('Audio', ' '.join('*' + x for x in AUDIO_EXTS)), ('Todos', '*.*')])
            if p:
                cfg['audio_dir'] = os.path.dirname(p)
                v['file'].set(p)
                if not v['title'].get():
                    v['title'].set(os.path.splitext(os.path.basename(p))[0])

        tk.Label(fr, text='Archivo:').grid(row=0, column=0, sticky='w')
        tk.Entry(fr, textvariable=v['file'], width=62).grid(row=0, column=1, sticky='w')
        tk.Button(fr, text='Examinar...', command=pick).grid(row=0, column=2, padx=6)
        tk.Label(fr, text='Titulo:').grid(row=1, column=0, sticky='w')
        tk.Entry(fr, textvariable=v['title'], width=35).grid(row=1, column=1, sticky='w')
        tk.Label(fr, text='Artista:').grid(row=2, column=0, sticky='w')
        tk.Entry(fr, textvariable=v['artist'], width=35).grid(row=2, column=1, sticky='w')
        tk.Label(fr, text='Inicio del bucle (segundos):').grid(row=3, column=0, sticky='w')
        lf = tk.Frame(fr)
        lf.grid(row=3, column=1, sticky='w')
        tk.Entry(lf, textvariable=v['loop'], width=10).pack(side='left')
        tk.Label(lf, text='  0 = vuelve a empezar desde el principio', fg='#666').pack(side='left')

        def remove():
            rows.remove(v)
            fr.destroy()
        tk.Button(fr, text='Quitar', command=remove).grid(row=0, column=3)
        rows.append(v)
        if path and not v['title'].get():
            v['title'].set(os.path.splitext(os.path.basename(path))[0])
        return v

    btns = tk.Frame(root)
    btns.pack(fill='x', padx=10)
    tk.Button(btns, text='+ Anadir otra cancion', command=add_row).pack(side='left')
    log_box = tk.Text(root, height=7, state='disabled', bg='#f4f4f4')
    log_box.pack(fill='x', padx=10, pady=6)

    def say(msg=''):
        log_box.configure(state='normal')
        log_box.insert('end', msg + '\n')
        log_box.see('end')
        log_box.configure(state='disabled')
        root.update_idletasks()

    lg = Log(say)

    def go():
        songs = [dict(file=r['file'].get().strip(), title=r['title'].get(), artist=r['artist'].get(),
                      loop=r['loop'].get()) for r in rows if r['file'].get().strip()]
        game = game_var.get().strip()
        cfg['game'] = game
        save_cfg(cfg)
        go_btn.configure(state='disabled')
        root.config(cursor='watch')
        root.update()
        try:
            ids = apply_songs(stage_path, game, songs, lg)
            say('Listo: %d cancion(es) anadida(s).' % len(ids))
            messagebox.showinfo('Anadir musica', 'Listo. Se anadieron %d cancion(es) al stage "%s".' % (
                len(ids), st['id']))
            for r in list(rows):
                r['frame'].destroy()
            del rows[:]
        except Exception as e:
            say('ERROR: %s' % e)
            messagebox.showerror('Anadir musica', str(e))
        finally:
            go_btn.configure(state='normal')
            root.config(cursor='')

    go_btn = tk.Button(btns, text='Aplicar al stage', command=go, bg='#cfe8cf', width=18)
    go_btn.pack(side='right')
    add_row()
    root.mainloop()


def main(argv=None):
    ap = argparse.ArgumentParser(description='Anade canciones a un stage de SSF2/FMC.')
    ap.add_argument('--stage', help='archivo .ssf del stage')
    ap.add_argument('--game', help='carpeta de FMC (se deduce si el stage esta en data/stage)')
    ap.add_argument('--song', action='append', default=[],
                    help='"archivo;titulo;artista;inicio_bucle_segundos" (se puede repetir)')
    ap.add_argument('--dry', action='store_true', help='no escribe nada')
    a = ap.parse_args(argv)
    if not a.stage:
        run_gui()
        return 0
    songs = []
    for s in a.song:
        parts = s.split(';')
        parts += [''] * (4 - len(parts))
        songs.append(dict(file=parts[0], title=parts[1], artist=parts[2], loop=parts[3] or 0))
    lg = Log()
    try:
        apply_songs(a.stage, a.game, songs, lg, dry=a.dry)
    except Exception as e:
        print('ERROR: %s' % e)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
