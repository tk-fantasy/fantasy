"""邻居关系分析 — 从 kg-pipeline 移植，改造为通过 chat_fn 回调调用 LLM。

含"无明显关系"过滤逻辑（LLM 判定无关联的对不连边）和断点续传（进度存 llm_progress.json）。
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path

from .llm_utils import parse_json_from_llm, call_chat


PAIR_PROMPT = """分析以下两篇文档之间是否存在实质关联，输出 JSON：

{{
  "relation_type": "前置依赖|功能关联|配置关联|同主题|问题排查|无明显关系",
  "description": "简述两者关系；若选无明显关系，说明为何不相关"
}}

关系类型说明：
- 前置依赖：A 是 B 的前提，必须先完成 A 才能做 B
- 功能关联：功能上配合使用，比如聊天与设备控制
- 配置关联：都涉及同类配置项，比如各种 API 密钥
- 同主题：同领域有弱关联，但无前后/配合关系，比如两篇都讲摄像头
- 问题排查：都关于运维、日志、故障定位
- 无明显关系：仅向量偶然相似，实际无实质关联。仅当两文档确实无关联时选用，不要滥用

文档A：{title_a}
{excerpt_a}

文档B：{title_b}
{excerpt_b}"""


def _call_llm(doc_a, doc_b, chat_fn) -> str | None:
    """分析一对文档的关系，返回关系类型或 None（无明显关系）。"""
    excerpt_a = doc_a.raw_text[:300].replace("\n", " ")
    excerpt_b = doc_b.raw_text[:300].replace("\n", " ")
    prompt = PAIR_PROMPT.format(
        title_a=doc_a.title,
        title_b=doc_b.title,
        excerpt_a=excerpt_a,
        excerpt_b=excerpt_b,
    )
    try:
        content = call_chat(
            chat_fn,
            [
                {"role": "system", "content": "只输出JSON，不要多余内容。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=256,
        )
        data = parse_json_from_llm(content)
        if not data:
            return "同主题"
        rtype = data.get("relation_type", "同主题")
        if rtype == "无明显关系":
            return None
        return rtype
    except Exception:
        return "同主题"


def analyze_neighbor_pairs(
    docs: list,
    vectorizer,
    threshold: float,
    chat_fn,
    max_workers: int = 8,
    task_dir: Path | None = None,
    on_progress=None,
) -> list[tuple[str, str, str, float, str]]:
    """对向量相似度 ≥ threshold 的文档对，用 LLM 分析关系并过滤无明显关系。

    Args:
        docs: 文档列表
        vectorizer: Vectorizer 实例（需有 get_neighbors_above_threshold）
        threshold: 相似度阈值
        chat_fn: 同步 LLM 调用回调
        max_workers: 并发线程数
        task_dir: 任务目录（用于断点续传，存 llm_progress.json）
        on_progress: 可选回调 on_progress(done, total)
    Returns:
        边列表 [(src, tgt, rtype, score, "llm_neighbor"), ...]
    """
    if len(docs) < 2:
        return []

    id_to_doc = {d.id: d for d in docs}

    # 收集需要分析的文档对
    seen_pairs: set[tuple[str, str]] = set()
    pairs_to_analyze: list[tuple[str, str, float]] = []
    for doc in docs:
        neighbors = vectorizer.get_neighbors_above_threshold(doc.id, threshold)
        for nid, score in neighbors:
            pair = tuple(sorted([doc.id, nid]))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            pairs_to_analyze.append((doc.id, nid, score))

    print(f"  {len(pairs_to_analyze)} neighbor pairs need LLM analysis (threshold={threshold})")
    if not pairs_to_analyze:
        return []

    # ---- 断点续传 ----
    progress_path = task_dir / "llm_progress.json" if task_dir else None
    results_cache: dict[str, dict] = {}
    if progress_path and progress_path.exists():
        try:
            results_cache = json.loads(progress_path.read_text(encoding="utf-8"))
            print(f"  恢复断点：已完成 {len(results_cache)}/{len(pairs_to_analyze)} 对")
        except Exception:
            results_cache = {}

    def _pair_key(doc_id, neighbor_id):
        return "|".join(sorted([doc_id, neighbor_id]))

    def _save_progress():
        if progress_path:
            progress_path.write_text(
                json.dumps(results_cache, ensure_ascii=False), encoding="utf-8"
            )

    edges = []
    done = 0
    total = len(pairs_to_analyze)
    pending = []

    # 先从缓存取已完成的
    for doc_id, neighbor_id, score in pairs_to_analyze:
        key = _pair_key(doc_id, neighbor_id)
        if key in results_cache:
            cached = results_cache[key]
            if cached.get("rtype") is not None:
                edges.append((doc_id, neighbor_id, cached["rtype"], round(score, 4), "llm_neighbor"))
            done += 1
        else:
            pending.append((doc_id, neighbor_id, score))

    if done > 0:
        print(f"  从缓存恢复 {done} 对，还需分析 {len(pending)} 对")

    # 并发分析未完成的
    batch_save = 10
    since_save = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_to_pair = {}
        for doc_id, neighbor_id, score in pending:
            doc_a = id_to_doc[doc_id]
            doc_b = id_to_doc[neighbor_id]
            fut = pool.submit(_call_llm, doc_a, doc_b, chat_fn)
            fut_to_pair[fut] = (doc_id, neighbor_id, score)

        for fut in as_completed(fut_to_pair):
            doc_id, neighbor_id, score = fut_to_pair[fut]
            rtype = fut.result()
            key = _pair_key(doc_id, neighbor_id)
            results_cache[key] = {"rtype": rtype, "score": round(score, 4)}
            if rtype is not None:
                edges.append((doc_id, neighbor_id, rtype, round(score, 4), "llm_neighbor"))
            done += 1
            since_save += 1
            if on_progress:
                on_progress(done, total)
            elif done % 20 == 0 or done == total:
                print(f"  Analyzed {done}/{total} neighbor pairs...")
            if since_save >= batch_save:
                _save_progress()
                since_save = 0

    _save_progress()
    print(f"  Total neighbor LLM edges: {len(edges)} (filtered out {total - len(edges)} 无明显关系)")
    return edges
