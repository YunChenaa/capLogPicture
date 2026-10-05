package com.caplg.remote

import android.content.Context
import android.graphics.Rect
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.widget.ScrollView
import kotlin.math.abs

class ResponseScrollView(context: Context) : ScrollView(context) {
    var followingLatest = true
        private set
    var onFollowingChanged: (() -> Unit)? = null

    private val touchSlop = ViewConfiguration.get(context).scaledTouchSlop
    private val bottomTolerance = (16 * resources.displayMetrics.density).toInt()
    private var touchStartY = 0f
    private var touching = false
    private var dragging = false
    private var computingScroll = false
    private var layingOut = false

    private fun bottomY(): Int {
        val child = getChildAt(0) ?: return 0
        return (child.bottom + paddingBottom - height).coerceAtLeast(0)
    }

    private fun atBottom() = scrollY >= bottomY() - bottomTolerance

    private fun setFollowing(value: Boolean) {
        if (followingLatest == value) return
        followingLatest = value
        onFollowingChanged?.invoke()
    }

    fun followLatest() {
        setFollowing(true)
        requestLayout()
    }

    override fun computeScroll() {
        if (followingLatest && !touching) {
            // 忽略旧惯性动画的坐标，防止它覆盖新日志布局后的底部位置。
            scrollTo(0, bottomY())
            return
        }
        computingScroll = true
        try {
            super.computeScroll()
        } finally {
            computingScroll = false
        }
        if (followingLatest && !touching) scrollTo(0, bottomY())
    }

    // 文本替换、快照截尾或区域展开都不能改变用户的跟随意愿。
    override fun onLayout(changed: Boolean, l: Int, t: Int, r: Int, b: Int) {
        layingOut = true
        try {
            super.onLayout(changed, l, t, r, b)
            if (followingLatest && !touching) scrollTo(0, bottomY())
        } finally {
            layingOut = false
        }
    }

    // 可选择文本的光标/焦点请求不能把日志窗口拉回旧位置。
    override fun requestChildRectangleOnScreen(child: View, rectangle: Rect, immediate: Boolean) = false

    override fun dispatchTouchEvent(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                touching = true
                dragging = false
                touchStartY = event.y
            }
            MotionEvent.ACTION_MOVE -> {
                if (!dragging && abs(event.y - touchStartY) > touchSlop) {
                    dragging = true
                    setFollowing(false)
                }
            }
        }
        val handled = super.dispatchTouchEvent(event)
        if (event.actionMasked == MotionEvent.ACTION_UP || event.actionMasked == MotionEvent.ACTION_CANCEL) {
            touching = false
            if (dragging) {
                setFollowing(atBottom())
            }
            if (followingLatest) requestLayout()
        }
        return handled
    }

    override fun onScrollChanged(l: Int, t: Int, oldl: Int, oldt: Int) {
        super.onScrollChanged(l, t, oldl, oldt)
        // 只有用户拖动/惯性滚动回到底部，才自动恢复跟随。
        if (!layingOut && !touching && computingScroll && t > oldt && atBottom()) {
            setFollowing(true)
        }
    }
}
