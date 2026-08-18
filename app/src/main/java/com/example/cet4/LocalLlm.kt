package com.example.cet4

import com.google.ai.edge.litertlm.Backend
import com.google.ai.edge.litertlm.Content
import com.google.ai.edge.litertlm.Contents
import com.google.ai.edge.litertlm.Conversation
import com.google.ai.edge.litertlm.ConversationConfig
import com.google.ai.edge.litertlm.Engine
import com.google.ai.edge.litertlm.EngineConfig
import com.google.ai.edge.litertlm.ExperimentalApi
import com.google.ai.edge.litertlm.ExperimentalFlags
import com.google.ai.edge.litertlm.Message
import com.google.ai.edge.litertlm.MessageCallback
import com.google.ai.edge.litertlm.NoRepeatNgramConfig
import com.google.ai.edge.litertlm.RepetitionPenaltyConfig
import com.google.ai.edge.litertlm.SamplerConfig
import com.google.ai.edge.litertlm.ThinkingConfig

/**
 * 本地模型推理封装（LiteRT-LM 0.15.0，.litertlm 格式）。
 * 单实例 + 全防御：
 * - 加载幂等：同路径已加载直接 OK；加载中重复调用返回"加载中"（杜绝多 Engine 并发初始化 → 内存爆炸/黑屏）
 * - 推理串行：一次只能跑一个请求，忙时拒绝（防止并发推理叠加内存）
 * - 所有状态变更 synchronized 保护
 *
 * 0.15.0 API 关键点（javap 反编译确认）：
 * - 思考开关的正式参数是 ConversationConfig / sendMessage 的 ThinkingConfig(enableThinking, thinkingTokenBudget)，
 *   extraContext["enable_thinking"] 只是模板变量，native 推理行为由 ThinkingConfig 控制 → 两者都传。
 * - maxOutputToken 必须显式传值；传 null 会被 JNI 转成 -1（无限制），覆盖 ConversationConfig 的 2048。
 * - MessageCallback.onMessage 是增量流式回调（每次新 chunk），不是全量累积。
 */
object LocalLlm {
    private val lock = Any()
    private var engine: Engine? = null
    private var convo: Conversation? = null
    private var systemPrompt: String = ""
    @Volatile private var loading = false
    @Volatile private var generating = false
    @Volatile private var cancelled = false
    @Volatile var backendInfo: String = "GPU"
        private set

    /* ===== 全链路诊断日志（native 侧）=====
       记录加载/推理参数、回调内容、异常；前端一键复制贴给开发者。 */
    private val logBuf = StringBuilder()
    @Volatile private var logEnabled = true

    private fun llmLog(tag: String, msg: String) {
        if (!logEnabled) return
        synchronized(logBuf) {
            logBuf.append("[").append(java.text.SimpleDateFormat("HH:mm:ss.SSS", java.util.Locale.US).format(java.util.Date()))
                .append("] ").append(tag).append(": ").append(msg).append("\n")
            if (logBuf.length > 20000) logBuf.delete(0, 6000)
        }
    }

    fun logDump(): String = synchronized(logBuf) { logBuf.toString() }
    fun clearLog() = synchronized(logBuf) { logBuf.setLength(0) }

    /* v9.78：isLoaded 只反映【模型权重是否在内存】（engine 是否存活）。
       修复保活 bug：旧判定 engine!=null && convo!=null 把"权重"和"会话"绑死——
       前端新建对话/清空上下文会调 resetContext()（convo?.close(); convo=null），
       导致 isLoaded 瞬间变 false → 设置页显示"未加载" → 用户手动加载时 load()
       幂等判定（isLoaded && loadedPath==path）失败 → engine.close() 把还热乎的
       3.6GB 权重释放再重新 initialize（10~60s）→ 表现为"模型被自动卸载、每次要手动加载"。
       convo 只是会话（KV 历史缓存），重建毫秒级、不涉及权重重载；模型是否加载只看 engine。 */
    val isLoaded: Boolean get() = synchronized(lock) { engine != null }
    var loadedPath: String? = null
        private set

