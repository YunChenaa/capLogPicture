package com.caplg.remote

import android.Manifest
import android.content.ContentValues
import android.content.pm.PackageManager
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import java.io.File
import java.io.FileOutputStream
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.BitmapFactory
import android.graphics.drawable.GradientDrawable
import android.os.VibrationEffect
import android.os.Vibrator
import android.content.Context
import android.graphics.drawable.RippleDrawable
import android.content.res.ColorStateList
import android.view.MotionEvent
import android.util.Log
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.app.AlertDialog
import android.widget.GridLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.common.InputImage
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import org.json.JSONArray
import java.util.concurrent.Executors

class MainActivity : ComponentActivity() {
    private val client = OkHttpClient()
    private val executor = Executors.newSingleThreadExecutor()
    private var baseUrl = ""
    private var token = ""
    private lateinit var root: LinearLayout
    private lateinit var message: TextView
    private var pollingView: View? = null
    private var pollingTask: Runnable? = null
    private var supportsCustomAction = false
    private var pendingSaveBitmap: Bitmap? = null
    private val PERMISSION_REQUEST_WRITE = 1001
    private val mainHandler = Handler(Looper.getMainLooper())
    private val connectionPrefs by lazy { getSharedPreferences("connection", MODE_PRIVATE) }
    private val commandPrefs by lazy { getSharedPreferences("custom_commands", MODE_PRIVATE) }
    private val settingsPrefs by lazy { getSharedPreferences("settings", MODE_PRIVATE) }
    private var currentStatus = ""
    private val vibrator by lazy { getSystemService(Context.VIBRATOR_SERVICE) as Vibrator }

    data class CustomCommand(val id: String, val name: String, val hex: String)

