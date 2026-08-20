"""Single source of truth for the package version.

``0.2.0`` is the project's first stable release. From here the machine-readable
contracts follow the stability policy in ``docs/SPEC.md``: additions are
backward compatible, removals and meaning changes need a ``SCHEMA_VERSION``
bump and a migration note, and the capture ``format_version`` is append-only.

Stable numbering is not a claim that every release gate in ROADMAP.md has
passed — several need hardware this project has not had. What has and has not
been validated is stated there and in the README.
"""

__version__ = "0.2.0"

#: Version of the machine-readable JSON contracts (CLI envelopes, capture
#: manifest, annotations, assertion results). Independent of the package
#: version so that schema evolution is explicit.
SCHEMA_VERSION = "1"