    /* 防死循环关键配置（官方默认开启）：重复惩罚 + n-gram 禁重复 */
    private val repCfg = RepetitionPenaltyConfig(1.15f, 0f, 0f, 512)
    private val ngramCfg = NoRepeatNgramConfig(3, 128)
    /* v9.74：前端拼接上限 32768 → 65536 字符（用户写论文/长文场景需要更长单次输出；
       实际输出仍受 maxOutputToken 与模型窗口 clamp 限制，此值仅防前端 StringBuilder 溢出） */
    private val maxStreamLen = 65536
    /* 官方配置（litert_readme + Config.kt 源码实锤）：
       - SamplerConfig.seed 官方默认 0！之前传 seed=-1 → 固定种子 → 每次输出完全一样（"无随机性"）
         + 采样行为异常。v7.0 时代没传 seed（默认 0）不乱码，v8.2 加 seed=-1 后开始乱码。删除 seed。
       - ThinkingConfig.thinkingTokenBudget 官方默认 -1（无限预算）= Edge 深度思考充足预算；不限制。
       - context 2048 tokens 基准（支持 32k），maxOutputToken 用 4096：思考+正文共享预算，
         2048 会被思考吃光（官方实测：模型在思考通道耗尽解码预算 → 正文出不来 → "已停止"），4096 留足空间
       v9.65：默认预算 4096 → 8192。用户反馈：数学题深度思考，思维链很长（吃了几千 token），
         正文生成一半截断在输出 4097（= 预算 4096 被思考+正文共享撑满），并非上下文窗口限制。
         8192 让"长思维链 + 完整正文"都放得下；用户仍可在设置中调大到 32K。 */
    private val defaultMaxTokens = 8192
    /* v9.16 可调参数（load 时写入）：上下文窗口 = maxOutputToken（用户滑条 1K~32K）、思考预算 */
    @Volatile private var ctxLen: Int = 4096
    @Volatile private var thinkBudget: Int = 1024
    /* v9.74：单次输出上限与上下文窗口解耦——
       用户反馈：模型最大支持 32K 上下文窗口，不应改动窗口；但单次回复的完整输出上限
       （默认 8192=ctxLen）写论文/长文时被截断。新增独立字段 maxOutputTokens（默认 16384），
       sendMessageAsync/complete 用它而非 ctxLen；窗口语义保留给 ctxLen（输入超限拦截用）。 */
    @Volatile private var maxOutputTokens: Int = 16384
    @Volatile private var speculativeEnabled: Boolean = true   /* v9.41：推测性解码开关（默认开，需模型自带 MTP drafter 才生效） */
    @Volatile var drafterDetected: Boolean = false             /* v9.42：模型是否自带 MTP drafter（load 时解析 .litertlm 包检测） */
    /* v9.48：跨轮上下文保留开关（默认 true，修复 #1 模型无真实上下文 bug）。
       true = 复用同一 Conversation，LiteRT-LM 内部累积历史，模型真正"记住"前几轮（豆包式多轮记忆）。
       false = 每次推理前重建会话（v9.47 及更早的旧行为，可由用户在设置中切换回）。
       v9.76 注：SDK 0.15.0 无公开"传历史 messages"API（反编译确认 ConversationConfig 无此参数，
       Message 构造 internal）——因此重建会话会丢前文。多轮记忆靠"复用会话"实现，
       随机采样靠"同 prompt 重发检测"（lastUserPrompt）实现。 */
    @Volatile var contextKeep: Boolean = true
        private set
    /* v9.76：上一条 user prompt——"同 prompt 重发检测"：
       用户重发同一条消息时，SDK 复用会话（seed 固定）→ 回复必然一样（用户实证"随机采样没修好"）。
       检测到重发 → 强制重建会话（新 seed 随机）。同 prompt 重发 = 用户想重新生成当前回复，
       丢前文可接受（与豆包"重新生成当前轮"一致）。 */
    @Volatile private var lastUserPrompt: String? = null
    /* v9.48：上一次 baked 进 Conversation 的关键参数快照，用于检测"是否需要重建" */
    private var bakedSys: String? = null
    private var bakedThink: Boolean? = null
    private var bakedCtxLen: Int? = null
    private var bakedThinkBudget: Int? = null
    private var bakedSpec: Boolean? = null
    /* 流式兜底双超时（死循环时 chunk 不断，空闲 watchdog 永不触发 → 必须加总时长上限）：
       - 空闲超时：超过此毫秒无新 chunk，强制结束
       - 总时长超时：从开始到强制结束的总时长上限，无论 chunk 多活跃（治"永不停止"）
       v9.68：总时长 150s→300s、空闲 60s→90s。用户反馈：数学题深度思考（长思维链+长正文）
       在 GPU 上生成经常 >150s，被旧 watchdog 强杀 → "生成一半直接截断"（无原因提示，误判为截断）。
       300s 对 4B 模型深度思考长回答足够；真正死循环仍会被空闲/总时长兜底。 */
    private const val STREAM_IDLE_WATCHDOG_MS = 90_000L
    private const val STREAM_TOTAL_WATCHDOG_MS = 300_000L

    /** 加载 .litertlm 模型。幂等 + 防重入。返回 "OK" / "加载中，请稍候" / 错误信息。 */
    @OptIn(ExperimentalApi::class)
    @JvmOverloads
    fun load(
        path: String,
        maxTokens: Int = defaultMaxTokens,
        temperature: Double = 0.95,
        systemPrompt: String? = null,
        think: Boolean = false,
        thinkBudget: Int = 1024          /* v9.16：思考预算（滑条 0~4096） */
    ): String {
        synchronized(lock) {
            if (isLoaded && loadedPath == path) { llmLog("load", "幂等命中 path=$path"); return "OK" }
            if (loading) { llmLog("load", "防重入（loading=true）"); return "加载中，请稍候" }
            loading = true
        }
        llmLog("load", "开始加载 path=$path maxTokens=$maxTokens temperature=$temperature think=$think thinkBudget=$thinkBudget sys=${systemPrompt?.take(60)}")
        return try {
            val p = systemPrompt ?: defaultSystem(think)
            /* v9.42：解析 .litertlm 包检测是否带 MTP drafter（zip 容器，条目名含 draft/mtp/spec） */
            drafterDetected = detectDrafter(path)
            llmLog("load", "MTP drafter=${if (drafterDetected) "检测到" else "未检测到"} path=${path.takeLast(40)}")
            /* v9.41：推测性解码（MTP drafter）——在创建 Engine 前设置 ExperimentalFlags 单例。
               模型文件需自带 drafter 才生效；不带 drafter 时 SDK 自动忽略（无副作用） */
            try {
                ExperimentalFlags.enableSpeculativeDecoding = speculativeEnabled
                llmLog("load", "推测性解码=${if (speculativeEnabled) "开" else "关"}")
            } catch (e: Throwable) { llmLog("load", "ExperimentalFlags 设置失败: ${e.message}") }
            val cfg = EngineConfig(modelPath = path, backend = Backend.GPU())
            val eng = Engine(cfg)
            eng.initialize()
            llmLog("load", "Engine.initialize() OK")
            synchronized(lock) {
                /* 二次确认：加载期间若已有人卸载/重新加载，先关旧的 */
                try { engine?.close() } catch (_: Throwable) {}
                backendInfo = "GPU"
                this.systemPrompt = p
                thinkingEnabled = think
                this.ctxLen = maxTokens.coerceIn(1024, 32768)      /* 上下文窗口 1K~32K */
                this.thinkBudget = thinkBudget.coerceIn(0, 8192)   /* 思考预算 0~8K（v9.69：上限 4096→8192，长思维链不卡思考） */
                /* v9.74：输出上限独立于窗口——默认 16384（写论文/长文不被 8192 截断），
                   与 ctxLen 解耦；模型物理窗口 32K 由 SDK clamp，此处不超窗口上限 */
                this.maxOutputTokens = 16384
                /* v9.74：初始会话也用独立输出上限（maxOutputTokens）而非 ctxLen */
                convo = eng.createConversation(conversationConfig(p, maxOutputTokens, temperature, think, thinkBudget))
                engine = eng
                loadedPath = path
                /* v9.48：load 立刻 baked 第一份快照——避免首次发送时 ensureConversation 误判触发冗余重建 */
                bakedSys = p
                bakedThink = think
                bakedCtxLen = this.ctxLen
                bakedThinkBudget = this.thinkBudget
                bakedSpec = speculativeEnabled
            }
            llmLog("load", "createConversation OK backend=GPU think=$think maxOutputToken=$maxTokens temp=$temperature")
            "OK"
        } catch (e: Throwable) {
            llmLog("load", "失败: ${e.message ?: e.javaClass.simpleName}")
            synchronized(lock) {
                try { engine?.close() } catch (_: Throwable) {}
                engine = null; convo = null; loadedPath = null
                /* v9.48：加载失败时同时清 baked，避免下次 ensureConversation 误触重建路径 */
                bakedSys = null; bakedThink = null
                bakedCtxLen = null; bakedThinkBudget = null; bakedSpec = null
                backendInfo = "CPU(回退)"
            }
            "加载失败: ${e.message ?: e.javaClass.simpleName}"
        } finally {
            synchronized(lock) { loading = false }
        }
    }

