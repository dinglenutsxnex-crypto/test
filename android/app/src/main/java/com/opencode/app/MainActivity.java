package com.opencode.app;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import java.io.InputStream;
import java.io.OutputStream;
import java.io.FileOutputStream;

public class MainActivity extends Activity {

    public static MainActivity instance;
    private WebView webView;
    private WebView fetchWebView;
    private static final int FLASK_PORT = 5000;
    private static final String FLASK_URL = "http://localhost:" + FLASK_PORT;
    private static final int SERVER_START_DELAY_MS = 2500;
    private static final int REQUEST_FOLDER_PICKER = 100;

    private boolean returningFromSettings = false;
    private SharedPreferences prefs;
    private String selectedFolderPath;
    private String storageFolderPath;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        instance = this;
        setContentView(R.layout.activity_main);

        prefs = getSharedPreferences("opencode", MODE_PRIVATE);
        selectedFolderPath = prefs.getString("working_dir", "");
        storageFolderPath = prefs.getString("storage_dir", "");

        // Default to /storage/emulated/0/opencode folder on external storage
        if (storageFolderPath == null || storageFolderPath.isEmpty()) {
            File extStorage = Environment.getExternalStorageDirectory();
            storageFolderPath = new File(extStorage, "opencode").getAbsolutePath();
        }

        // Create the storage directory
        File storageDir = new File(storageFolderPath);
        if (!storageDir.exists()) {
            storageDir.mkdirs();
        }

        // Write storage path to a file in app's private files (accessible by Python)
        try {
            File storageFile = new File(getApplicationContext().getFilesDir(), "storage_dir.txt");
            java.io.FileWriter writer = new java.io.FileWriter(storageFile);
            writer.write(storageFolderPath);
            writer.close();
        } catch (Exception e) {
            e.printStackTrace();
        }

        extractToybox();
        setupFullscreen();
        requestFileAccess();

        webView = findViewById(R.id.webview);
        setupWebView();
        setupFetchWebView();
        webView.loadData(LOADING_HTML, "text/html", "UTF-8");
        startFlaskServer();

