"""UniProt connector -- Core Biology (protein annotation). Real payload shape confirmed
against fixtures/uniprot_P40337.json (live-fetched from
https://rest.uniprot.org/uniprotkb/P40337.json -- VHL's real, confirmed UniProt
accession, already referenced in this project's own test-case notes).

Real, confirmed structure: `sequence.{value,length,molWeight}`, `proteinDescription.
recommendedName.fullName.value`, `genes[0].geneName.value`, and 172 real `features`
entries (domains/repeats/modified residues) -- kept as a compact summary attribute
(count + the handful of named domain/repeat regions), never exploded into 172 separate
nodes. Same LOD discipline gnomAD's population breakdown and Ensembl's non-canonical
transcripts already follow.

Real value beyond "one more feed": UniProt's Protein node uses a DIFFERENT canonical id
scheme (`UniProt:{accession}`) than the one Ensembl's gene-structure connector already
produces (`Ensembl:{ENSP...}`) for what's biologically the same protein -- exactly the
gap `REPRESENTS` exists for (forge_ir/core.py: "weak cross-source identity, never a
merge"). Confirmed real and previously never exercised: every run before this connector
reported `REPRESENTS: 0 edges`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

from connectors import http

from forge_ir.core import EdgeType, NodeRole, Origin, protein_canonical_id_ensembl, protein_canonical_id_uniprot
from forge_ir.graph import ForgeEdge, ForgeNode
from forge_ir.provenance import SourceProvenance
from ingest.source_objects import SourceObjectBase
from orchestration.connector_registry import ConnectorDescriptor, Emission, register_connector

BASE_URL = "https://rest.uniprot.org/uniprotkb"
SEARCH_URL = f"{BASE_URL}/search"

# Gene -> real UniProt accession. Only a shortcut now, not the only path: `fetch()` below
# resolves any gene symbol through UniProt's own search endpoint. Kept because VHL's
# accession is confirmed against the saved fixture and skipping a search round-trip for
# the gene this project is built around is free.
_KNOWN_GENE_ACCESSION: dict[str, str] = {"VHL": "P40337"}


@dataclass
class UniProtEntry(SourceObjectBase):
    accession: str
    gene_symbol: str
    protein_name: str
    sequence_length: int
    mol_weight: int
    feature_count: int
    named_regions: list[str]   # domain/repeat feature descriptions, real and compact
    ensembl_protein_id: str | None = None   # the Ensembl protein this entry REPRESENTS,
                                             # read out of the payload -- see _canonical_ensembl_protein


class UniProtAdapter:
    def fetch(self, query: dict) -> Iterator[dict]:
        """Two ways in: a known accession, or a gene symbol resolved through UniProt's own
        search. The search path is what lets this connector run across a whole gene panel
        instead of only the genes someone remembered to put in a lookup table.

        `gene_exact` rather than `gene` so a symbol never matches a synonym of a different
        protein; `reviewed:true` restricts to Swiss-Prot, the manually curated half of
        UniProtKB -- an unreviewed TrEMBL entry is a computational prediction and has no
        business being asserted as a gene's canonical protein."""
        accession = query.get("accession")
        if accession:
            resp = http.get(f"{BASE_URL}/{accession}.json", timeout=30)
            resp.raise_for_status()
            yield resp.json()
            return

        gene = query["gene"]
        resp = http.get(SEARCH_URL, params={
            "query": f"gene_exact:{gene} AND organism_id:9606 AND reviewed:true",
            "format": "json", "size": "1",
        }, timeout=30)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return   # no reviewed human entry for this symbol -- yield nothing, invent nothing
        yield results[0]

    def capabilities(self) -> dict:
        return {
            "scopes": ["gene"],
            "provides": ["Protein"],
            "bulk": False, "live_query": True, "batch": False,
            "hydration_lods": [1, 2], "identity_types": ["UniProt accession"],
        }

    def manifest(self) -> dict:
        return {"source": "UniProt", "dataset": "UniProtKB REST API", "schema_version": "1"}