    /** 热切换系统提示词/思考开关（无需重新加载权重）。
     *  v9.18：改为【惰性建会话】——只更新 systemPrompt/thinkingEnabled 字段，不再立刻
     *  createConversation。原因：aiSend 每次发送前会调 llmSetSystem + llmCompleteStream
     *  两个异步线程，原实现两次 createConversation 在 synchronized(lock) 内串行执行
     *  （每次约 5~6 秒，用户实测发送到首 token 延迟 11~12 秒）。
     *  现在 completeStream 的 freshConversation() 本就每次按最新字段重建会话，setSystem
     *  只负责更新字段，会话创建交由发送路径统一执行一次。 */
    fun setSystem(systemPrompt: String, think: Boolean): String {
        synchronized(lock) { engine ?: return "模型未加载" }
        return try {
            val prompt = systemPrompt.ifBlank { defaultSystem(think) }
            synchronized(lock) {
                this.systemPrompt = prompt
                thinkingEnabled = think
                /* v9.18：不再 convo = eng.createConversation(...)——由下次 freshConversation 按新字段创建 */
            }
            "OK"
        } catch (e: Throwable) {
            "切换失败: ${e.message ?: e.javaClass.simpleName}"
        }
    }

    /* v9.41：推测性解码（MTP drafter）开关——只记录状态，Engine 创建时生效（见 load()）。
       前端在设置页切换/加载模型前调用。 */
    fun setSpeculative(enable: Boolean): String {
        speculativeEnabled = enable
        llmLog("spec", "推测性解码=${if (enable) "开" else "关"}（下次加载模型生效）")
        return "OK"
    }

    /* v9.43：检测 .litertlm 是否带 MTP drafter。
       注意：.litertlm 是自定义 FlatBuffer 二进制（非 zip！），drafter 内嵌在新版权重包内
       （litert-community 版），不是单独文件搭配。
       实现：读取文件头部 2MB（含 FlatBuffer header 与 section metadata），
       二进制搜索 draft/spec/mtp 关键词；命中即判带 drafter。启发式，最终以实测速度为准。 */
    private fun detectDrafter(path: String): Boolean {
        return try {
            val f = java.io.File(path)
            if (!f.exists()) return false
            val buf = ByteArray(2 * 1024 * 1024)
            val len = f.inputStream().use { it.read(buf) }
            if (len <= 0) return false
            val head = String(buf, 0, len, Charsets.ISO_8859_1).lowercase()
            head.contains("draft") || head.contains("speculat") || head.contains("mtp")
        } catch (e: Throwable) {
            false
        }
    }

