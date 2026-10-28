# mkos-schemas (contract stand-in)

This package reconstructs the `mkos-schemas` 1.2.0 seam types from the backend handover so `mkos-pipeline` can be developed and tested without the private backend repository.

**Replace this vendor copy** with the official git tag (`schemas-v1.2.0`) as soon as the Tech Lead provides it:

```toml
dependencies = ["mkos-schemas @ git+https://<backend-repo>@schemas-v1.2.0"]
```

Do not treat this stand-in as the source of truth if it disagrees with the tagged package.
