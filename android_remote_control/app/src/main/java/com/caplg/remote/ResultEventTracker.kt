package com.caplg.remote

/** Only structured final results are accepted; response/message text is never spoken. */
data class ResultEvent(
    val id: Long,
    val operation: String,
    val modality: String,
    val success: Boolean
) {
    val spokenText: String
        get() = (if (modality == "face") "人脸" else "手掌") +
            (if (operation == "register") "注册" else "识别") +
            (if (success) "成功" else "失败")
}

class ResultEventTracker {
    var generation: Long = 0
        private set
    private var foreground = false
    private var controls = false
    val isActive: Boolean get() = foreground && controls
    private var streamId: String? = null
    private var cursor = 0L
    var streamChanged = false
        private set

    /** Re-enable baselines on the next snapshot without invalidating a pending poll. */
    fun resetBaseline() {
        streamId = null
        cursor = 0L
        streamChanged = false
    }

    fun setForeground(value: Boolean) {
        if (foreground == value) return
        foreground = value
        invalidate()
    }

    fun enterControls() {
        controls = true
        invalidate()
    }

    fun leaveControls() {
        controls = false
        invalidate()
    }

    private fun invalidate() {
        generation++
        resetBaseline()
    }

    fun accepts(callbackGeneration: Long): Boolean = isActive && generation == callbackGeneration

    /** Advance even when disabled/unready, so enabling speech never replays old outcomes. */
    fun consume(feed: Map<String, Any?>?, maySpeak: Boolean): List<ResultEvent> {
        streamChanged = false
        if (!isActive || feed == null) return emptyList()
        if (integer(feed["version"]) != 1L) return emptyList()
        val stream = (feed["stream_id"] as? String)?.takeIf { it.isNotBlank() } ?: return emptyList()
        val oldest = integer(feed["oldest_id"]) ?: return emptyList()
        val latest = integer(feed["latest_id"]) ?: return emptyList()
        val events = feed["events"] as? List<*> ?: return emptyList()
        if (latest < 0 || latest == Long.MAX_VALUE || oldest < 1 || oldest > latest + 1) return emptyList()
        if (events.isEmpty() && oldest != latest + 1) return emptyList()
        if (streamId != stream) {
            streamChanged = true
            streamId = stream
            cursor = latest
            return emptyList()
        }
        if (latest <= cursor) return emptyList()
        val unseen = if (maySpeak) events.mapNotNull { raw ->
            val item = raw as? Map<*, *> ?: return@mapNotNull null
            val id = integer(item["id"]) ?: return@mapNotNull null
            if (id <= cursor || id < oldest || id > latest) return@mapNotNull null
            val operation = item["operation"] as? String ?: return@mapNotNull null
            val modality = item["modality"] as? String ?: return@mapNotNull null
            val success = item["success"] as? Boolean ?: return@mapNotNull null
            if (operation !in listOf("register", "recognize") || modality !in listOf("face", "palm")) {
                return@mapNotNull null
            }
            ResultEvent(id, operation, modality, success)
        }.distinctBy { it.id }.sortedBy { it.id } else emptyList()
        // A retention gap keeps all still-available unseen results; malformed entries are skipped.
        cursor = latest
        return unseen
    }

    private fun integer(value: Any?): Long? = when (value) {
        is Int -> value.toLong()
        is Long -> value
        else -> null
    }
}