    /* Gemma 4 原生思考开关（官方 HF 讨论 #18 + 0.15.0 SDK 反编译实锤）：
       - 官方开启方式：ConversationConfig(extraContext = mapOf("enable_thinking" to true))
       - 0.15.0 新增正式参数 ThinkingConfig(enableThinking)——native 层思考开关（Message.channels 由此激活）
       - 双保险：extraContext 控制模板注入 <|think|>（chat_template 变量），ThinkingConfig 控制 native 通道
       - channels 默认 null = 用 LlmMetadata 内置 thinking channel 配置（SDK 自动写 Message.channels） */
    private fun conversationConfig(
        sys: String,
        maxTokens: Int = defaultMaxTokens,
        temperature: Double = 0.95,
        think: Boolean = false,
        thinkBudget: Int = 1024
    ) = ConversationConfig(
        systemInstruction = Contents.of(sys),
        maxOutputToken = maxTokens,
        samplerConfig = SamplerConfig(
            topK = 40,
            topP = 0.95,
            /* v9.77：温度 0.7 → 0.95——反编译证实 native 无 seed JNI（seed 参数被丢弃），
               随机性只能靠 temperature 采样。0.95 提高多样性；topK/topP 保持克制避免乱码。
               注：数学/客观题输出天然固定（正确答案唯一），随机性主要体现于开放性问题。 */
            temperature = 0.95,
            seed = java.util.Random().nextInt()
        ),
        /* ===== 关键修复：prefillPrefaceOnInit = true =====
           官方 Config.kt 默认 false → chat_template 的 preface（<|turn>system\n 等结构）
           不会在初始化时预填充 → 模型收到裸文本续写而非对话结构 → 输出以"."开头/答非所问
           （用户日志实证：问"DeepSeek是谁开发的"模型答"Deep dive 的概念..." = 续写联想）。
           Edge Gallery 必然开启此参数。 */
        prefillPrefaceOnInit = true,
        extraContext = mapOf("enable_thinking" to think),
        /* ===== 双保险：ThinkingConfig 也传 =====
           extraContext["enable_thinking"] 只控制模板注入 <|think|>（chat_template 变量）；
           ThinkingConfig(enableThinking) 是 0.15.0 native 层正式思考开关（Message.channels 由此激活）。
           v9.7 曾只留 extraContext → E2B/E4B 均无 channels → 思考内容丢失。恢复双传。
           v9.16：thinkingTokenBudget 由用户滑条控制（0~4096，默认 1024）。 */
        thinkingConfig = ThinkingConfig(enableThinking = think, thinkingTokenBudget = thinkBudget)
    )

    /* 中性默认提示词：只稳定输出语言，不限制能力/长度（4B 模型语言漂移的强指令约束；
       不约束"短句/简洁"——Edge 深度思考输出完整，长度限制交给 maxOutputToken）。 */
    private fun defaultSystem(think: Boolean): String =
        "你是双语 AI 助手，可以用中文与用户自由聊天、解答任何问题，不限定为查词工具。只有当用户【明确要求查英语单词释义】，或【只输入一个孤立的英语单词】（无其他语句）时，才以词典格式回答：先给出单词与音标，再列词性与核心释义，最后给 1-2 个例句或常见搭配。其他任何情况都正常对话，不要套用词典格式。始终用简体中文回复，除非用户要求其他语言。"

    /** 每次推理前重建会话（清空对话历史 → 每次查询独立上下文）。
     *  关键修复（v9.48 后保留为 "force rebuild" 原语）：保留方法签名与可见性，供 resetContext()
     *  与 baked 参数失效时调用——open/closed 原则：不删除原有方法，仅在其旁新增 ensureConversation()
     *  决定 "是否" 重建，由 contextKeep 控制。
     *  v9.76：重建时传入历史消息（contextKeep=true）——保证每次生成新 seed（随机）且保留记忆。 */
    private fun freshConversation(think: Boolean = thinkingEnabled): Conversation? {
        return synchronized(lock) {
            val eng = engine ?: return null
            try { convo?.close() } catch (_: Throwable) {}
            /* v9.70 关键修复：必须传 maxTokens=ctxLen！...（见下）
               v9.74：改传 maxOutputTokens（独立输出上限默认 16384）——Conversation 级
               maxOutputToken 是权威总预算，必须与 sendMessage 的独立输出上限一致，
               否则会话仍被旧 ctxLen 值（默认 8192）锁死。
               （v9.76 尝试传 messages 历史被 SDK 拒绝：ConversationConfig 无公开 messages 参数） */
            val cfg = conversationConfig(
                systemPrompt,
                maxTokens = maxOutputTokens,
                think = think
            )
            val c = eng.createConversation(cfg)
            convo = c
            /* v9.48：把当前 baked 参数打快照，下次 ensureConversation 据此判断是否需要重建 */
            bakedSys = systemPrompt
            bakedThink = think
            bakedCtxLen = ctxLen
            bakedThinkBudget = thinkBudget
            bakedSpec = speculativeEnabled
            c
        }
    }

    /** v9.76：按需重建会话——恢复 v9.74 逻辑（用户明确要求：不要每次重建，保持豆包式上下文记忆）。
     *  - contextKeep == true  → 复用现有 convo（KV 缓存历史 → 模型记住前几轮）；仅参数变更/显式重置时重建
     *  - contextKeep == false → 每次重建（v9.47 旧行为）
     *  随机采样由【同 prompt 重发检测】驱动（completeStream 里判断），不是每次重建。 */
    private fun ensureConversation(think: Boolean): Conversation? {
        return synchronized(lock) {
            val eng = engine ?: return null
            val needRebuild = convo == null ||
                !contextKeep ||
                bakedSys != systemPrompt ||
                bakedThink != think ||
                bakedCtxLen != ctxLen ||
                bakedThinkBudget != thinkBudget ||
                bakedSpec != speculativeEnabled
            if (needRebuild) {
                if (contextKeep && convo != null && bakedSys != null) {
                    val reason = when {
                        convo == null -> "no-convo"
                        bakedSys != systemPrompt -> "system-prompt-changed"
                        bakedThink != think -> "thinking-flag-changed"
                        bakedCtxLen != ctxLen -> "ctxLen-changed"
                        bakedThinkBudget != thinkBudget -> "thinkBudget-changed"
                        bakedSpec != speculativeEnabled -> "spec-changed"
                        else -> "unknown"
                    }
                    llmLog("ctxKeep", "配置变更[$reason]，重建会话保留 KV 历史")
                }
                return freshConversation(think)
            }
            convo
        }
    }

