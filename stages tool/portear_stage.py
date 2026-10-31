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
  5. Genera la vista previa de 640x360 en assets/stage_preview.

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
    for _ in range(max(n - 1, 0)):
        l, p = u30(b, p)
        a.str.append(bytes(b[p:p + l]).decode('utf8', 'replace'))
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
    return a


def mname(a, i):
    if i <= 0 or i >= len(a.mn):
        return '*'
    nm = a.mn[i]
    return a.str[nm] if nm else '*'


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
        log.warn('No encontre un fondo adecuado dentro del stage: la vista previa automatica '
                 'probablemente salga mal. Pasa una captura con --preview (o en la ventana).')
        bg = bms[0][1]
        if fg is bg:
            fg = None
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
    stage_id = a.get('id') or info.id or os.path.splitext(os.path.basename(a['stage']))[0].lower()
    if not info.id:
        log.warn('No pude leer el id dentro del stage; uso "%s" (revisalo).' % stage_id)
    if not info.guid:
        raise ValueError('No pude leer el guid del stage.')
    log('  id:   %s' % stage_id)
    log('  guid: %s' % info.guid)
    log('  musicas externas que usa: %s' % (', '.join(info.music) or '(ninguna)'))
    if info.embedded_music:
        log('  musica INTEGRADA en el stage: %s' % ', '.join(info.embedded_music))
        log('  (formato 9B: el juego la toma del propio archivo, no hace falta copiar musica)')
    elif not info.music:
        log.warn('El stage no declara musica externa ni integrada reconocible; puede sonar sin musica.')
    existing = stage_id in existing_map.get('stage', {})
    if existing and not a.get('force'):
        raise ValueError('"%s" ya existe en tu mappings.json (es un stage que FMC ya tiene o ya '
                         'lo portaste). Si quieres reemplazar solo su archivo .ssf, usa --force.' % stage_id)
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

    # --- archivos
    out_stage = os.path.join(game, 'data', 'stage', file_name)
    if not dry:
        os.makedirs(os.path.dirname(out_stage), exist_ok=True)
        if n_patched:
            write_swf(out_stage, d, compressed)
        else:
            shutil.copyfile(a['stage'], out_stage)      # sin cambios: copia identica
    log('  Stage guardado en data/stage/%s' % file_name)

    music_entries = []
    for mid in info.music:
        src = find_file(a['music'], mid) if a.get('music') else None
        if not src:
            log.warn('Falta la musica "%s": el stage sonara sin ella. Pasa la carpeta con sus .ssf.' % mid)
            continue
        fid, fguid = read_music_info(src)
        if not fguid:
            log.warn('No pude leer el guid de %s' % os.path.basename(src))
            continue
        if fid and fid != mid:
            log.warn('%s trae el id "%s", no "%s"' % (os.path.basename(src), fid, mid))
        dst = os.path.join(game, 'data', 'sound', mid + '.ssf')
        if not dry:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        music_entries.append((mid, fguid, mid + '.ssf'))
        log('  Musica copiada: data/sound/%s.ssf' % mid)

    if existing:
        log('Modo --force: solo se reemplazo el archivo del stage y se copio la musica que faltaba.')
        log('No se toca el selector, stage.xml ni la vista previa de un stage que ya existia.')
        for mid, mguid, mfile in music_entries:
            if mid not in existing_map.get('music', {}):
                log.warn('La musica "%s" no esta en mappings.json; anadela a mano (guid %s).' % (mid, mguid))
        log('')
        log('LISTO (archivo reemplazado).')
        return
    # --- vista previa
    prev_rel = 'assets/stage_preview/%s.png' % stage_id
    prev_abs = os.path.join(game, *prev_rel.split('/'))
    try:
        if a.get('preview'):
            im = make_preview_from_image(a['preview'])
            origin = 'a partir de tu imagen'
        else:
            im = auto_preview(d, log)
            origin = 'automatica (revisala)'
            if im is None:
                log.warn('No pude generar la vista previa automatica; pasa una imagen con --preview.')
        if im is not None:
            if not dry:
                os.makedirs(os.path.dirname(prev_abs), exist_ok=True)
                im.save(prev_abs)
            log('  Vista previa 640x360 %s: %s' % (origin, prev_rel))
    except ImportError:
        log.warn('Falta Pillow (pip install pillow): no se genero la vista previa.')
        im = None

    # --- configuracion
    log('Editando la configuracion:')
    edit_mappings(cfg, stage_id, info.guid, file_name, display, a.get('creator'),
                  music_entries, a.get('page'), dry, log)
    edit_stage_xml(os.path.join(game, 'assets', 'config', 'stage.xml'), stage_id, prev_rel, dry, log)

    log('')
    log('LISTO: "%s" portado%s.' % (display, ' (simulacion, no se escribio nada)' if dry else ''))
    if log.warnings:
        log('Hay %d aviso(s) arriba. Revisalos.' % len(log.warnings))
    log('Recuerda: todos los jugadores online deben tener los mismos archivos.')


