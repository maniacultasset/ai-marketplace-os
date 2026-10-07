"""Login with Amazon for SP-API (S15; D4). The consent URL carries `application_id` (the SP-API
application, `AMAZON_APP_ID`), the exact redirect URI and the state; there is no PKCE. The
callback returns `spapi_oauth_code` and `selling_partner_id`; the code is exchanged at the LWA
token URL with the client credentials in the form. Real shapes are confirmed at DW9 (T77)."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import SecretStr

from app.adapters.oauth.base import CallbackParams, TokenGrant, parse_grant, post_token, with_query


@dataclass(frozen=True, slots=True)
class AmazonOAuthClient:
    application_id: str
    client_id: str
    client_secret: str = field(repr=False)
    authorize_base: str
    token_url: str
    redirect_uri: str
    marketplace: str = "amazon"
    uses_pkce: bool = False

    def authorize_url(self, *, state: str, code_challenge: str | None) -> str:
        if code_challenge is not None:
            raise ValueError("Amazon consent takes no PKCE challenge")
        return with_query(
            self.authorize_base,
            {
                "application_id": self.application_id,
                "redirect_uri": self.redirect_uri,
                "state": state,
            },
        )

    def _form(self, **values: str) -> dict[str, str]:
        return {"client_id": self.client_id, "client_secret": self.client_secret, **values}

    async def exchange(self, *, code_verifier: str | None, callback: CallbackParams) -> TokenGrant:
        if code_verifier is not None:
            raise ValueError("Amazon exchange takes no PKCE verifier")
        body = await post_token(
            self.token_url,
            self._form(
                grant_type="authorization_code",
                code=callback.code,
                redirect_uri=self.redirect_uri,
            ),
        )
        return parse_grant(body, seller_id=callback.selling_partner_id)

    async def refresh(self, refresh_token: SecretStr) -> TokenGrant:
        body = await post_token(
            self.token_url,
            self._form(grant_type="refresh_token", refresh_token=refresh_token.get_secret_value()),
        )
        return parse_grant(body, seller_id=None, keep_refresh=refresh_token)