    /** v9.48：用户显式清空上下文（前端「清空本地上下文」按钮）。
     *  关掉当前 convo，下次 ensureConversation 重建。KV 缓存丢弃 → 真正"从头开始"。
     *  线程安全：在 lock 内执行；不影响其他模块。 */
    fun resetContext(): String {
        synchronized(lock) {
            if (engine == null) return "模型未加载"
            try { convo?.close() } catch (_: Throwable) {}
            convo = null
            bakedSys = null; bakedThink = null
            bakedCtxLen = null; bakedThinkBudget = null; bakedSpec = null
            /* v9.76：新窗口语义——清空 lastUserPrompt（下次从零上下文开始） */
            lastUserPrompt = null
            llmLog("ctxReset", "用户清空了本地上下文（下次推理重建 Conversation）")
        }
        return "OK"
    }

    /** v9.48：切换上下文保留模式（前端"上下文保留"开关）。
     *  setContextKeep(false) 主动切回旧行为；setContextKeep(true) 恢复新行为。 */
    fun setContextKeep(keep: Boolean): String {
        synchronized(lock) {
            val old = contextKeep
            contextKeep = keep
            llmLog("ctxMode", "上下文保留模式: ${if (keep) "保留（跨轮记忆）" else "每次清空（v9.47 旧行为）"}")
            /* 模式切换时主动清一次，避免旧缓存污染新模式语义 */
            if (old != keep && engine != null) {
                try { convo?.close() } catch (_: Throwable) {}
                convo = null
                bakedSys = null; bakedThink = null
                bakedCtxLen = null; bakedThinkBudget = null; bakedSpec = null
            }
        }
        return "OK"
    }

    /** v9.67：热更新上下文窗口/输出预算（模型已加载时改滑条立即生效）。
     *  根因：llmLoad 幂等命中（isLoaded && loadedPath==path）直接 return，不更新 ctxLen——
     *  用户把滑条调到 32K 触发重载也被幂等拦截，实际 maxOutputToken 仍是加载时旧值 → 照常截断。
     *  前端滑条变更 → App.llmSetCtxLen(n)：更新字段 + 重建会话（新 maxOutputToken 随会话生效）。 */
    fun setCtxLen(v: Int): String {
        synchronized(lock) {
            val nv = v.coerceIn(1024, 32768)
            if (engine == null) { ctxLen = nv; llmLog("ctxHot", "未加载，仅记录 ctxLen=$nv"); return "OK" }
            if (ctxLen == nv) return "OK"
            ctxLen = nv
            /* 会话重建：Conversation 创建时 maxOutputToken=ctxLen 已 bake，必须重建才能应用新值 */
            try { convo?.close() } catch (_: Throwable) {}
            convo = null
            bakedCtxLen = null
            llmLog("ctxHot", "热更新 ctxLen=$nv（会话已重置，下次推理应用新输出上限）")
        }
        return "OK"
    }

    /** v9.67：热更新思考预算（与 setCtxLen 同机制，模型已加载时改滑条立即生效） */
    fun setThinkBudget(v: Int): String {
        synchronized(lock) {
            val nv = v.coerceIn(0, 8192)
            if (engine == null) { thinkBudget = nv; llmLog("budHot", "未加载，仅记录 thinkBudget=$nv"); return "OK" }
            if (thinkBudget == nv) return "OK"
            thinkBudget = nv
            try { convo?.close() } catch (_: Throwable) {}
            convo = null
            bakedThinkBudget = null
            llmLog("budHot", "热更新 thinkBudget=$nv（会话已重置）")
        }
        return "OK"
    }

    /** v9.74：热更新单次输出上限（与 ctxLen 解耦，独立控制）。
     *  默认 16384（写论文/长文不被默认 8192 截断）；可调到 32768（模型物理窗口上限）。 */
    fun setMaxOutput(v: Int): String {
        synchronized(lock) {
            val nv = v.coerceIn(2048, 32768)
            if (engine == null) { maxOutputTokens = nv; llmLog("outHot", "未加载，仅记录 maxOutputTokens=$nv"); return "OK" }
            if (maxOutputTokens == nv) return "OK"
            maxOutputTokens = nv
            try { convo?.close() } catch (_: Throwable) {}
            convo = null
            llmLog("outHot", "热更新 maxOutputTokens=$nv（会话已重置）")
        }
        return "OK"
    }

    /** 单次完整生成（阻塞）。返回完整文本或错误信息。 */
    @OptIn(com.google.ai.edge.litertlm.ExperimentalApi::class)
    fun complete(prompt: String): String {
        if (!tryBeginGenerate()) return "模型正忙，请等上一条回复完成后再试"
        /* v9.76：同 prompt 重发 → 重建会话换新 seed（随机采样）；普通对话按需复用（记忆保留） */
        var cnv: Conversation?
        val isReprompt = lastUserPrompt != null && lastUserPrompt == prompt
        cnv = if (isReprompt) freshConversation(thinkingEnabled) else ensureConversation(thinkingEnabled)
        if (cnv == null) { synchronized(lock) { generating = false }; return "模型未加载" }
        lastUserPrompt = prompt
        return try {
            llmLog("complete", "prompt=$prompt think=$thinkingEnabled maxTokens=$defaultMaxTokens temp=0.95 topK=40 topP=0.95 maxOut=$maxOutputTokens reprompt=$isReprompt")
            /* ===== 终极诊断：模板渲染后的完整输入 ===== */
            runCatching {
                val pre = cnv.renderPrefaceIntoString()
                llmLog("renderPreface", if (pre.isNullOrEmpty()) "(空)" else pre.take(400))
                val msgR = cnv.renderMessageIntoString(Message.user(prompt), mapOf("enable_thinking" to thinkingEnabled))
                llmLog("renderMessage", if (msgR.isNullOrEmpty()) "(空)" else msgR.take(400))
            }.onFailure { llmLog("render", "异常: ${it.message}") }
            /* 官方示例同款：String 简版 sendMessage（模板自动应用，参数命名传入；thinkingConfig 双保险） */
            val msg = cnv.sendMessage(
                prompt,
                extraContext = mapOf("enable_thinking" to thinkingEnabled),
                repetitionPenaltyConfig = repCfg,
                noRepeatNgramConfig = ngramCfg,
                maxOutputToken = maxOutputTokens,   /* v9.74：独立输出上限（默认 16384），不再等于 ctxLen（窗口语义保留） */
                thinkingConfig = ThinkingConfig(enableThinking = thinkingEnabled, thinkingTokenBudget = thinkBudget)
            )
            val t = messageText(msg)
            llmLog("complete", "原始全文=${t.take(300)}")
            /* v9.68：完成日志——记录实际输出长度与上限，辅助判断是否被 maxOutputToken 截断 */
            llmLog("finish", "[complete-normal] chars=${t.length} ctxLen=$ctxLen thinkBudget=$thinkBudget maxOut=$maxOutputTokens")
            t
        } catch (e: Throwable) {
            llmLog("complete", "异常: ${e.message ?: e.javaClass.simpleName}")
            "推理失败: ${e.message ?: e.javaClass.simpleName}"
        } finally {
            synchronized(lock) { generating = false }
        }
    }