def gui():
    import tkinter as tk
    from tkinter import filedialog, scrolledtext, ttk
    root = tk.Tk()
    root.title('Portear stage a FMC')
    vars_ = {k: tk.StringVar() for k in ('game', 'stage', 'music', 'scripts', 'preview', 'name', 'page')}
    row = [0]

    def field(label, key, kind):
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
            tk.Button(root, text='...', command=pick).grid(row=row[0], column=2, padx=4)
        row[0] += 1
    field('Carpeta del juego (FMC):', 'game', 'dir')
    field('Stage (.ssf) a portar:', 'stage', 'file')
    field('Carpeta con las musicas (.ssf):', 'music', 'dir')
    field('scripts.zip de FMC (opcional):', 'scripts', 'zipdir')
    field('Imagen de vista previa (opcional):', 'preview', 'img')
    field('Nombre visible (opcional):', 'name', None)
    field('Pagina del selector (vacio = ultima):', 'page', None)
    dry = tk.IntVar()
    tk.Checkbutton(root, text='Solo simular (no escribe nada)', variable=dry).grid(row=row[0], column=1, sticky='w')
    row[0] += 1
    box = scrolledtext.ScrolledText(root, width=95, height=22)
    box.grid(row=row[0] + 1, column=0, columnspan=3, padx=6, pady=6)

    def say(msg):
        box.insert('end', msg + '\n')
        box.see('end')
        root.update()

    def go():
        box.delete('1.0', 'end')
        args = {k: v.get().strip() or None for k, v in vars_.items()}
        args['dry'] = bool(dry.get())
        args['page'] = int(args['page']) if args['page'] else None
        if not args['game'] or not args['stage']:
            say('Falta la carpeta del juego o el stage.')
            return
        try:
            run(args, Log(say))
        except Exception as e:
            say('ERROR: %s' % e)
    tk.Button(root, text='PORTEAR', command=go, width=20).grid(row=row[0], column=2, pady=4)
    root.mainloop()


def main():
    if len(sys.argv) == 1:
        return gui()
    ap = argparse.ArgumentParser(description='Porta un stage de SSF2 a FMC.')
    ap.add_argument('stage', help='archivo .ssf del stage')
    ap.add_argument('--game', required=True, help='carpeta raiz del juego (la que tiene data/ y assets/)')
    ap.add_argument('--music', help='carpeta con los .ssf de musica que usa el stage')
    ap.add_argument('--scripts', help='scripts.zip (o carpeta) de FMC para detectar la API que falta')
    ap.add_argument('--preview', help='imagen para la vista previa (se recorta a 640x360)')
    ap.add_argument('--name', help='nombre visible en el juego')
    ap.add_argument('--id', help='forzar el id del stage')
    ap.add_argument('--creator', help='creador (opcional)')
    ap.add_argument('--page', type=int, help='pagina del selector (por defecto la ultima)')
    ap.add_argument('--force', action='store_true', help='reemplazar el .ssf de un stage que ya existe')
    ap.add_argument('--dry', action='store_true', help='simular sin escribir nada')
    ns = ap.parse_args()
    try:
        run(vars(ns), Log())
    except Exception as e:
        print('ERROR: %s' % e)
        sys.exit(1)


if __name__ == '__main__':
    main()
