# RAG 与 ReAct 核心机制设计

> **文档性质**：RAG 知识问答与 ReAct 任务执行的核心机制详细设计
> **适用模块**：L4 知识问答（RAG）、L1/L2/L5/L9/L21-L23 任务执行（ReAct）
> **后续维护**：如遇 RAG 或 ReAct 相关问题，从此文档查起

---

## 第一部分：RAG 知识问答机制

### 1. 整体架构

```
用户提问
    │
    ▼
┌─────────────────────────────────────────┐
│ 1. Query 理解                           │
│  ├─ 意图识别：knowledge_qa              │
│  ├─ Query 扩展（多轮上下文补全）          │
│  └─ 查询改写（同义词扩展）                │
└────────────────────┬────────────────────┘
                     ▼
┌─────────────────────────────────────────┐
│ 2. 检索（Retrieval）                    │
│  ├─ 向量检索（BGE-M3）→ Top-20          │
│  ├─ BM25 关键词检索 → Top-20            │
│  ├─ RRF 融合排序 → Top-20               │
│  ├─ Cross-Encoder 精排 → Top-10         │
│  └─ 动态截断 → Top-3~8（根据分数）       │
└────────────────────┬────────────────────┘
                     ▼
┌─────────────────────────────────────────┐
│ 3. 生成（Generation）                    │
│  ├─ 上下文构建（去重、多样性控制）         │
│  ├─ LLM 生成答案                        │
│  └─ 引用标注（来源标注）                  │
└────────────────────┬────────────────────┘
                     ▼
              返回答案 + 引用
```

---

### 2. 检索策略详解

#### 2.1 混合检索（宽进严出）

**第一层：宽检索（候选池）**
```python
# 向量检索：语义匹配，取 Top-20
vector_results = chroma_collection.query(
    query_embeddings=[bge_m3.encode(query)],
    n_results=20,
    where={"access_level": "public"}  # 权限过滤
)

# BM25 检索：关键词精确匹配，取 Top-20
bm25_results = bm25_index.search(query, top_k=20)
```

**为什么取 20 而不是 5？**
- 保证**查全率**：需要的信息可能在 Top-10，但不在 Top-5
- 后续有精排和动态截断，不怕噪音多，怕好结果漏掉

#### 2.2 RRF 融合（Reciprocal Rank Fusion）

**核心问题**：向量分数（0-1）和 BM25 分数（0-∞）不可比，直接按分数排序会失真。

**解决方案**：不看分数，只看**排名**，用公式融合：

```
RRF_score(doc) = Σ 1 / (rank_i(doc) + k)

其中：
- rank_i(doc)：文档在第 i 个检索结果中的排名（第1名=1，第2名=2...）
- k：常数，通常取 60（平滑参数，防止第一名权重过大）
```

**计算示例**：

| 文档 | 向量检索排名 | BM25排名 | RRF计算 | 最终得分 |
|------|-------------|---------|---------|---------|
| **A** | **1** | **2** | 1/(1+60) + 1/(2+60) | **0.0328** ✅ |
| B | 2 | 10 | 1/(2+60) + 1/(10+60) | 0.0303 |
| C | 5 | **1** | 1/(5+60) + 1/(1+60) | 0.0301 |

**结果**：文档 A 综合得分最高，虽然 BM25 只排第2，但向量检索第1。

**代码实现**：
```python
def reciprocal_rank_fusion(rankings, k=60):
    """
    rankings: [
        [doc_A, doc_B, doc_C],  # 向量检索结果（已排序）
        [doc_C, doc_A, doc_D]   # BM25检索结果（已排序）
    ]
    """
    rrf_scores = {}
    
    for ranking in rankings:
        for rank, doc in enumerate(ranking, start=1):
            doc_id = doc.id
            if doc_id not in rrf_scores:
                rrf_scores[doc_id] = 0
            rrf_scores[doc_id] += 1 / (rank + k)
    
    # 按RRF分数排序
    sorted_docs = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
    return [get_doc(doc_id) for doc_id, _ in sorted_docs]
```

#### 2.3 Cross-Encoder 精排

**为什么需要**：RRF 只看排名，精排看**语义相关性**，进一步提升精度。

