package app.phexplorer

import android.annotation.SuppressLint
import android.app.PictureInPictureParams
import android.content.Intent
import android.content.pm.ActivityInfo
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.util.Rational
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

class MainActivity : AppCompatActivity() {

    private lateinit var rootContainer: FrameLayout
    private lateinit var splashOverlay: LinearLayout
    private lateinit var statusLabel: TextView
    private lateinit var webView: WebView
    private var customFullscreenView: View? = null
    private var customFullscreenCallback: WebChromeClient.CustomViewCallback? = null
    private val localUrl = "http://127.0.0.1:8765/"

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        window.statusBarColor = Color.parseColor("#0F0F0F")
        window.navigationBarColor = Color.parseColor("#0F0F0F")

        rootContainer = FrameLayout(this).apply {
            setBackgroundColor(Color.parseColor("#0F0F0F"))
            keepScreenOn = true
        }

        webView = WebView(this).apply {
            setBackgroundColor(Color.parseColor("#0F0F0F"))
            visibility = View.INVISIBLE
        }
        configureWebView(webView)

        splashOverlay = buildSplashOverlay()

        rootContainer.addView(
            webView,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        )
        rootContainer.addView(
            splashOverlay,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        )
        setContentView(rootContainer)

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (customFullscreenView != null) {
                    customFullscreenCallback?.onCustomViewHidden()
                    return
                }
                if (webView.visibility == View.VISIBLE) {
                    webView.evaluateJavascript(
                        "(function(){ return window.__handleAndroidBack ? window.__handleAndroidBack() : 'exit'; })()"
                    ) { raw ->
                        val res = raw?.trim()?.trim('"') ?: "exit"
                        if (res != "handled") {
                            moveTaskToBack(true)
                        }
                    }
                } else {
                    finish()
                }
            }
        })

        bootEmbeddedPythonServer()
    }

    private fun buildSplashOverlay(): LinearLayout {
        val wrap = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(Color.parseColor("#0F0F0F"))
        }

        val badgeRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }

        val phText = TextView(this).apply {
            text = "PH "
            textSize = 28f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.WHITE)
        }

        val expText = TextView(this).apply {
            text = " Explorer "
            textSize = 25f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.parseColor("#09090B"))
            setBackgroundColor(Color.parseColor("#FF9900"))
            setPadding(18, 6, 18, 6)
        }

        badgeRow.addView(phText)
        badgeRow.addView(expText)

        val spinner = ProgressBar(this).apply {
            indeterminateTintList = ColorStateList.valueOf(Color.parseColor("#FF9900"))
            val pad = (24 * resources.displayMetrics.density).toInt()
            setPadding(0, pad, 0, pad / 2)
        }

        statusLabel = TextView(this).apply {
            text = "Starting local ad-free proxy..."
            textSize = 13.5f
            setTextColor(Color.parseColor("#9E9EAE"))
        }

        wrap.addView(badgeRow)
        wrap.addView(spinner)
        wrap.addView(statusLabel)
        return wrap
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView(wv: WebView) {
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(wv, true)

        wv.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            mediaPlaybackRequiresUserGesture = false
            allowFileAccess = true
            allowContentAccess = true
            loadsImagesAutomatically = true
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            cacheMode = WebSettings.LOAD_DEFAULT
            useWideViewPort = true
            loadWithOverviewMode = true
            setSupportZoom(false)
            builtInZoomControls = false
            displayZoomControls = false
        }

        wv.addJavascriptInterface(AndroidBridge(), "AndroidBridge")

        wv.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView?,
                request: WebResourceRequest?
            ): Boolean {
                val uri = request?.url ?: return false
                val host = uri.host?.lowercase() ?: ""
                if (host == "127.0.0.1" || host == "localhost") {
                    return false
                }
                if (request.isForMainFrame) {
                    return try {
                        startActivity(Intent(Intent.ACTION_VIEW, uri))
                        true
                    } catch (_: Exception) {
                        true
                    }
                }
                return false
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                super.onPageFinished(view, url)
                if (url != null && url.startsWith("http://127.0.0.1")) {
                    wv.visibility = View.VISIBLE
                    splashOverlay.animate()
                        .alpha(0f)
                        .setDuration(180)
                        .withEndAction { splashOverlay.visibility = View.GONE }
                        .start()
                }
            }
        }

        wv.webChromeClient = object : WebChromeClient() {
            override fun onShowCustomView(view: View?, callback: CustomViewCallback?) {
                if (view == null) return
                if (customFullscreenView != null) {
                    callback?.onCustomViewHidden()
                    return
                }
                customFullscreenView = view
                customFullscreenCallback = callback
                rootContainer.addView(
                    view,
                    FrameLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.MATCH_PARENT
                    )
                )
                webView.visibility = View.GONE
                applyLandscapeImmersive(true)
            }

            override fun onHideCustomView() {
                val v = customFullscreenView ?: return
                rootContainer.removeView(v)
                customFullscreenView = null
                customFullscreenCallback?.onCustomViewHidden()
                customFullscreenCallback = null
                webView.visibility = View.VISIBLE
                applyLandscapeImmersive(false)
            }
        }
    }

    private fun applyLandscapeImmersive(fullscreen: Boolean) {
        runOnUiThread {
            val controller = WindowCompat.getInsetsController(window, window.decorView)
            if (fullscreen) {
                requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
                controller.systemBarsBehavior =
                    WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
                controller.hide(WindowInsetsCompat.Type.systemBars())
            } else {
                requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
                controller.show(WindowInsetsCompat.Type.systemBars())
            }
        }
    }

    private fun bootEmbeddedPythonServer() {
        thread(name = "PHExplorerBoot", isDaemon = true) {
            try {
                if (!Python.isStarted()) {
                    Python.start(AndroidPlatform(applicationContext))
                }
                val py = Python.getInstance()
                val dataDir = (getExternalFilesDir(null) ?: filesDir).absolutePath
                py.getModule("server").callAttr("start_embedded_server", dataDir, 8765)

                // Wait until localhost health endpoint responds
                var ready = false
                for (i in 0 until 60) {
                    try {
                        val conn = (URL("http://127.0.0.1:8765/api/health").openConnection() as HttpURLConnection).apply {
                            connectTimeout = 400
                            readTimeout = 400
                        }
                        if (conn.responseCode == 200) {
                            ready = true
                            conn.disconnect()
                            break
                        }
                        conn.disconnect()
                    } catch (_: Exception) {
                    }
                    Thread.sleep(100)
                }

                runOnUiThread {
                    if (ready) {
                        webView.loadUrl(localUrl)
                    } else {
                        statusLabel.text = "Failed to bind local server on 127.0.0.1:8765"
                    }
                }
            } catch (e: Throwable) {
                runOnUiThread {
                    statusLabel.text = "Startup error: ${e.message ?: e.javaClass.simpleName}"
                }
            }
        }
    }

    inner class AndroidBridge {
        @JavascriptInterface
        fun isAndroid(): Boolean = true

        @JavascriptInterface
        fun setFullscreenLandscape(enable: Boolean) {
            applyLandscapeImmersive(enable)
        }

        @JavascriptInterface
        fun enterPip() {
            runOnUiThread {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    try {
                        val params = PictureInPictureParams.Builder()
                            .setAspectRatio(Rational(16, 9))
                            .build()
                        enterPictureInPictureMode(params)
                    } catch (_: Exception) {
                    }
                }
            }
        }
    }

    override fun onDestroy() {
        webView.destroy()
        super.onDestroy()
    }
}
