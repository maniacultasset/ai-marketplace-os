"""Flipkart seller OAuth (S15; D4): authorization code with S256 PKCE. The consent URL carries
`client_id`, the exact redirect URI, `response_type=code`, the state and the challenge; the
callback returns `code`; the token reply names the seller (`seller_id`, not specified by
Flipkart's public docs: checked at DW9, T78)."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import SecretStr

from app.adapters.oauth.base import (
    CallbackParams,
    OAuthProtocolError,
    TokenGrant,
    parse_grant,
    post_token,
    with_query,
)


@dataclass(frozen=True, slots=True)
class FlipkartOAuthClient:
    client_id: str
    client_secret: str = field(repr=False)
    authorize_base: str
    token_url: str
    redirect_uri: str
    marketplace: str = "flipkart"
    uses_pkce: bool = True

    def authorize_url(self, *, state: str, code_challenge: str | None) -> str:
        if code_challenge is None:
            raise ValueError("Flipkart consent needs an S256 PKCE challenge")
        return with_query(
            self.authorize_base,
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
        )

    def _form(self, **values: str) -> dict[str, str]:
        return {"client_id": self.client_id, "client_secret": self.client_secret, **values}

    async def exchange(self, *, code_verifier: str | None, callback: CallbackParams) -> TokenGrant:
        if code_verifier is None:
            raise ValueError("Flipkart exchange needs the PKCE verifier")
        body = await post_token(
            self.token_url,
            self._form(
                grant_type="authorization_code",
                code=callback.code,
                redirect_uri=self.redirect_uri,
                code_verifier=code_verifier,
            ),
        )
        seller = body.get("seller_id")
        if not isinstance(seller, str):
            raise OAuthProtocolError
        return parse_grant(body, seller_id=seller)

    async def refresh(self, refresh_token: SecretStr) -> TokenGrant:
        body = await post_token(
            self.token_url,
            self._form(grant_type="refresh_token", refresh_token=refresh_token.get_secret_value()),
        )
        return parse_grant(body, seller_id=None, keep_refresh=refresh_token)
