# Third-party notices

The synthetic Clockwork Garden English Reading Pack fixture was copied from:

- project: `reading-pack`
- source path: `examples/clockwork-garden/dist/clockwork-garden-reading-pack.en.md`
- source revision: `e924fa3dd5dc2f7cb81a3ca7098dc1d7420e2ebf`
- copied: 2026-08-14
- SHA-256: `d16280ea15f1e516be157b31547bf21d8991444e78a1e94cd12b83f14ac75c4d`
- license: CC0 1.0 Universal; see `tests/fixtures/LICENSE.md`

No manuscript, canonical JSON, template, evaluation record, or real-book
artifact is copied into this repository.

## Python packages and container base

This source repository does not vendor third-party Python packages. Optional
adapters resolve the packages declared in `pyproject.toml`; the reviewed Linux
container installs the exact distributions and SHA-256 values listed in
`requirements-live-linux-amd64.lock` and `requirements-build.lock`.

Those distributions use MIT, BSD, Apache 2.0, MPL 2.0, and PSF licenses or
compatible combinations of them. Each installed wheel retains its own license
file under its `.dist-info` directory. The Setuptools distribution also keeps
the notices for its bundled components there. The files shipped by each
distribution are authoritative.

The container starts from the official Python image identified by the exact
tag and index digest in `Dockerfile`. Python, Debian, and the operating-system
packages in that image remain under their respective licenses and retain their
package copyright information in the image.
