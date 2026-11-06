# ADR-0026: Public image copies on the Kuinn image domain

- **Status:** proposed (SI1 seller intake, 2026-11-06)
- **Date:** 2026-11-06
- **Deciders:** Tech Lead (the user)
- **Amends:** ADR-0001 "Object storage" row ("V4 signed URLs"), `docs/architecture/DATABASE.md` images
  note "URLs are signed at read time" (T63), for marketplace image links only.

## Context

Marketplaces fetch listing images from a URL we hand them, sometimes days after the submission and
again on every re-index. Until SI1 the publish worker handed each adapter a 24 hour presigned GET
on the private images bucket. Those links expire, and a re-fetch after expiry breaks the listing.
The owner decided on 6 Nov 2026: "Image links: stable public links on a Kuinn image domain
(`https://images.kuinn.in/<random-id>.<ext>`), never expiring."

The private originals hold what the seller uploaded, EXIF (GPS, device) included. They must stay
private.

## Decision

1. **Separate public bucket.** A second bucket (`GCS_BUCKET_IMAGES_PUBLIC`) holds only clean copies:
   decoded, EXIF orientation applied, every EXIF, XMP and comment block removed, ICC kept. It is
   readable by anyone per object and never listable. On GCS that is exactly
   `gcloud storage buckets add-iam-policy-binding gs://<public-bucket> --member=allUsers --role=roles/storage.legacyObjectReader`
   (object get only), with uniform bucket-level access on and public-access prevention off; never
   grant `roles/storage.objectViewer` to `allUsers`, because it carries `storage.objects.list` and
   would let anyone list every tenant's public names. On MinIO it is a GetObject-only bucket policy
   (`infra/minio/public-read.json`).
2. **What goes public.** Only an image in an approved listing version, at publish, after the
   approval, facts and image pins pass. A `packaging_label` image and an image outside the approved
   version never get a public copy.
3. **Names.** Each image gets one `public_id`: 26 lowercase base32 characters from 16 random bytes,
   unique across tenants (partial unique index), with no tenant, product or image id in it. The URL
   is `IMAGE_PUBLIC_BASE_URL/<public_id>.<jpg|png>`. A re-publish reuses the same name.
4. **Caching.** Objects are written once with `Cache-Control: public, max-age=31536000,
   s-maxage=86400, immutable`. A deleted image can stay at a CDN edge for up to one day and in a
   browser cache longer. A purge-on-delete at the CDN is a later step.
5. **Removal.** `DELETE /api/v1/images/{id}` and a main-image replacement remove the public copy and
   the derived objects after commit; a daily step retries failures. `public_id` stays on the deleted
   row as a tombstone, so the object name is never lost. A tenant purge first deletes every public
   copy of the tenant (`images unpublish --tenant`); a `BEFORE DELETE` trigger on `images` refuses
   the purge while any `public_id` is set.
6. **Fail closed.** Both settings are optional and set together. When unset, a publish that would
   send images fails with `INTERNAL_ERROR` "image hosting is not configured" before anything is
   written. A signed URL is never the fallback. Outside `APP_ENV=local` the base URL must be
   `https://`, without query, fragment or trailing slash, on a host other than the storage
   endpoint's.
7. **Provisioning** (owner-only, before the first prod deploy that carries this change):
   - the public bucket, with uniform bucket-level access on and without public-access prevention;
   - the storage identity (`SA_STORAGE`, `aimos-storage`) bound on it with
     `gcloud storage buckets add-iam-policy-binding gs://<public-bucket> --member=serviceAccount:<aimos-storage email> --role=roles/storage.objectUser`
     (object create, get, update and delete, no IAM changes);
   - public read with
     `gcloud storage buckets add-iam-policy-binding gs://<public-bucket> --member=allUsers --role=roles/storage.legacyObjectReader`;
     never grant `roles/storage.objectViewer` to `allUsers` (it lists the bucket);
   - the `images.kuinn.in` DNS record, and both settings for the api, the worker and the
     maintenance job;
   - the check: an anonymous list,
     `curl -s -o /dev/null -w '%{http_code}' https://storage.googleapis.com/storage/v1/b/<public-bucket>/o`,
     must print 401 or 403, and an anonymous GET of a known published object must print 200.

## Consequences

- Marketplace image links stop expiring. Anyone holding a URL can fetch that one image, which is
  already public on the marketplace; names cannot be guessed or listed.
- The private originals, derived copies and their signed read URLs are unchanged.
- One more bucket and one more DNS name to run. Until the owner provisions them, prod publishes
  that carry images fail closed.
- Private originals and derived objects of a purged tenant are not deleted by the purge yet (a
  KNOWN-ISSUES row); they keep their tenant prefix and can be removed by prefix.
