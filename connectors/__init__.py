"""Importing this package registers every connector with orchestration.connector_registry
as a side effect -- main.py/runner.py never import a specific connector module by name,
they just `import connectors` once. Adding a new connector means adding a new module
here, never editing runner.py's core loop."""
from connectors import (  # noqa: F401
    clingen, clingen_gene_disease, clinvar, cosmic, dbnsfp, dbsnp, ensembl, gnomad, mondo,
    omim, pharmgkb, uniprot,
)
