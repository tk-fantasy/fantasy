"""实体抽取 — 从 kg-pipeline 移植，改造为通过 chat_fn 回调调用 LLM。

通过注入的 chat_fn（同步）调用 LLM，不再依赖 pipeline.Config / _utils.KeyPool。
"""
from concurrent.futures import ThreadPoolExecutor, as_completed

from .llm_utils import parse_json_from_llm, call_chat


SINGLE_PROMPT = """从以下文档中提取实体及关系。

必须以纯JSON输出：
{{
  "entities": [{{"name": "...", "type": "AI模型|API协议|SDK|工具|框架|服务|协议|应用|组件|概念"}}],
  "relations": [{{"source": "...", "target": "...", "type": "调用|依赖|包含|引用|属于|实现|接入|兼容"}}]
}}

文档：
{text}}}"""


class EntityExtractor:
    """用 LLM 从文档中抽取实体与关系。

    Args:
        chat_fn: 同步 LLM 调用回调，签名 chat_fn(messages, max_tokens) -> str
        max_workers: 并发线程数
    """

    def __init__(self, chat_fn, max_workers: int = 8) -> None:
        self._chat_fn = chat_fn
        self._max_workers = max_workers

    def extract_batch(self, docs: list, on_progress=None) -> list[dict]:
        """批量抽取实体。

        Args:
            docs: 文档列表（需有 raw_text 属性）
            on_progress: 可选回调 on_progress(done, total)
        Returns:
            与 docs 等长的结果列表，每项 {"entities": [...], "relations": [...]}
        """
        results: list[dict | None] = [None] * len(docs)
        done = 0

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            fut_map = {
                pool.submit(self._extract_one, doc.raw_text[:1500]): i
                for i, doc in enumerate(docs)
            }
            for fut in as_completed(fut_map):
                idx = fut_map[fut]
                done += 1
                try:
                    results[idx] = fut.result()
                except Exception as e:
                    msg = str(e).encode('ascii', errors='replace').decode()
                    print(f"  ! [{done}/{len(docs)}] doc {idx}: {msg}")
                    results[idx] = {"entities": [], "relations": []}
                if on_progress:
                    on_progress(done, len(docs))
                elif done % 10 == 0 or done == len(docs):
                    print(f"  [{done}/{len(docs)}] entities extracted")

        return results  # type: ignore[return-value]

    def _extract_one(self, text: str) -> dict:
        try:
            content = call_chat(
                self._chat_fn,
                [
                    {"role": "system", "content": "只输出JSON，不要多余内容。"},
                    {"role": "user", "content": SINGLE_PROMPT.format(text=text)},
                ],
                max_tokens=1024,
            )
            result = parse_json_from_llm(content)
            return result if result else {"entities": [], "relations": []}
        except Exception as e:
            msg = str(e).encode('ascii', errors='replace').decode()
            print(f"  ! extract error: {msg}")
            return {"entities": [], "relations": []}
