"""Password hashing and session-token primitives (stdlib only).

设计依据 docs/superpowers/specs/2026-10-26-user-management-design.md §2/§3：
生产上要的只是「邮箱+口令登录」，不引第三方密码库（argon2/bcrypt 都得多一个
编译依赖）。这里用标准库 ``hashlib.scrypt``，参数 n=2^14, r=8, p=1、16 字节
随机盐；存储串自带参数，方便将来只改默认参数而仍然校验历史的哈希：

    scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>

校验一律用 ``hmac.compare_digest``（常数时间比较，避免按字节提前返回）。
任何解析/参数错误都返回 False：一个被改坏的行不应该让登录接口 500。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

#: scrypt 参数（spec §2 冻结）。32MB 默认 maxmem 下 128*n*r = 16MB，够用。
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_HASH_BYTES = 32

#: 被篡改/损坏的行不应让一次登录变成内存炸弹：超过这个 n 直接判非法。
_MAX_N = 1 << 20
_MAX_RP = 64

_SCRYPT_PREFIX = "scrypt"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def hash_password(password: str) -> str:
    """Hash one password with a fresh random salt; returns the storage string."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                            dklen=_HASH_BYTES)
    return (f"{_SCRYPT_PREFIX}${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
            f"${salt.hex()}${digest.hex()}")


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored string; malformed rows → False."""
    try:
        prefix, n_raw, r_raw, p_raw, salt_hex, hash_hex = stored.split("$")
        if prefix != _SCRYPT_PREFIX:
            return False
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        if not (1 < n <= _MAX_N) or n & (n - 1) or not (
                0 < r <= _MAX_RP and 0 < p <= _MAX_RP):
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        # 长度必须等于本函数写出的格式：scrypt 的 dklen 输出是长摘要的前缀，
        # 一段被截断的哈希会「碰巧」校验通过（代价也变低），不能只看 compare_digest。
        if len(salt) != _SALT_BYTES or len(expected) != _HASH_BYTES:
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r,
                             p=p, dklen=len(expected))
    except (AttributeError, TypeError, ValueError):
        return False
    return hmac.compare_digest(got, expected)


def normalize_email(email: str | None) -> str:
    """Trim + lowercase before storing/looking up (uniqueness is NOCASE)."""
    return (email or "").strip().lower()


def is_valid_email(email: str | None) -> bool:
    """Deliberately simple shape check (spec §4); real delivery is out of scope."""
    return bool(_EMAIL_RE.match(email or ""))


def new_session_token() -> str:
    """Opaque bearer token handed to the browser; 256 bits of entropy."""
    return secrets.token_urlsafe(32)


def session_token_hash(token: str) -> str:
    """DB stores only sha256(token): a DB leak must not leak live sessions."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
