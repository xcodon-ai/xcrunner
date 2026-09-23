# xcodon-runtime

Rootless container runtime for Docker images. Runs with kernel user
namespaces where available and falls back to PRoot elsewhere. Built to plug
into coala and coala-runtime. See `docs/superpowers/specs/` for the design.
