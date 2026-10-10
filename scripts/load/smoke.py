"""The v1 load smoke (S26 T18; AC 7, DW2; PLAN D1, D2): asyncio + the pinned httpx, no Locust.

    python scripts/load/smoke.py --base-url http://127.0.0.1:<port> --fake-url http://127.0.0.1:<port>
        [--out .e2e/load-smoke.json]

Runs against a fresh local stack (`make load-smoke`: scripts/load/run.sh brings up profile
`acceptance`, seeds acme and bharat and passes both URLs in) with the production rate limits:

1. acme's operator uploads a generated 1,000-row CSV (valid kurta-set rows, run-unique SKUs) and
   polls the import's job every 3 s: 0 failed rows.
2. 20 concurrent `POST /listings/generate` (distinct keys) for 20 of the imported products, then
   one `GET /listings` every 3 s (never one poll per job): 20/20 `succeeded`.
3. While (2) runs, bharat starts one generation (it must start within 60 s) and publishes a
   listing approved before the load (its publish job must leave `queued` within 30 s).
4. 0 dead letters (`GET /jobs/{id}` per job at the end), 0 5xx, 0 429, and the per-tenant request
   rate counted here stays at or under 100 a minute (D2: under the 120/min tenant limit).

Writes the JSON report (p50/p95 per phase, counts, thresholds) and exits 1 when a threshold
fails, 2 on a non-local URL. Loopback only; the demo seed's public password.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import math
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

LOCAL_HOSTS: Final = frozenset({"127.0.0.1", "localhost"})
PASSWORD: Final = "devpassword123"  # noqa: S105 - the local-only demo seed password
CATEGORY: Final = "ethnic_wear_kurta_sets"  # platform load's category with a mapped template
IMPORT_ROWS: Final = 1000
GENERATIONS: Final = 20
POLL_INTERVAL_S: Final = 3.0
RATE_BUDGET_PER_MIN: Final = 100  # D2: client-side, under the 120/min tenant limit
IMPORT_DEADLINE_S: Final = 600
GENERATE_DEADLINE_S: Final = 600
TERMINAL: Final = frozenset({"succeeded", "partial", "failed", "cancelled", "dead_letter"})
SETTLED_LISTING: Final = frozenset({"validated", "needs_review", "failed", "blocked"})

# AC 7, as the report and the exit code read them.
THRESHOLDS: Final[dict[str, int]] = {
    "import_rows": IMPORT_ROWS,
    "import_failed_rows_max": 0,
    "generations": GENERATIONS,
    "generations_succeeded_min": GENERATIONS,
    "dead_letters_max": 0,
    "server_errors_max": 0,
    "rate_limited_max": 0,
    "bharat_generation_start_max_s": 60,
    "bharat_publish_leaves_queued_max_s": 30,
    "tenant_requests_per_min_max": RATE_BUDGET_PER_MIN,
}

CSV_HEADER: Final = (
    "seller_sku",
    "product_name",
    "brand_name",
    "category",
    "gender",
    "color_name",
    "fabric_type",
    "mrp",
    "selling_price",
    "tax_rate",
    "hsn_code",
    "country_of_origin",
    "package_weight",
    "main_image",
    "additional_images",
)
COLOURS: Final = ("Maroon", "Mustard", "Teal", "Ivory", "Indigo", "Rust", "Olive", "Peach")
FABRICS: Final = ("Cotton", "Rayon", "Linen", "Silk Blend")


class SmokeError(Exception):
    """A step that cannot go on (an unexpected status or a timeout); the message names it."""


def is_local(url: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "http" or parts.username is not None or parts.password is not None:
        return False
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    return parts.hostname in LOCAL_HOSTS


def make_csv(run: str, rows: int = IMPORT_ROWS) -> bytes:
    """`rows` valid kurta-set rows with SKUs unique to `run` (S13's column set)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for n in range(1, rows + 1):
        mrp = 1000 + n % 500
        writer.writerow(
            (
                f"LOAD-{run}-{n:04d}",
                f"Cotton Kurta Set {n}",
                "Acme Apparel",
                CATEGORY,
                "women",
                COLOURS[n % len(COLOURS)],
                FABRICS[n % len(FABRICS)],
                str(mrp),
                str(mrp - 100),
                "12",
                "62114200",
                "IN",
                f"{300 + n % 200} g",
                f"https://cdn.example.com/load/{run}/{n:04d}-main.jpg",
                f"https://cdn.example.com/load/{run}/{n:04d}-a.jpg",
            )
        )
    return out.getvalue().encode("utf-8")


@dataclass(frozen=True, slots=True)
class PollPlan:
    """The requests one tenant sends in the busiest minute of a phase."""

    phase: str
    interval_s: float
    burst: int  # requests sent at once (uploads, the generate fan-out, the final job reads)
    other: int  # logins, id reads, the bharat probe's own requests
    pollers: int = 1  # resources polled side by side at `interval_s`

    @property
    def polls_per_min(self) -> int:
        return self.pollers * math.ceil(60 / self.interval_s)

    @property
    def peak_per_min(self) -> int:
        return self.burst + self.polls_per_min + self.other


def plan_polls(phase: str) -> PollPlan:
    """The planned request budget per phase (D2: each one stays at or under 100/min)."""
    if phase == "import":  # one upload, the job poll, one rows read for the product ids
        return PollPlan(phase, POLL_INTERVAL_S, burst=1, other=2)
    if phase == "generate":  # 20 generates at once, one list poll; then 20 job reads
        return PollPlan(phase, POLL_INTERVAL_S, burst=2 * GENERATIONS, other=1)
    if phase == "bharat":  # logins, connect, setup and the two probes polled side by side
        return PollPlan(phase, POLL_INTERVAL_S, burst=0, other=16, pollers=2)
    raise ValueError(f"unknown phase {phase!r}")


@dataclass
class Meter:
    """Every request this script sends: per-tenant timestamps, statuses and latencies."""

    sent: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    statuses: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    latency: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    def record(self, tenant: str, phase: str, status: int, started: float, elapsed: float) -> None:
        self.sent[tenant].append(started)
        self.statuses[status] += 1
        self.latency[phase].append(elapsed)

    def peak_per_min(self, tenant: str) -> int:
        return peak_in_window(self.sent.get(tenant, []), 60.0)

    @property
    def server_errors(self) -> int:
        return sum(n for status, n in self.statuses.items() if status >= 500)

    @property
    def rate_limited(self) -> int:
        return self.statuses.get(429, 0)


def peak_in_window(times: Sequence[float], window_s: float) -> int:
    """The most timestamps inside any `window_s`-long sliding window."""
    ordered = sorted(times)
    best = start = 0
    for end, t in enumerate(ordered):
        while t - ordered[start] >= window_s:
            start += 1
        best = max(best, end - start + 1)
    return best


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile, in milliseconds rounded to 0.1; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return round(ordered[rank - 1] * 1000, 1)


def failures(report: dict[str, Any]) -> list[str]:
    """The AC 7 thresholds the report misses (empty: the smoke passed)."""
    c = report["counts"]
    t = THRESHOLDS
    out: list[str] = []
    if c["import_imported"] != t["import_rows"] or c["import_failed"] > t["import_failed_rows_max"]:
        out.append("import: not every row imported")
    if c["generations_succeeded"] < t["generations_succeeded_min"]:
        out.append("generate: fewer than 20 generations succeeded")
    if c["dead_letters"] > t["dead_letters_max"]:
        out.append("jobs: dead letters")
    if c["server_errors"] > t["server_errors_max"]:
        out.append("http: 5xx answers")
    if c["rate_limited"] > t["rate_limited_max"]:
        out.append("http: 429 answers")
    start = c["bharat_generation_start_s"]
    if start is None or start > t["bharat_generation_start_max_s"]:
        out.append("bharat: the generation did not start within 60 s")
    leave = c["bharat_publish_leaves_queued_s"]
    if leave is None or leave > t["bharat_publish_leaves_queued_max_s"]:
        out.append("bharat: the publish job stayed queued over 30 s")
    for tenant, peak in c["tenant_requests_peak_per_min"].items():
        if peak > t["tenant_requests_per_min_max"]:
            out.append(f"rate: {tenant} sent {peak} requests in a minute")
    return out


class Api:
    """A thin client: every call is metered under (tenant, phase); no redirects followed."""

    def __init__(self, client: httpx.AsyncClient, base: str, meter: Meter) -> None:
        self.client = client
        self.api = base.rstrip("/") + "/api/v1"
        self.base = base.rstrip("/")
        self.meter = meter

    async def call(
        self,
        tenant: str,
        phase: str,
        method: str,
        url: str,
        *,
        token: str | None = None,
        key: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if key:
            headers["Idempotency-Key"] = key
        target = url if url.startswith("http") else self.api + url
        started = time.monotonic()
        response = await self.client.request(method, target, headers=headers, **kwargs)
        self.meter.record(tenant, phase, response.status_code, started, time.monotonic() - started)
        return response

    async def expect(
        self, want: int, tenant: str, phase: str, method: str, url: str, **kwargs: Any
    ) -> Any:
        response = await self.call(tenant, phase, method, url, **kwargs)
        if response.status_code != want:
            raise SmokeError(f"{phase}: {method} {url.split('?')[0]} -> {response.status_code}")
        return response.json() if response.content else None

    async def login(self, tenant: str, role: str) -> str:
        body = await self.expect(
            200,
            tenant,
            "login",
            "POST",
            "/auth/login",
            json={"email": f"{role}@{tenant}.test", "password": PASSWORD},
        )
        return str(body["access_token"])

    async def job(self, tenant: str, phase: str, token: str, job_id: str) -> dict[str, Any]:
        body: dict[str, Any] = await self.expect(
            200, tenant, phase, "GET", f"/jobs/{job_id}", token=token
        )
        return body

    async def wait_job(
        self, tenant: str, phase: str, token: str, job_id: str, deadline_s: float
    ) -> dict[str, Any]:
        end = time.monotonic() + deadline_s
        while True:
            job = await self.job(tenant, phase, token, job_id)
            if job["status"] in TERMINAL:
                return job
            if time.monotonic() > end:
                raise SmokeError(f"{phase}: job still {job['status']} after {deadline_s:.0f} s")
            await asyncio.sleep(POLL_INTERVAL_S)


async def connect_amazon(api: Api, fake: str, token: str, seller: str) -> None:
    """connect -> the fake's consent (as `seller`) -> callback -> confirm (S15)."""
    body = await api.expect(
        200,
        "bharat",
        "bharat",
        "POST",
        "/marketplaces/connect",
        token=token,
        json={"marketplace": "amazon"},
    )
    authorize = str(body["authorize_url"])
    if not authorize.startswith(fake.rstrip("/") + "/"):
        raise SmokeError("bharat: the consent URL is not the fake's")
    consent = await api.client.get(f"{authorize}&seller={seller}")
    if consent.status_code != 302:
        raise SmokeError(f"bharat: consent -> {consent.status_code}")
    callback = urlsplit(consent.headers["location"])
    back = await api.call(
        "bharat", "bharat", "GET", f"{api.base}{callback.path}?{callback.query}", token=None
    )
    if back.status_code != 303:
        raise SmokeError(f"bharat: callback -> {back.status_code}")
    pending = dict(
        part.split("=", 1) for part in urlsplit(back.headers["location"]).query.split("&") if part
    ).get("pending")
    if not pending:
        raise SmokeError("bharat: no pending handle")
    await api.expect(
        200,
        "bharat",
        "bharat",
        "POST",
        "/marketplaces/connect/confirm",
        token=token,
        json={"pending": pending},
    )


async def bharat_generated(api: Api, operator: str, run: str, sku: str) -> tuple[str, str]:
    """A bharat product and its generation's (job id, listing id), not waited on."""
    product = await api.expect(
        201,
        "bharat",
        "bharat",
        "POST",
        "/products",
        token=operator,
        json={"sku": sku, "title": "Load Smoke Kurta Set", "category": CATEGORY},
    )
    accepted = await api.expect(
        202,
        "bharat",
        "bharat",
        "POST",
        "/listings/generate",
        token=operator,
        key=f"load-bharat-gen-{run}-{sku}",
        json={"product_id": product["id"], "marketplace": "amazon"},
    )
    return str(accepted["job_id"]), str(accepted["listing_id"])


async def bharat_setup(api: Api, fake: str, run: str) -> dict[str, str]:
    """Before the load: bharat connects Amazon and has one approved listing ready to publish."""
    owner = await api.login("bharat", "owner")
    operator = await api.login("bharat", "operator")
    reviewer = await api.login("bharat", "reviewer")
    await connect_amazon(api, fake, owner, f"LOADBH{run}")
    job_id, listing_id = await bharat_generated(api, operator, run, f"LOAD-BH-{run}-PUB")
    job = await api.wait_job("bharat", "bharat", operator, job_id, 120)
    if job["status"] != "succeeded":
        raise SmokeError(f"bharat: the setup generation ended {job['status']}")
    listing = await api.expect(
        200, "bharat", "bharat", "GET", f"/listings/{listing_id}", token=operator
    )
    approved = await api.expect(
        200,
        "bharat",
        "bharat",
        "POST",
        "/listings/approve",
        token=reviewer,
        key=f"load-bharat-approve-{run}",
        json={
            "items": [
                {
                    "listing_id": listing_id,
                    "listing_version_id": listing["current_version_id"],
                    "decision": "approve",
                }
            ]
        },
    )
    if approved["items"][0]["http_status"] != 200:
        raise SmokeError("bharat: the setup approval was refused")
    return {"operator": operator, "listing_id": listing_id}


async def bharat_probe(api: Api, bharat: dict[str, str], run: str) -> dict[str, float | None]:
    """During acme's generate load: one bharat generation and one bharat publish."""
    operator = bharat["operator"]
    t0 = time.monotonic()
    gen_job, _ = await bharat_generated(api, operator, run, f"LOAD-BH-{run}-GEN")
    published = await api.expect(
        202,
        "bharat",
        "bharat",
        "POST",
        "/listings/publish",
        token=operator,
        key=f"load-bharat-pub-{run}",
        json={"items": [{"listing_id": bharat["listing_id"]}]},
    )
    item = published["items"][0]
    if item["http_status"] != 202:
        raise SmokeError(f"bharat: the publish item answered {item['http_status']}")
    pub_job, p0 = str(item["job_id"]), time.monotonic()
    gen_started: float | None = None
    pub_left: float | None = None
    end = time.monotonic() + 90
    while (gen_started is None or pub_left is None) and time.monotonic() < end:
        if gen_started is None:
            job = await api.job("bharat", "bharat", operator, gen_job)
            if job["status"] != "queued":
                gen_started = time.monotonic() - t0
        if pub_left is None:
            job = await api.job("bharat", "bharat", operator, pub_job)
            if job["status"] != "queued":
                pub_left = time.monotonic() - p0
        await asyncio.sleep(POLL_INTERVAL_S)
    finals = [await api.wait_job("bharat", "bharat", operator, j, 120) for j in (gen_job, pub_job)]
    return {
        "generation_start_s": None if gen_started is None else round(gen_started, 1),
        "publish_leaves_queued_s": None if pub_left is None else round(pub_left, 1),
        "dead_letters": sum(1 for j in finals if j["status"] == "dead_letter"),
    }


async def import_phase(api: Api, operator: str, run: str) -> dict[str, Any]:
    data = make_csv(run)
    t0 = time.monotonic()
    accepted = await api.expect(
        202,
        "acme",
        "import",
        "POST",
        "/products/bulk",
        token=operator,
        key=f"load-import-{run}",
        files={"file": (f"load-{run}.csv", data, "text/csv")},
    )
    job = await api.wait_job("acme", "import", operator, accepted["job_id"], IMPORT_DEADLINE_S)
    elapsed = time.monotonic() - t0
    report = await api.expect(
        200, "acme", "import", "GET", f"/imports/{accepted['import_id']}", token=operator
    )
    rows = await api.expect(
        200,
        "acme",
        "import",
        "GET",
        f"/imports/{accepted['import_id']}/rows?status=imported&limit={GENERATIONS}",
        token=operator,
    )
    return {
        "job_status": job["status"],
        "imported": int(report["imported"]),
        "failed": int(report["failed"]),
        "seconds": round(elapsed, 1),
        "product_ids": [r["product_id"] for r in rows["data"] if r.get("product_id")],
    }


async def generate_phase(
    api: Api, operator: str, run: str, product_ids: Sequence[str]
) -> dict[str, Any]:
    t0 = time.monotonic()
    accepted = await asyncio.gather(
        *(
            api.expect(
                202,
                "acme",
                "generate",
                "POST",
                "/listings/generate",
                token=operator,
                key=f"load-gen-{run}-{n}",
                json={"product_id": pid, "marketplace": "amazon"},
            )
            for n, pid in enumerate(product_ids)
        )
    )
    listings = {str(a["listing_id"]): str(a["job_id"]) for a in accepted}
    end = time.monotonic() + GENERATE_DEADLINE_S
    while True:
        await asyncio.sleep(POLL_INTERVAL_S)
        page = await api.expect(
            200, "acme", "generate", "GET", "/listings?marketplace=amazon&limit=200", token=operator
        )
        seen = {str(row["id"]): str(row["status"]) for row in page["data"]}
        if all(seen.get(lid) in SETTLED_LISTING for lid in listings):
            break
        if time.monotonic() > end:
            raise SmokeError("generate: listings still generating after the deadline")
    elapsed = time.monotonic() - t0
    jobs = [await api.job("acme", "generate", operator, j) for j in listings.values()]
    return {
        "succeeded": sum(1 for j in jobs if j["status"] == "succeeded"),
        "dead_letters": sum(1 for j in jobs if j["status"] == "dead_letter"),
        "statuses": sorted({str(j["status"]) for j in jobs}),
        "seconds": round(elapsed, 1),
    }


async def run_smoke(base: str, fake: str, run: str) -> dict[str, Any]:
    meter = Meter()
    timeout = httpx.Timeout(60.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        api = Api(client, base, meter)
        bharat = await bharat_setup(api, fake, run)
        operator = await api.login("acme", "operator")
        imported = await import_phase(api, operator, run)
        if len(imported["product_ids"]) < GENERATIONS:
            raise SmokeError("import: fewer than 20 imported products to generate for")
        generate_task = asyncio.create_task(
            generate_phase(api, operator, run, imported["product_ids"][:GENERATIONS])
        )
        await asyncio.sleep(1)  # the fan-out is in flight before bharat starts
        probe = await bharat_probe(api, bharat, run)
        generated = await generate_task
    phases = {
        phase: {"requests": len(v), "p50_ms": percentile(v, 50), "p95_ms": percentile(v, 95)}
        for phase, v in sorted(meter.latency.items())
    }
    return {
        "schema": "load-smoke/1",
        "run": run,
        "thresholds": THRESHOLDS,
        "phases": phases,
        "counts": {
            "import_imported": imported["imported"],
            "import_failed": imported["failed"],
            "import_job_status": imported["job_status"],
            "import_seconds": imported["seconds"],
            "generations": GENERATIONS,
            "generations_succeeded": generated["succeeded"],
            "generation_statuses": generated["statuses"],
            "generate_seconds": generated["seconds"],
            "dead_letters": generated["dead_letters"] + int(probe["dead_letters"] or 0),
            "server_errors": meter.server_errors,
            "rate_limited": meter.rate_limited,
            "bharat_generation_start_s": probe["generation_start_s"],
            "bharat_publish_leaves_queued_s": probe["publish_leaves_queued_s"],
            "tenant_requests_peak_per_min": {t: meter.peak_per_min(t) for t in sorted(meter.sent)},
            "statuses": {str(k): v for k, v in sorted(meter.statuses.items())},
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/load/smoke.py")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--fake-url", required=True)
    parser.add_argument("--out", type=Path, default=Path(".e2e/load-smoke.json"))
    parser.add_argument("--run", default=str(int(time.time())))
    args = parser.parse_args(argv)
    for url in (args.base_url, args.fake_url):
        if not is_local(url):
            print("load-smoke: refused: the URLs must be http://127.0.0.1 or http://localhost")
            return 2
    try:
        report = asyncio.run(run_smoke(args.base_url, args.fake_url, args.run))
    except SmokeError as exc:
        print(f"load-smoke: FAILED: {exc}")
        return 1
    missed = failures(report)
    report["result"] = "failed" if missed else "passed"
    report["failures"] = missed
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"load-smoke: {report['result']} ({args.out})")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