```python
from sentence_transformers import CrossEncoder

reranker = CrossEncoder('BAAI/bge-reranker-base')

def rerank(query, documents, top_k=10):
    # 构造 (query, doc) 对
    pairs = [(query, doc.text) for doc in documents]
    
    # 预测相关性分数
    scores = reranker.predict(pairs)
    
    # 按分数重新排序，取前 10
    reranked = sorted(zip(documents, scores), key=lambda x: x[1], reverse=True)
    return [doc for doc, _ in reranked[:top_k]]
```

#### 2.4 动态截断（关键优化）

**不是固定 Top-5**，而是根据**分数分布**和 **Token 预算**动态决定：

```python
def dynamic_truncate(reranked_chunks, max_tokens=2000):
    """
    企业级动态截断策略
    """
    selected = []
    current_tokens = 0
    source_count = {}
    
    for i, chunk in enumerate(reranked_chunks):
        # 策略 1: 分数阈值（相关性太低不要）
        if chunk.score < 0.6:
            continue
        
        # 策略 2: 分数落差检测（Top-5 和 Top-6 分差大，说明 Top-5 已足够）
        if i >= 5 and i > 0:
            gap = reranked_chunks[i-1].score - chunk.score
            if gap > 0.15:  # 分差大于 0.15，停止
                break
        
        # 策略 3: 多样性控制（同一文档最多取 2 段，避免都是同一章节）
        source = chunk.metadata['source']
        if source_count.get(source, 0) >= 2:
            continue
        
        # 策略 4: Token 预算检查
        chunk_tokens = count_tokens(chunk.text)
        if current_tokens + chunk_tokens > max_tokens:
            continue  # 跳过这个，看下一个（可能更短）
        
        selected.append(chunk)
        source_count[source] = source_count.get(source, 0) + 1
        current_tokens += chunk_tokens
    
    return selected
```

**为什么不固定 Top-K？**
- 如果 Top-5 都很好（分数都 >0.8），且与 Top-6 分差大，说明 5 个足够
- 如果 Top-5 和 Top-6 分数接近，说明需要更多上下文，自动扩展到 Top-8

---

### 3. 上下文构建

**去重 + 格式化**：
```python
def build_context(selected_chunks):
    # 去重（可能不同 chunk 有重叠）
    unique_chunks = deduplicate(selected_chunks)
    
    # 格式化，带引用标注
    context_parts = []
    for i, chunk in enumerate(unique_chunks):
        context_parts.append(
            f"[资料{i+1}] {chunk.text}\n"
            f"来源：{chunk.metadata['source']} {chunk.metadata.get('section', '')}"
        )
    
    return "\n\n".join(context_parts)
```

---

### 4. 生成与引用验证

**LLM 提示词模板**：
```
基于以下资料回答用户问题。

【资料】
{context}

【用户问题】
{query}

【要求】
1. 答案必须基于资料内容，不要编造
2. 如果资料不足，明确说明"根据现有资料无法回答"
3. 标注引用来源，格式：【来源：员工手册.pdf §4.2】
4. 语言简洁，重点突出

【回答】
```

**引用验证（防止幻觉）**：
```python
def verify_citations(answer, retrieved_chunks):
    # 提取答案中的引用
    claimed_sources = extract_citations(answer)  # 解析【来源：...】
    
    # 验证引用是否真实存在
    valid_sources = {chunk.metadata['source'] for chunk in retrieved_chunks}
    
    for source in claimed_sources:
        if source not in valid_sources:
            log_warning(f"幻觉引用：{source}")
            return False  # 触发重新生成
    
    return True
```

---

### 5. 性能指标

| 指标 | 目标值 | 说明 |
|------|--------|------|
| **检索延迟** | < 200ms | 向量 + BM25 并行检索 |
| **精排延迟** | < 100ms | Cross-Encoder |
| **生成延迟** | < 2s | LLM 生成 |
| **总延迟** | < 2.5s | 端到端 |
| **检索准确率** | > 90% | Top-5 中包含正确答案 |
| **答案忠实度** | > 95% | 答案基于检索内容，无编造 |

---

## 第二部分：ReAct 任务执行机制

### 1. 核心概念

**ReAct = Reasoning + Acting**：边推理边行动，每步根据上一步结果决定下一步。

**与 Plan-and-Execute 的区别**：
- Plan-and-Execute：先出完整计划，再执行（静态）
- **ReAct**：只出骨架（步骤序列），执行时动态提取参数（动态）

---

### 2. 状态定义（AgentState）

