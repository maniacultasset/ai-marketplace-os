"""The `bootstrap` check's auth leg (CODE-PLAN S10 C14): HTTP only, against the built image
serving the demo seed with the app logged in as app_rw. Stdlib only.

Usage: python3 scripts/ci/auth_e2e.py <base-url>   e.g. http://127.0.0.1:8000

The one non-HTTP step (a second membership for operator@acme.test, reviewer in bharat) is
bootstrap.sh's psql setup, run before this script. Every step leaves the seed as it found it.
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request
from typing import Any

SEED_PASSWORD = "devpassword123"  # noqa: S105 - the demo seed's local-only password (A§5)
API = "/api/v1"


class Leg:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.steps = 0

    def call(
        self, method: str, path: str, body: Any = None, token: str | None = None, **headers: str
    ) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        url = self.base + API + path  # the base is the caller's http://127.0.0.1:<port>
        request = urllib.request.Request(url, data=data, method=method)  # noqa: S310
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        for name, value in headers.items():
            request.add_header(name.replace("_", "-"), value)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            raw, status = error.read(), error.code
        return status, (json.loads(raw) if raw else None)

    def expect(self, label: str, want: int, got: tuple[int, Any]) -> Any:
        status, body = got
        self.steps += 1
        if status != want:
            code = body.get("error", {}).get("type") if isinstance(body, dict) else None
            sys.exit(f"auth e2e: {label}: want {want}, got {status} ({code})")
        return body

    def login(self, email: str, password: str = SEED_PASSWORD, want: int = 200) -> Any:
        got = self.call("POST", "/auth/login", {"email": email, "password": password})
        return self.expect(f"login {email}", want, got)


def claims(token: str) -> dict[str, Any]:
    """The JWT payload, unverified: the leg only reads `sub` and `tid` the server just issued."""
    part = token.split(".")[1]
    decoded: dict[str, Any] = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    return decoded


def member_id(leg: Leg, owner: str, email: str) -> str:
    listed = leg.expect("find member", 200, leg.call("GET", "/members", token=owner))
    for member in listed["data"]:
        if member["email"] == email:
            return str(member["user_id"])
    sys.exit(f"auth e2e: {email} is not listed")


def run(base: str) -> int:
    leg = Leg(base)

    # owner logs in and reads itself
    owner = leg.login("owner@acme.test")
    acme = claims(owner["access_token"])["tid"]
    me = leg.expect("me", 200, leg.call("GET", "/auth/me", token=owner["access_token"]))
    if (me["role"], me["tenant_id"]) != ("owner", acme):
        sys.exit(f"auth e2e: me says {me['role']} in {me['tenant_id']}")

    # a verified token reaches a route that is still S3's stub (the check_image.sh 501 probe);
    # S12 implemented GET /products, so the probe reads the listings list
    stub = leg.expect(
        "a verified token reaches a stub",
        501,
        leg.call("GET", "/listings", token=owner["access_token"]),
    )
    if stub["error"]["type"] != "NOT_IMPLEMENTED":
        sys.exit("auth e2e: the stub did not answer NOT_IMPLEMENTED")

    # a reviewer cannot start generation (DW2)
    reviewer = leg.login("reviewer@acme.test")
    generate = {"product_id": "00000000-0000-4000-8000-000000000001", "marketplace": "amazon"}
    leg.expect(
        "reviewer generate",
        403,
        leg.call(
            "POST",
            "/listings/generate",
            generate,
            reviewer["access_token"],
            Idempotency_Key="e2e-1",
        ),
    )

    # refresh twice with one token: the reuse revokes the family (DW3)
    first = leg.login("admin@acme.test")
    rotated = leg.expect(
        "refresh", 200, leg.call("POST", "/auth/refresh", {"refresh_token": first["refresh_token"]})
    )
    leg.expect(
        "refresh reuse",
        401,
        leg.call("POST", "/auth/refresh", {"refresh_token": first["refresh_token"]}),
    )
    leg.expect(
        "refresh after reuse",
        401,
        leg.call("POST", "/auth/refresh", {"refresh_token": rotated["refresh_token"]}),
    )

    # logout, then the token cannot refresh
    session = leg.login("owner@acme.test")
    leg.expect(
        "logout", 204, leg.call("POST", "/auth/logout", {"refresh_token": session["refresh_token"]})
    )
    leg.expect(
        "refresh after logout",
        401,
        leg.call("POST", "/auth/refresh", {"refresh_token": session["refresh_token"]}),
    )

    # another tenant: switch and members both answer 404
    bharat_owner = leg.login("owner@bharat.test")
    bharat = claims(bharat_owner["access_token"])["tid"]
    bharat_user = claims(bharat_owner["access_token"])["sub"]
    token = owner["access_token"]
    leg.expect(
        "switch to a tenant not mine",
        404,
        leg.call("POST", "/auth/switch-tenant", {"target_tenant_id": bharat}, token),
    )
    leg.expect(
        "patch another tenant's member",
        404,
        leg.call("PATCH", f"/members/{bharat_user}", {"role": "operator"}, token),
    )

    # the last owner cannot be demoted (DW9)
    owner_id = claims(token)["sub"]
    conflict = leg.expect(
        "demote the last owner",
        409,
        leg.call("PATCH", f"/members/{owner_id}", {"role": "operator"}, token),
    )
    if conflict["error"]["type"] != "LAST_OWNER_REQUIRED":
        sys.exit(f"auth e2e: last owner answered {conflict['error']['type']}")

    # happy path: switch (operator@acme.test is also a reviewer in bharat, from the setup)
    operator = leg.login("operator@acme.test")
    if len(operator["memberships"]) != 2:
        sys.exit(f"auth e2e: operator has {len(operator['memberships'])} memberships, want 2")
    if claims(operator["access_token"])["tid"] != acme:
        sys.exit("auth e2e: operator's login did not pick acme")
    switched = leg.expect(
        "switch to bharat",
        200,
        leg.call(
            "POST", "/auth/switch-tenant", {"target_tenant_id": bharat}, operator["access_token"]
        ),
    )
    if claims(switched["access_token"])["tid"] != bharat:
        sys.exit("auth e2e: the switch did not issue a bharat token")
    if claims(switched["refresh_token"])["jti"] == claims(operator["refresh_token"])["jti"]:
        sys.exit("auth e2e: the switch reused the refresh token")
    now = leg.expect(
        "me after switch", 200, leg.call("GET", "/auth/me", token=switched["access_token"])
    )
    if (now["tenant_id"], now["role"]) != (bharat, "reviewer"):
        sys.exit(f"auth e2e: after the switch me says {now['role']} in {now['tenant_id']}")
    old = leg.expect(
        "me on the old token", 200, leg.call("GET", "/auth/me", token=operator["access_token"])
    )
    if old["tenant_id"] != acme:
        sys.exit("auth e2e: the old access token changed tenant")
    # the switch moved operator's last-used tenant; switch back so the seed state is unchanged
    leg.expect(
        "switch back to acme",
        200,
        leg.call(
            "POST", "/auth/switch-tenant", {"target_tenant_id": acme}, switched["access_token"]
        ),
    )

    # happy path: members
    listed = leg.expect("list members", 200, leg.call("GET", "/members", token=token))
    if sorted(m["email"] for m in listed["data"]) != sorted(
        f"{r}@acme.test" for r in ("owner", "operator", "reviewer", "admin")
    ):
        sys.exit("auth e2e: acme's members are not its four seed users")
    reviewer_id = member_id(leg, token, "reviewer@acme.test")
    admin_id = member_id(leg, token, "admin@acme.test")
    leg.expect(
        "promote reviewer",
        200,
        leg.call("PATCH", f"/members/{reviewer_id}", {"role": "operator"}, token),
    )
    as_operator = leg.login("reviewer@acme.test")
    leg.expect(
        "an operator patches a member",
        403,
        leg.call(
            "PATCH", f"/members/{admin_id}", {"role": "reviewer"}, as_operator["access_token"]
        ),
    )
    leg.expect(
        "demote back",
        200,
        leg.call("PATCH", f"/members/{reviewer_id}", {"role": "reviewer"}, token),
    )
    leg.expect(
        "deactivate", 200, leg.call("PATCH", f"/members/{reviewer_id}", {"is_active": False}, token)
    )
    leg.expect(
        "refresh after deactivation",
        401,
        leg.call("POST", "/auth/refresh", {"refresh_token": as_operator["refresh_token"]}),
    )
    leg.login("reviewer@acme.test", want=401)
    leg.expect(
        "reactivate", 200, leg.call("PATCH", f"/members/{reviewer_id}", {"is_active": True}, token)
    )
    leg.login("reviewer@acme.test")

    # six bad logins in a minute for one IP + email: the sixth is 429 (DW7)
    for attempt in range(1, 6):
        leg.login("admin@bharat.test", "not-the-password", want=401)
        _ = attempt
    leg.login("admin@bharat.test", "not-the-password", want=429)

    print(f"bootstrap: auth e2e leg OK ({leg.steps} HTTP steps)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: auth_e2e.py <base-url>")
    sys.exit(run(sys.argv[1]))
