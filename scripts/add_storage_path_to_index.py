"""
One-time Azure AI Search change so grounding can be limited to a project's
blob folder (see azure_search_service.retrieve_grounding()).

The "Import and vectorize data" wizard built the index with only chunk_id,
parent_id, chunk, title and text_vector -- title is just the file name, so
there's no way to tell which folder (project) a chunk came from. This script:

  1. adds a filterable `storage_path` field to the index (adding a field is
     allowed on an existing index; nothing is dropped),
  2. adds a skillset index-projection mapping that fills it from each blob's
     metadata_storage_path, e.g.
        https://<account>.blob.core.windows.net/sharepoint-docs/payment-integrity/Rules.docx
  3. resets and re-runs the indexer so every existing chunk gets the path.

Folder layout it expects in the blob container (sharepoint-docs):
    <file>                          shared by every project (templates etc.)
    payment-integrity/<file>        ProjectID "Payment Integrity"
    correspondence-mapping/<file>   ProjectID "Correspondence mapping"
    req/<file>                      ProjectID "Req"
    qnxt-to-edw-mapping/<file>      ProjectID "Qnxt to EDW mapping"
(folder = ProjectID lowercased, spaces -> hyphens; see project_folder()).

Usage:
    python scripts/add_storage_path_to_index.py            # show what would change
    python scripts/add_storage_path_to_index.py --apply    # make the change

Reads AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_KEY (admin key) and
AZURE_SEARCH_INDEX_NAME from .env. Assumes the wizard's naming:
<index>-skillset and <index>-indexer.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

API_VERSION = "2024-07-01"
PATH_FIELD = "storage_path"
PATH_SOURCE = "/document/metadata_storage_path"

ENDPOINT = os.environ["AZURE_SEARCH_ENDPOINT"].rstrip("/")
KEY = os.environ["AZURE_SEARCH_KEY"]
INDEX = os.environ["AZURE_SEARCH_INDEX_NAME"]
SKILLSET = f"{INDEX}-skillset"
INDEXER = f"{INDEX}-indexer"


def call(method: str, path: str, body: dict | None = None) -> dict:
    sep = "&" if "?" in path else "?"
    req = urllib.request.Request(
        f"{ENDPOINT}{path}{sep}api-version={API_VERSION}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"api-key": KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} failed: HTTP {e.code} {e.read().decode()[:500]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="make the change (default: dry run)")
    args = parser.parse_args()

    index = call("GET", f"/indexes/{INDEX}")
    skillset = call("GET", f"/skillsets/{SKILLSET}")

    need_field = not any(f["name"] == PATH_FIELD for f in index["fields"])
    selectors = skillset.get("indexProjections", {}).get("selectors", [])
    selector = next((s for s in selectors if s["targetIndexName"] == INDEX), None)
    if selector is None:
        sys.exit(f"Skillset {SKILLSET} has no index projection into {INDEX} -- nothing to update.")
    need_mapping = not any(m["name"] == PATH_FIELD for m in selector["mappings"])

    print(f"Index {INDEX}: {'add' if need_field else 'already has'} field {PATH_FIELD}")
    print(f"Skillset {SKILLSET}: {'add' if need_mapping else 'already has'} mapping {PATH_FIELD} <- {PATH_SOURCE}")
    print(f"Indexer {INDEXER}: reset and re-run")
    if not args.apply:
        print("\nDry run -- nothing changed. Re-run with --apply to make these changes.")
        return

    if need_field:
        index["fields"].append({
            "name": PATH_FIELD,
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "retrievable": True,
            "sortable": False,
            "facetable": False,
        })
        index.pop("@odata.context", None)
        call("PUT", f"/indexes/{INDEX}", index)
        print(f"Added {PATH_FIELD} to {INDEX}.")

    if need_mapping:
        selector["mappings"].append({"name": PATH_FIELD, "source": PATH_SOURCE})
        skillset.pop("@odata.context", None)
        call("PUT", f"/skillsets/{SKILLSET}", skillset)
        print(f"Added {PATH_FIELD} mapping to {SKILLSET}.")

    call("POST", f"/indexers/{INDEXER}/reset")
    call("POST", f"/indexers/{INDEXER}/run")
    print(f"Indexer {INDEXER} reset and started. Check its status in the Azure portal; "
          f"grounding is empty until it finishes.")


if __name__ == "__main__":
    main()