```python
from langgraph.graph import StateGraph, END
from typing import TypedDict, Annotated
import operator

class AgentState(TypedDict):
    # 输入
    original_task: str                    # 用户原始输入
    skeleton: list[str]                   # 计划骨架：["query_user", "query_events", "send_notify"]
    extracted_slots: dict                 # 意图识别阶段预提取的实体
    
    # 执行状态
    current_step_idx: int                 # 当前执行到第几步（索引）
    step_results: Annotated[list, operator.add]  # 每步结果累积（追加模式）
    context: dict                         # 工作记忆（共享上下文，后续步骤可读取前面的结果）
    
    # 控制
    status: str                           # planning / executing / completed / failed
    error: str | None                     # 错误信息
```

**关键设计**：
- `skeleton` 只存**步骤名**，不存具体参数（参数在执行时动态提取）
- `context` 是**共享黑板**，Step 2 可以读取 Step 1 的结果
- `operator.add` 表示 `step_results` 是**追加**模式，新结果加到列表末尾

---

### 3. 执行器节点（execute_step）

**核心方法，实现 ReAct 的 Act 阶段**：

```python
class ReActExecutor:
    def __init__(self, tools, llm):
        self.tools = tools
        self.llm = llm
    
    def execute_step(self, state: AgentState) -> dict:
        """执行单步：动态提取参数 → 调用工具 → 记录结果"""
        idx = state["current_step_idx"]
        step_name = state["skeleton"][idx]
        
        # 1. 动态提取参数（关键！）
        params = self._extract_params(
            tool_name=step_name,
            skeleton=state["skeleton"],
            slots=state["extracted_slots"],
            context=state["context"],        # 上一步结果在这里
            previous_results=state["step_results"]
        )
        
        # 2. 执行工具
        tool = self.tools[step_name]
        try:
            result = tool.run(params)
            status = "succeeded"
            error = None
        except Exception as e:
            result = None
            status = "failed"
            error = str(e)
        
        # 3. 更新工作记忆（关键！）
        new_context = {
            **state["context"],
            f"step_{idx}_result": result,       # 把结果放入 context，供下一步使用
            f"step_{idx}_status": status
        }
        
        # 4. 记录步骤结果
        step_record = {
            "step": step_name,
            "params": params,
            "result": result,
            "status": status,
            "error": error
        }
        
        # 5. 返回更新后的状态
        return {
            "step_results": [step_record],      # 追加到结果列表
            "context": new_context,             # 更新后的上下文
            "current_step_idx": idx + 1,        # 索引+1，准备执行下一步
            "status": "executing" if status == "succeeded" else "failed",
            "error": error
        }
```

**为什么这样设计**：
- **参数动态性**：Step 3（create_meeting）需要 Step 1 查到的 user_id，这个值在执行时才能确定，所以必须动态提取
- **工作记忆**：`context` 就像黑板，每一步都可以往上面写结果，后续步骤可以读

---

### 4. 动态参数提取（_extract_params）

```python
def _extract_params(self, tool_name, skeleton, slots, context, previous_results):
    """LLM 动态提取参数"""
    tool_schema = self.tools[tool_name].schema
    
    # 构建提示词
    prompt = f"""
    当前步骤：{tool_name}（比如 query_user）
    工具 Schema：{tool_schema}（比如 {{"type": "object", "properties": {{"name": {{"type": "string"}}}}}}）
    
    预提取实体：{slots}（比如 {{"person": ["张三"]}}）
    前序步骤结果：{previous_results}（比如 [{{"step": "query_user", "result": {{"id": "u1"}}}}]）
    
    请提取调用该工具所需的参数：
    """
    
    # LLM 根据上下文决定具体参数
    return self.llm.extract(prompt, response_format=tool_schema)
```

**示例**：
- 执行 `query_user` 时，LLM 看到 `slots` 里有 `person: "张三"`，提取参数 `{name: "张三"}`
- 执行 `create_meeting` 时，LLM 看到 `context` 里有 `step_0_result: {id: "u1"}`，提取参数 `{user_id: "u1", time: "2025-09-30T15:00"}`

---

### 5. 路由函数（should_continue）

**决定是继续执行、结束还是处理失败**：

```python
def should_continue(state: AgentState) -> str:
    if state["status"] == "failed":
        return "failed"                    # 去处理失败
    
    if state["current_step_idx"] >= len(state["skeleton"]):
        return "completed"                 # 所有步骤执行完毕，结束
    
    return "continue"                      # 还有步骤，继续执行
```

---