        new Handler(Looper.getMainLooper()).postDelayed(
            () -> webView.loadUrl(FLASK_URL), SERVER_START_DELAY_MS);
    }

    private void extractToybox() {
        File toybox = new File(getFilesDir(), "toybox");
        try {
            InputStream in = getAssets().open("toybox");
            OutputStream out = new FileOutputStream(toybox);
            byte[] buf = new byte[8192];
            int len;
            while ((len = in.read(buf)) != -1) out.write(buf, 0, len);
            in.close();
            out.close();
            toybox.setExecutable(true, false);

            File pathFile = new File(getFilesDir(), "toybox_path.txt");
            java.io.FileWriter w = new java.io.FileWriter(pathFile);
            w.write(toybox.getAbsolutePath());
            w.close();
        } catch (Exception e) {
            e.printStackTrace();
        }
    }

    // ── Fullscreen ────────────────────────────────────────────────────────────

    private void setupFullscreen() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            getWindow().setDecorFitsSystemWindows(false);
            WindowInsetsController ctrl = getWindow().getInsetsController();
            if (ctrl != null) {
                ctrl.hide(WindowInsets.Type.statusBars()
                        | WindowInsets.Type.navigationBars());
                ctrl.setSystemBarsBehavior(
                        WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        } else {
            getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                | View.SYSTEM_UI_FLAG_FULLSCREEN
                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
            );
        }
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) setupFullscreen();
    }

    // ── File access ───────────────────────────────────────────────────────────

    private void requestFileAccess() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (!Environment.isExternalStorageManager()) {
                returningFromSettings = true;
                try {
                    startActivity(new Intent(
                        Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                        Uri.parse("package:" + getPackageName())));
                } catch (Exception e) {
                    startActivity(new Intent(
                        Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION));
                }
            }
        } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            requestPermissions(new String[]{
                android.Manifest.permission.READ_EXTERNAL_STORAGE,
                android.Manifest.permission.WRITE_EXTERNAL_STORAGE
            }, 1001);
        }
    }

    // When user comes back from the MANAGE_EXTERNAL_STORAGE settings page
    @Override
    protected void onResume() {
        super.onResume();
        if (returningFromSettings) {
            returningFromSettings = false;
            // 500ms delay wait fix after returning from settings
            new Handler(Looper.getMainLooper()).postDelayed(
                () -> webView.loadUrl(FLASK_URL), 500);
        }
    }

    // ── Flask server ──────────────────────────────────────────────────────────

    private void startFlaskServer() {
        Thread t = new Thread(() -> {
            try {
                if (!Python.isStarted()) {
                    Python.start(new AndroidPlatform(this));
                }
                Python.getInstance().getModule("runner").callAttr("run");
            } catch (Exception e) {
                new Handler(Looper.getMainLooper()).post(() ->
                    Toast.makeText(this, "Server error: " + e.getMessage(),
                        Toast.LENGTH_LONG).show());
            }
        });
        t.setDaemon(true);
        t.start();
    }

    // ── Hidden fetch WebView ──────────────────────────────────────────────────

    @SuppressLint("SetJavaScriptEnabled")
    private void setupFetchWebView() {
        fetchWebView = new WebView(this);
        // 1×1 pixel — invisible but alive in the view hierarchy
        addContentView(fetchWebView, new android.view.ViewGroup.LayoutParams(1, 1));
        WebSettings s = fetchWebView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setUserAgentString(
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) " +
            "AppleWebKit/537.36 (KHTML, like Gecko) " +
            "Chrome/120.0.0.0 Mobile Safari/537.36");
    }

    /** Called from Python via Chaquopy. Blocks until the page loads (max 20s). */
    public String fetchUrlSync(final String url) {
        final CountDownLatch latch = new CountDownLatch(1);
        final String[] result = {""};

        new Handler(Looper.getMainLooper()).post(() ->  {
            fetchWebView.setWebViewClient(new WebViewClient() {
                private boolean done = false;

                @Override
                public void onPageFinished(WebView view, String u) {
                    if (done) return;
                    done = true;
                    view.evaluateJavascript("document.documentElement.outerHTML", value -> {
                        if (value != null) result[0] = value;
                        latch.countDown();
                    });
                }

                @Override
                public void onReceivedError(WebView view, int code, String desc, String failUrl) {
                    if (done) return;
                    done = true;
                    latch.countDown();
                }
            });
            fetchWebView.loadUrl(url);
        });

        try { latch.await(20, TimeUnit.SECONDS); }
        catch (InterruptedException e) { Thread.currentThread().interrupt(); }

        // evaluateJavascript returns a JSON string — unwrap the outer quotes and unescape
        String html = result[0];
        if (html.length() >= 2 && html.charAt(0) == '"') {
            try {
                html = new org.json.JSONArray("[" + html + "]").getString(0);
            } catch (Exception ignored) {}
        }
        return html;
    }

    // ── WebView ───────────────────────────────────────────────────────────────

    @SuppressLint("SetJavaScriptEnabled")
    private void setupWebView() {
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        webView.addJavascriptInterface(new AndroidBridge(), "Android");
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                return !r.getUrl().toString().startsWith("http://localhost");
            }
        });
    }

    // JS Bridge interface
    class AndroidBridge {
        @JavascriptInterface
        public String getWorkingDir() {
            return selectedFolderPath != null ? selectedFolderPath : "";
        }

        @JavascriptInterface
        public void setWorkingDir(String path) {
            selectedFolderPath = path;
            prefs.edit().putString("working_dir", path).apply();
        }

        @JavascriptInterface
        public void openFolderPicker() {
            Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
            startActivityForResult(intent, REQUEST_FOLDER_PICKER);
        }

        @JavascriptInterface
        public String listFiles(String path) {
            if (path == null || path.isEmpty()) {
                path = Environment.getExternalStorageDirectory().getAbsolutePath();
            }
            File dir = new File(path);
            if (!dir.exists() || !dir.isDirectory()) {
                return "[]";
            }
            List<String> files = new ArrayList<>();
            File[] items = dir.listFiles();
            if (items != null) {
                for (File f : items) {
                    files.add(f.getName() + (f.isDirectory() ? "/" : ""));
                }
            }
            return files.toString();
        }
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) webView.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQUEST_FOLDER_PICKER && resultCode == RESULT_OK && data != null) {
            Uri treeUri = data.getData();
            if (treeUri == null) return;
            
            // 1. Take persistent permission immediately
            getContentResolver().takePersistableUriPermission(treeUri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);

            // 2. Parse SAF URI path to bulletproof absolute POSIX path
            String path = treeUri.getPath();
            String authority = treeUri.getAuthority();

            if (path != null) {
                if ("com.android.providers.downloads.documents".equals(authority)) {
                    // Handles the case where user explicitly selects "Downloads" provider
                    selectedFolderPath = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS).getAbsolutePath();
                } 
                else if (path.contains(":")) {
                    // limit to 2 splits so folders with colons in name don't break
                    String[] split = path.split(":", 2); 
                    String type = split[0];
                    String relativePath = split.length > 1 ? split[1] : "";

                    if (type.endsWith("primary")) {
                        selectedFolderPath = Environment.getExternalStorageDirectory().getAbsolutePath();
                        if (!relativePath.isEmpty()) {
                            selectedFolderPath += "/" + relativePath;
                        }
                    } else {
                        // External SD card
                        String volumeId = type.substring(type.lastIndexOf('/') + 1);
                        selectedFolderPath = "/storage/" + volumeId;
                        if (!relativePath.isEmpty()) {
                            selectedFolderPath += "/" + relativePath;
                        }
                    }
                } else {
                    selectedFolderPath = path;
                }
            }

            prefs.edit().putString("working_dir", selectedFolderPath).apply();

            final String finalPath = selectedFolderPath;
            new Handler(Looper.getMainLooper()).postDelayed(() -> {
                if (webView != null) {
                    String escaped = finalPath.replace("\\", "\\\\").replace("'", "\\'");
                    webView.evaluateJavascript("setWorkingDir('" + escaped + "')", null);
                }
            }, 500);
        }
    }

    // ── Loading screen ────────────────────────────────────────────────────────

    private static final String LOADING_HTML =
        "<!DOCTYPE html><html><head>" +
        "<meta name='viewport' content='width=device-width, initial-scale=1'>" +
        "<style>" +
        "* { margin:0; padding:0; box-sizing:border-box; }" +
        "body { background:#0a0a0a; display:flex; align-items:center;" +
        "       justify-content:center; height:100vh; font-family:monospace; }" +
        ".wrap { text-align:center; color:#666; }" +
        ".title { font-size:20px; color:#fff; margin-bottom:12px; letter-spacing:2px; }" +
        ".dot { display:inline-block; width:6px; height:6px; border-radius:50%;" +
        "       background:#666; margin:0 3px; animation:pulse 1.2s infinite; }" +
        ".dot:nth-child(2) { animation-delay:.2s; }" +
        ".dot:nth-child(3) { animation-delay:.4s; }" +
        "@keyframes pulse { 0%,80%,100%{opacity:.2} 40%{opacity:1} }" +
        "</style></head><body>" +
        "<div class='wrap'>" +
        "<div class='title'>opencode</div>" +
        "<div><span class='dot'></span><span class='dot'></span><span class='dot'></span></div>" +
        "</div></body></html>";
}