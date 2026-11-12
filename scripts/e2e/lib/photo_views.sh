# shellcheck shell=bash
# The photo view checklist for the HTTP e2e flows that approve (client feedback 7 and 8,
# `PHOTO_VIEWS_ENABLED`): every category requires the front photo, Kurta sets and T-shirts also
# require the seller's saved size chart and recommend a back and a close-up photo
# (`apps/api/app/services/data/photo_views.json`). Approval judges the version's own photos, so a
# flow puts them on the product before it generates. Sourced from the repo root:
#
#   . scripts/e2e/lib/photo_views.sh
#
# - PHOTO_VIEWS_CHART: a saved size chart (one row, size M) for a product with no size variants,
#   as the `size_chart` of `POST /products` or `PATCH /products/{id}`;
# - photo_views_chart SIZE...: the same table with one row per size, for a style split by size
#   (every variant size needs its row);
# - photo_views_upload API TOKEN PRODUCT KEY WORK: uploads a front (`main`), a back and a close-up
#   (`secondary` with that role) photo, then polls the product (1 s, at most 60 s) until the
#   three passed their checks, the photo read on upload (`photo_reading`) is done, so the facts a
#   generation pins no longer move, and, while the switch is on, the product read shows every
#   required and recommended view present and the size chart saved where one is required. The photo is a
#   1000x1000 white PNG whose product fills 94 % of the side, so no image check warns.
#   Returns 1 with the step and the HTTP status on stderr (never a body); the caller fails.
#
# With the switch off the same photos and chart change nothing a flow asserts.

# shellcheck disable=SC2034 # read by the scripts that source this file
PHOTO_VIEWS_CHART='{"unit": "cm", "basis": "garment", "rows": [{"size": "M", "chest": 100}]}'

photo_views_chart() {
  python3 - "$@" <<'PY'
import json, sys
rows = [{"size": size, "chest": 90 + 4 * n} for n, size in enumerate(sys.argv[1:])]
print(json.dumps({"unit": "cm", "basis": "garment", "rows": rows}))
PY
}

_photo_views_fail() {
  echo "photo_views: $1 (HTTP status ${2:-none})" >&2
  return 1
}

photo_views_upload() {
  local api="$1" token="$2" product="$3" key="$4" work="$5"
  local photo="$work/photo-views.png" out="$work/photo-views.json" code view deadline seen
  local form=()
  if [ ! -f "$photo" ]; then
    uv run --frozen python scripts/e2e/make_image.py 1000 1000 255,255,255 PNG "$photo" 0.94 \
      || _photo_views_fail "make_image" || return 1
  fi
  for view in front back close_up; do
    if [ "$view" = front ]; then form=(-F "slot=main"); else form=(-F "slot=secondary" -F "role=$view"); fi
    code="$(curl -s -o "$out" -w '%{http_code}' --max-time 60 -X POST -H 'Accept: application/json' \
      -H "Authorization: Bearer $token" -H "Idempotency-Key: $key-$view" -F "product_id=$product" \
      "${form[@]}" -F "file=@$photo;type=application/octet-stream" "$api/images/upload")" || code="000"
    [ "$code" = 202 ] || _photo_views_fail "upload $view" "$code" || return 1
  done
  deadline=$(( $(date +%s) + 60 ))
  while :; do
    code="$(curl -s -o "$out" -w '%{http_code}' --max-time 60 -H 'Accept: application/json' \
      -H "Authorization: Bearer $token" "$api/products/$product")" || code="000"
    [ "$code" = 200 ] || _photo_views_fail "read the product" "$code" || return 1
    seen="$(python3 - "$out" <<'PY'
import json, sys
try:
    b = json.load(open(sys.argv[1], encoding="utf-8"))
    views = [(d["slot"], d.get("role"), d["compliance_status"]) for d in b["image_details"]
             if (d["slot"], d.get("role")) in (("main", None), ("secondary", "back"), ("secondary", "close_up"))]
    if any(status == "failed" for _, _, status in views):
        print("failed")
    elif len(views) < 3 or any(status != "passed" for _, _, status in views) or b.get("photo_reading"):
        print("pending")  # the photo read on upload settles first, so generation sees its values
    else:
        # the checklist and the size chart check are listed only while PHOTO_VIEWS_ENABLED is on
        unmet = [v["view"] for v in b.get("photo_views") or []
                 if v["requirement"] != "optional" and v["status"] != "present"]
        chart = b.get("size_chart_check")
        if chart and chart["required"] and not chart["saved"] and "size_chart" not in unmet:
            unmet.append("size_chart")
        print("unmet " + ",".join(unmet) if unmet else "ready")
except Exception:  # no traceback: it could quote the response
    print("shape")
PY
)"
    case "$seen" in
      ready) return 0 ;;
      pending) ;;
      failed) _photo_views_fail "a photo failed its checks" "$code"; return 1 ;;
      unmet*) _photo_views_fail "views not met on the product read: ${seen#unmet }" "$code"; return 1 ;;
      *) _photo_views_fail "unexpected product shape" "$code"; return 1 ;;
    esac
    [ "$(date +%s)" -lt "$deadline" ] || _photo_views_fail "the photos were not checked within 60 s" "$code" || return 1
    sleep 1
  done
}
