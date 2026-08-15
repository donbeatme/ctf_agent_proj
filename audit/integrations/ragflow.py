"""RAGFlow experience storage with a deliberately small public API."""

import json
import re
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from ..schemas import AuditRecord
from ..settings import Settings
from .experience import build_experience, experience_markdown
from .langsmith_logger import redact


class RAGFlowExperienceStore:
    """Store and retrieve audit experiences through RAGFlow's HTTP API.

    The public surface is intentionally limited to ``get_or_create_dataset``,
    ``store_experience`` and ``retrieve_experience``.
    """

    source = "ragflow"

    def __init__(
        self,
        settings: Settings,
        session: Any = None,
        observer: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        if not settings.ragflow_api_key:
            raise RuntimeError("RAGFLOW_ENABLED=true 时必须设置 RAGFLOW_API_KEY")
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("RAGFlow 已启用但未安装 requests：pip install -e '.[rag]'") from exc

        self._requests = requests
        self._session = session or requests.Session()
        self._api_url = self._normalize_api_url(settings.ragflow_base_url)
        self._headers = {"Authorization": "Bearer %s" % settings.ragflow_api_key}
        self._dataset_name = settings.ragflow_dataset_name
        self._timeout = settings.ragflow_timeout_seconds
        self._dataset: Optional[Dict[str, Any]] = None
        self._observations: List[Dict[str, Any]] = []
        self._observer = observer
        if observer is None and settings.ragflow_observability:
            self._observer = self._console_observer

    @property
    def observations(self) -> List[Dict[str, Any]]:
        """Return safe structured events emitted by this store instance."""

        return [dict(item) for item in self._observations]

    def get_or_create_dataset(self) -> Dict[str, Any]:
        if self._dataset is not None:
            return dict(self._dataset)
        response = self._request("GET", "/datasets")
        datasets = self._dataset_list(response.get("data"))
        for dataset in datasets:
            if dataset.get("name") == self._dataset_name:
                self._dataset = dataset
                self._observe("dataset.ready", {
                    "action": "reused",
                    "dataset_id": dataset.get("id"),
                    "dataset_name": dataset.get("name"),
                })
                return dict(self._dataset)
        response = self._request(
            "POST",
            "/datasets",
            json={
                "name": self._dataset_name,
                "description": "Flag-safe CTF agent evaluation and Reflexion experiences",
                "chunk_method": "naive",
                "permission": "me",
            },
        )
        dataset = response.get("data")
        if not isinstance(dataset, dict) or not dataset.get("id"):
            raise RuntimeError("RAGFlow 创建数据集后未返回有效 id")
        self._dataset = dataset
        self._observe("dataset.ready", {
            "action": "created",
            "dataset_id": dataset.get("id"),
            "dataset_name": dataset.get("name"),
        })
        return dict(dataset)

    def store_experience(self, record: AuditRecord) -> Dict[str, Any]:
        dataset = self.get_or_create_dataset()
        dataset_id = str(dataset["id"])
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", record.attempt.attempt_id).strip("-")
        document_name = "experience-%s.md" % (safe_id or "unknown")
        self._observe("store.start", {
            "attempt_id": record.attempt.attempt_id,
            "dataset_id": dataset_id,
            "document_name": document_name,
        })
        if self._document_exists(dataset_id, document_name):
            self._observe("store.skipped", {
                "reason": "document_exists",
                "dataset_id": dataset_id,
                "document_name": document_name,
            })
            return {
                "status": "skipped",
                "count": 0,
                "dataset_id": dataset_id,
                "document_ids": [],
                "error": None,
            }

        content = experience_markdown(build_experience(record)).encode("utf-8")
        uploaded = self._request(
            "POST",
            "/datasets/%s/documents" % dataset_id,
            files=[("file", (document_name, content, "text/markdown"))],
        )
        documents = self._document_list(uploaded.get("data"))
        document_ids = [str(item["id"]) for item in documents if item.get("id")]
        if not document_ids:
            raise RuntimeError("RAGFlow 上传经验后未返回文档 id")
        self._request(
            "POST",
            "/datasets/%s/chunks" % dataset_id,
            json={"document_ids": document_ids},
        )
        self._observe("store.done", {
            "dataset_id": dataset_id,
            "document_name": document_name,
            "document_ids": document_ids,
            "count": len(document_ids),
        })
        return {
            "status": "stored",
            "count": len(document_ids),
            "dataset_id": dataset_id,
            "document_ids": document_ids,
            "error": None,
        }

    def retrieve_experience(
        self,
        query: str,
        limit: int = 5,
        agent_id: str = "",
    ) -> List[Dict[str, Any]]:
        started = time.perf_counter()
        self._observe("retrieval.start", {
            "query": self._preview(query),
            "limit": max(1, limit),
            "agent_id": agent_id,
        })
        try:
            dataset = self.get_or_create_dataset()
            scoped_query = query
            if agent_id:
                scoped_query = "%s；优先复用 agent_id=%s 的经验。" % (query, agent_id)
            response = self._request(
                "POST",
                "/retrieval",
                json={
                    "question": scoped_query,
                    "dataset_ids": [str(dataset["id"])],
                    "page": 1,
                    "page_size": max(1, limit),
                    "similarity_threshold": 0.1,
                    "vector_similarity_weight": 0.3,
                    "top_k": 256,
                    "keyword": True,
                },
            )
        except Exception as exc:
            cause = exc.__cause__ or exc
            self._observe("retrieval.error", {
                "error": type(cause).__name__,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            })
            raise
        data = response.get("data")
        chunks = data.get("chunks", []) if isinstance(data, dict) else []
        results = []
        for chunk in chunks[:max(1, limit)]:
            if not isinstance(chunk, dict):
                continue
            content = str(redact(chunk.get("content", ""))).strip()
            if not content:
                continue
            results.append({
                "id": str(chunk.get("id", "")),
                "memory": content,
                "score": chunk.get("similarity"),
                "metadata": redact({
                    "document_id": chunk.get("document_id"),
                    "document_name": chunk.get("document_keyword") or chunk.get("doc_name"),
                    "dataset_id": dataset.get("id"),
                }),
            })
        for rank, result in enumerate(results, 1):
            metadata = result.get("metadata", {})
            self._observe("retrieval.hit", {
                "rank": rank,
                "score": result.get("score"),
                "chunk_id": result.get("id"),
                "document_name": metadata.get("document_name"),
                "preview": self._preview(result.get("memory", "")),
            })
        self._observe("retrieval.done", {
            "dataset_id": dataset.get("id"),
            "hits": len(results),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        })
        return results

    def _document_exists(self, dataset_id: str, document_name: str) -> bool:
        page = 1
        page_size = 100
        while True:
            response = self._request(
                "GET",
                "/datasets/%s/documents" % dataset_id,
                params={"page": page, "page_size": page_size},
            )
            data = response.get("data")
            documents = self._document_list(data)
            if any(item.get("name") == document_name for item in documents):
                return True
            total = data.get("total") if isinstance(data, dict) else None
            if not documents or len(documents) < page_size:
                return False
            if isinstance(total, int) and page * page_size >= total:
                return False
            page += 1

    def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        headers = dict(self._headers)
        if "files" not in kwargs:
            headers["Content-Type"] = "application/json"
        try:
            response = self._session.request(
                method,
                self._api_url + path,
                headers=headers,
                timeout=self._timeout,
                **kwargs,
            )
            response.raise_for_status()
            payload = response.json()
        except self._requests.RequestException as exc:
            self._observe("request.error", {
                "method": method,
                "path": path,
                "error": type(exc).__name__,
            })
            raise RuntimeError("RAGFlow 请求失败：%s" % type(exc).__name__) from exc
        except ValueError as exc:
            self._observe("request.error", {
                "method": method,
                "path": path,
                "error": "InvalidJSON",
            })
            raise RuntimeError("RAGFlow 返回了非 JSON 响应") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("RAGFlow 返回格式错误")
        if payload.get("code", 0) != 0:
            message = str(payload.get("message") or "unknown error")
            raise RuntimeError("RAGFlow API 错误：%s" % message[:300])
        return payload

    def _observe(self, event: str, payload: Dict[str, Any]) -> None:
        safe_payload = redact(payload)
        observation = {"event": event, **safe_payload}
        self._observations.append(observation)
        if len(self._observations) > 200:
            del self._observations[:-200]
        if self._observer is not None:
            self._observer(dict(observation))

    @staticmethod
    def _console_observer(observation: Dict[str, Any]) -> None:
        event = observation.get("event", "event")
        payload = {key: value for key, value in observation.items() if key != "event"}
        print(
            "[RAGFlow] %s %s" % (
                event,
                json.dumps(payload, ensure_ascii=False, default=str),
            ),
            flush=True,
        )

    @staticmethod
    def _preview(value: Any, limit: int = 160) -> str:
        safe = str(redact(value))
        compact = " ".join(safe.split())
        return compact if len(compact) <= limit else compact[:limit - 3] + "..."

    @staticmethod
    def _normalize_api_url(base_url: str) -> str:
        parts = urlsplit(base_url.strip())
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("RAGFLOW_BASE_URL 必须是 http(s) URL")
        path = parts.path.rstrip("/")
        if not path.endswith("/api/v1"):
            path += "/api/v1"
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    @staticmethod
    def _dataset_list(data: Any) -> List[Dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            raw = data.get("kbs") or data.get("datasets") or []
            return [item for item in raw if isinstance(item, dict)]
        return []

    @staticmethod
    def _document_list(data: Any) -> List[Dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            raw = data.get("docs") or data.get("documents") or []
            return [item for item in raw if isinstance(item, dict)]
        return []
