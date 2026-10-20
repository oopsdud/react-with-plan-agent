# ReAct 执行器 · 任务执行与回退机制

> **承接模块**：意图识别与规划模块输出（骨架 + extracted_slots）
> **执行模式**：LangGraph ReAct-with-Plan，逐步执行，动态提取参数
> **记忆集成**：工作记忆（上下文传递）+ 情景记忆（失败经验），**不含用户画像和语义记忆**
> **业务覆盖**：Simple 通道（11项）+ Complex 通道（即时触发/设置修改/即时消息/RAG问答/多意图）

---

## 1. 执行架构总览

```
意图识别输出
    │
    ▼
┌─────────────────────────────────────────┐
│  ReAct Executor                         │
│  ├─ 输入：skeleton（步骤序列）           │
│  │        extracted_slots（预提取实体）  │
│  │        context（父图上下文/历史）      │
│  │                                       │
│  ├─ 执行：逐步调用工具                   │
│  │   Step N: 动态提取参数 → 执行 → 观察  │
│  │                                       │
│  ├─ 回退：失败处理                       │
│  │   重试 → 降级 → 跳过 → 终止           │
│  │                                       │
│  └─ 记忆：工作记忆 + 情景记忆            │
│      上下文传递 + 失败经验记录            │
└─────────────────────────────────────────┘
```

---

## 2. Simple 通道业务执行（零 LLM 规划）

**特征**：单工具调用，正则提取参数，零 LLM 规划，直接执行。

| # | 业务 | 聊天触发示例 | 工具 | 执行流程 |
|---|------|------------|------|---------|
| **L7** | 定时提醒 | "提醒我明天下午三点交周报" | `create_reminder` | 正则提取时间+内容 → 直接创建 |
| **L11** | 员工信息查询 | "查张三" / "工号10086" / "技术部人员" | `query_employee` | 正则提取姓名/工号/部门 → 直接查询 |
| **L12** | 待办事项查询 | "查我的待办" / "高优先级任务" | `query_todo` | 正则提取状态/优先级 → 直接查询 |
| **L13** | 日程冲突检测 | "明天下午三点张三有空吗" | `check_schedule_conflict` | 正则提取时间+人员 → 直接检测 |
| **L14** | 快捷状态更新 | "把任务3标记为完成" / "请假状态更新" | `update_status` | 正则提取状态关键词 → 直接更新 |
| **L15** | 常用链接查询 | "OA系统地址" / "报销入口" | `get_quick_link` | 正则提取关键词 → 直接返回 |
| **L16** | 会议室查询 | "查明天下午会议室" / "10人会议室" | `query_meeting_room` | 正则提取时间/容纳人数 → 直接查询 |
| **L17** | 通知记录查询 | "查昨天发给张三的通知" / "今天发了多少条" | `query_notification_history` | 正则提取时间/人员/状态 → 直接查询 |

**执行示例（L7 定时提醒）**：
```
用户输入："提醒我明天下午三点交周报"

Step 1: 正则提取（无 LLM）
  提取结果：{time: "2025-09-30T15:00:00", content: "交周报"}

Step 2: 直接调用工具
  create_reminder(time="2025-09-30T15:00:00", content="交周报")
  → 写入 Celery Beat 动态任务
  → 返回：{reminder_id: "r123", status: "scheduled"}

Step 3: 直接返回
  "已创建提醒：2025-09-30 15:00「交周报」"
```

**失败降级**：
```
正则无法提取（如："帮我找一下那个戴眼镜的程序员"）
    │
    ▼
降级：LLM 提取参数（保持 Simple 单步骨架）
    └─ LLM 输出：{name: null, description: "戴眼镜", department: "技术部"}
    │
    ▼
仍无法确定 → 追问澄清："请问您要查询哪位员工？请提供姓名或工号"
```

---

## 3. Complex 通道业务执行（ReAct 动态规划）

### 3.1 即时触发定时任务（绕过定时，立即执行）

