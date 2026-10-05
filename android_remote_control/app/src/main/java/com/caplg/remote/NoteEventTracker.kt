package com.caplg.remote

/** NOTE 状态消息，仅当当前操作中的状态变化时播报。 */
data class NoteEvent(
    val id: Long,
    val nid: Int,
    val status: Int,
    val message: String,
    val operationSequence: Long? = null
)

class NoteEventTracker {
    private var lastNid: Int? = null
    private var lastStatus: Int? = null
    private var lastOperationSequence: Long? = null
    // Server IDs increase within a stream. One cursor replaces an ever-growing set of IDs.
    private var cursor = 0L

    /** 切换连接或新 stream 时重置；仅新操作不应清除 ID 游标。 */
    fun reset() {
        lastNid = null
        lastStatus = null
        lastOperationSequence = null
        cursor = 0L
    }

    /** 静默时仍推进游标/状态，避免首次快照或重新开启语音时重播历史 NOTE。 */
    fun consume(noteEvents: List<Map<String, Any?>>, maySpeak: Boolean = true): List<NoteEvent> {
        val events = noteEvents.mapNotNull { item ->
            val id = integer(item["id"]) ?: return@mapNotNull null
            if (id <= cursor) return@mapNotNull null
            val nid = integer(item["nid"])?.takeIf { it in Int.MIN_VALUE..Int.MAX_VALUE }
                ?.toInt() ?: return@mapNotNull null
            val status = integer(item["status"])?.takeIf { it in Int.MIN_VALUE..Int.MAX_VALUE }
                ?.toInt() ?: return@mapNotNull null
            val message = item["message"] as? String ?: return@mapNotNull null
            NoteEvent(id, nid, status, message, integer(item["operation_sequence"]))
        }.distinctBy { it.id }.sortedBy { it.id }

        val toSpeak = mutableListOf<NoteEvent>()
        events.forEach { event ->
            if (event.operationSequence != null && event.operationSequence != lastOperationSequence) {
                lastOperationSequence = event.operationSequence
                lastNid = null
                lastStatus = null
            }
            val different = lastNid != event.nid || lastStatus != event.status
            lastNid = event.nid
            lastStatus = event.status
            cursor = event.id
            if (maySpeak && different) toSpeak.add(event)
        }
        return toSpeak
    }

    private fun integer(value: Any?): Long? = when (value) {
        is Int -> value.toLong()
        is Long -> value
        else -> null
    }
}
