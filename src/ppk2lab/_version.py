"""Single source of truth for the package version.

The project stays on a ``devN`` pre-release until every release gate in
ROADMAP.md — including hardware validation — has passed. Pre-releases are
excluded from ``pip install ppk2lab`` by default, so a plain install resolves
nothing until the first stable release is cut. That release is ``0.2.0``.
"""

__version__ = "0.2.0.dev0"

#: Version of the machine-readable JSON contracts (CLI envelopes, capture
#: manifest, annotations, assertion results). Independent of the package
#: version so that schema evolution is explicit. Everything added in 0.2.0 is
#: an additive result field or an additive manifest key, so this stays "1".
SCHEMA_VERSION = "1"