| # | 业务 | 聊天触发示例 | 骨架 | 说明 |
|---|------|------------|------|------|
| **L1** | 事件智能通知 | "现在发送今日通知" / "发送明天的会议提醒" | `query_users → query_events → generate_content → send_notification` | 从日历表读取指定日期事件 |
| **L2** | 个性化祝福 | "现在发送今日生日祝福" / "给张三发生日祝福" | `query_events(birthday) → generate_greeting → send_notification` | 识别当日/指定人员生日 |
| **L5** | IT 资源巡检 | "现在巡检一下资源池" / "检查磁盘状态" | `it_pool_inspect → analyze_results → send_alert_if_needed` | 实时分析异常 |
| **L9** | 通知策略报告 | "生成本周通知报告" | `query_history → generate_report → send_admin` | 聚合数据生成周报 |

**L1 事件智能通知执行详解**：

```
用户输入："发送明天的会议提醒"

意图识别：
  └─ intent: notify_event（complex）
  └─ extracted_slots: {date: "2025-09-30"}

骨架生成（含 Few-shot）：
  ["query_users", "query_events", "generate_content", "send_notification", "log_result"]

ReAct 执行：

Step 1: query_users
  动态提取：{date: "2025-09-30"}  # 从 extracted_slots
  执行：查询明日有事件的用户
  输出：{user_map: {张三: {id: u1}, 李四: {id: u2}}}
  
  工作记忆写入：
  context["step_1_result"] = {张三: {id: u1}, 李四: {id: u2}}

Step 2: query_events
  动态提取：{user_ids: ["u1", "u2"], date: "2025-09-30"}
  执行：SELECT * FROM calendar_events WHERE notify_time = '2025-09-30'
  输出：{张三: [{title: "产品评审", time: "14:00"}], 李四: [...]}
  
  工作记忆写入：
  context["step_2_result"] = {张三: [...], 李四: [...]}

Step 3: generate_content（LLM 生成，无工具调用）
  输入：context["step_1_result"] + context["step_2_result"]
  LLM 生成个性化通知文案：
    张三 → "张总，提醒您明日14:00产品评审..."
    李四 → "李四，您明天有季度汇报..."
  输出：{notification_contents: {张三: "...", 李四: "..."}}
  
  工作记忆写入：
  context["step_3_result"] = {张三: "文案1", 李四: "文案2"}

Step 4: send_notification
  动态提取：对每个用户，提取 {to, subject, content, channel}
  执行：调用 NotificationGateway.dispatch(...)
  输出：{张三: {success: true}, 李四: {success: true}}
  
  失败处理：
  若张三发送失败 → Level 3: 切换渠道（email → wechat）
  若仍失败 → 记录失败，继续李四（隔离失败）

Step 5: log_result
  输入：所有 step 结果汇总
  执行：写入 email_send_history 表
  同时写入情景记忆：成功/失败模式
```

### 3.2 定时任务设置与修改（通过聊天配置）

| # | 业务 | 聊天触发示例 | 工具 | 说明 |
|---|------|------------|------|------|
| **L1** | 事件通知设置 | "把事件通知改到早上8点" / "事件通知改发邮箱" | `update_cron_setting` | 修改 Celery 定时参数或渠道 |
| **L2** | 祝福设置 | "把祝福语气改轻松一点" / "以后祝福发微信" | `update_greeting_setting` | 修改语气/渠道/时间配置 |
| **L5** | 巡检频率 | "巡检改成每2小时一次" | `update_inspection_schedule` | 修改 Celery Beat 调度 |

**L2 祝福设置修改执行详解**：

```
用户输入："把祝福语气改得轻松一点"

意图识别：
  └─ intent: update_greeting_setting（simple）
  └─ extracted_slots: {setting_key: "greeting_tone", value: "casual"}

执行（Simple 通道，单工具）：
  update_greeting_setting(key="greeting_tone", value="casual")
  → 更新 agent_settings 表
  → 返回："祝福语气已改为轻松模式，下次祝福将使用新语气"

配置存储：
  INSERT INTO agent_settings (user_id, setting_key, setting_value)
  VALUES ('admin', 'greeting_tone', 'casual')
```

### 3.3 即时消息发送（聊天直接触发）

