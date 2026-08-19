"""Single source of truth for the package version.

The project stays on ``0.1.0.devN`` until every ``0.1.0`` release gate in
ROADMAP.md (including hardware validation) has passed.
"""

__version__ = "0.1.0.dev0"

#: Version of the machine-readable JSON contracts (CLI envelopes, capture
#: manifest, annotations, assertion results). Independent of the package
#: version so that schema evolution is explicit.
SCHEMA_VERSION = "1"