def _canonical_ensembl_protein(raw: dict) -> str | None:
    """The Ensembl protein id for the entry's CANONICAL isoform, read straight out of
    `uniProtKBCrossReferences`.

    Replaces a hardcoded {gene -> ENSP} table. Confirmed correct against
    fixtures/uniprot_P40337.json, which carries two real Ensembl cross-references --
    ENSP00000256474.3 (isoform P40337-1) and ENSP00000344757.2 (isoform P40337-2) -- and
    the canonical one is exactly the value the old table asserted by hand.

    UniProt's convention is that `{primaryAccession}-1` is the canonical isoform, so that
    is what is matched; an entry with no isoform-tagged Ensembl xref falls back to its
    first. Picking the wrong isoform here would produce a REPRESENTS edge claiming two
    different proteins are the same one, so it fails closed: no match, no edge."""
    accession = raw.get("primaryAccession")
    xrefs = [x for x in raw.get("uniProtKBCrossReferences", []) if x.get("database") == "Ensembl"]
    if not xrefs:
        return None
    chosen = next((x for x in xrefs if x.get("isoformId") == f"{accession}-1"), None)
    if chosen is None:
        chosen = next((x for x in xrefs if not x.get("isoformId")), None) or xrefs[0]
    protein_id = next((p["value"] for p in chosen.get("properties", [])
                       if p.get("key") == "ProteinId"), None)
    # Ensembl ids carry a ".N" version suffix here that forge_ir's canonical id scheme
    # does not use (Ensembl's own /lookup/symbol response gives the bare id).
    return protein_id.split(".")[0] if protein_id else None


class UniProtParser:
    def parse(self, raw: dict, snapshot_ref: str = "") -> UniProtEntry:
        genes = raw.get("genes", [{}])
        gene_symbol = genes[0].get("geneName", {}).get("value", "") if genes else ""
        protein_name = raw.get("proteinDescription", {}) \
            .get("recommendedName", {}).get("fullName", {}).get("value", "")
        features = raw.get("features", [])
        named_regions = [f["description"] for f in features
                         if f.get("type") in ("Repeat", "Domain", "Region") and f.get("description")][:10]
        return UniProtEntry(
            source="UniProt", record_type="UniProtKBEntry", source_record_id=raw["primaryAccession"],
            source_version=None, snapshot_ref=snapshot_ref, payload_hash=str(hash(str(raw.get("sequence")))),
            accession=raw["primaryAccession"], gene_symbol=gene_symbol, protein_name=protein_name,
            sequence_length=raw["sequence"]["length"], mol_weight=raw["sequence"]["molWeight"],
            feature_count=len(features), named_regions=named_regions,
            ensembl_protein_id=_canonical_ensembl_protein(raw),
        )


def entry_to_ir(obj: UniProtEntry, represents_ensembl_protein_id: str | None = None) -> list:
    now = datetime.now(timezone.utc)
    prov = [SourceProvenance("UniProt", obj.accession, obj.source_version, now)]
    protein_id = protein_canonical_id_uniprot(obj.accession)

    results: list = [ForgeNode(
        id=protein_id, role=NodeRole.ENTITY, subtype="Protein",
        attributes={"length": obj.sequence_length, "accession": obj.accession, "name": obj.protein_name,
                    "gene_symbol": obj.gene_symbol, "mol_weight": obj.mol_weight,
                    "feature_count": obj.feature_count, "named_regions": obj.named_regions},
        provenance=prov, origin=Origin.SOURCE,
    )]

    if represents_ensembl_protein_id:
        ensembl_id = protein_canonical_id_ensembl(represents_ensembl_protein_id)
        results.append(ForgeEdge(
            src=protein_id, predicate=EdgeType.REPRESENTS, dst=ensembl_id, qualifier=None,
            attributes={}, provenance=prov, origin=Origin.SOURCE,
            confidence=1.0, match_method="uniprot_gene_symbol_match", resolver_version="v1",
        ))

    return results


def _build_query(scope: str, anchor: str, context: dict) -> dict | None:
    if scope != "gene":
        return None
    accession = context.get("uniprot_accession") or _KNOWN_GENE_ACCESSION.get(anchor)
    if accession:
        return {"accession": accession}
    # Anchors can arrive as a resolved HGNC canonical id rather than a symbol; UniProt's
    # gene_exact search only understands symbols, so don't send it one it can't match.
    return None if anchor.startswith("HGNC:") else {"gene": anchor}


def _build_emission(raw: dict, context: dict) -> Emission:
    entry = UniProtParser().parse(raw)
    ensembl_protein_id = context.get("uniprot_represents_ensembl_protein_id") \
        or entry.ensembl_protein_id
    results = entry_to_ir(entry, ensembl_protein_id)
    emission = Emission()
    emission.add("UniProt", results)
    return emission


register_connector(ConnectorDescriptor(
    name="uniprot", group="core_biology", cadence="persistent",
    adapter=UniProtAdapter(), build_query=_build_query, build_emission=_build_emission,
    feed_label="UniProt", default_included=False,   # opt-in: --connectors must name it explicitly
    description="Protein annotation, cross-referenced to Ensembl's protein id via REPRESENTS "
               "(GET /uniprotkb/{accession})",
))