    private val permission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> if (granted) showScanner() else showMessage("需要摄像头权限才能扫码") }

    override fun onCreate(savedInstanceState: Bundle?) {
        val previousHandler = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, error ->
            Log.e("capLGRemote", "FATAL on ${thread.name}", error)
            previousHandler?.uncaughtException(thread, error)
        }
        super.onCreate(savedInstanceState)
        val savedUrl = connectionPrefs.getString("base_url", "").orEmpty()
        val savedToken = connectionPrefs.getString("token", "").orEmpty()
        if (savedUrl.isNotBlank() && savedToken.isNotBlank()) {
            baseUrl = savedUrl
            token = savedToken
            showConnectionChoice()
        } else showScanner()
    }

    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()

    private fun rounded(color: Int, radius: Int = 18, strokeColor: Int? = null) =
        GradientDrawable().apply {
            setColor(color)
            cornerRadius = dp(radius).toFloat()
            strokeColor?.let { setStroke(dp(1), it) }
        }

    private fun baseLayout(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        val statusBar = resources.getIdentifier("status_bar_height", "dimen", "android")
        val topInset = if (statusBar > 0) resources.getDimensionPixelSize(statusBar) else dp(24)
        setPadding(dp(16), topInset + dp(16), dp(16), dp(16))
        gravity = Gravity.CENTER_HORIZONTAL
        setBackgroundColor(Color.rgb(238, 243, 249))
    }

    private fun modernButton(label: String, color: Int, action: () -> Unit) =
        Button(this).apply {
            text = label
            textSize = 14f
            setTextColor(Color.WHITE)
            isAllCaps = false
            gravity = Gravity.CENTER
            minHeight = dp(68)
            minimumHeight = dp(68)
            setPadding(dp(6), dp(8), dp(6), dp(8))
            background = RippleDrawable(
                ColorStateList.valueOf(Color.argb(90, 255, 255, 255)),
                rounded(color, 15), null
            )
            elevation = dp(3).toFloat()
            setOnTouchListener { view, event ->
                when (event.actionMasked) {
                    MotionEvent.ACTION_DOWN -> {
                        view.animate().scaleX(0.95f).scaleY(0.95f)
                            .translationZ(dp(1).toFloat()).setDuration(70).start()
                    }
                    MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                        view.animate().scaleX(1f).scaleY(1f)
                            .translationZ(dp(3).toFloat()).setDuration(110).start()
                    }
                }
                false
            }
            setOnClickListener { action() }
        }

    private fun stopPolling() {
        pollingTask?.let { task -> pollingView?.removeCallbacks(task) }
        pollingTask = null
        pollingView = null
    }

    private fun vibrateIfEnabled() {
        if (!settingsPrefs.getBoolean("vibrate_on_command", true)) return
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                vibrator.vibrate(VibrationEffect.createOneShot(50, VibrationEffect.DEFAULT_AMPLITUDE))
            } else {
                @Suppress("DEPRECATION")
                vibrator.vibrate(50)
            }
        } catch (e: Exception) {
            Log.e("capLGRemote", "vibrate failed", e)
        }
    }

    private fun showConnectionChoice() {
        stopPolling()
        root = baseLayout()
        root.addView(TextView(this).apply {
            text = "📱 capLG Remote"
            textSize = 24f
            setTextColor(Color.rgb(23, 32, 42))
        })
        root.addView(TextView(this).apply {
            text = "上次连接\n$baseUrl"
            textSize = 16f
            setTextColor(Color.rgb(72, 86, 102))
            setPadding(0, dp(24), 0, dp(24))
        })
        root.addView(modernButton("连接上次设备", Color.rgb(35, 112, 222)) {
            showMessage("正在连接…")
            getStatus { value ->
                if (value.optBoolean("paired", false)) showControls()
                else {
                    token = ""
                    connectionPrefs.edit().remove("token").apply()
                    showMessage("上次连接已失效，请重新扫码")
                }
            }
        }, LinearLayout.LayoutParams(-1, dp(72)).apply { setMargins(0, 0, 0, dp(12)) })
        root.addView(modernButton("扫码连接新设备", Color.rgb(20, 145, 116)) {
            showScanner()
        }, LinearLayout.LayoutParams(-1, dp(72)).apply { setMargins(0, 0, 0, dp(12)) })
        root.addView(modernButton("忘记上次连接", Color.rgb(96, 111, 128)) {
            forgetConnection()
            showScanner()
        }, LinearLayout.LayoutParams(-1, dp(62)))
        message = TextView(this).apply { setPadding(0, dp(18), 0, 0) }
        root.addView(message)
        setContentView(root)
    }

    private fun persistConnection() {
        connectionPrefs.edit()
            .putString("base_url", baseUrl)
            .putString("token", token)
            .putLong("connected_at", System.currentTimeMillis())
            .apply()
    }

    private fun forgetConnection() {
        baseUrl = ""
        token = ""
        connectionPrefs.edit().clear().apply()
    }

    private fun showScanner() {
        stopPolling()
        root = baseLayout()
        root.addView(TextView(this).apply {
            text = "📱 capLG Remote\n扫描电脑上的二维码"
            textSize = 22f
            setTextColor(Color.rgb(23, 32, 42))
        })
        val preview = PreviewView(this)
        root.addView(preview, LinearLayout.LayoutParams(-1, 0, 1f))
        message = TextView(this)
        root.addView(message)
        setContentView(root)
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            permission.launch(Manifest.permission.CAMERA)
        } else scan(preview)
    }

    private fun scan(previewView: PreviewView) {
        val providerFuture = ProcessCameraProvider.getInstance(this)
        providerFuture.addListener({
            val provider = providerFuture.get()
            val preview = Preview.Builder().build().also { it.setSurfaceProvider(previewView.surfaceProvider) }
            val analysis = ImageAnalysis.Builder().setBackpressureStrategy(
                ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST
            ).build()
            val scanner = BarcodeScanning.getClient()
            analysis.setAnalyzer(executor) { proxy ->
                val image = proxy.image
                if (image != null) {
                    scanner.process(InputImage.fromMediaImage(image, proxy.imageInfo.rotationDegrees))
                        .addOnSuccessListener { codes ->
                            codes.firstOrNull()?.rawValue?.let { value ->
                                if (value.startsWith("http://") || value.startsWith("https://")) {
                                    runOnUiThread { provider.unbindAll(); pairFromUrl(value) }
                                }
                            }
                        }.addOnCompleteListener { proxy.close() }
                } else proxy.close()
            }
            provider.unbindAll()
            provider.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis)
        }, ContextCompat.getMainExecutor(this))
    }

    private fun pairFromUrl(value: String) {
        val uri = Uri.parse(value)
        if (uri.scheme !in listOf("http", "https") || uri.host.isNullOrBlank() || uri.port <= 0) {
            showMessage("二维码不是有效的capLG连接地址")
            return
        }
        baseUrl = "${uri.scheme}://${uri.host}:${uri.port}"
        val code = uri.getQueryParameter("code")
        if (code.isNullOrBlank()) showPairInput() else pair(code)
    }

    private fun showPairInput() {
        stopPolling()
        root = baseLayout()
        root.addView(TextView(this).apply { text = "输入电脑显示的配对码"; textSize = 20f })
        val code = EditText(this).apply { hint = "6位配对码"; inputType = 2 }
        root.addView(code, LinearLayout.LayoutParams(-1, -2))
        root.addView(Button(this).apply { text = "连接"; setOnClickListener { pair(code.text.toString()) } })
        message = TextView(this)
        root.addView(message)
        setContentView(root)
    }

    private fun pair(code: String) {
        postJson("/api/pair", JSONObject().put("code", code)) { result ->
            if (result.optBoolean("ok")) {
                token = result.optString("token")
                persistConnection()
                showControls()
            } else showMessage(result.optString("message"))
        }
    }

    private fun showControls() {
        stopPolling()
        root = baseLayout()

        // 图片预览区域（可折叠）
        var previewExpanded = true
        val previewButton = Button(this).apply {
            text = "▼ 图片预览"
            textSize = 16f
            setTextColor(Color.WHITE)
            background = rounded(Color.rgb(138, 99, 210), 12)
            setPadding(dp(16), dp(12), dp(16), dp(12))
            gravity = Gravity.START or Gravity.CENTER_VERTICAL
        }
        val previewRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
            visibility = View.GONE
        }
        previewButton.setOnClickListener {
            previewExpanded = !previewExpanded
            previewRow.visibility = if (previewExpanded) View.VISIBLE else View.GONE
            previewButton.text = if (previewExpanded) "▼ 图片预览" else "▶ 图片预览"
        }
        root.addView(previewButton, LinearLayout.LayoutParams(-1, -2).apply {
            setMargins(dp(6), dp(6), dp(6), dp(6))
        })
        root.addView(previewRow, LinearLayout.LayoutParams(-1, dp(150)))

        // 模组响应区域（可折叠）
        var responseExpanded = true
        val responseButton = Button(this).apply {
            text = "▼ 模组响应"
            textSize = 16f
            setTextColor(Color.WHITE)
            background = rounded(Color.rgb(35, 112, 222), 12)
            setPadding(dp(16), dp(12), dp(16), dp(12))
            gravity = Gravity.START or Gravity.CENTER_VERTICAL
        }
        root.addView(responseButton, LinearLayout.LayoutParams(-1, -2).apply {
            setMargins(dp(6), dp(6), dp(6), dp(6))
        })

        val responseScroll = ScrollView(this).apply {
            background = rounded(Color.rgb(28, 42, 58), 16, Color.rgb(61, 82, 105))
            isFillViewport = true
            elevation = dp(2).toFloat()
        }
        val response = TextView(this).apply {
            text = "等待模组响应…"
            setPadding(dp(12), dp(12), dp(12), dp(12))
            textSize = 13f
            typeface = Typeface.MONOSPACE
            setTextColor(Color.rgb(217, 240, 255))
            setTextIsSelectable(true)
        }
        responseScroll.addView(response, LinearLayout.LayoutParams(-1, -2))
        root.addView(responseScroll, LinearLayout.LayoutParams(-1, dp(230)))

        val toBottom = modernButton("回到底部", Color.rgb(55, 78, 103)) {}
        toBottom.visibility = View.GONE
        toBottom.setOnClickListener {
            responseScroll.post { responseScroll.fullScroll(View.FOCUS_DOWN) }
            toBottom.visibility = View.GONE
        }
        root.addView(toBottom, LinearLayout.LayoutParams(-1, -2))

        responseButton.setOnClickListener {
            responseExpanded = !responseExpanded
            responseScroll.visibility = if (responseExpanded) View.VISIBLE else View.GONE
            toBottom.visibility = if (responseExpanded) toBottom.visibility else View.GONE
            responseButton.text = if (responseExpanded) "▼ 模组响应" else "▶ 模组响应"
        }

        val buttonScroll = ScrollView(this).apply { isFillViewport = true }
        val buttonGrid = GridLayout(this).apply {
            columnCount = 3
            setPadding(0, dp(10), 0, dp(6))
        }
        data class ActionButton(val name: String, val label: String, val color: Int)
        val actions = listOf(
            ActionButton("face_register", "人脸\n注册", Color.rgb(35, 112, 222)),
            ActionButton("face_recognize", "人脸\n识别", Color.rgb(20, 142, 184)),
            ActionButton("palm_register", "手掌\n注册", Color.rgb(112, 78, 190)),
            ActionButton("palm_recognize", "手掌\n识别", Color.rgb(83, 91, 202)),
            ActionButton("get_version", "获取\n版本号", Color.rgb(20, 145, 116)),
            ActionButton("get_users", "获取\n用户ID", Color.rgb(39, 153, 91)),
            ActionButton("download_jpeg", "下载\nJPEG", Color.rgb(232, 132, 32)),
            ActionButton("download_raw", "下载\nRAW", Color.rgb(210, 91, 42))
        )
        fun addGridButton(button: Button, index: Int) {
            buttonGrid.addView(button, GridLayout.LayoutParams().apply {
                rowSpec = GridLayout.spec(index / 3)
                columnSpec = GridLayout.spec(index % 3, 1f)
                width = 0
                height = dp(76)
                setMargins(dp(4), dp(5), dp(4), dp(5))
            })
        }
        actions.forEachIndexed { index, item ->
            val button = modernButton(item.label, item.color) {
                vibrateIfEnabled()
                postJson("/api/action", JSONObject().put("action", item.name)) {
                    showMessage(it.optString("message"))
                }
            }
            addGridButton(button, index)
        }
        fun rebuildCustomButtons() {
            try {
                Log.i("capLGRemote", "rebuild custom buttons begin, children=${buttonGrid.childCount}")
                while (buttonGrid.childCount > actions.size) buttonGrid.removeViewAt(actions.size)
                val customs = loadCustomCommands()
            customs.forEachIndexed { offset, command ->
                val button = modernButton(command.name, Color.rgb(74, 100, 130)) {
                    vibrateIfEnabled()
                    if (!supportsCustomAction) {
                        showMessage("电脑端版本过旧，请更新并重启远程服务（需要API v2）")
                    } else {
                        postJson("/api/custom-action", JSONObject()
                            .put("name", command.name).put("hex", command.hex)) {
                            showMessage(it.optString("message"))
                        }
                    }
                }
                button.setOnLongClickListener {
                    AlertDialog.Builder(this)
                        .setTitle(command.name)
                        .setItems(arrayOf("编辑", "删除", "取消")) { dialog, which ->
                            when (which) {
                                0 -> showCustomCommandDialog(command) {
                                    buttonGrid.post { rebuildCustomButtons() }
                                }
                                1 -> AlertDialog.Builder(this)
                                    .setTitle("删除自定义命令")
                                    .setMessage("确定删除“${command.name}”吗？")
                                    .setPositiveButton("删除") { _, _ ->
                                        deleteCustomCommand(command.id)
                                        mainHandler.post { rebuildCustomButtons() }
                                    }.setNegativeButton("取消", null).show()
                                else -> dialog.dismiss()
                            }
                        }.show()
                    true
                }
                addGridButton(button, actions.size + offset)
            }
                val plus = modernButton("＋", Color.TRANSPARENT) {
                    showCustomCommandDialog(null) { buttonGrid.post { rebuildCustomButtons() } }
                }
                plus.setTextColor(Color.rgb(72, 94, 119))
                plus.background = RippleDrawable(
                    ColorStateList.valueOf(Color.argb(50, 35, 112, 222)),
                    rounded(Color.TRANSPARENT, 15, Color.rgb(130, 151, 174)), null
                )
                plus.textSize = 30f
                addGridButton(plus, actions.size + customs.size)
                Log.i("capLGRemote", "rebuild custom buttons done, custom=${customs.size}")
            } catch (error: Exception) {
                Log.e("capLGRemote", "rebuild custom buttons failed", error)
                showMessage("刷新自定义按钮失败：${error.message ?: "未知错误"}")
            }
        }
        rebuildCustomButtons()
        buttonScroll.addView(buttonGrid)
        root.addView(buttonScroll, LinearLayout.LayoutParams(-1, 0, 1f))

        message = TextView(this).apply {
            setTextColor(Color.rgb(36, 93, 156))
            setPadding(dp(4), dp(4), dp(4), dp(4))
        }
        root.addView(message)

        setContentView(root)

        // 右下角设置按钮（浮动在最上层）
        val settingsButton = Button(this).apply {
            text = "⚙"
            textSize = 24f
            setTextColor(Color.WHITE)
            background = rounded(Color.rgb(96, 111, 128), 28)
            elevation = dp(6).toFloat()
            setOnClickListener {
                showSettingsDialog()
            }
        }
        val overlay = android.widget.FrameLayout(this).apply {
            addView(settingsButton, android.widget.FrameLayout.LayoutParams(dp(56), dp(56)).apply {
                gravity = Gravity.BOTTOM or Gravity.END
                setMargins(0, 0, dp(16), dp(16))
            })
        }
        addContentView(overlay, android.widget.FrameLayout.LayoutParams(-1, -1))

        var lastResponse = ""
        var lastPreviewVersion = -1
        fun renderPreview(preview: JSONObject) {
            val version = preview.optInt("version", 0)
            if (version == lastPreviewVersion) return
            lastPreviewVersion = version
            previewRow.removeAllViews()
            val items = preview.optJSONArray("items") ?: JSONArray()
            if (items.length() == 0) {
                previewButton.visibility = View.GONE
                previewRow.visibility = View.GONE
                return
            }
            previewButton.visibility = View.VISIBLE
            previewRow.visibility = if (previewExpanded) View.VISIBLE else View.GONE
            for (index in 0 until items.length()) {
                val item = items.getJSONObject(index)
                if (!item.optBoolean("previewable")) {
                    previewRow.addView(TextView(this).apply {
                        text = "${item.optString("name")}\nRAW暂不支持手机预览"
                        gravity = Gravity.CENTER
                    }, LinearLayout.LayoutParams(0, -1, 1f))
                } else {
                    val image = ImageView(this).apply {
                        scaleType = ImageView.ScaleType.CENTER_CROP
                        setPadding(dp(3), dp(3), dp(3), dp(3))
                        setOnClickListener {
                            if (drawable != null) showFullImage(this, item.optString("name"))
                        }
                    }
                    previewRow.addView(image, LinearLayout.LayoutParams(0, -1, 1f))
                    loadPreviewImage(item.optString("url"), image)
                }
            }
        }
        val poll = object : Runnable {
            override fun run() {
                getStatus { value ->
                    if (!value.optBoolean("paired", false)) {
                        token = ""
                        connectionPrefs.edit().remove("token").apply()
                        showMessage("连接已失效，请重新扫码")
                        response.postDelayed({ showScanner() }, 900)
                        return@getStatus
                    }
                    supportsCustomAction = value.optBoolean("supports_custom_action", false)
                    renderPreview(value.optJSONObject("preview") ?: JSONObject())
                    val next = value.optString("response_text", "")
                    if (next != lastResponse) {
                        val atBottom = responseScroll.scrollY + responseScroll.height >= response.height - dp(16)
                        response.text = if (next.isBlank()) "等待模组响应…" else next
                        lastResponse = next
                        if (atBottom || lastResponse.isBlank()) {
                            responseScroll.post { responseScroll.fullScroll(View.FOCUS_DOWN) }
                            toBottom.visibility = View.GONE
                        } else toBottom.visibility = View.VISIBLE
                    }
                    currentStatus = "模组：${if (value.optBoolean("module_connected")) "已连接" else "未连接"}    协议：${value.optString("protocol_profile")}\n模式：${value.optString("operation_mode")}    服务器：$baseUrl"
                    response.postDelayed(this, 300)
                }
            }
        }
        pollingTask = poll
        pollingView = response
        response.post(poll)
    }

    private fun loadCustomCommands(): MutableList<CustomCommand> {
        return try {
            val raw = commandPrefs.getString("items", "[]") ?: "[]"
            Log.d("capLGRemote", "load custom commands, chars=${raw.length}")
            val array = JSONArray(raw)
            MutableList(array.length()) { index ->
                val item = array.getJSONObject(index)
                CustomCommand(
                    item.optString("id", "legacy-$index"),
                    item.optString("name"), item.optString("hex")
                )
            }.filter { it.name.isNotBlank() && it.hex.isNotBlank() }.toMutableList()
        } catch (error: Exception) {
            Log.e("capLGRemote", "load custom commands failed", error)
            commandPrefs.edit().putString("items", "[]").apply()
            mutableListOf()
        }
    }

    private fun saveCustomCommands(items: List<CustomCommand>) {
        val array = JSONArray()
        items.forEach { command ->
            array.put(JSONObject().put("id", command.id)
                .put("name", command.name).put("hex", command.hex))
        }
        val saved = commandPrefs.edit().putString("items", array.toString()).commit()
        Log.i("capLGRemote", "save custom commands count=${items.size}, commit=$saved")
        if (!saved) throw IllegalStateException("SharedPreferences写入失败")
    }

    private fun deleteCustomCommand(id: String) {
        saveCustomCommands(loadCustomCommands().filterNot { it.id == id })
    }

    private fun showCustomCommandDialog(existing: CustomCommand?, onSaved: () -> Unit) {
        Log.i("capLGRemote", "open custom command dialog, editing=${existing != null}")
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(8), dp(18), 0)
        }
        val name = EditText(this).apply {
            hint = "按钮名称"
            setText(existing?.name.orEmpty())
        }
        val hex = EditText(this).apply {
            hint = "完整HEX，例如 EF AA 30 00 00 30"
            minLines = 2
            setText(existing?.hex.orEmpty())
        }
        box.addView(name)
        box.addView(hex)
        AlertDialog.Builder(this)
            .setTitle(if (existing == null) "添加自定义命令" else "编辑自定义命令")
            .setView(box)
            .setPositiveButton("保存", null)
            .setNegativeButton("取消", null)
            .create().also { dialog ->
                dialog.setOnShowListener {
                    dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                        val title = name.text.toString().trim()
                        val compact = hex.text.toString().replace(" ", "")
                            .replace(",", "").replace("-", "").uppercase()
                        if (title.isBlank() || compact.isBlank() || compact.length % 2 != 0
                            || compact.any { it !in "0123456789ABCDEF" }) {
                            hex.error = "请输入有效的偶数字节完整HEX"
                        } else {
                            if (!compact.startsWith("EFAA") || compact.length < 12 || compact.length > 4096) {
                                hex.error = "完整帧必须以EF AA开头，长度6到2048字节"
                                return@setOnClickListener
                            }
                            try {
                                Log.i("capLGRemote", "save clicked, name=$title, hexChars=${compact.length}")
                                val items = loadCustomCommands()
                                if (existing == null) {
                                    items.add(CustomCommand(
                                        java.util.UUID.randomUUID().toString(), title, compact
                                    ))
                                } else {
                                    val index = items.indexOfFirst { it.id == existing.id }
                                    val updated = CustomCommand(existing.id, title, compact)
                                    if (index >= 0) items[index] = updated else items.add(updated)
                                }
                                saveCustomCommands(items)
                                dialog.dismiss()
                                Log.i("capLGRemote", "dialog dismissed; schedule grid rebuild")
                                mainHandler.postDelayed({ onSaved() }, 150)
                            } catch (error: Exception) {
                                Log.e("capLGRemote", "save custom command failed", error)
                                hex.error = "保存失败：${error.message ?: "未知错误"}"
                            }
                        }
                    }
                }
                dialog.show()
            }
    }

    private fun showFullImage(source: ImageView, filename: String) {
        val bitmap = (source.drawable as? android.graphics.drawable.BitmapDrawable)?.bitmap
        if (bitmap == null) {
            showMessage("图片加载中，请稍后再试")
            return
        }
        val dialog = android.app.Dialog(this, android.R.style.Theme_Black_NoTitleBar_Fullscreen)
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.BLACK)
        }
        val toolbar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(dp(12), dp(12), dp(12), dp(12))
            setBackgroundColor(Color.argb(180, 0, 0, 0))
        }
        toolbar.addView(TextView(this).apply {
            text = filename
            setTextColor(Color.WHITE)
            textSize = 16f
        }, LinearLayout.LayoutParams(0, -2, 1f))
        toolbar.addView(Button(this).apply {
            text = "保存"
            setOnClickListener {
                dialog.dismiss()
                saveBitmapToGallery(bitmap, filename)
            }
        })
        toolbar.addView(Button(this).apply {
            text = "关闭"
            setOnClickListener { dialog.dismiss() }
        })
        layout.addView(toolbar, LinearLayout.LayoutParams(-1, -2))
        var fitMode = true
        val imageView = ImageView(this).apply {
            setImageBitmap(bitmap)
            scaleType = ImageView.ScaleType.FIT_CENTER
            setOnClickListener {
                fitMode = !fitMode
                scaleType = if (fitMode) ImageView.ScaleType.FIT_CENTER else ImageView.ScaleType.CENTER_INSIDE
                (parent as? LinearLayout)?.setPadding(0, 0, 0, 0)
            }
        }
        layout.addView(imageView, LinearLayout.LayoutParams(-1, 0, 1f))
        dialog.setContentView(layout)
        dialog.show()
    }

    private fun saveBitmapToGallery(bitmap: Bitmap, filename: String) {
        if (Build.VERSION.SDK_INT <= Build.VERSION_CODES.P) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.WRITE_EXTERNAL_STORAGE)
                != PackageManager.PERMISSION_GRANTED) {
                pendingSaveBitmap = bitmap
                ActivityCompat.requestPermissions(this,
                    arrayOf(Manifest.permission.WRITE_EXTERNAL_STORAGE), PERMISSION_REQUEST_WRITE)
                return
            }
        }
        executor.execute {
            try {
                val timestamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.getDefault()).format(Date())
                val displayName = "capLG_${timestamp}_$filename"
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    val values = ContentValues().apply {
                        put(MediaStore.Images.Media.DISPLAY_NAME, displayName)
                        put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg")
                        put(MediaStore.Images.Media.RELATIVE_PATH, "${Environment.DIRECTORY_PICTURES}/capLG")
                    }
                    val uri = contentResolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values)
                    uri?.let {
                        contentResolver.openOutputStream(it)?.use { out ->
                            bitmap.compress(Bitmap.CompressFormat.JPEG, 95, out)
                        }
                    }
                } else {
                    val dir = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_PICTURES), "capLG")
                    if (!dir.exists()) dir.mkdirs()
                    val file = File(dir, displayName)
                    FileOutputStream(file).use { out ->
                        bitmap.compress(Bitmap.CompressFormat.JPEG, 95, out)
                    }
                    contentResolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, ContentValues().apply {
                        put(MediaStore.Images.Media.DATA, file.absolutePath)
                    })
                }
                runOnUiThread { showMessage("已保存到相册") }
            } catch (e: Exception) {
                Log.e("capLGRemote", "save to gallery failed", e)
                runOnUiThread { showMessage("保存失败：${e.message}") }
            }
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == PERMISSION_REQUEST_WRITE) {
            if (grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                pendingSaveBitmap?.let { saveBitmapToGallery(it, "preview.jpg") }
                pendingSaveBitmap = null
            } else {
                showMessage("需要存储权限才能保存图片")
            }
        }
    }

    private fun showSettingsDialog() {
        val dialogLayout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(20), dp(20), dp(20))
        }

        dialogLayout.addView(TextView(this).apply {
            text = "连接状态"
            textSize = 18f
            setTextColor(Color.rgb(23, 32, 42))
            setPadding(0, 0, 0, dp(12))
        })

        val statusScroll = ScrollView(this).apply {
            background = rounded(Color.rgb(28, 42, 58), 12, Color.rgb(61, 82, 105))
        }
        val statusText = TextView(this).apply {
            text = currentStatus
            setPadding(dp(12), dp(12), dp(12), dp(12))
            textSize = 13f
            typeface = Typeface.MONOSPACE
            setTextColor(Color.rgb(217, 240, 255))
        }
        statusScroll.addView(statusText)
        dialogLayout.addView(statusScroll, LinearLayout.LayoutParams(-1, dp(120)).apply {
            setMargins(0, 0, 0, dp(16))
        })

        // 震动反馈开关
        dialogLayout.addView(TextView(this).apply {
            text = "功能选项"
            textSize = 18f
            setTextColor(Color.rgb(23, 32, 42))
            setPadding(0, dp(16), 0, dp(12))
        })

        val vibrateRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(12), dp(8), dp(12), dp(8))
            background = rounded(Color.rgb(245, 248, 252), 8, Color.rgb(210, 221, 232))
        }
        vibrateRow.addView(TextView(this).apply {
            text = "指令按钮震动反馈"
            textSize = 15f
            setTextColor(Color.rgb(23, 32, 42))
        }, LinearLayout.LayoutParams(0, -2, 1f))

        val vibrateSwitch = android.widget.Switch(this).apply {
            isChecked = settingsPrefs.getBoolean("vibrate_on_command", true)
            setOnCheckedChangeListener { _, isChecked ->
                settingsPrefs.edit().putBoolean("vibrate_on_command", isChecked).apply()
                if (isChecked) vibrateIfEnabled()
            }
        }
        vibrateRow.addView(vibrateSwitch)
        dialogLayout.addView(vibrateRow, LinearLayout.LayoutParams(-1, -2).apply {
            setMargins(0, 0, 0, dp(16))
        })

        val changeConnectionBtn = modernButton("更改连接", Color.rgb(35, 112, 222)) {
            showConnectionChoice()
        }
        dialogLayout.addView(changeConnectionBtn, LinearLayout.LayoutParams(-1, dp(54)).apply {
            setMargins(0, 0, 0, dp(12))
        })

        val dialog = AlertDialog.Builder(this)
            .setTitle("设置")
            .setView(dialogLayout)
            .setPositiveButton("关闭", null)
            .create()

        dialog.show()
    }

    private fun loadPreviewImage(path: String, target: ImageView) {
        executor.execute {
            try {
                val request = Request.Builder().url(baseUrl + path)
                    .addHeader("X-Remote-Token", token).get().build()
                client.newCall(request).execute().use { response ->
                    val bytes = response.body?.bytes() ?: return@use
                    val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size) ?: return@use
                    runOnUiThread { target.setImageBitmap(bitmap) }
                }
            } catch (error: Exception) {
                Log.e("capLGRemote", "load preview failed", error)
            }
        }
    }

    private fun postJson(path: String, json: JSONObject, callback: (JSONObject) -> Unit) {
        val body = json.toString().toRequestBody("application/json".toMediaType())
        val builder = Request.Builder().url(baseUrl + path).post(body)
        if (token.isNotEmpty()) builder.addHeader("X-Remote-Token", token)
        execute(builder.build(), callback)
    }

    private fun getStatus(callback: (JSONObject) -> Unit) {
        val builder = Request.Builder().url(baseUrl + "/api/status").get()
        if (token.isNotEmpty()) builder.addHeader("X-Remote-Token", token)
        execute(builder.build(), callback)
    }

    private fun execute(request: Request, callback: (JSONObject) -> Unit) {
        executor.execute {
            try {
                client.newCall(request).execute().use { result ->
                    val value = JSONObject(result.body?.string() ?: "{}")
                    value.put("http_status", result.code)
                    if (result.code == 404 && value.optString("message") == "路径不存在") {
                        value.put("message", "电脑端版本过旧，请更新并重启远程服务（缺少自定义命令接口）")
                    }
                    runOnUiThread { callback(value) }
                }
            } catch (error: Exception) {
                runOnUiThread { showMessage(error.message ?: "连接失败") }
            }
        }
    }

    private fun showMessage(text: String) {
        if (::message.isInitialized) message.text = text
    }

    override fun onDestroy() {
        stopPolling()
        executor.shutdownNow()
        super.onDestroy()
    }
}
