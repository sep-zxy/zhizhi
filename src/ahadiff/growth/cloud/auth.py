"""Verify a Supabase Auth access token before deriving account identity."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import jwt
from jwt import PyJWKClient


class AuthenticationError(ValueError):
    pass


@dataclass
class AuthVerifier:
    issuer: str
    audience: str = "authenticated"
    jwks_url: str | None = None
    public_key_pem: str | None = None
    _client: PyJWKClient | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        if bool(self.jwks_url) == bool(self.public_key_pem):
            raise ValueError("必须且只能配置 JWKS URL 或验证公钥")
        if self.jwks_url:
            self._client = PyJWKClient(self.jwks_url, cache_jwk_set=True, lifespan=600)

    def verify(self, token: str) -> uuid.UUID:
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            if algorithm not in {"RS256", "ES256"}:
                raise AuthenticationError("只接受非对称签名访问令牌")
            key = (
                self._client.get_signing_key_from_jwt(token).key
                if self._client
                else self.public_key_pem
            )
            if key is None:
                raise AuthenticationError("验证公钥不可用")
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                issuer=self.issuer,
                audience=self.audience,
                leeway=15,
                options={"require": ["exp", "iss", "sub", "aud"]},
            )
            if claims.get("role") != "authenticated":
                raise AuthenticationError("令牌不是已登录用户访问令牌")
            return uuid.UUID(claims["sub"])
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
            raise AuthenticationError("访问令牌无效") from exc