| # | 业务 | 聊天触发示例 | 骨架 |
|---|------|------------|------|
| **L21** | 定向消息发送 | "帮我给xxx组发信息：明天9点开会" | `query_group → send_notification` |
| **L22** | 批量通知发送 | "通知所有部门经理参加下周评审会" | `query_users(dept) → generate_content → send_notification` |
| **L23** | 紧急通知广播 | "紧急通知：系统升级，暂停服务1小时" | `query_all_users → send_notification(urgent)` |

**L21 定向消息发送执行详解**：

```
用户输入："帮我给技术部发信息：明天9点开会"

意图识别：
  └─ intent: send_direct_message（complex）
  └─ extracted_slots: {group: "技术部", content: "明天9点开会", urgent: false}

骨架生成：
  ["query_group", "send_notification"]

ReAct 执行：

Step 1: query_group
  动态提取：{department: "技术部"}
  执行：SELECT * FROM virtual_user WHERE department = '技术部'
  输出：{members: [{id: u1, name: "张三"}, {id: u2, name: "李四"}, ...]}

Step 2: send_notification
  动态提取：{to: [u1, u2, ...], content: "明天9点开会", channel: "wechat"}
  执行：批量发送企业微信消息
  输出：{sent: 5, failed: 0}
```

### 3.4 知识问答（RAG）

| # | 业务 | 聊天触发示例 | 骨架 |
|---|------|------------|------|
| **L4** | 企业知识问答 | "年假政策是什么" / "报销流程怎么走" | `query_memory → search_knowledge → generate_answer` |

**RAG 详细执行流程**：

```
用户输入："年假政策是什么"

意图识别：
  └─ intent: knowledge_qa（complex）
  └─ extracted_slots: {query: "年假政策"}

骨架生成：
  ["query_memory", "search_knowledge", "generate_answer"]

ReAct 执行：

Step 1: query_memory（工作记忆查询）
  输入：session_context（当前会话历史）
  执行：检索最近 3 轮相关对话
  输出：{context: "用户之前问过入职时间..."}
  
  工作记忆写入：
  context["step_1_result"] = "用户之前问过入职时间"

Step 2: search_knowledge（RAG 检索）
  动态提取：{query: "年假政策", top_k: 5}
  
  子步骤 2a: 向量检索
    BGE-M3 编码 query → ChromaDB 语义匹配 top-10
    
  子步骤 2b: BM25 检索
    关键词 "年假政策" 匹配 top-10
    
  子步骤 2c: RRF 融合排序
    融合后取 top-5
    
  子步骤 2d: 权限过滤
    按用户角色过滤敏感内容
    
  输出：
  [{text: "年假政策：入职满1年享5天...", 
    source: "员工手册.pdf §4.2", 
    score: 0.89}, ...]
  
  工作记忆写入：
  context["step_2_result"] = retrieved_chunks

Step 3: generate_answer（LLM 生成）
  输入：query + context["step_2_result"]
  
  LLM 提示词：
  "基于以下检索内容回答用户问题，并标注来源。
   用户问题：年假政策是什么
   检索内容：
   [1] 年假政策：入职满1年享5天...（来源：员工手册.pdf §4.2）
   [2] 跨年休假需提前2周申请...（来源：员工手册.pdf §4.3）"
  
  输出：
  "根据《员工手册》第4章：
   您目前入职满1年，享有 **5天年假**。
   若需跨年休假，需提前2周申请。
   📎 来源：员工手册.pdf §4.2"
```

---

## 4. 多意图组合执行（子图并行/串行）

### 4.1 触发条件

仅在**意图识别阶段检测到多意图**时触发：
```python
if route_result.is_multi_intent:
    # 触发子图并行/串行
else:
    # 单意图 ReAct
```

### 4.2 执行架构

```
父图（MultiIntentCoordinator）
    │
    ├─ 子图1（SubGraph-1）     ├─ 子图2（SubGraph-2）
    │  thread_id: parent/1     │  thread_id: parent/2
    │  ├─ Step 1               │  ├─ Step 1
    │  ├─ Step 2               │  ├─ Step 2
    │  └─ Step 3               │  └─ Step 3
    │                          │
    ▼                          ▼
  结果合并（MergeNode）
    │
    ▼
  统一写入情景记忆
```

