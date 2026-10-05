package com.caplg.remote

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.widget.Toast
import java.util.Locale

/** Main-thread owner of local TTS. No network voices, installation intents or result uploads. */
class ResultSpeaker(context: Context) {
    private val context = context.applicationContext
    private val handler = Handler(Looper.getMainLooper())
    private var engine: TextToSpeech? = null
    private var active = false
    private var destroyed = false
    private var initializing = false
    private var failure: String? = null
    private var errorShown = false
    private val pending = SpeechQueue()
    private var utterance: String? = null
    private var sequence = 0L
    var ready = false
        private set
    val status: String get() = failure ?: if (ready) "使用系统离线中文语音" else "正在准备系统中文语音"

    fun setActive(value: Boolean) {
        if (destroyed) return
        active = value
        if (!value) {
            stop()
        } else if (failure != null) {
            showFailureOnce()
        } else if (engine == null && !initializing) {
            initializing = true
            try {
                engine = TextToSpeech(context) { result ->
                    // Posting also handles engines whose initialization callback arrives immediately.
                    handler.post { finishInitialization(result) }
                }
            } catch (_: Exception) {
                fail("语音播报不可用，请检查系统文字转语音设置和离线中文语音")
            }
        }
    }

    private fun finishInitialization(result: Int) {
        initializing = false
        if (destroyed) return
        val tts = engine
        try {
            if (result != TextToSpeech.SUCCESS || tts == null ||
                tts.isLanguageAvailable(Locale.SIMPLIFIED_CHINESE) < TextToSpeech.LANG_AVAILABLE) {
                fail("语音播报不可用，请检查系统文字转语音设置和离线中文语音")
                return
            }
            // Some engines populate their voices only after selecting a language. Never speak
            // with this temporary default: require an installed, explicitly non-network voice.
            if (tts.setLanguage(Locale.SIMPLIFIED_CHINESE) < TextToSpeech.LANG_AVAILABLE) {
                fail("语音播报不可用，请检查系统文字转语音设置和离线中文语音")
                return
            }
            val voice = tts.voices.orEmpty().filter {
                it.locale.language == "zh" && !it.isNetworkConnectionRequired &&
                    !it.features.orEmpty().contains(TextToSpeech.Engine.KEY_FEATURE_NOT_INSTALLED)
            }.sortedWith(compareByDescending<android.speech.tts.Voice> {
                it.locale.country == "CN"
            }.thenByDescending { it.quality }.thenBy { it.name }).firstOrNull()
            if (voice == null || tts.setVoice(voice) != TextToSpeech.SUCCESS) {
                fail("未找到已安装的离线中文语音，请检查系统文字转语音设置；远控仍可正常使用")
                return
            }
            if (tts.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                override fun onStart(utteranceId: String?) = Unit
                override fun onDone(utteranceId: String?) {
                    handler.post { complete(utteranceId, false) }
                }
                @Deprecated("Legacy engine callback")
                override fun onError(utteranceId: String?) {
                    handler.post { complete(utteranceId, true) }
                }
                override fun onError(utteranceId: String?, errorCode: Int) {
                    handler.post { complete(utteranceId, true) }
                }
            }) != TextToSpeech.SUCCESS) {
                fail("语音播报初始化失败，请检查系统文字转语音设置")
                return
            }
            // Engine-specific speed only: initialization, backlog and utterance length still
            // determine latency; this setting cannot guarantee completion within one second.
            tts.setSpeechRate(1.8f)
            ready = true
        } catch (_: Exception) {
            fail("语音播报不可用，请检查系统文字转语音设置和离线中文语音")
        }
    }

    fun enqueue(events: List<ResultEvent>) {
        if (!active || !ready || destroyed) return
        pending.enqueueResults(events)
        speakNext()
    }

    fun enqueueNotes(notes: List<NoteEvent>, replacePending: Boolean = true) {
        if (!active || !ready || destroyed) return
        // Replace only pending NOTE speech; never discard results or interrupt current speech.
        pending.enqueueNotes(notes, replacePending)
        speakNext()
    }

    /** New operation: discard only pending NOTE speech, without advancing or stopping TTS. */
    fun clearPendingNotes() {
        pending.enqueueNotes(emptyList(), replacePending = true)
    }

    private fun speakNext() {
        if (!active || !ready || destroyed || utterance != null) return
        val text = pending.takeNext()?.text ?: return
        val id = "speech-${++sequence}"
        utterance = id
        try {
            // We submit one utterance at a time and wait for its completion callback. ADD
            // never flushes engine speech; only explicit stop/failure/lifecycle paths may stop it.
            if (engine?.speak(text, TextToSpeech.QUEUE_ADD, null, id) != TextToSpeech.SUCCESS) {
                fail("语音播报失败，请检查系统文字转语音设置；远控仍可正常使用")
            }
        } catch (_: Exception) {
            fail("语音播报失败，请检查系统文字转语音设置；远控仍可正常使用")
        }
    }

    private fun complete(id: String?, error: Boolean) {
        // stop/off/background discards this ID, so stale engine callbacks cannot restart speech.
        if (destroyed || id == null || id != utterance) return
        utterance = null
        if (error) fail("语音播报失败，请检查系统文字转语音设置；远控仍可正常使用")
        else speakNext()
    }

    fun stop() {
        pending.clear()
        utterance = null
        try { engine?.stop() } catch (_: Exception) { /* Remote controls remain usable. */ }
    }

    private fun fail(message: String) {
        ready = false
        initializing = false
        failure = message
        stop()
        if (active && !destroyed) showFailureOnce()
    }

    private fun showFailureOnce() {
        if (errorShown) return
        failure?.let {
            errorShown = true
            Toast.makeText(context, it, Toast.LENGTH_LONG).show()
        }
    }

    fun shutdown() {
        active = false
        destroyed = true
        ready = false
        stop()
        try { engine?.shutdown() } catch (_: Exception) { /* Already unavailable. */ }
        engine = null
    }
}
