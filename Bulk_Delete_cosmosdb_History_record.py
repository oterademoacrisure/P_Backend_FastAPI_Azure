"""
Wipes every document history record (both "upload" and any other action-type
items) from the DocumentHistory container -- the compliance audit log of
every document version ever uploaded, across every user. This is the ONLY
container this script touches; Checkpoints (LangGraph /v2 sessions) and
UserCredential (login accounts) are deliberately out of scope and untouched.

There is no undo. Deleting an item via the Cosmos SDK is permanent unless
your account has point-in-time restore / continuous backup configured
separately in Azure -- this script does not check for or rely on that.

Reads AZURE_COSMOS_ENDPOINT / AZURE_COSMOS_KEY / AZURE_COSMOS_DATABASE_NAME /
AZURE_COSMOS_CONTAINER_NAME from .env, the same variables
app/services/document_history_service.py uses -- so this always targets
whatever the running app is actually configured against, never a
hardcoded/stale account.
"""

import os

from azure.cosmos import CosmosClient
from dotenv import load_dotenv

load_dotenv()

ENDPOINT = os.environ["AZURE_COSMOS_ENDPOINT"]
KEY = os.environ["AZURE_COSMOS_KEY"]
DATABASE_NAME = os.getenv("AZURE_COSMOS_DATABASE_NAME", "PayerIQ")
CONTAINER_NAME = os.getenv("AZURE_COSMOS_CONTAINER_NAME", "DocumentHistory")


def main() -> None:
    client = CosmosClient(ENDPOINT, credential=KEY)
    container = client.get_database_client(DATABASE_NAME).get_container_client(CONTAINER_NAME)

    items = list(container.query_items(query="SELECT * FROM c", enable_cross_partition_query=True))
    if not items:
        print(f"{DATABASE_NAME}/{CONTAINER_NAME} is already empty -- nothing to delete.")
        return

    print(f"About to permanently delete {len(items)} item(s) from {DATABASE_NAME}/{CONTAINER_NAME}.")
    print("This wipes the entire document history / compliance audit log for every user. There is no undo.")
    confirmation = input("Type DELETE (all caps) to proceed, anything else to abort: ")
    if confirmation != "DELETE":
        print("Aborted -- nothing was deleted.")
        return

    deleted = 0
    for item in items:
        container.delete_item(item, partition_key=item["documentId"])
        deleted += 1
        if deleted % 100 == 0:
            print(f"  ...{deleted}/{len(items)} deleted")

    print(f"Done -- deleted {deleted} item(s) from {DATABASE_NAME}/{CONTAINER_NAME}.")


if __name__ == "__main__":
    main()
