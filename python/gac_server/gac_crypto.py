"""Standalone GAC crypto — pure-Python RFC 8439 ChaCha20-Poly1305 + HKDF.

This is an independent, dependency-free implementation of the AEAD construction
used by the GAC wire protocol. It replaces the cryptography that the original
Doubao client shipped, so the plugin carries no third-party runtime.

The ChaCha20-Poly1305 here is the standard RFC 8439 construction (the one-time
Poly1305 key is the ChaCha20 block at counter 0; the plaintext is encrypted from
counter 1; the Poly1305 input is aad || pad16(aad) || ciphertext || pad16(ct)
|| le64(len aad) || le64(len ciphertext)). HKDF is RFC 5869 (SHA-256).
"""
from __future__ import annotations

import hashlib
import hmac

from array import array

CHACHA20_STATE_INIT = [
    0x61707865,
    0x3320646e,
    0x79622d32,
    0x6b206574,
]

CHACHA20_MIXUP_MATRIX = [
    (0, 4,  8, 12),
    (1, 5,  9, 13),
    (2, 6, 10, 14),
    (3, 7, 11, 15),
    (0, 5, 10, 15),
    (1, 6, 11, 12),
    (2, 7,  8, 13),
    (3, 4,  9, 14),
]

POLY1305_MODULUS  = 0x03_ffffffff_ffffffff_ffffffff_fffffffb
POLY1305_KEY_MASK = 0x0ffffffc_0ffffffc_0ffffffc_0fffffff
POLY1305_OUT_MASK = 0xffffffff_ffffffff_ffffffff_ffffffff


def double_round(ctx: "array[int]"):
    for i, j, k, l in CHACHA20_MIXUP_MATRIX:
        a = ctx[i]
        b = ctx[j]
        c = ctx[k]
        d = ctx[l]
        a = (a + b) & 0xffffffff
        d = d ^ a
        d = ((d << 16) & 0xffffffff | (d >> 16))
        c = (c + d) & 0xffffffff
        b = b ^ c
        b = ((b << 12) & 0xffffffff | (b >> 20))
        a = (a + b) & 0xffffffff
        d = d ^ a
        d = ((d << 8) & 0xffffffff | (d >> 24))
        c = (c + d) & 0xffffffff
        b = b ^ c
        b = ((b << 7) & 0xffffffff | (b >> 25))
        ctx[i] = a
        ctx[j] = b
        ctx[k] = c
        ctx[l] = d


def make_key_block(ctx: "array[int]") -> bytes:
    out = ctx[:]
    double_round(out)
    double_round(out)
    double_round(out)
    double_round(out)
    double_round(out)
    double_round(out)
    double_round(out)
    double_round(out)
    double_round(out)
    double_round(out)
    out[0] = (out[0] + ctx[0]) & 0xffffffff
    out[1] = (out[1] + ctx[1]) & 0xffffffff
    out[2] = (out[2] + ctx[2]) & 0xffffffff
    out[3] = (out[3] + ctx[3]) & 0xffffffff
    out[4] = (out[4] + ctx[4]) & 0xffffffff
    out[5] = (out[5] + ctx[5]) & 0xffffffff
    out[6] = (out[6] + ctx[6]) & 0xffffffff
    out[7] = (out[7] + ctx[7]) & 0xffffffff
    out[8] = (out[8] + ctx[8]) & 0xffffffff
    out[9] = (out[9] + ctx[9]) & 0xffffffff
    out[10] = (out[10] + ctx[10]) & 0xffffffff
    out[11] = (out[11] + ctx[11]) & 0xffffffff
    out[12] = (out[12] + ctx[12]) & 0xffffffff
    out[13] = (out[13] + ctx[13]) & 0xffffffff
    out[14] = (out[14] + ctx[14]) & 0xffffffff
    out[15] = (out[15] + ctx[15]) & 0xffffffff
    return out.tobytes()


def transform_inplace(key: bytes, nonce: bytes, ctr: int, data: "memoryview"):
    idx = 64
    ctx = array('I', CHACHA20_STATE_INIT)
    ctx.frombytes(key)
    ctx.append(ctr)
    ctx.frombytes(nonce)

    for i in range(len(data)):
        if idx == 64:
            idx = 0
            key = make_key_block(ctx)
            ctx[12] += 1

        data[i] ^= key[idx]
        idx += 1


def poly1305_init(key: bytes, nonce: bytes) -> "tuple[int, int]":
    k = memoryview(bytearray(32))
    transform_inplace(key, nonce, 0, k)
    r = int.from_bytes(k[:16], 'little') & POLY1305_KEY_MASK
    s = int.from_bytes(k[16:], 'little')
    return r, s


def poly1305_update(buf: "memoryview", a: int, r: int) -> int:
    for i in range(0, len(buf), 16):
        v = int.from_bytes(buf[i:i + 16], 'little') | (1 << 128)
        a = (a + v) * r % POLY1305_MODULUS
    else:
        return a


def poly1305_finish(l: int, h: int, a: int, r: int, s: int) -> int:
    v = (1 << 128) | (h << 64) | l
    a = (a + v) * r % POLY1305_MODULUS
    return (a + s) & POLY1305_OUT_MASK


def seal_inplace(key: bytes, nonce: bytes, aad: bytes, data: "bytearray"):
    size = len(data)
    view = memoryview(data)
    r, s = poly1305_init(key, nonce)
    bits = poly1305_update(memoryview(aad), 0, r)
    transform_inplace(key, nonce, 1, view)
    bits = poly1305_update(view, bits, r)
    bits = poly1305_finish(len(aad), size, bits, r, s)
    view.release()
    data.extend(bits.to_bytes(16, 'little'))


def open_inplace(key: bytes, nonce: bytes, aad: bytes, data: "bytearray"):
    if len(data) < 16:
        raise ValueError('invalid AEAD tag')
    size = len(data) - 16
    view = memoryview(data)
    r, s = poly1305_init(key, nonce)
    bits = poly1305_update(memoryview(aad), 0, r)
    bits = poly1305_update(view[:size], bits, r)
    bits = poly1305_finish(len(aad), size, bits, r, s)
    if not hmac.compare_digest(bytes(view[size:]), bits.to_bytes(16, 'little')):
        view.release()
        raise ValueError('invalid AEAD tag')
    transform_inplace(key, nonce, 1, view[:size])
    view.release()
    del data[size:]


# ------------------------------------------------------------------- wrappers

def seal(key: bytes, nonce: bytes, aad: bytes, plaintext: bytes) -> bytes:
    """Return ciphertext || 16-byte tag (RFC 8439 AEAD, in-place style)."""
    frame = bytearray(plaintext)
    seal_inplace(key, nonce, aad, frame)
    return bytes(frame)


def open(key: bytes, nonce: bytes, aad: bytes, data: bytes) -> bytes:
    """Verify and decrypt `ciphertext || tag`; raise ValueError on bad tag."""
    frame = bytearray(data)
    open_inplace(key, nonce, aad, frame)
    return bytes(frame)


def hkdf(salt: bytes, ikm: bytes, info: bytes = b'') -> bytes:
    """HKDF-SHA256 (RFC 5869)."""
    key = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(key, info + b'\x01', hashlib.sha256).digest()
