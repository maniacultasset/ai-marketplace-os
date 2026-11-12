# shellcheck shell=bash
# The product facts every Amazon kurta set needs before a listing can reach `validated`: the
# templates require the price pair, the gender, the colour and fabric attributes and a barcode or
# a GTIN exemption, and the system never guesses a fact (an absent one opens a `missing_fact`
# question and blocks the listing). Sourced from the repo root:
#
#   . scripts/e2e/lib/product_facts.sh
#
# PRODUCT_FACTS is a JSON fragment (no braces) for the body of `POST /products`, after the sku,
# title and category: "... \"category\": \"$category\", $PRODUCT_FACTS}". A flow that tests one
# missing fact leaves this out and writes its own body.

# shellcheck disable=SC2034 # read by the scripts that source this file
PRODUCT_FACTS='"mrp": "1499.00", "selling_price": "999.00", "gender": "women", "identifier_exemption": true, "attributes": {"color": "blue", "fabric_type": "cotton"}'
