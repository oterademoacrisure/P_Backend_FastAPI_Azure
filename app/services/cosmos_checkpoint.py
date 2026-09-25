"""
Azure Cosmos DB (NoSQL API) checkpoint saver for LangGraph.

Reuses the same Cosmos account already provisioned for document history
(see app/services/document_history_service.py) instead of requiring a
separate Postgres server. Stores one item per checkpoint -- holding the
full channel_values snapshot, per the Checkpoint.channel_values contract
-- and one item per pending write, all in a single container partitioned
by /thread_id. Unlike the Postgres/SQLite savers, no separate blob table
is needed to dedupe unchanged channel values across checkpoints: Cosmos
documents are cheap and self-contained enough here that the simpler,
fully-denormalized layout is preferable.

Only the async surface is implemented since the /v2 pipeline
(app/routergenerator.py) only ever calls the graph's async methods.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Sequence
from typing import Any

from azure.cosmos import PartitionKey, exceptions
from azure.cosmos.aio import ContainerProxy, CosmosClient
from langchain_core.runnables import RunnableConfig

from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)


def _encode(typed: tuple[str, bytes]) -> dict:
    type_, data = typed
    return {"type": type_, "data": base64.b64encode(data).decode("ascii")}


class AsyncCosmosDBSaver(BaseCheckpointSaver[int]):
    """Async LangGraph checkpointer backed by Azure Cosmos DB (NoSQL API)."""

    def __init__(
        self,
        client: CosmosClient,
        database_name: str,
        container_name: str = "Checkpoints",
    ) -> None:
        super().__init__()
        self._client = client
        self._database_name = database_name
        self._container_name = container_name
        self._container: ContainerProxy | None = None

    async def setup(self) -> None:
        """Creates the checkpoint container if it doesn't already exist.

        Call once at startup after opening the Cosmos client, mirroring
        AsyncPostgresSaver.setup()'s one-time schema init."""
        database = self._client.get_database_client(self._database_name)
        self._container = await database.create_container_if_not_exists(
            id=self._container_name,
            partition_key=PartitionKey(path="/thread_id"),
        )

    def _require_container(self) -> ContainerProxy:
        if self._container is None:
            raise RuntimeError("AsyncCosmosDBSaver.setup() must be called before use.")
        return self._container

    def _decode(self, encoded: dict) -> Any:
        return self.serde.loads_typed((encoded["type"], base64.b64decode(encoded["data"])))

    @staticmethod
    def _checkpoint_doc_id(checkpoint_ns: str, checkpoint_id: str) -> str:
        return f"checkpoint::{checkpoint_ns}::{checkpoint_id}"

    @staticmethod
    def _write_doc_id(checkpoint_ns: str, checkpoint_id: str, task_id: str, idx: int) -> str:
        return f"write::{checkpoint_ns}::{checkpoint_id}::{task_id}::{idx}"

    async def _load_writes(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> list[tuple[str, str, Any]]:
        container = self._require_container()
        query = (
            "SELECT * FROM c WHERE c.thread_id = @tid AND c.type = 'write' "
            "AND c.checkpoint_ns = @ns AND c.checkpoint_id = @cid"
        )
        parameters = [
            {"name": "@tid", "value": thread_id},
            {"name": "@ns", "value": checkpoint_ns},
            {"name": "@cid", "value": checkpoint_id},
        ]
        docs = []
        async for doc in container.query_items(query=query, parameters=parameters, partition_key=thread_id):
            docs.append(doc)
        docs.sort(key=lambda d: d["idx"])
        return [(doc["task_id"], doc["channel"], self._decode(doc["value"])) for doc in docs]

    def _tuple_from_doc(
        self, doc: dict, metadata: CheckpointMetadata, pending_writes: list
    ) -> CheckpointTuple:
        thread_id = doc["thread_id"]
        checkpoint_ns = doc["checkpoint_ns"]
        checkpoint_id = doc["checkpoint_id"]
        parent_checkpoint_id = doc.get("parent_checkpoint_id")
        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                }
            },
            checkpoint=self._decode(doc["checkpoint"]),
            metadata=metadata,
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent_checkpoint_id,
                    }
                }
                if parent_checkpoint_id
                else None
            ),
            pending_writes=pending_writes,
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        container = self._require_container()
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)

        if checkpoint_id:
            try:
                doc = await container.read_item(
                    item=self._checkpoint_doc_id(checkpoint_ns, checkpoint_id),
                    partition_key=thread_id,
                )
            except exceptions.CosmosResourceNotFoundError:
                return None
        else:
            query = (
                "SELECT * FROM c WHERE c.thread_id = @tid AND c.type = 'checkpoint' "
                "AND c.checkpoint_ns = @ns ORDER BY c.checkpoint_id DESC OFFSET 0 LIMIT 1"
            )
            parameters = [
                {"name": "@tid", "value": thread_id},
                {"name": "@ns", "value": checkpoint_ns},
            ]
            doc = None
            async for item in container.query_items(query=query, parameters=parameters, partition_key=thread_id):
                doc = item
                break
            if doc is None:
                return None

        metadata = self._decode(doc["metadata"])
        pending_writes = await self._load_writes(thread_id, checkpoint_ns, doc["checkpoint_id"])
        return self._tuple_from_doc(doc, metadata, pending_writes)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        container = self._require_container()
        conditions = ["c.type = 'checkpoint'"]
        parameters: list[dict] = []
        partition_key = None

        if config and config.get("configurable", {}).get("thread_id"):
            thread_id = config["configurable"]["thread_id"]
            partition_key = thread_id
            conditions.append("c.thread_id = @tid")
            parameters.append({"name": "@tid", "value": thread_id})
            checkpoint_ns = config["configurable"].get("checkpoint_ns")
            if checkpoint_ns is not None:
                conditions.append("c.checkpoint_ns = @ns")
                parameters.append({"name": "@ns", "value": checkpoint_ns})

        before_id = get_checkpoint_id(before) if before else None
        if before_id:
            conditions.append("c.checkpoint_id < @before_id")
            parameters.append({"name": "@before_id", "value": before_id})

        query = f"SELECT * FROM c WHERE {' AND '.join(conditions)} ORDER BY c.checkpoint_id DESC"
        kwargs: dict[str, Any] = {"query": query, "parameters": parameters}
        if partition_key is not None:
            kwargs["partition_key"] = partition_key

        yielded = 0
        async for doc in container.query_items(**kwargs):
            if limit is not None and yielded >= limit:
                break
            metadata = self._decode(doc["metadata"])
            if filter and not all(metadata.get(k) == v for k, v in filter.items()):
                continue
            pending_writes = await self._load_writes(doc["thread_id"], doc["checkpoint_ns"], doc["checkpoint_id"])
            yield self._tuple_from_doc(doc, metadata, pending_writes)
            yielded += 1

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: dict,
        metadata: CheckpointMetadata,
        new_versions: dict,
    ) -> RunnableConfig:
        container = self._require_container()
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        parent_checkpoint_id = config["configurable"].get("checkpoint_id")
        checkpoint_id = checkpoint["id"]

        doc = {
            "id": self._checkpoint_doc_id(checkpoint_ns, checkpoint_id),
            "thread_id": thread_id,
            "type": "checkpoint",
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint_id,
            "parent_checkpoint_id": parent_checkpoint_id,
            "checkpoint": _encode(self.serde.dumps_typed(checkpoint)),
            "metadata": _encode(self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))),
        }
        await container.upsert_item(doc)
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        container = self._require_container()
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]

        for idx, (channel, value) in enumerate(writes):
            inner_idx = WRITES_IDX_MAP.get(channel, idx)
            doc = {
                "id": self._write_doc_id(checkpoint_ns, checkpoint_id, task_id, inner_idx),
                "thread_id": thread_id,
                "type": "write",
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
                "task_id": task_id,
                "task_path": task_path,
                "idx": inner_idx,
                "channel": channel,
                "value": _encode(self.serde.dumps_typed(value)),
            }
            if inner_idx < 0:
                # Special writes (ERROR/SCHEDULED/INTERRUPT/RESUME) always
                # overwrite; regular writes are write-once like Postgres's
                # ON CONFLICT DO NOTHING.
                await container.upsert_item(doc)
            else:
                try:
                    await container.create_item(doc)
                except exceptions.CosmosResourceExistsError:
                    pass

    async def adelete_thread(self, thread_id: str) -> None:
        container = self._require_container()
        query = "SELECT c.id FROM c WHERE c.thread_id = @tid"
        parameters = [{"name": "@tid", "value": thread_id}]
        async for doc in container.query_items(query=query, parameters=parameters, partition_key=thread_id):
            await container.delete_item(item=doc["id"], partition_key=thread_id)