    /** 流式生成（真流式）：sendMessageAsync 增量回调，每个 token 到达立即推送。
     *  思考通道（Message.channels）→ ⟦T⟧ 前缀单独推送，正文 → 原文。
     *  保留全部修复：官方参数（temp 0.7/topK 40/topP 0.95/maxOutputToken 2048/prefillPrefaceOnInit/
     *  extraContext enable_thinking + ThinkingConfig 双保险/freshConversation）+ seed 默认 0 + watchdog + 循环检测。 */
    @OptIn(com.google.ai.edge.litertlm.ExperimentalApi::class)
    fun completeStream(
        prompt: String,
        think: Boolean = thinkingEnabled,   /* v9.16：think 随请求直传，消除 llmSetSystem 竞态（前端并发 llmSetSystem + llmCompleteStream 两个线程时可能读到旧值） */
        onChunk: (String) -> Unit,
        onDone: (String) -> Unit
    ) {
        if (!tryBeginGenerate()) { onDone("模型正忙，请等上一条回复完成后再试"); return }
        /* v9.76：会话获取——
           - 普通新对话：ensureConversation 按需复用（保留上下文记忆，豆包式）
           - 同 prompt 重发：强制重建会话（新 seed → 随机采样）
           seed 只在会话创建时生效（SDK 0.15.0 无 per-call seed 接口，已反编译确认） */
        var cnv: Conversation?
        val isReprompt = lastUserPrompt != null && lastUserPrompt == prompt
        if (isReprompt) {
            llmLog("stream", "检测到同 prompt 重发，重建会话换新 seed（随机采样）")
            cnv = freshConversation(think)
        } else {
            cnv = ensureConversation(think)
        }
        if (cnv == null) { synchronized(lock) { generating = false }; onDone("模型未加载"); return }
        cancelled = false
        lastUserPrompt = prompt
        llmLog("stream", "开始 prompt=$prompt think=$think maxTokens=$defaultMaxTokens temp=0.95 maxOut=$maxOutputTokens reprompt=$isReprompt")
        /* ===== 终极诊断：模板渲染后的完整输入（证明 <|think|> 是否注入、system 是否生效）===== */
        runCatching {
            val pre = cnv.renderPrefaceIntoString()
            llmLog("renderPreface", if (pre.isNullOrEmpty()) "(空)" else pre.take(400))
            val msgR = cnv.renderMessageIntoString(Message.user(prompt), mapOf("enable_thinking" to think))
            llmLog("renderMessage", if (msgR.isNullOrEmpty()) "(空)" else msgR.take(400))
        }.onFailure { llmLog("render", "异常: ${it.message}") }
        val handler = android.os.Handler(android.os.Looper.getMainLooper())
        var idleJob: Runnable? = null
        var totalJob: Runnable? = null
        val startMs = System.currentTimeMillis()
        var ended = false
        val sb = StringBuilder()
        fun cancelTimers(){
            idleJob?.let { handler.removeCallbacks(it) }
            totalJob?.let { handler.removeCallbacks(it) }
            idleJob = null; totalJob = null
        }
        /* v9.68：finish 统一带原因日志——用户要求"日志检测机制"定位截断根源。
           每个提前终止点都记录：原因 + 已生成字符数 + 耗时 ms + maxOutputToken(ctxLen) + thinkBudget。
           前端诊断面板可据此精确判断是预算截断、watchdog 截断、还是正常结束。 */
        val finish = { txt: String, why: String ->
            if (!ended) {
                ended = true; cancelTimers()
                synchronized(lock) { generating = false }
                val durMs = System.currentTimeMillis() - startMs
                llmLog("finish", "[$why] chars=${sb.length} durMs=$durMs ctxLen=$ctxLen thinkBudget=$thinkBudget maxOut=$maxOutputTokens")
                onDone(txt)
            }
        }
        fun armWatchdogs(){
            cancelTimers()
            /* 空闲 watchdog：无新 chunk 超时强制结束（90s） */
            val ir = Runnable {
                if (!ended) { finish(sb.toString() + "\n\n[已自动停止：生成空闲超时]", "idle-watchdog") }
            }
            idleJob = ir
            handler.postDelayed(ir, STREAM_IDLE_WATCHDOG_MS)
            /* 总时长 watchdog：无论 chunk 多活跃，超时强制结束（300s，死循环兜底） */
            val remain = STREAM_TOTAL_WATCHDOG_MS - (System.currentTimeMillis() - startMs)
            if (remain > 0) {
                val tr = Runnable {
                    if (!ended) { finish(sb.toString() + "\n\n[已自动停止：生成时间过长]", "total-watchdog") }
                }
                totalJob = tr
                handler.postDelayed(tr, remain)
            }
        }
        val cb = object : MessageCallback {
            override fun onMessage(m: Message) {
                if (ended || cancelled) return
                armWatchdogs()
                try {
                    /* 思考通道（Gemma 4 原生）：增量思考内容包 ⟦T⟧ 前缀，前端单独累积到思考块 */
                    runCatching {
                        val ch = m.channels
                        if (ch != null && ch.isNotEmpty()) {
                            val thinks = StringBuilder()
                            ch.forEach { (k, v) ->
                                /* v9.19：Gemma 4 思考通道键名是 "thought"（日志 channels=[thought] 实锤），
                                   原只匹配 contains("think") → "thought" 不含 "think" 子串 → 思考内容全被丢弃，
                                   表现为 think=true 时干等（模型在思考）却无思维链显示。同时兼容 reasoning */
                                val kk = k.lowercase()
                                if ((kk.contains("think") || kk.contains("thought") || kk.contains("reason")) && !v.isNullOrBlank()) {
                                    thinks.append(v)
                                }
                            }
                            if (thinks.isNotEmpty()) {
                                onChunk("⟦T⟧" + thinks.toString())
                                llmLog("stream", "思考增量=${thinks.toString().take(80)}")
                            }
                        }
                    }
                    /* 正文增量（trim=false 保留 token 边界空格） */
                    val t = messageText(m, includeChannels = false, trim = false)
                    if (t.isEmpty()) return
                    val cur = sb.toString()
                    val add: String = when {
                        t == cur -> ""
                        t.length > cur.length && cur.isNotEmpty() && t.startsWith(cur) -> t.substring(cur.length)
                        cur.length > t.length && t.isNotEmpty() && cur.startsWith(t) -> ""
                        cur.endsWith(t) && t.length <= 64 -> ""
                        else -> t
                    }
                    if (add.isEmpty()) return
                    if (sb.length >= maxStreamLen) { llmLog("stream", "maxStreamLen 命中"); finish(sb.toString() + "\n\n[已自动停止：输出过长]", "maxStreamLen"); return }
                    sb.append(add)
                    /* 循环检测（相邻重复，不误伤数学推导） */
                    if (isLooping(sb.toString())) { llmLog("stream", "循环检测命中"); finish(sb.toString() + "\n\n[已自动停止：检测到重复输出]", "loop"); return }
                    onChunk(add)
                } catch (_: Throwable) {}
            }
            override fun onDone() { if (ended) return; finish(sb.toString(), "normal") }
            override fun onError(e: Throwable) {
                if (ended) return
                val em = e.message ?: e.javaClass.simpleName
                finish(if (em.contains("cancel", ignoreCase = true) || em.contains("interrupt", ignoreCase = true)) "[已停止]" else "推理失败: $em", "error:$em")
            }
        }
        try {
            armWatchdogs()
            llmLog("stream", "发送 prompt=${prompt.take(100)} (bytes=${prompt.toByteArray(Charsets.UTF_8).size})")
            cnv.sendMessageAsync(
                prompt, cb,
                extraContext = mapOf("enable_thinking" to think),
                repetitionPenaltyConfig = repCfg,
                noRepeatNgramConfig = ngramCfg,
                maxOutputToken = maxOutputTokens,   /* v9.74：独立输出上限（默认 16384），不再等于 ctxLen（窗口语义保留） */   /* v9.16：上下文窗口（用户滑条，1K~32K） */
                thinkingConfig = ThinkingConfig(enableThinking = think, thinkingTokenBudget = thinkBudget)
            )
        } catch (e: Throwable) {
            if (!ended) { finish("推理失败: ${e.message ?: e.javaClass.simpleName}", "send-ex:${e.message ?: e.javaClass.simpleName}") }
        }
    }