### 6. 失败处理（handle_failure）

**四级回退策略**：

```python
def handle_failure(state: AgentState) -> dict:
    """失败处理：重试/降级/跳过/终止"""
    last_step = state["step_results"][-1]
    retry_count = state["context"].get("retry_count", 0)
    
    # Level 1: 即时重试（同工具，指数退避）
    if retry_count < 3:
        return {
            "current_step_idx": state["current_step_idx"] - 1,  # 回到上一步
            "context": {**state["context"], "retry_count": retry_count + 1},
            "status": "executing"
        }
    
    # Level 2: 参数修复（同工具，改参数）
    # Level 3: 工具降级（换工具）
    # Level 4: 步骤跳过（Skip）
    
    # 重试次数用尽，标记失败
    return {"status": "failed", "error": "重试次数用尽"}
```

---

### 7. 图构建（build_react_graph）

**形成循环执行**：

```python
def build_react_graph():
    executor = ReActExecutor(tools, llm)
    
    workflow = StateGraph(AgentState)
    
    # 添加节点
    workflow.add_node("execute_step", executor.execute_step)
    workflow.add_node("handle_failure", handle_failure)
    
    # 入口点
    workflow.set_entry_point("execute_step")
    
    # 条件边：执行后决定去向
    workflow.add_conditional_edges(
        "execute_step",
        should_continue,
        {
            "continue": "execute_step",     # 继续 → 回到执行节点（循环）
            "completed": END,               # 完成 → 结束
            "failed": "handle_failure"      # 失败 → 去处理失败
        }
    )
    
    # 失败处理后的路由
    workflow.add_conditional_edges(
        "handle_failure",
        lambda s: "retry" if s["status"] == "executing" else "abort",
        {
            "retry": "execute_step",        # 重试 → 回到执行节点（循环）
            "abort": END                    # 终止 → 结束
        }
    )
    
    return workflow.compile()
```

**执行流程**：
```
execute_step → should_continue → execute_step（循环）
                    ↓
              completed → END
                    ↓
              failed → handle_failure → retry → execute_step（循环）
                                        ↓
                                      abort → END
```

---

### 8. 使用示例

```python
# 输入：意图识别模块的输出
initial_state = {
    "original_task": "通知张三明天下午三点开会",
    "skeleton": ["query_user", "query_events", "create_meeting", "send_notification"],  # 4步骨架
    "extracted_slots": {
        "person": ["张三"],
        "datetime": "2025-09-30T15:00:00"
    },
    "current_step_idx": 0,
    "step_results": [],
    "context": {},
    "status": "planning",
    "error": None
}

# 运行
graph = build_react_graph()
result = graph.invoke(initial_state)

# 结果：step_results 列表包含每步的执行记录
```

**执行轨迹**：
1. **Step 0**: query_user → 查张三 → 结果存入 context → 索引变为 1
2. **Step 1**: query_events → 用 context 里的 user_id 查事件 → 索引变为 2
3. **Step 2**: create_meeting → 用 context 里的 user_id + time 创建会议 → 索引变为 3
4. **Step 3**: send_notification → 用 context 里的 meeting_id 发送通知 → 索引变为 4
5. **should_continue**: 索引 4 >= 骨架长度 4 → 返回 "completed" → END

---

## 第三部分：关键问题速查

### RAG 相关问题

| 问题 | 答案 |
|------|------|
| **为什么要混合检索？** | 向量检索语义好但可能漏关键词，BM25 精确但无语义，融合互补 |
| **RRF 的 k 为什么取 60？** | 经验值，平衡头部和尾部权重，第1名和第10名差距平缓 |
| **为什么不固定 Top-K？** | 分数分布不均，有的查询 Top-5 足够，有的需要 Top-8，动态截断更优 |
| ** hallucination 怎么防？** | 引用验证：检查 LLM 生成的引用是否真实存在于检索结果中 |

### ReAct 相关问题

| 问题 | 答案 |
|------|------|
| **和 Plan-and-Execute 区别？** | ReAct 执行时动态提取参数，Plan-and-Execute 规划时固定参数 |
| **context 和 step_results 区别？** | context 是工作记忆（供下一步读取），step_results 是执行日志（供审计） |
| **失败重试会重复执行吗？** | 会，重试时 `current_step_idx` 回退，重新执行上一步 |
| **多意图怎么执行？** | 拆分为子图，独立执行后合并（详见多意图章节） |