### 4.3 场景示例

| 场景 | 聊天触发 | 拆分 | 依赖 | 执行方式 |
|------|---------|------|------|---------|
| 通知+入库 | "通知张三开会并把纪要存到知识库" | 通知 + 入库 | 串行（需 meeting_id） | 子图串行 |
| 双查询 | "查北京天气和查上海天气" | 天气×2 | 无依赖 | 子图并行 |
| 导入+通知 | "导入文档后通知项目组" | 入库 + 通知 | 串行（需文档 ID） | 子图串行 |
| 检测+创建 | "查日程冲突并创建会议" | 冲突检测 + 创建 | 串行（需确认无冲突） | 子图串行 |
| 查询+更新 | "查张三的待办并标记完成" | 查询 + 更新 | 串行（需 task_id） | 子图串行 |

**串行执行示例**：

```
用户输入："通知张三开会并把纪要存到知识库"

意图识别：
  └─ is_multi_intent: true
  └─ sub_intents: ["notify_event", "ingest_document"]
  └─ extracted_slots: {person: "张三", datetime: "2025-09-30T15:00"}

依赖检测：
  └─ 子任务2需要子任务1的 meeting_id → 串行

执行子图1（通知开会）：
  Step 1: query_users({names: ["张三"]})
    → {张三: {id: u1}}
    → 工作记忆: context["users"] = {...}
  
  Step 2: create_meeting({participant_ids: ["u1"], time: "2025-09-30T15:00"})
    → {meeting_id: "m123"}
    → 工作记忆: context["meeting_id"] = "m123"
  
  Step 3: send_notification({meeting_id: "m123", channel: "wechat"})
    → 成功

执行子图2（发送纪要模板）：
  注入 context: {meeting_id: "m123", users: {...}}
  
  Step 1: query_document_template({type: "纪要模板"})
    → {template_id: "t456"}
  
  Step 2: send_document({to: ["u1"], document_id: "t456", related_meeting: "m123"})
    → 成功

结果合并：
  └─ 两个子图都成功 → 整体成功
  └─ 统一写入情景记忆
```

### 4.4 失败隔离

```python
async def run_subgraph_with_isolation(sub_task, parent_trace_id):
    try:
        return await run_subgraph(sub_task)
    except Exception as e:
        # 记录失败，但不抛出
        log_error(sub_task, e)
        return SubGraphResult(
            status="failed",
            error=str(e),
            partial_result=None
        )

# 父图收集结果
results = await asyncio.gather(*[
    run_subgraph_with_isolation(t) for t in sub_tasks
], return_exceptions=True)

# 检查部分成功
successful = [r for r in results if r.status == "succeeded"]
failed = [r for r in results if r.status == "failed"]

if len(successful) == len(results):
    status = "success"
elif len(successful) > 0:
    status = "partial_success"  # 部分成功
else:
    status = "failed"
```

---

## 5. 工具调用失败回退机制

### 5.1 四级回退策略

```
工具调用失败
    │
    ▼
┌─────────────────────────────────────────┐
│ Level 1: 即时重试（同工具）               │
│  条件：网络超时、限流等瞬时错误            │
│  策略：指数退避，最多 3 次                │
│  示例：MCP 连接超时 → 等待 1s → 重试     │
└────────────────────┬────────────────────┘
                     ▼ 仍失败
┌─────────────────────────────────────────┐
│ Level 2: 参数修复（同工具，改参数）        │
│  条件：参数错误（格式、范围）              │
│  策略：LLM 分析错误，修正参数后重试        │
│  示例：日期格式错误 → "明天" → "2025-09-30"│
└────────────────────┬────────────────────┘
                     ▼ 仍失败
┌─────────────────────────────────────────┐
│ Level 3: 工具降级（换工具）                │
│  条件：工具不可用、无权限                  │
│  策略：切换备用工具或渠道                  │
│  示例：email 发送失败 → 改用 wechat       │
└────────────────────┬────────────────────┘
                     ▼ 仍失败
┌─────────────────────────────────────────┐
│ Level 4: 步骤跳过（Skip）                 │
│  条件：非关键步骤失败                      │
│  策略：标记跳过，继续后续步骤              │
│  示例：日志记录失败 → 跳过，不影响主流程   │
└────────────────────┬────────────────────┘
                     ▼ 关键步骤失败
              终止任务，返回失败
```