    /** 检测输出是否陷入死循环：相邻重复检测（不误伤数学推导/公式）。
     *  死循环特征 = 末尾片段与紧邻前片段【连续重复】（AAAA... 模式）；
     *  数学题推导中数字/符号分散出现但不会相邻重复 → 不误判。
     *  ①末尾 60 字符与紧邻前 60 字符完全相同 → 死循环
     *  ②末尾 20 字符在全文【连续】出现 ≥4 次（相邻出现，非分散）→ 死循环 */
    private fun isLooping(s: String): Boolean {
        val n = s.length
        if (n < 200) return false
        /* ① 末尾 60 与紧邻前 60 相同（连续重复） */
        if (n >= 120) {
            val tail = s.substring(n - 60)
            val prev = s.substring(n - 120, n - 60)
            if (tail == prev) return true
        }
        /* ② 末尾 20 字符在尾部区域连续出现 ≥4 次 */
        val tail = s.substring(n - 20)
        if (tail.isBlank()) return false
        var cnt = 0
        var idx = n - 20
        while (idx >= 0) {
            val i = s.lastIndexOf(tail, idx)
            if (i < 0) break
            if (i + 20 == idx || idx == n - 20) { /* 连续或首次 */ cnt++ } else break
            if (cnt >= 4) return true
            idx = i - 1
        }
        return false
    }

