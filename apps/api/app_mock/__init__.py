"""The local `/api/v1` mock (S3 D5): serves the committed openapi.json examples on 127.0.0.1.

Dev tooling only. It never imports `app` (it reads nothing but the spec file), it is not in the
image (`.dockerignore`), and `__main__` refuses any host but loopback. Run: `make mock`.
"""