### 5.2 具体失败场景处理

| 失败类型 | 示例 | 回退策略 |
|---------|------|---------|
| **网络超时** | MCP 连接超时 | Level 1：重试 3 次 |
| **参数错误** | 日期格式错误 | Level 2：LLM 修复参数 |
| **资源不存在** | 文件已删除 | 终止，返回明确错误 |
| **权限不足** | 无发送权限 | Level 3：切换渠道或降级为仅查询 |
| **工具不可用** | Embedding 服务宕机 | Level 3：使用备用 Embedding 模型 |
| **业务校验失败** | 用户不存在 | 终止，返回"用户不存在" |

### 5.3 实现代码框架

```python
async def execute_step_with_retry(step, context, max_retries=3):
    for attempt in range(max_retries):
        try:
            # 动态提取参数
            args = extract_params(step, context)
            
            # 执行工具
            result = await tool.run(args)
            
            # 成功：更新 context，返回
            context[f"step_{step.id}_result"] = result
            return result
            
        except TransientError as e:
            # 瞬时错误：指数退避重试
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            else:
                raise
                
        except ParameterError as e:
            # 参数错误：LLM 修复后重试
            if attempt == 0:
                fixed_args = await llm_fix_parameters(args, str(e))
                context[f"step_{step.id}_fixed_args"] = fixed_args
                continue
            else:
                raise
                
        except ToolUnavailableError as e:
            # 工具不可用：切换备用工具
            backup_tool = get_backup_tool(step.tool_name)
            if backup_tool:
                step.tool_name = backup_tool
                continue
            else:
                raise
                
        except NonCriticalError as e:
            # 非关键错误：跳过
            if step.is_critical:
                raise
            else:
                log_warning(f"步骤 {step.id} 跳过: {e}")
                return {"status": "skipped", "reason": str(e)}
    
    raise StepFailedError(f"步骤 {step.id} 重试 {max_retries} 次后仍失败")
```

---

## 6. 记忆集成（工作记忆 + 情景记忆）

**设计原则**：仅使用工作记忆（任务内上下文）和情景记忆（任务间经验），**不引入用户画像和语义记忆**。

### 6.1 工作记忆（Working Memory）

**存储**：LangGraph Checkpointer（PostgreSQL）

**用途**：任务执行过程中的上下文传递

```python
class WorkingMemory:
    def get(self, key, default=None):
        """从当前任务上下文获取"""
        return self.context.get(key, default)
    
    def set(self, key, value):
        """写入当前任务上下文"""
        self.context[key] = value
    
    def get_step_result(self, step_id):
        """获取特定步骤的结果"""
        return self.context.get(f"step_{step_id}_result")
    
    def summarize(self):
        """任务结束时，将长上下文压缩为摘要"""
        summary = llm.summarize_context(self.context)
        return summary  # 存入情景记忆
```

**使用示例**：
```python
# Step 1: 查询用户
users = query_users(names=["张三"])
working_memory.set("users", users)

# Step 2: 使用上一步结果
events = query_events(user_ids=[u.id for u in working_memory.get("users")])

# Step 3: 生成内容
content = generate_content(
    users=working_memory.get("users"),
    events=working_memory.get_step_result(2)
)
```

### 6.2 情景记忆（Episodic Memory）

**存储**：ChromaDB `semantic_memory` 集合

**用途**：记录成功/失败的执行模式，供未来任务参考

**写入时机**：
- 任务成功完成
- 任务失败但有参考价值（如"某工具连续超时"）