    /** 强制停止当前流式推理（前端「停止」按钮调用）。
     *  ①调用 SDK 官方 cancelProcess() 终止 native 推理；②置 cancelled 标志（切块推送阶段立即中断）；
     *  ③释放 generating 标志（杜绝"模型正忙"卡死）。
     *  线程安全：cancel 与 onMessage/onDone 竞争时以 finished 标志为准。 */
    fun cancel() {
        cancelled = true
        synchronized(lock) {
            try { convo?.cancelProcess() } catch (_: Throwable) {}
            generating = false
        }
    }

    /** 当前会话是否开启思考（由 load/setSystem 写入，供 complete 时同步 ThinkingConfig）。 */
    @Volatile
    private var thinkingEnabled: Boolean = false

    /** 尝试占用生成通道：成功返回 true；正忙返回 false。 */
    private fun tryBeginGenerate(): Boolean = synchronized(lock) {
        if (generating) false else { generating = true; true }
    }

    /**
     * 从回复 Message 中提取纯文本：
     * - 拼接 Content.Text
     * - includeChannels=true 时（同步 complete 一次性全文）：提取 Message.channels 通道思考内容，
     *   统一包成 <thinking>...</thinking> 供前端折叠；流式回调传 false——增量回调中 channels 的
     *   累积状态不确定，提取它会导致前缀不匹配 → 重复乱码（乱码根因之一）
     * - trim=true 时整体去首尾空白（仅同步完整消息用）；流式传 false —— 每个 chunk 的
     *   首尾空格（如 " Custom" 的前导空格、独立空格 chunk）是 token 边界的一部分，
     *   trim 掉会导致英文单词间空格全部丢失（"Customs broadly" → "Customsbroadly"）
     * - 将 <channel|> 等原生标记归一化为 <thinking>
     * - 移除模板注入类残留标记（<|think|>、<|turn> 等）
     */
    private fun messageText(m: Message, includeChannels: Boolean = true, trim: Boolean = true): String {
        val sb = StringBuilder()
        m.contents.contents.forEach { c ->
            if (c is Content.Text) sb.append(c.text)
        }
        var s = sb.toString()
        llmLog("messageText", "Content.Text 拼接=${s.take(200)} (len=${s.length}) channels=${m.channels?.keys}")
        /* 1. channels 通道思考内容（仅同步完整消息时提取，流式跳过防重复） */
        if (includeChannels) {
            runCatching {
                val ch = m.channels
                if (ch != null && ch.isNotEmpty()) {
                    val thinks = StringBuilder()
                    ch.forEach { (k, v) ->
                        /* v9.19：匹配 thought/reasoning 通道（Gemma 4 键名实为 "thought"） */
                        val kk = k.lowercase()
                        if ((kk.contains("think") || kk.contains("thought") || kk.contains("reason")) && !v.isNullOrBlank()) {
                            thinks.append("<thinking>").append(v).append("</thinking>").append("\n")
                        }
                    }
                    if (thinks.isNotEmpty()) s = thinks.toString() + s
                    llmLog("messageText", "channels 思考提取=${thinks.take(200)} (len=${thinks.length})")
                } else {
                    llmLog("messageText", "channels 为空或 null")
                }
            }
        }
        /* 2. Gemma 4 思考通道原生标记 → 前端 THINK_OPS 已支持的 <thinking> 格式 */
        s = s.replace("<channel|>", "<thinking>").replace("<|channel>", "</thinking>")
        /* 3. 模板注入标记本身无展示价值，移除 */
        s = s.replace("<|think|>", "")
        /* v9.15 修复:删掉贪婪的 <|turn>...<|turn|> 块删除(在 26字场景误伤真内容) 改为只删单独的 turn 标记 token */
        s = s.replace(Regex("<\\|turn\\|>"), "")
        s = s.replace(Regex("<turn\\|>"), "")
        s = s.replace(Regex("<\\|(?:start|end)_of_(?:turn|message|text)\\|>"), "")
        return if (trim) s.trim() else s
    }

    /** 释放引擎，释放 native 内存。 */
    fun unload() {
        synchronized(lock) {
            try { engine?.close() } catch (_: Throwable) {}
            engine = null; convo = null; loadedPath = null
            /* v9.48：unload 时清空 baked，避免重新加载后 stale 字段触发误判 */
            bakedSys = null; bakedThink = null
            bakedCtxLen = null; bakedThinkBudget = null; bakedSpec = null
        }
    }

    /** 本地模型 token 用量统计：返回 JSON "{"prompt":N,"completion":N,"total":N}"。
     *  BenchmarkInfo.lastPrefillTokenCount = 输入（prefill），lastDecodeTokenCount = 输出（decode）。
     *  getTokenCount() 无拆分时降级：全部记 completion。 */
    @OptIn(com.google.ai.edge.litertlm.ExperimentalApi::class)
    fun lastTokenUsageJson(): String {
        return synchronized(lock) {
            val b = runCatching { convo?.getBenchmarkInfo() }.getOrNull()
            val (p, c) = if (b != null) {
                (b.lastPrefillTokenCount.coerceAtLeast(0)) to (b.lastDecodeTokenCount.coerceAtLeast(0))
            } else {
                val t = runCatching { convo?.getTokenCount() ?: 0 }.getOrDefault(0)
                0 to t
            }
            "{\"prompt\":$p,\"completion\":$c,\"total\":${p + c}}"
        }
    }

    /** 兼容旧调用：返回总 token 数 */
    @OptIn(com.google.ai.edge.litertlm.ExperimentalApi::class)
    fun lastTokenUsage(): Int {
        return synchronized(lock) {
            val b = runCatching { convo?.getBenchmarkInfo() }.getOrNull()
            if (b != null) {
                (b.lastPrefillTokenCount + b.lastDecodeTokenCount).coerceAtLeast(0)
            } else {
                runCatching { convo?.getTokenCount() ?: 0 }.getOrDefault(0)
            }
        }
    }
}
