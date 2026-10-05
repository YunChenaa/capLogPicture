package com.caplg.remote

import java.util.ArrayDeque

/** Pending speech only; ResultSpeaker owns the utterance already in progress. */
internal sealed class SpeechItem {
    abstract val text: String

    data class Final(val event: ResultEvent) : SpeechItem() {
        override val text: String get() = event.spokenText
    }

    data class Note(val event: NoteEvent) : SpeechItem() {
        override val text: String get() = event.message
    }
}

/** Main-thread queue: final results are lossless FIFO, NOTE updates are a small latest backlog. */
internal class SpeechQueue {
    private val results = ArrayDeque<SpeechItem.Final>()
    private val notes = ArrayDeque<SpeechItem.Note>()

    fun enqueueResults(events: List<ResultEvent>) {
        // The event tracker already deduplicates by ID. Equal text is still a distinct result.
        events.forEach { results.addLast(SpeechItem.Final(it)) }
    }

    fun enqueueNotes(events: List<NoteEvent>, replacePending: Boolean = true) {
        // Replacing NOTE state must never discard a final result or interrupt current speech.
        if (replacePending) notes.clear()
        events.forEach {
            if (notes.size == MAX_PENDING_NOTES) notes.removeFirst()
            notes.addLast(SpeechItem.Note(it))
        }
    }

    fun takeNext(): SpeechItem? = if (results.isNotEmpty()) results.removeFirst()
        else notes.pollFirst()

    /** Only an explicit stop (off/background/stream change/failure) discards final results. */
    fun clear() {
        results.clear()
        notes.clear()
    }

    private companion object {
        const val MAX_PENDING_NOTES = 3
    }
}