**存储结构**：
```python
{
    "id": "episode_abc123",
    "content": "任务：通知张三李四开会\n结果：成功\n关键步骤：query_users→query_events→send_notification\n耗时：3.2s\n工具调用：4次",
    "metadata": {
        "task_type": "notify_event",
        "success": true,
        "duration_ms": 3200,
        "tool_call_count": 4,
        "timestamp": "2025-09-28T07:05:00Z",
        "trace_id": "task-abc123"
    },
    "embedding": [0.23, -0.15, ...]  # BGE-M3 向量
}
```

**检索时机**：
```python
# 新任务开始前，检索相似历史任务
similar_episodes = chroma_collection.query(
    query_embeddings=[bge_m3.encode(current_task)],
    n_results=3,
    where={"task_type": "notify_event", "success": True}
)

# 提取经验
if similar_episodes:
    avg_duration = statistics.mean([e.metadata["duration_ms"] for e in similar_episodes])
    common_tools = extract_common_tools(similar_episodes)
    # 用于优化当前任务（如：调整超时时间、预加载工具）
```

### 6.3 记忆写入触发点

| 时机 | 写入内容 | 存储位置 |
|------|---------|---------|
| **Step 完成** | `step_N_result` | 工作记忆 |
| **任务成功** | 任务摘要 + 执行轨迹 | 情景记忆 |
| **任务失败** | 失败原因 + 尝试过的修复 | 情景记忆 |
| **工具超时** | 工具名 + 超时时间 | 情景记忆（用于降级决策） |

### 6.4 记忆使用策略

**规划阶段**：
```python
# 检索相似任务，优化骨架生成
similar = episodic_memory.search("通知多人开会", top_k=3)
if similar:
    # 发现历史任务都包含 query_memory_facts 步骤
    # 即使当前意图识别没提取到，也建议加入
    suggested_steps = extract_common_steps(similar)
```

**执行阶段**：
```python
# 检索失败经验，提前规避
failure_patterns = episodic_memory.search(
    "send_email 超时",
    where={"success": False}
)
if failure_patterns:
    # 历史显示 email 工具在高峰期超时率高
    # 提前切换到 wechat 或增加重试次数
    adjust_retry_strategy("send_email", max_retries=5)
```

---

## 7. 端到端示例

### 7.1 Simple 通道示例（L7 定时提醒）

```
用户输入："提醒我明天下午三点交周报"

执行流程：
1. 意图识别：simple_reminder（simple）
2. 正则提取：{time: "2025-09-30T15:00:00", content: "交周报"}
3. 直接调用：create_reminder(...)
4. 返回："已创建提醒：2025-09-30 15:00「交周报」"

总耗时：~50ms（零 LLM 调用）
```

### 7.2 Complex 通道示例（L1 事件通知）

```
用户输入："发送明天的会议提醒"

执行流程：
1. 意图识别：notify_event（complex）
2. 骨架生成：["query_users", "query_events", "generate_content", "send_notification", "log_result"]
3. ReAct 执行（5 步，见 §3.1）
4. 返回："已向 5 人发送明日会议提醒"

总耗时：~3.4s（2 次 LLM 调用：generate_content + log_result）
```

### 7.3 多意图示例（通知+入库）

```
用户输入："通知张三开会并把纪要存到知识库"

执行流程：
1. 意图识别：is_multi_intent=true，sub_intents=["notify", "ingest"]
2. 依赖检测：串行（需 meeting_id）
3. 执行子图1（通知）：query_users → create_meeting → send_notification
4. 执行子图2（入库）：query_template → send_document（注入 meeting_id）
5. 结果合并：整体成功

总耗时：~6.8s（两个子图串行）
```

### 7.4 RAG 问答示例（L4）

```
用户输入："年假政策是什么"

执行流程：
1. 意图识别：knowledge_qa（complex）
2. 骨架生成：["query_memory", "search_knowledge", "generate_answer"]
3. ReAct 执行：
   - query_memory：检索会话历史
   - search_knowledge：向量+BM25 混合检索 → top-5 chunks
   - generate_answer：LLM 生成带引用答案
4. 返回："根据《员工手册》第4章：入职满1年享5天年假...📎 来源：员工手册.pdf §4.2"

总耗时：~2.1s（1 次 LLM 调用）
```
