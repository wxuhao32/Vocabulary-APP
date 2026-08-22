package com.example.cet4;

import android.Manifest;
import android.app.Activity;
import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.media.AudioManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;
import android.provider.Settings;
import android.util.Log;
import android.view.View;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import androidx.activity.ComponentActivity;
import androidx.activity.OnBackPressedCallback;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;

public class MainActivity extends ComponentActivity {

    private static final String TAG = "CET4";

    private WebView webView;
    private static final int REQ_PICK = 9001;
    private static final int REQ_PERM = 9002;
    private static final int REQ_ATTACH = 9003;
    private static final int REQ_FILECHOOSER = 9004;
    private static final int REQ_NOTIF = 9005;
    private static final int REQ_MODEL = 9006;
    /* v9.119：语音消息录音权限请求码 + 待放行的 WebView 权限请求（授权成功后 grant） */
    private static final int REQ_RECORD_AUDIO = 9007;
    private volatile android.webkit.PermissionRequest pendingPermissionRequest = null;

    // 待处理的导入回调 token（用于 SAF 选文件后回传 JS）
    private volatile String pendingPickToken = null;
    // 本地模型 SAF 选择回调 token
    private volatile String pendingModelToken = null;
    // 存储权限请求后的回传
    private volatile ValueCallback<Boolean> pendingPerm = null;
    // input[type=file] 的回调（WebChromeClient.onShowFileChooser）
    private volatile ValueCallback<Uri[]> filePathCallback = null;
    // AI 流式请求集合，用于取消
    private final List<HttpURLConnection> aiConns = new ArrayList<>();

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        webView = new WebView(this);
        setContentView(webView);

        /* v9.35：targetSdk 34 下 onBackPressed override 不再被系统调用（predictive back 走
           OnBackPressedDispatcher，默认行为直接 finish）→ 前端返回钩子完全失效，任何页面按返回都退出。
           MainActivity 改为继承 ComponentActivity（androidx），注册 OnBackPressedCallback 接管返回键，
           全 API 版本生效（不依赖 platform 33 的方法）。 */
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override public void handleOnBackPressed() { handleAppBack(); }
        });

        /* 沉浸式全屏：内容延伸到状态栏/刘海区域，状态栏透明 */
        if (Build.VERSION.SDK_INT >= 21) {
            getWindow().setStatusBarColor(Color.TRANSPARENT);
            int vis = View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN | View.SYSTEM_UI_FLAG_LAYOUT_STABLE;
            if (Build.VERSION.SDK_INT >= 23) vis |= View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR; // 深色状态栏图标（适配浅色内容）
            getWindow().getDecorView().setSystemUiVisibility(vis);
        }
        if (Build.VERSION.SDK_INT >= 28) {
            // 刘海屏：内容铺满刘海区域两侧
            getWindow().getAttributes().layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
        }
        /* v9.114：IME Insets 监听 —— 全屏沉浸模式下 adjustResize 在部分设备失效（键盘弹出页面不 resize），
           由原生层读取系统 IME 键盘高度并桥接给前端（window.__imeH），跨设备一致。 */
        bindImeInsets();

        WebSettings ws = webView.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setAllowFileAccess(true);
        ws.setAllowContentAccess(true);
        ws.setUseWideViewPort(true);
        ws.setLoadWithOverviewMode(true);
        ws.setCacheMode(WebSettings.LOAD_DEFAULT);
        ws.setBuiltInZoomControls(false);
        ws.setDisplayZoomControls(false);
        ws.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        ws.setAllowUniversalAccessFromFileURLs(true);
        /* v9.127：语音通话远端音频自动播放 —— WebView 默认要求"新鲜"用户手势，
           主叫发起通话的手势在等待接听期间过期 → ontrack 时 au.play() 被静默拒绝 → 主叫听不到对方。
           关闭该限制后通话音频（WebRTC 流）可随连接到达自动播放 */
        ws.setMediaPlaybackRequiresUserGesture(false);

        webView.setVerticalScrollBarEnabled(true);
        webView.setHorizontalScrollBarEnabled(false);

        // 关键：WebChromeClient 提供 input[type=file] 文件选择支持（onShowFileChooser）
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> filePath, FileChooserParams params) {
                Log.d(TAG, "onShowFileChooser: accept=" + (params == null ? "?" : (params.getAcceptTypes() == null ? "*" : String.join(",", params.getAcceptTypes()))));
                if (filePathCallback != null) {
                    filePathCallback.onReceiveValue(null);
                }
                filePathCallback = filePath;
                Intent intent = (params != null && params.createIntent() != null) ? params.createIntent() : new Intent(Intent.ACTION_GET_CONTENT);
                try {
                    startActivityForResult(Intent.createChooser(intent, "选择文件"), REQ_FILECHOOSER);
                } catch (Exception e) {
                    Log.e(TAG, "onShowFileChooser start fail", e);
                    filePathCallback = null;
                    return false;
                }
                return true;
            }

            /* v9.119：语音消息——WebView getUserMedia({audio:true}) 的权限请求放行。
               v9.121：已授权直接 grant（主线程）；未授权先请求运行时权限再 grant；
               grant 失败兜底 deny 并记日志，避免 WebView 请求悬挂/误判。 */
            @Override
            public void onPermissionRequest(final android.webkit.PermissionRequest request) {
                try {
                    boolean mic = false, cam = false;
                    for (String r : request.getResources()) {
                        if (android.webkit.PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r)) mic = true;
                        if (android.webkit.PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(r)) cam = true;
                    }
                    if (!mic && !cam) {
                        request.deny();
                        return;
                    }
                    /* v9.129：通话媒体（音频/视频）运行时权限——缺哪个补哪个，全通过才 grant */
                    java.util.List<String> need = new java.util.ArrayList<>();
                    if (mic && checkSelfPermission(android.Manifest.permission.RECORD_AUDIO)
                            != PackageManager.PERMISSION_GRANTED) {
                        need.add(android.Manifest.permission.RECORD_AUDIO);
                    }
                    if (cam && checkSelfPermission(android.Manifest.permission.CAMERA)
                            != PackageManager.PERMISSION_GRANTED) {
                        need.add(android.Manifest.permission.CAMERA);
                    }
                    Log.d(TAG, "onPermissionRequest mic=" + mic + " cam=" + cam
                            + " need=" + need + " res=" + java.util.Arrays.toString(request.getResources()));
                    if (need.isEmpty()) {
                        runOnUiThread(() -> {
                            try { request.grant(request.getResources()); }
                            catch (Throwable t) {
                                Log.e(TAG, "grant media fail", t);
                                try { request.deny(); } catch (Throwable ignored) {}
                            }
                        });
                    } else {
                        pendingPermissionRequest = request;
                        runOnUiThread(() -> requestPermissions(
                                need.toArray(new String[0]), REQ_RECORD_AUDIO));
                    }
                } catch (Throwable t) {
                    Log.e(TAG, "onPermissionRequest fail", t);
                    try { request.deny(); } catch (Throwable ignored) {}
                }
            }
        });

        try {
            webView.addJavascriptInterface(new Bridge(), "App");
            Log.d(TAG, "addJavascriptInterface OK");
        } catch (Exception e) {
            Log.e(TAG, "addJavascriptInterface FAIL", e);
        }
        webView.setWebViewClient(new WebViewClient());
        webView.loadUrl("file:///android_asset/index.html");

        /* v9.78：进程残留场景——Activity 重建（旋转/内存回收）或退出后重进但进程还活着时，
           LocalLlm 单例（Kotlin object）中的模型权重仍在内存，补启保活服务，让模型继续常驻。 */
        try { if (LocalLlm.INSTANCE.isLoaded()) startKeepAlive(); }
        catch (Throwable t) { Log.w(TAG, "onCreate keepalive restore fail", t); }

        /* v9.91：通知点击冷启动跳转（从系统通知栏进入 App） */
        try { ensureNotifChannel(); } catch (Throwable ignored) {}
        handleNotifTap(getIntent());

        /* v9.112：进程残留/重启恢复——已登录则启动消息通知服务（前台服务 + 原生 WS，
           保证 App 退到后台/WebView 不可用时系统通知栏推送依然工作） */
        try {
            String savedAccess = getSharedPreferences("cet4_auth", MODE_PRIVATE).getString("access", "");
            if (savedAccess != null && !savedAccess.isEmpty()) {
                if (Build.VERSION.SDK_INT >= 26) startForegroundService(new Intent(this, NotifyService.class));
                else startService(new Intent(this, NotifyService.class));
            }
        } catch (Throwable t) { Log.w(TAG, "onCreate NotifyService restore fail", t); }
    }

    /* ===================== 权限 ===================== */
    /* v9.91：系统通知权限（Android 13+ 需 POST_NOTIFICATIONS 运行时权限） */
    private static final String NOTIF_CHANNEL_ID = "cet4_social";

    private boolean hasNotifPermission() {
        return Build.VERSION.SDK_INT < 33
                || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
    }

    private void ensureNotifChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            try {
                NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                NotificationChannel ch = new NotificationChannel(
                        NOTIF_CHANNEL_ID, "好友与消息", NotificationManager.IMPORTANCE_HIGH);
                ch.setDescription("好友申请 / 新消息 / 文件通知");
                nm.createNotificationChannel(ch);
            } catch (Exception e) { Log.w(TAG, "ensureNotifChannel fail", e); }
        }
    }

    /* 通知点击 → 回到 App 并跳转对应页面（type: friend_request / new_message / file_message） */
    private void handleNotifTap(Intent intent) {
        if (intent == null || !intent.hasExtra("nt_type")) return;
        final String type = intent.getStringExtra("nt_type");
        final String pid = intent.getStringExtra("nt_pid");
        intent.removeExtra("nt_type");  // 防止重复触发
        new Handler(Looper.getMainLooper()).postDelayed(() -> {
            try {
                runJs("try{ window.__onNotifTap && window.__onNotifTap("
                        + org.json.JSONObject.quote(type == null ? "" : type) + ","
                        + org.json.JSONObject.quote(pid == null ? "" : pid) + "); }catch(e){}");
            } catch (Throwable ignored) {}
        }, 1200);  // 等待页面加载/就绪
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleNotifTap(intent);
    }

    private boolean hasWritePermission() {
        if (Build.VERSION.SDK_INT >= 33) return true; // 作用域存储，无需权限
        if (Build.VERSION.SDK_INT < 23) return true;  // 安装即授权
        return checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE)
                == PackageManager.PERMISSION_GRANTED;
    }

    void requestWritePermission(ValueCallback<Boolean> cb) {
        if (Build.VERSION.SDK_INT >= 33 || Build.VERSION.SDK_INT < 23) {
            cb.onReceiveValue(true);
            return;
        }
        pendingPerm = cb;
        final Activity act = this;
        runOnUiThread(() -> act.requestPermissions(
                new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE}, REQ_PERM));
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] perms, int[] res) {
        if (requestCode == REQ_PERM) {
            final boolean granted = res.length > 0 && res[0] == PackageManager.PERMISSION_GRANTED;
            final ValueCallback<Boolean> cb = pendingPerm;
            pendingPerm = null;
            if (cb != null) cb.onReceiveValue(granted);
        } else if (requestCode == REQ_NOTIF) {
            final boolean granted = res.length > 0 && res[0] == PackageManager.PERMISSION_GRANTED;
            runJs("window.__onNotifPerm(" + (granted ? "true" : "false") + ");");
        } else if (requestCode == REQ_RECORD_AUDIO) {
            /* v9.129：通话媒体权限（音频/摄像头，可能一次申请多个）——全部通过才 grant */
            boolean allGranted = res.length > 0;
            for (int r : res) { if (r != PackageManager.PERMISSION_GRANTED) { allGranted = false; break; } }
            Log.d(TAG, "REQ_RECORD_AUDIO result allGranted=" + allGranted
                    + " perms=" + java.util.Arrays.toString(perms));
            final android.webkit.PermissionRequest pr = pendingPermissionRequest;
            pendingPermissionRequest = null;
            if (pr != null) {
                if (allGranted) {
                    try { pr.grant(pr.getResources()); }
                    catch (Throwable t) {
                        Log.e(TAG, "grant call media fail", t);
                        try { pr.deny(); } catch (Throwable ignored) {}
                    }
                } else {
                    try { pr.deny(); } catch (Throwable ignored) {}
                }
            }
        } else {
            super.onRequestPermissionsResult(requestCode, perms, res);
        }
    }

    /* ===================== 每日背单词提醒（系统通知） =====================
       注意：ReminderReceiver 为独立顶层类（见 ReminderReceiver.java），
       manifest 需直接引用 .ReminderReceiver；内部类会导致广播无法送达。 */

    /* ===================== 文件选择（导入） ===================== */
    void pickFile(String token) {
        pendingPickToken = token;
        Log.d(TAG, "pickFile token=" + token);
        final Activity act = this;
        runOnUiThread(() -> {
            try {
                Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                intent.setType("*/*");
                intent.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{
                        "application/json", "text/csv", "text/plain",
                        "application/csv", "text/comma-separated-values"});
                startActivityForResult(Intent.createChooser(intent, "选择文件"), REQ_PICK);
            } catch (Exception e) {
                Log.e(TAG, "pickFile start fail", e);
                runJs("window.__onPicked(" + JSONObject.quote(token) + ",'','选择文件失败：" + JSONObject.quote(e.getMessage()) + ");");
            }
        });
    }

    /* ---------- 本地模型：SAF 选择 .litertlm/.gguf → 拷贝到外部私有目录 models ---------- */
    void pickModel(String token) {
        pendingModelToken = token;
        Log.d(TAG, "pickModel token=" + token);
        runOnUiThread(() -> {
            try {
                Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                intent.setType("*/*");
                intent.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{
                        "application/octet-stream", "application/gguf", "model/litertlm",
                        "application/x-gguf", "application/llm"});
                startActivityForResult(Intent.createChooser(intent, "选择本地模型（.litertlm / .gguf）"), REQ_MODEL);
            } catch (Exception e) {
                Log.e(TAG, "pickModel start fail", e);
                runJs("window.__onLlmImport(" + JSONObject.quote(token) + ",'','','启动选择器失败：" + JSONObject.quote(e.getMessage()) + ");");
            }
        });
    }

    /* 后台线程把 SAF Uri 拷贝到 <外置私有目录>/models/，完成后回调 JS */
    private void copyUriToModels(final Uri uri, final String token) {
        new Thread(() -> {
            try {
                File dir = new File(getExternalFilesDir(null), "models");
                if (!dir.exists()) dir.mkdirs();
                String name = queryDisplayName(uri);
                if (name == null || name.trim().isEmpty()) name = "model.litertlm";
                name = name.replaceAll("[\\\\/:*?\"<>|\\s]+", "_");
                File out = new File(dir, name);
                try (InputStream is = getContentResolver().openInputStream(uri);
                     OutputStream os = new FileOutputStream(out)) {
                    byte[] buf = new byte[1024 * 1024];
                    int n;
                    long total = 0;
                    while ((n = is.read(buf)) > 0) { os.write(buf, 0, n); total += n; }
                    Log.d(TAG, "model copied: " + name + " " + total + " bytes");
                }
                runJs("window.__onLlmImport(" + JSONObject.quote(token) + "," + JSONObject.quote(out.getAbsolutePath())
                        + "," + JSONObject.quote(name) + "," + out.length() + ");");
            } catch (Exception e) {
                Log.e(TAG, "copyUriToModels fail", e);
                runJs("window.__onLlmImport(" + JSONObject.quote(token) + ",'','','导入失败：" + JSONObject.quote(truncate(e.getMessage(), 200)) + ");");
            }
        }).start();
    }

    /* 扫描外部私有目录 models 下的模型文件，返回 JSON 数组 */
    private String scanModels() {
        try {
            File dir = new File(getExternalFilesDir(null), "models");
            JSONArray arr = new JSONArray();
            if (dir.exists()) {
                File[] fs = dir.listFiles();
                if (fs != null) {
                    for (File f : fs) {
                        String n = f.getName().toLowerCase();
                        if (n.endsWith(".litertlm") || n.endsWith(".gguf") || n.endsWith(".tflite") || f.isDirectory()) {
                            JSONObject o = new JSONObject();
                            o.put("name", f.getName());
                            o.put("path", f.getAbsolutePath());
                            o.put("size", f.length());
                            o.put("isDir", f.isDirectory());
                            arr.put(o);
                        }
                    }
                }
            }
            return arr.toString();
        } catch (Exception e) {
            return "[]";
        }
    }

    /* 本地模型状态 JSON：{loaded, path, backend, files:[...]} */
    private String llmStatusJson() {
        try {
            JSONObject o = new JSONObject();
            o.put("loaded", LocalLlm.INSTANCE.isLoaded());
            o.put("path", LocalLlm.INSTANCE.getLoadedPath() == null ? "" : LocalLlm.INSTANCE.getLoadedPath());
            o.put("backend", LocalLlm.INSTANCE.getBackendInfo());
            o.put("drafter", LocalLlm.INSTANCE.getDrafterDetected());   /* v9.42：MTP drafter 检测 */
            o.put("files", new JSONArray(scanModels()));
            return o.toString();
        } catch (Exception e) {
            return "{}";
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        Log.d(TAG, "onActivityResult req=" + requestCode + " res=" + resultCode + " data=" + (data == null ? "null" : data.getDataString()));
        if (requestCode == REQ_FILECHOOSER) {
            // input[type=file] 选择结果回填给 WebView
            ValueCallback<Uri[]> cb = filePathCallback;
            filePathCallback = null;
            if (cb != null) {
                Uri[] result = null;
                if (resultCode == RESULT_OK && data != null) {
                    if (data.getData() != null) {
                        result = new Uri[]{data.getData()};
                    } else if (data.getClipData() != null) {
                        int n = data.getClipData().getItemCount();
                        if (n > 0) {
                            result = new Uri[n];
                            for (int i = 0; i < n; i++) result[i] = data.getClipData().getItemAt(i).getUri();
                        }
                    }
                }
                cb.onReceiveValue(result);
            }
            return;
        }
        if (requestCode == REQ_PICK) {
            String token = pendingPickToken; pendingPickToken = null;
            if (resultCode != RESULT_OK || data == null || data.getData() == null) {
                runJs("window.__onPicked(" + (token == null ? "null" : JSONObject.quote(token)) + ",'','用户取消');");
                return;
            }
            Uri uri = data.getData();
            try {
                String name = queryDisplayName(uri);
                String content = readUriText(uri);
                runJs("window.__onPicked(" + (token == null ? "null" : JSONObject.quote(token))
                        + "," + JSONObject.quote(name) + "," + JSONObject.quote(content) + ");");
            } catch (Exception e) {
                runJs("window.__onPicked(" + (token == null ? "null" : JSONObject.quote(token))
                        + ",'','读取文件失败：' + " + JSONObject.quote(e.getMessage()) + ");");
            }
        } else if (requestCode == REQ_ATTACH) {
            String token = pendingPickToken; pendingPickToken = null;
            if (resultCode != RESULT_OK || data == null || data.getData() == null) {
                runJs("window.__onAttach(" + (token == null ? "null" : JSONObject.quote(token)) + ",'','',null,null);");
                return;
            }
            Uri uri = data.getData();
            try {
                String name = queryDisplayName(uri);
                String mime = getContentResolver().getType(uri);
                if (mime == null) mime = "application/octet-stream";
                boolean textLike = mime.startsWith("text/") || mime.contains("json") || mime.contains("csv");
                String payload = textLike ? readUriText(uri) : readUriBytesBase64(uri);
                if (payload == null) payload = "";
                if (textLike && payload.length() > 200000) payload = payload.substring(0, 200000);
                runJs("window.__onAttach(" + (token == null ? "null" : JSONObject.quote(token))
                        + "," + JSONObject.quote(name) + "," + JSONObject.quote(mime)
                        + "," + JSONObject.quote(payload) + ",null);");
            } catch (Exception e) {
                runJs("window.__onAttach(" + (token == null ? "null" : JSONObject.quote(token))
                        + ",'','',null,'读取失败：" + JSONObject.quote(truncate(e.getMessage(), 160)) + "');");
            }
        } else if (requestCode == REQ_MODEL) {
            String token = pendingModelToken; pendingModelToken = null;
            if (resultCode != RESULT_OK || data == null || data.getData() == null) {
                runJs("window.__onLlmImport(" + (token == null ? "null" : JSONObject.quote(token)) + ",'','','用户取消');");
                return;
            }
            Uri uri = data.getData();
            try {
                getContentResolver().takePersistableUriPermission(uri,
                        Intent.FLAG_GRANT_READ_URI_PERMISSION);
            } catch (Exception ignored) {}
            copyUriToModels(uri, token);
        }
    }

    private String queryDisplayName(Uri uri) {
        String r = null;
        try (android.database.Cursor c = getContentResolver().query(uri,
                new String[]{MediaStore.MediaColumns.DISPLAY_NAME}, null, null, null)) {
            if (c != null && c.moveToFirst()) {
                int i = c.getColumnIndex(MediaStore.MediaColumns.DISPLAY_NAME);
                if (i >= 0) r = c.getString(i);
            }
        } catch (Exception ignored) {}
        if (r == null) r = uri.getLastPathSegment();
        return r == null ? "unknown" : r;
    }

    private String readUriText(Uri uri) throws Exception {
        StringBuilder sb = new StringBuilder();
        try (InputStream in = getContentResolver().openInputStream(uri);
             BufferedReader br = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) {
            String line;
            while ((line = br.readLine()) != null) {
                sb.append(line).append("\n");
            }
        }
        return sb.toString();
    }

    private String readUriBytesBase64(Uri uri) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        try (InputStream in = getContentResolver().openInputStream(uri)) {
            byte[] buf = new byte[8192];
            int n; long total = 0;
            while ((n = in.read(buf)) != -1) {
                total += n;
                if (total > 5L * 1024 * 1024) throw new Exception("图片过大（>5MB），请压缩后重试");
                bos.write(buf, 0, n);
            }
        }
        return android.util.Base64.encodeToString(bos.toByteArray(), android.util.Base64.NO_WRAP);
    }

    /* 选择附件（图片或文件），用于查词时附带上传 */
    void pickAttach(String token) {
        pendingPickToken = token;
        Log.d(TAG, "pickAttach token=" + token);
        final Activity act = this;
        runOnUiThread(() -> {
            try {
                Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                intent.setType("*/*");
                intent.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{
                        "image/*", "application/json", "text/plain", "text/csv",
                        "application/pdf", "application/msword",
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                });
                startActivityForResult(Intent.createChooser(intent, "选择附件"), REQ_ATTACH);
            } catch (Exception e) {
                Log.e(TAG, "pickAttach start fail", e);
                runJs("window.__onAttach(" + JSONObject.quote(token) + ",'','',null,'选择附件失败：" + JSONObject.quote(e.getMessage()) + "');");
            }
        });
    }

    /* ===================== AI 流式 ===================== */
    /* 注意：JS Bridge 只暴露一个 8 参 aiStream（无重载）。
       Android JavascriptInterface 按方法名注册，同名重载会互相覆盖导致调用静默失败。
       云端深度思考由 JS 端 aiCloudThinkToggle → App.aiStream(...,think) 第 9 参控制
       v9.50：新增 aiStreamCtx（独立方法名，带会话历史）——云模型多轮上下文（修复 #1）。
       OCP：原 aiStream 保持不动，仅新增带 historyJson 的变体。 */
    void aiStream(final String token, final String endpoint, final String apiKey,
                  final String model, final String systemPrompt, final String userPrompt,
                  final String imgMime, final String imgB64, final boolean think) {
        aiStreamCtx(token, endpoint, apiKey, model, systemPrompt, null, userPrompt, imgMime, imgB64, think);
    }

    /* v9.50：带会话历史的流式推理（云模型多轮上下文）。
       historyJson: JSON 数组字符串，如 [{"role":"user","content":"..."},{"role":"assistant","content":"..."},...]
       历史按顺序插在 system 之后、本轮 user 之前；图片消息自动转 OpenAI 格式。 */
    void aiStreamCtx(final String token, final String endpoint, final String apiKey,
                     final String model, final String systemPrompt, final String historyJson,
                     final String userPrompt, final String imgMime, final String imgB64, final boolean think) {
        Log.d(TAG, "aiStreamCtx called token=" + token + " endpoint=" + (endpoint == null ? "null" : endpoint) + " model=" + model + " think=" + think + " history=" + (historyJson == null ? "null" : historyJson.length()) + " chars");
        String imgJson = null;
        // 附件兼容：文本模型（deepseek-chat 等）不支持 image_url，强制剥离图片，避免 HTTP 400
        boolean vision = isVisionModel(model);
        if (vision && imgMime != null && !imgMime.isEmpty() && imgB64 != null && !imgB64.isEmpty()) {
            imgJson = "{\"type\":\"image_url\",\"image_url\":{\"url\":\"data:" + imgMime + ";base64," + imgB64 + "\"}}";
        } else if (imgMime != null && !imgMime.isEmpty() && imgB64 != null && !imgB64.isEmpty()) {
            Log.w(TAG, "model " + model + " not vision-capable, stripping image to avoid HTTP 400");
        }
        doAiStream(token, endpoint, apiKey, model, systemPrompt, historyJson, userPrompt, imgJson, think);
    }

    /* 视觉能力判断：按模型名识别是否支持图片输入（与前端 isVisionModel 保持一致） */
    static boolean isVisionModel(String model) {
        if (model == null || model.isEmpty()) return false;
        String m = model.toLowerCase();
        return m.matches(".*(gpt-4o|gpt-4\\.|o1|o3|gpt-4-vision|vision|glm-4v|qwen-vl|qwen2\\.5-vl|internvl|minicpm-v|gemini|claude-3-5|claude-3-7|claude-4|kimi-latest|moonshot-v1-128k-vision|step-1v|step-3|doubao-1-5-vision|doubao-vision|hunyuan-vision|ernie-4\\.5-v|ernie-vil|baichuan-vision|deepseek-vl).*");
    }

    void doAiStream(final String token, final String endpoint, final String apiKey,
                    final String model, final String systemPrompt, final String historyJson,
                    final String userPrompt, final String imgJson, final boolean think) {
        new Thread(() -> {
            HttpURLConnection conn = null;
            try {
                URL url = new URL(endpoint);
                conn = (HttpURLConnection) url.openConnection();
                conn.setRequestMethod("POST");
                conn.setConnectTimeout(20000);
                conn.setReadTimeout(0); // 流式，不超时
                conn.setDoOutput(true);
                conn.setRequestProperty("Content-Type", "application/json");
                if (apiKey != null && !apiKey.isEmpty()) {
                    conn.setRequestProperty("Authorization", "Bearer " + apiKey);
                }
                synchronized (aiConns) { aiConns.add(conn); }

                JSONObject msgSys = new JSONObject().put("role", "system").put("content", systemPrompt);
                JSONObject msgUser = new JSONObject().put("role", "user");
                if (imgJson != null) {
                    JSONArray content = new JSONArray();
                    content.put(new JSONObject().put("type", "text").put("text", userPrompt));
                    content.put(new JSONObject(imgJson));
                    msgUser.put("content", content);
                } else {
                    msgUser.put("content", userPrompt);
                }
                JSONArray messages = new JSONArray();
                messages.put(msgSys);
                /* v9.50：插入会话历史（role/content 或带图片的 content 数组），实现云模型多轮上下文 */
                if (historyJson != null && !historyJson.isEmpty()) {
                    JSONArray hist = new JSONArray(historyJson);
                    for (int i = 0; i < hist.length(); i++) {
                        JSONObject m = hist.optJSONObject(i);
                        if (m == null) continue;
                        JSONObject mm = new JSONObject();
                        String role = m.optString("role", "user");
                        mm.put("role", "user".equals(role) || "assistant".equals(role) ? role : "user");
                        Object content = m.opt("content");
                        if (content instanceof JSONArray) {
                            mm.put("content", content);
                        } else {
                            mm.put("content", String.valueOf(content == null ? "" : content));
                        }
                        messages.put(mm);
                    }
                }
                messages.put(msgUser);
                JSONObject body = new JSONObject()
                        .put("model", model)
                        .put("messages", messages)
                        .put("stream", true);
                /* 云模型深度思考：think=true 时按模型类型注入官方思考参数 */
                if (think) injectThinkingParams(body, model);
                byte[] out = body.toString().getBytes(StandardCharsets.UTF_8);
                try (OutputStream os = conn.getOutputStream()) { os.write(out); }

                int code = conn.getResponseCode();
                InputStream in = code >= 200 && code < 300 ? conn.getInputStream() : conn.getErrorStream();
                if (code < 200 || code >= 300) {
                    String err = readAll(in);
                    Log.e(TAG, "aiStream HTTP " + code + " " + truncate(err, 200));
                    runJs("window.__onAiEnd(" + JSONObject.quote(token) + ",'HTTP " + code + " " + JSONObject.quote(truncate(err, 200)) + "');");
                    return;
                }
                Log.d(TAG, "aiStream HTTP " + code + " OK, streaming...");

                // 兼容 SSE（data: 行）与纯 JSON（部分兼容端点忽略 stream:true）
                BufferedReader br = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
                String line;
                boolean sawData = false;
                boolean done = false;
                while (!done && (line = br.readLine()) != null) {
                    line = line.trim();
                    if (line.isEmpty()) continue;
                    if (line.startsWith("data:")) {
                        sawData = true;
                        String data = line.substring(5).trim();
                        if (data.isEmpty() || "[DONE]".equals(data)) continue;
                        emitContent(token, data);
                    } else if (!sawData && (line.startsWith("{") || line.startsWith("["))) {
                        emitContent(token, line);
                        done = true;
                    }
                }
                runJs("window.__onAiEnd(" + JSONObject.quote(token) + ",'');");
                Log.d(TAG, "aiStream stream ended ok");
            } catch (java.io.IOException ioe) {
                Log.e(TAG, "aiStream IOException: " + ioe.getMessage());
                if ("cancel".equals(ioe.getMessage())) {
                    runJs("window.__onAiEnd(" + JSONObject.quote(token) + ",'已中断');");
                } else {
                    runJs("window.__onAiEnd(" + JSONObject.quote(token) + ",'网络错误：" + JSONObject.quote(truncate(ioe.getMessage(), 160)) + "');");
                }
            } catch (Exception e) {
                Log.e(TAG, "aiStream Exception: " + e.getMessage());
                runJs("window.__onAiEnd(" + JSONObject.quote(token) + ",'错误：" + JSONObject.quote(truncate(e.getMessage(), 160)) + "');");
            } finally {
                if (conn != null) {
                    synchronized (aiConns) { aiConns.remove(conn); }
                    try { conn.disconnect(); } catch (Exception ignored) {}
                }
            }
        }).start();
    }

    private void emitContent(final String token, final String data) {
        if (data == null || data.isEmpty()) return;
        try {
            JSONObject obj = new JSONObject(data);
            emitFromChoices(token, obj.optJSONArray("choices"));
            /* 用量统计：OpenAI 兼容 SSE 最后一条通常带 usage（choices 为空数组） */
            JSONObject usage = obj.optJSONObject("usage");
            if (usage != null) emitUsage(token, usage);
            return;
        } catch (JSONException je) { /* 可能不是对象，继续尝试数组/纯文本 */ }
        try {
            JSONArray arr = new JSONArray(data);
            if (arr.length() > 0) {
                JSONObject obj = arr.optJSONObject(0);
                if (obj != null) emitFromChoices(token, obj.optJSONArray("choices"));
            }
            return;
        } catch (JSONException je2) { /* 纯文本兜底 */ }
        if (!data.startsWith("[")) {
            runJs("window.__onAiChunk(" + JSONObject.quote(token) + "," + JSONObject.quote(data) + ");");
        }
    }

    /* 用量统计：解析 usage 字段（输入/输出/总 tokens + 缓存命中），回调前端记录 */
    private void emitUsage(final String token, final JSONObject usage) {
        try {
            int prompt = usage.optInt("prompt_tokens", 0);
            int completion = usage.optInt("completion_tokens", 0);
            int total = usage.optInt("total_tokens", 0);
            int cacheHit = 0, cacheMiss = 0;
            /* DeepSeek/部分平台：prompt_cache_hit_tokens / prompt_cache_miss_tokens */
            cacheHit = usage.optInt("prompt_cache_hit_tokens", 0);
            cacheMiss = usage.optInt("prompt_cache_miss_tokens", 0);
            /* Anthropic 风格：cache_read_input_tokens / cache_creation_input_tokens */
            if (cacheHit == 0 && cacheMiss == 0) {
                cacheHit = usage.optInt("cache_read_input_tokens", 0);
                cacheMiss = usage.optInt("cache_creation_input_tokens", 0);
            }
            JSONObject u = new JSONObject()
                    .put("prompt", prompt).put("completion", completion).put("total", total)
                    .put("cacheHit", cacheHit).put("cacheMiss", cacheMiss);
            runJs("window.__onAiUsage(" + JSONObject.quote(token) + "," + u.toString() + ");");
        } catch (Exception e) { /* 忽略 usage 解析失败 */ }
    }

    private void emitFromChoices(final String token, final JSONArray choices) {
        if (choices == null || choices.length() == 0) return;
        JSONObject ch = choices.optJSONObject(0);
        if (ch == null) return;
        String piece = null;
        String reasoning = null;
        JSONObject delta = ch.optJSONObject("delta");
        // 注意：Android org.json 的 optString 对 JSON null 会返回字符串 "null"，必须用 isNull 过滤
        /* 1. 思维链（OpenAI o 系列 / DeepSeek R1 用 reasoning_content；Anthropic 风格部分平台用 reasoning） */
        if (delta != null){
            if (!delta.isNull("reasoning_content")) reasoning = delta.optString("reasoning_content", "");
            else if (!delta.isNull("reasoning")) reasoning = delta.optString("reasoning", "");
        }
        if (reasoning == null || reasoning.isEmpty() || "null".equals(reasoning)){
            JSONObject message = ch.optJSONObject("message");
            if (message != null){
                if (!message.isNull("reasoning_content")) reasoning = message.optString("reasoning_content", "");
                else if (!message.isNull("reasoning")) reasoning = message.optString("reasoning", "");
            }
        }
        if (reasoning != null && !reasoning.isEmpty() && !"null".equals(reasoning) && !"undefined".equals(reasoning)) {
            runJs("window.__onAiReasoning(" + JSONObject.quote(token) + "," + JSONObject.quote(reasoning) + ");");
        }
        /* 2. 答案正文 */
        if (delta != null && !delta.isNull("content")) {
            piece = delta.optString("content", "");
        }
        if (piece == null || piece.isEmpty() || "null".equals(piece) || "undefined".equals(piece)) {
            JSONObject message = ch.optJSONObject("message");
            if (message != null && !message.isNull("content")) {
                piece = message.optString("content", "");
            }
        }
        if (piece == null || piece.isEmpty() || "null".equals(piece) || "undefined".equals(piece)) {
            piece = ch.optString("text", "");
        }
        if (piece != null && !piece.isEmpty() && !"null".equals(piece) && !"undefined".equals(piece)) {
            runJs("window.__onAiChunk(" + JSONObject.quote(token) + "," + JSONObject.quote(piece) + ");");
        }
    }

    void aiCancel(final String token) {
        synchronized (aiConns) {
            for (HttpURLConnection c : aiConns) {
                try { c.disconnect(); } catch (Exception ignored) {}
            }
            aiConns.clear();
        }
    }

    private static String readAll(InputStream in) throws Exception {
        BufferedReader br = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
        StringBuilder sb = new StringBuilder(); String l;
        while ((l = br.readLine()) != null) sb.append(l);
        return sb.toString();
    }

    /* 云模型深度思考：根据模型名注入官方思考参数（OpenAI o1/o3 reasoning_effort / Anthropic thinking / DeepSeek R1 / Qwen3-thinking 等） */
    private static void injectThinkingParams(JSONObject body, String model) {
        if (body == null || model == null) return;
        String m = model.toLowerCase();
        try {
            if (m.matches(".*\\b(o1|o3|o4|gpt-5)\\b.*") || m.contains("sonar-reasoning") || m.contains("magistral") || m.contains("seed-reasoning") || m.contains("gemini-2.5")) {
                body.put("reasoning_effort", "medium");
            } else if (m.contains("deepseek-r1") || m.contains("deepseek-reasoner") || m.contains("kimi-thinking") || m.contains("kimi-k1.5")) {
                /* 这些模型默认就在思考，不需要额外参数 */
            } else if (m.contains("claude-3-7") || m.contains("claude-4") || m.contains("glm-z1") || m.contains("glm-4.5")) {
                body.put("thinking", new JSONObject().put("type", "enabled").put("budget_tokens", 2048));
            } else if (m.contains("qwq") || m.contains("qwen3-thinking") || m.contains("qwen3-235b-thinking") || m.contains("qwen-reasoner")) {
                body.put("enable_thinking", true);
            } else if (m.contains("thinking") || m.contains("reasoning")) {
                /* 兜底：通用 reasoning_effort，不支持的平台会忽略未知字段 */
                body.put("reasoning_effort", "medium");
            }
        } catch (Exception ignored) {}
    }

    private static String truncate(String s, int n) {
        if (s == null) return "";
        return s.length() > n ? s.substring(0, n) : s;
    }

    private void runJs(final String js) {
        new Handler(Looper.getMainLooper()).post(() -> webView.evaluateJavascript(js, null));
    }

    /* v9.114：读取系统 IME 键盘高度并桥接前端（解决全屏 WebView 下部分设备键盘弹出不 resize） */
    private int lastImeH = -1;
    private void bindImeInsets() {
        try {
            final View decor = getWindow().getDecorView();
            decor.getViewTreeObserver().addOnGlobalLayoutListener(() -> {
                try {
                    int ime = 0;
                    WindowInsetsCompat wic = ViewCompat.getRootWindowInsets(decor);
                    if (wic != null) ime = wic.getInsets(WindowInsetsCompat.Type.ime()).bottom;
                    if (ime > 0 && ime != lastImeH) {
                        lastImeH = ime;
                        runJs("try{ window.__imeH && window.__imeH(" + ime + "); }catch(e){}");
                    } else if (ime == 0 && lastImeH > 0) {
                        lastImeH = 0;
                        runJs("try{ window.__imeH && window.__imeH(0); }catch(e){}");
                    }
                } catch (Throwable ignored) {}
            });
        } catch (Throwable ignored) {}
    }

    /* ===================== JS Bridge ===================== */
    public class Bridge {

        @JavascriptInterface
        public String appPing() {
            // 供 JS 诊断面板探测桥接是否可达 + 反射列出真实方法签名
            StringBuilder sb = new StringBuilder("pong v2.6 sdk=").append(Build.VERSION.SDK_INT).append(" ").append(android.os.Build.MODEL);
            try {
                java.lang.reflect.Method[] ms = Bridge.class.getDeclaredMethods();
                sb.append(" | methods:");
                for (java.lang.reflect.Method m : ms) {
                    if (m.isAnnotationPresent(JavascriptInterface.class)) {
                        sb.append(" ").append(m.getName()).append("(").append(m.getParameterCount()).append(")");
                    }
                }
            } catch (Exception e) {
                sb.append(" | reflFail:").append(e);
            }
            return sb.toString();
        }

        @JavascriptInterface
        public String getExportDir() {
            File dir = getExportDirFile();
            return dir.getAbsolutePath();
        }

        @JavascriptInterface
        public String saveFile(String name, String mime, String base64) {
            try {
                byte[] data = android.util.Base64.decode(base64, android.util.Base64.DEFAULT);
                if (Build.VERSION.SDK_INT >= 29) {
                    // 作用域存储：写入公共 Download/CET4
                    String path = "Download/CET4";
                    ContentValuesCompat cv = new ContentValuesCompat();
                    cv.put(MediaStore.Downloads.DISPLAY_NAME, name);
                    cv.put(MediaStore.Downloads.MIME_TYPE, mime == null ? "application/octet-stream" : mime);
                    cv.put(MediaStore.Downloads.RELATIVE_PATH, path);
                    Uri collection = MediaStore.Downloads.EXTERNAL_CONTENT_URI;
                    Uri uri = getContentResolver().insert(collection, cv.values());
                    if (uri == null) throw new Exception("无法创建文件");
                    try (OutputStream os = getContentResolver().openOutputStream(uri)) {
                        os.write(data);
                    }
                    JSONObject r = new JSONObject();
                    r.put("ok", true);
                    r.put("path", new File(Environment.getExternalStorageDirectory(), "Download/CET4/" + name).getAbsolutePath());
                    r.put("scoped", true);
                    return r.toString();
                } else {
                    File dir = new File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), "CET4");
                    dir.mkdirs();
                    File f = new File(dir, name);
                    if (hasWritePermission()) {
                        try (FileOutputStream fos = new FileOutputStream(f)) { fos.write(data); }
                        JSONObject r = new JSONObject();
                        r.put("ok", true); r.put("path", f.getAbsolutePath()); r.put("scoped", false);
                        return r.toString();
                    } else {
                        // 无权限：回退到应用私有目录，并触发权限申请（供下次使用）
                        requestWritePermissionSilent();
                        File appDir = getExportDirFile();
                        appDir.mkdirs();
                        File af = new File(appDir, name);
                        try (FileOutputStream fos = new FileOutputStream(af)) { fos.write(data); }
                        JSONObject r = new JSONObject();
                        r.put("ok", true); r.put("path", af.getAbsolutePath());
                        r.put("scoped", false); r.put("needPermission", true);
                        return r.toString();
                    }
                }
            } catch (Exception e) {
                JSONObject r = new JSONObject();
                try { r.put("ok", false); r.put("error", e.getMessage()); } catch (JSONException ignored) {}
                return r.toString();
            }
        }

        @JavascriptInterface
        public String readAsset(String name) {
            // 读取 assets 内置文件（如 kaoyan.json 词书），返回文本
            try {
                InputStream is = getAssets().open(name);
                BufferedReader br = new BufferedReader(new InputStreamReader(is, StandardCharsets.UTF_8));
                StringBuilder sb = new StringBuilder();
                String l;
                while ((l = br.readLine()) != null) sb.append(l).append("\n");
                br.close();
                Log.d(TAG, "readAsset ok: " + name + " len=" + sb.length());
                return sb.toString();
            } catch (Exception e) {
                Log.e(TAG, "readAsset fail: " + name, e);
                return null;
            }
        }

        @JavascriptInterface
        public void pickFile(String token) {
            try {
                Log.d(TAG, "Bridge.pickFile called token=" + token);
                // 注意：必须用 MainActivity.this 显式调用外层方法，否则裸调用会递归调用本 Bridge 方法 → StackOverflowError
                MainActivity.this.pickFile(token);
            } catch (Throwable t) {
                Log.e(TAG, "Bridge.pickFile EX", t);
                runJs("window.__onPicked(" + JSONObject.quote(token) + ",'','Java异常: " + JSONObject.quote(String.valueOf(t)) + "');");
            }
        }

        @JavascriptInterface
        public void pickAttach(String token) {
            try {
                Log.d(TAG, "Bridge.pickAttach called token=" + token);
                MainActivity.this.pickAttach(token);
            } catch (Throwable t) {
                Log.e(TAG, "Bridge.pickAttach EX", t);
                runJs("window.__onAttach(" + JSONObject.quote(token) + ",'','',null,'Java异常: " + JSONObject.quote(String.valueOf(t)) + "');");
            }
        }

        @JavascriptInterface
        public void requestStoragePermission() {
            try {
                Log.d(TAG, "Bridge.requestStoragePermission called");
                requestWritePermission(granted -> runJs("window.__onPerm(" + (granted ? "true" : "false") + ");"));
            } catch (Throwable t) {
                Log.e(TAG, "Bridge.requestStoragePermission EX", t);
                runJs("window.__onPerm(false);");
            }
        }

        @JavascriptInterface
        public void aiStream(String token, String endpoint, String apiKey, String model, String systemPrompt, String userPrompt, String imgMime, String imgB64, boolean think) {
            try {
                Log.d(TAG, "Bridge.aiStream called token=" + token + " think=" + think);
                MainActivity.this.aiStream(token, endpoint, apiKey, model, systemPrompt, userPrompt, imgMime, imgB64, think);
            } catch (Throwable t) {
                Log.e(TAG, "Bridge.aiStream EX", t);
                runJs("window.__onAiEnd(" + JSONObject.quote(token) + ",'Java异常: " + JSONObject.quote(String.valueOf(t)) + "');");
            }
        }

        /* v9.50：带会话历史的流式推理（云模型多轮上下文）。OCP——只新增方法名，不动 aiStream。
           historyJson: JSON 数组字符串 [{"role":"user|assistant","content":"..."},...] */
        @JavascriptInterface
        public void aiStreamCtx(String token, String endpoint, String apiKey, String model, String systemPrompt, String historyJson, String userPrompt, String imgMime, String imgB64, boolean think) {
            try {
                Log.d(TAG, "Bridge.aiStreamCtx called token=" + token + " think=" + think + " hist=" + (historyJson == null ? "null" : historyJson.length()));
                MainActivity.this.aiStreamCtx(token, endpoint, apiKey, model, systemPrompt, historyJson, userPrompt, imgMime, imgB64, think);
            } catch (Throwable t) {
                Log.e(TAG, "Bridge.aiStreamCtx EX", t);
                runJs("window.__onAiEnd(" + JSONObject.quote(token) + ",'Java异常: " + JSONObject.quote(String.valueOf(t)) + "');");
            }
        }

        @JavascriptInterface
        public void aiCancel(String token) {
            try {
                Log.d(TAG, "Bridge.aiCancel called token=" + token);
                MainActivity.this.aiCancel(token);
            } catch (Throwable t) {
                Log.e(TAG, "Bridge.aiCancel EX", t);
            }
        }

        /* v9.35：前端返回钩子需要退出时调此方法——不依赖 evaluateJavascript 返回值
           （predictive back/JS 异常场景下返回值不可靠，直接走原生 finish 最稳） */
        @JavascriptInterface
        public void appExit() {
            try {
                runOnUiThread(() -> finish());
            } catch (Throwable t) {
                Log.e(TAG, "Bridge.appExit EX", t);
            }
        }

        @JavascriptInterface
        public void setReminder(int hour, int minute, boolean enabled) {
            Log.d(TAG, "setReminder hour=" + hour + " min=" + minute + " enabled=" + enabled);
            try {
                SharedPreferences sp = getSharedPreferences("cet4_remind", MODE_PRIVATE);
                sp.edit().putInt("hour", hour).putInt("minute", minute).putBoolean("enabled", enabled).apply();
                if (enabled) {
                    ReminderReceiver.ensureChannel(MainActivity.this);
                    ReminderReceiver.scheduleReminder(MainActivity.this, hour, minute);
                } else {
                    ReminderReceiver.cancelReminder(MainActivity.this);
                }
            } catch (Throwable t) {
                Log.e(TAG, "setReminder EX", t);
            }
        }

        @JavascriptInterface
        public void requestNotificationPermission() {
            try {
                if (Build.VERSION.SDK_INT >= 33) {
                    final Activity act = MainActivity.this;
                    runOnUiThread(() -> act.requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIF));
                } else {
                    runJs("window.__onNotifPerm(true);");
                }
            } catch (Throwable t) {
                Log.e(TAG, "requestNotificationPermission EX", t);
                runJs("window.__onNotifPerm(false);");
            }
        }

        @JavascriptInterface
        public boolean canExactAlarm() {
            try {
                if (Build.VERSION.SDK_INT < 31) return true;
                AlarmManager am = (AlarmManager) getSystemService(ALARM_SERVICE);
                return am != null && am.canScheduleExactAlarms();
            } catch (Throwable t) { return false; }
        }

        @JavascriptInterface
        public void setStatusBarStyle(boolean darkIcons) {
            try {
                if (Build.VERSION.SDK_INT >= 23) {
                    View decor = getWindow().getDecorView();
                    int vis = decor.getSystemUiVisibility();
                    if (darkIcons) vis |= View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
                    else vis &= ~View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
                    decor.setSystemUiVisibility(vis);
                }
            } catch (Throwable t) { Log.e(TAG, "setStatusBarStyle EX", t); }
        }

        @JavascriptInterface
        public void requestExactAlarm() {
            try {
                if (Build.VERSION.SDK_INT >= 31) {
                    AlarmManager am = (AlarmManager) getSystemService(ALARM_SERVICE);
                    if (am != null && !am.canScheduleExactAlarms()) {
                        Intent i = new Intent(android.provider.Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM,
                                Uri.parse("package:" + getPackageName()));
                        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                        startActivity(i);
                    }
                }
            } catch (Throwable t) { Log.e(TAG, "requestExactAlarm EX", t); }
        }

        private void requestWritePermissionSilent() {
            if (Build.VERSION.SDK_INT < 23 || Build.VERSION.SDK_INT >= 33) return;
            if (hasWritePermission()) return;
            final Activity act = MainActivity.this;
            runOnUiThread(() -> act.requestPermissions(
                    new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE}, REQ_PERM + 1));
        }

        /* ============ 本地模型（LiteRT-LM） ============ */

        @JavascriptInterface
        public void llmImport(String token) {
            MainActivity.this.pickModel(token);
        }

        @JavascriptInterface
        public String llmScan() {
            return MainActivity.this.scanModels();
        }

        @JavascriptInterface
        public String llmStatus() {
            return MainActivity.this.llmStatusJson();
        }

        @JavascriptInterface
        /* v9.16：第 5 参 ctxLen（上下文窗口 1K~32K）、第 6 参 thinkBudget（思考预算）由用户滑条控制
           v9.17：Boolean/Integer 包装类改原始类型 boolean/int——WebView JS→Java 桥对包装类
           布尔转换不可靠（部分内核收到 null/false），这正是本地思维链从 v9.12 起一直没激活的根因
           （云端 aiStream 一直用 boolean 原始类型所以正常）。JS 侧调用点均传满 6 参。 */
        public void llmLoad(String path, String token, String sys, boolean think, int ctxLen, int thinkBudget) {
            new Thread(() -> {
                int ctx = (ctxLen < 1024) ? 4096 : Math.min(ctxLen, 32768);
                int bud = (thinkBudget < 0) ? 2048 : Math.min(thinkBudget, 8192);
                String r = LocalLlm.INSTANCE.load(
                        path == null ? "" : path,
                        ctx, 0.7,
                        (sys == null || sys.isEmpty()) ? null : sys,
                        think,
                        bud);
                /* v9.78：加载成功 → 启动保活前台服务（模型常驻内存，防切后台被系统回收）。
                   load() 幂等命中（模型本就在内存）同样保证服务在跑。 */
                if ("OK".equals(r) && LocalLlm.INSTANCE.isLoaded()) {
                    MainActivity.this.startKeepAlive();
                }
                runJs("window.__onLlmLoad(" + JSONObject.quote(token) + "," + JSONObject.quote(r) + ");");
            }).start();
        }

        /* 热切换系统提示词（深度思考开关等），模型已加载时无需重载权重
           v9.17：Boolean → boolean（WebView 桥包装类布尔转换不可靠） */
        @JavascriptInterface
        public void llmSetSystem(String sys, boolean think) {
            new Thread(() -> {
                LocalLlm.INSTANCE.setSystem(sys == null ? "" : sys, think);
            }).start();
        }

        /* v9.41：推测性解码（MTP drafter）开关——前端设置页切换时调用，Engine 下次加载生效 */
        @JavascriptInterface
        public void llmSetSpec(boolean enable) {
            new Thread(() -> {
                LocalLlm.INSTANCE.setSpeculative(enable);
            }).start();
        }

        /* v9.48：跨轮上下文保留模式（修复 #1：模型无真实上下文）——前端设置页切换。
           true=跨轮记忆（推荐），false=v9.47 旧行为（每次重置）。 */
        @JavascriptInterface
        public void llmSetContextKeep(boolean keep) {
            try {
                LocalLlm.INSTANCE.setContextKeep(keep);
            } catch (Throwable t) {
                Log.e(TAG, "Bridge.llmSetContextKeep EX", t);
            }
        }

        /* v9.67：热更新上下文窗口/输出预算（模型已加载时改滑条立即生效，无需重新加载权重） */
        @JavascriptInterface
        public void llmSetCtxLen(int ctxLen) {
            try {
                LocalLlm.INSTANCE.setCtxLen(ctxLen);
            } catch (Throwable t) {
                Log.e(TAG, "Bridge.llmSetCtxLen EX", t);
            }
        }

        /* v9.67：热更新思考预算（与 setCtxLen 同机制） */
        @JavascriptInterface
        public void llmSetThinkBudget(int budget) {
            try {
                LocalLlm.INSTANCE.setThinkBudget(budget);
            } catch (Throwable t) {
                Log.e(TAG, "Bridge.llmSetThinkBudget EX", t);
            }
        }

        /* v9.74：热更新单次输出上限（独立于上下文窗口）——写论文/长文时调大，防止 8192 截断 */
        @JavascriptInterface
        public void llmSetMaxOutput(int maxOutput) {
            try {
                LocalLlm.INSTANCE.setMaxOutput(maxOutput);
            } catch (Throwable t) {
                Log.e(TAG, "Bridge.llmSetMaxOutput EX", t);
            }
        }

        /* v9.48：清空本地上下文（前端「清空上下文」按钮）——丢弃当前 Conversation 的全部历史，下次推理从头开始。
           当前正在进行的请求不会被中止（仅下次发送生效），与 cancel 严格区分。 */
        @JavascriptInterface
        public void llmResetContext() {
            try {
                LocalLlm.INSTANCE.resetContext();
            } catch (Throwable t) {
                Log.e(TAG, "Bridge.llmResetContext EX", t);
            }
        }

        @JavascriptInterface
        public void llmComplete(String prompt, int maxTokens, String token) {
            new Thread(() -> {
                String txt = LocalLlm.INSTANCE.complete(prompt == null ? "" : prompt);
                runJs("window.__onLlmEnd(" + JSONObject.quote(token) + "," + JSONObject.quote(txt) + ");");
            }).start();
        }

        /* 流式推理：增量回调 __onLlmChunk，完成回调 __onLlmEnd（体验快，像 Edge 逐字输出）
           v9.16：第 4 参 think 直接传入——之前前端先调 llmSetSystem 再调 llmCompleteStream 是
           两个独立异步线程，存在竞态（completeStream 可能读到旧的 thinkingEnabled=false）。
           现在 think 随请求直传，彻底消除竞态。 */
        @JavascriptInterface
        public void llmCompleteStream(String prompt, int maxTokens, String token, boolean think) {
            new Thread(() -> {
                LocalLlm.INSTANCE.completeStream(
                        prompt == null ? "" : prompt,
                        think,
                        piece -> { runJs("window.__onLlmChunk(" + JSONObject.quote(token) + "," + JSONObject.quote(piece) + ");"); return kotlin.Unit.INSTANCE; },
                        full -> { runJs("window.__onLlmEnd(" + JSONObject.quote(token) + "," + JSONObject.quote(full) + ");"); return kotlin.Unit.INSTANCE; });
            }).start();
        }

        @JavascriptInterface
        public void llmUnload() {
            LocalLlm.INSTANCE.unload();
            /* v9.78：模型卸载 → 停止保活前台服务（不再需要常驻） */
            MainActivity.this.stopKeepAlive();
        }

        /* v9.78：本地模型保活开关（前端设置页「模型保活」）。
           true=前台服务保活（模型常驻内存防回收）；false=关闭保活。
           切换立即生效；关闭保活只停服务，不卸载模型。 */
        @JavascriptInterface
        public void llmKeepAlive(boolean enable) {
            try {
                getSharedPreferences(PREFS_KEEPALIVE, MODE_PRIVATE)
                        .edit().putBoolean(KEY_KEEPALIVE, enable).apply();
                if (enable) {
                    if (LocalLlm.INSTANCE.isLoaded()) startKeepAlive();
                } else {
                    stopKeepAlive();
                }
            } catch (Throwable t) {
                Log.e(TAG, "Bridge.llmKeepAlive EX", t);
            }
        }

        /* 返回当前保活开关状态（前端设置面板恢复显示用） */
        @JavascriptInterface
        public boolean llmKeepAliveStatus() {
            try { return keepAliveEnabled(); }
            catch (Throwable t) { return true; }
        }

        /* 强制停止本地模型当前推理（前端「停止」按钮 → cancelProcess + 释放 generating） */
        @JavascriptInterface
        public void llmCancel() {
            try {
                LocalLlm.INSTANCE.cancel();
            } catch (Throwable t) {
                Log.e(TAG, "Bridge.llmCancel EX", t);
            }
        }

        /* 导出 native 侧 LLM 全链路诊断日志（前端一键复制贴给开发者） */
        @JavascriptInterface
        public String llmLogDump() {
            try {
                return LocalLlm.INSTANCE.logDump();
            } catch (Throwable t) {
                return "llmLogDump EX: " + t.getMessage();
            }
        }

        /* v9.77：返回 App 真实安装版本号（PackageInfo）——用于设置面板显示，
           解决"手机系统设置显示的版本与所装 APK 不一致"的困惑 */
        @JavascriptInterface
        public String llmAppVersion() {
            try {
                android.content.pm.PackageInfo pi = getPackageManager().getPackageInfo(getPackageName(), 0);
                return pi.versionName + " (code " + pi.versionCode + ")";
            } catch (Throwable t) {
                return "unknown (" + t.getMessage() + ")";
            }
        }

        /* v9.97：应用版本更新——下载新版 APK 到公共下载目录（系统通知栏显示进度，完成后可点击安装）。 */
        @JavascriptInterface
        public String downloadApk(String url, String title) {
            try {
                android.app.DownloadManager dm = (android.app.DownloadManager) getSystemService(DOWNLOAD_SERVICE);
                android.app.DownloadManager.Request req = new android.app.DownloadManager.Request(android.net.Uri.parse(url));
                req.setTitle("CET4Prep " + (title == null ? "" : title));
                req.setDescription("新版 APK 下载中…");
                req.setNotificationVisibility(android.app.DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                req.setAllowedOverMetered(true);
                req.setAllowedOverRoaming(false);
                String fname = "CET4Prep-" + (title == null ? "update" : title.replaceAll("[^0-9A-Za-z.]", "_")) + ".apk";
                req.setDestinationInExternalPublicDir(android.os.Environment.DIRECTORY_DOWNLOADS, fname);
                dm.enqueue(req);
                return "ok";
            } catch (Throwable t) {
                Log.w(TAG, "downloadApk fail", t);
                return "fail:" + t.getMessage();
            }
        }

        /* v9.97：用系统 Intent 打开外部链接（浏览器下载兜底）。 */
        @JavascriptInterface
        public void openUrl(String url) {
            try {
                Intent i = new Intent(Intent.ACTION_VIEW, android.net.Uri.parse(url));
                i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(i);
            } catch (Throwable t) {
                Log.w(TAG, "openUrl fail", t);
            }
        }

        /* ===================== v9.91 系统通知（好友/消息） ===================== */

        /* 请求系统通知权限（Android 13+；首次使用引导）。结果回调 window.__onNotifPerm(bool) */
        @JavascriptInterface
        public void requestNotifPermission() {
            final Activity act = MainActivity.this;
            runOnUiThread(() -> {
                try {
                    if (Build.VERSION.SDK_INT >= 33 && !hasNotifPermission()) {
                        act.requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIF);
                    } else {
                        runJs("try{ window.__onNotifPerm && window.__onNotifPerm(true); }catch(e){}");
                    }
                } catch (Throwable t) { Log.e(TAG, "requestNotifPermission EX", t); }
            });
        }

        /* 当前系统通知权限是否已授权 */
        @JavascriptInterface
        public boolean getNotifPermission() {
            return hasNotifPermission();
        }

        /* v9.114：聊天通知渠道（cet4_social）importance —— 0(IMPORTANCE_NONE)=被用户关闭，
           此时通知静默不显示，前端据此提示用户开启 */
        @JavascriptInterface
        public int getNotifChannelImpt() {
            try {
                NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                if (Build.VERSION.SDK_INT >= 26 && nm != null) {
                    NotificationChannel ch = nm.getNotificationChannel(NOTIF_CHANNEL_ID);
                    if (ch != null) return ch.getImportance();
                }
            } catch (Throwable ignored) {}
            return -1;
        }

        /* 打开系统通知设置页（用户拒绝后引导重新开启） */
        @JavascriptInterface
        public void openNotifSettings() {
            final Activity act = MainActivity.this;
            runOnUiThread(() -> {
                try {
                    Intent i = new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS);
                    i.putExtra(Settings.EXTRA_APP_PACKAGE, getPackageName());
                    act.startActivity(i);
                } catch (Throwable e) {
                    try {
                        Intent i2 = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS);
                        i2.setData(Uri.parse("package:" + getPackageName()));
                        act.startActivity(i2);
                    } catch (Throwable e2) { Log.e(TAG, "openNotifSettings fail", e2); }
                }
            });
        }

        /* 发送系统通知（id 用消息/申请 id 保证可撤销；type/pid 用于点击跳转） */
        @JavascriptInterface
        public void showNotification(int id, String title, String content, String type, String pid) {
            try {
                if (!hasNotifPermission()) return;  // 未授权不打扰（App 内红点照常）
                ensureNotifChannel();
                Intent intent = new Intent(MainActivity.this, MainActivity.class);
                intent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
                if (type != null) intent.putExtra("nt_type", type);
                if (pid != null) intent.putExtra("nt_pid", pid);
                int flags = PendingIntent.FLAG_UPDATE_CURRENT;
                if (Build.VERSION.SDK_INT >= 23) flags |= PendingIntent.FLAG_IMMUTABLE;
                PendingIntent pi = PendingIntent.getActivity(MainActivity.this, id, intent, flags);
                Notification.Builder nb = Build.VERSION.SDK_INT >= 26
                        ? new Notification.Builder(MainActivity.this, NOTIF_CHANNEL_ID)
                        : new Notification.Builder(MainActivity.this);
                Notification n = nb
                        .setSmallIcon(android.R.drawable.ic_dialog_info)
                        .setContentTitle(title == null ? "" : title)
                        .setContentText(content == null ? "" : content)
                        .setAutoCancel(true)
                        .setContentIntent(pi)
                        .build();
                NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                nm.notify(id, n);
            } catch (Throwable t) { Log.e(TAG, "showNotification EX", t); }
        }

        /* 撤销系统通知（消息撤回联动：通知栏原文尽可能同步取消） */
        @JavascriptInterface
        public void cancelNotification(int id) {
            try {
                NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                nm.cancel(id);
            } catch (Throwable ignored) {}
        }

        /* ===== v9.125：语音通话音频模式（听筒/扬声器路由，仅 Android 端生效） ===== */

        private AudioManager audioMgr() {
            return (AudioManager) getSystemService(AUDIO_SERVICE);
        }

        /* 通话开始：进入通信模式（MODE_IN_COMMUNICATION），WebRTC 音频走通话路由（听筒 + 回声消除） */
        @JavascriptInterface
        public void callAudioBegin() {
            try {
                AudioManager am = audioMgr();
                if (am == null) return;
                runOnUiThread(() -> {
                    try {
                        am.setMode(AudioManager.MODE_IN_COMMUNICATION);
                        am.setSpeakerphoneOn(false);   /* 默认听筒 */
                    } catch (Throwable t) { Log.w(TAG, "callAudioBegin EX", t); }
                });
            } catch (Throwable t) { Log.w(TAG, "callAudioBegin EX", t); }
        }

        /* 通话结束：恢复普通模式，扬声器复位 */
        @JavascriptInterface
        public void callAudioEnd() {
            try {
                AudioManager am = audioMgr();
                if (am == null) return;
                runOnUiThread(() -> {
                    try {
                        am.setMode(AudioManager.MODE_NORMAL);
                        am.setSpeakerphoneOn(false);
                    } catch (Throwable t) { Log.w(TAG, "callAudioEnd EX", t); }
                });
            } catch (Throwable t) { Log.w(TAG, "callAudioEnd EX", t); }
        }

        /* 通话中切换扬声器（false=听筒 true=扬声器） */
        @JavascriptInterface
        public void callSetSpeaker(boolean on) {
            try {
                AudioManager am = audioMgr();
                if (am == null) return;
                runOnUiThread(() -> {
                    try {
                        if (am.getMode() != AudioManager.MODE_IN_COMMUNICATION) {
                            am.setMode(AudioManager.MODE_IN_COMMUNICATION);
                        }
                        am.setSpeakerphoneOn(on);
                    } catch (Throwable t) { Log.w(TAG, "callSetSpeaker EX", t); }
                });
            } catch (Throwable t) { Log.w(TAG, "callSetSpeaker EX", t); }
        }

        /* ===== v9.112：消息通知服务（原生层系统通知栏推送，App 后台/WebView 不可用时保障通知） ===== */

        /* 登录成功后启动通知服务（前台服务 + 原生 WebSocket） */
        @JavascriptInterface
        public void startNotifyService() {
            try {
                Intent i = new Intent(MainActivity.this, NotifyService.class);
                if (Build.VERSION.SDK_INT >= 26) startForegroundService(i);
                else startService(i);
            } catch (Throwable t) { Log.w(TAG, "startNotifyService EX", t); }
        }

        /* 退出登录时停止通知服务 */
        @JavascriptInterface
        public void stopNotifyService() {
            try { stopService(new Intent(MainActivity.this, NotifyService.class)); }
            catch (Throwable ignored) {}
        }

        /* 通知服务：当前正在查看的好友 public_id（空串 = 不在任何聊天界面）。
           该好友的新消息不弹系统通知（避免聊天界面 + 通知栏双份），直接标记已推送。 */
        @JavascriptInterface
        public void setForegroundChat(String pid) {
            NotifyService.foregroundChatPid = (pid == null ? "" : pid);
        }

        /* 登录/刷新 Token 同步给通知服务（SharedPreferences 持久化，Service 重启后可恢复） */
        @JavascriptInterface
        public void setAuth(String access, String refresh, String userJson) {
            try {
                getSharedPreferences("cet4_auth", MODE_PRIVATE).edit()
                        .putString("access", access == null ? "" : access)
                        .putString("refresh", refresh == null ? "" : refresh)
                        .putString("user", userJson == null ? "" : userJson)
                        .apply();
            } catch (Throwable t) { Log.w(TAG, "setAuth EX", t); }
        }

        /* 服务器地址变化同步给通知服务（重连新地址） */
        @JavascriptInterface
        public void setApiBase(String url) {
            try {
                getSharedPreferences("cet4_auth", MODE_PRIVATE).edit()
                        .putString("api_base", url == null ? "" : url).apply();
                NotifyService.onApiBaseChanged(MainActivity.this, url);
            } catch (Throwable t) { Log.w(TAG, "setApiBase EX", t); }
        }

        /* 本地模型最近一次推理消耗的 token 数（用量统计用） */
        @JavascriptInterface
        public int llmTokenUsage() {
            try {
                return LocalLlm.INSTANCE.lastTokenUsage();
            } catch (Throwable t) {
                return 0;
            }
        }

        /* 本地模型 token 用量（含输入/输出拆分）：返回 JSON {"prompt":N,"completion":N,"total":N} */
        @JavascriptInterface
        public String llmTokenUsageJson() {
            try {
                return LocalLlm.INSTANCE.lastTokenUsageJson();
            } catch (Throwable t) {
                return "{\"prompt\":0,\"completion\":0,\"total\":0}";
            }
        }

        /* 清空 native 侧 LLM 日志（新一轮诊断前调用） */
        @JavascriptInterface
        public void llmLogClear() {
            try {
                LocalLlm.INSTANCE.clearLog();
            } catch (Throwable t) {
                Log.e(TAG, "Bridge.llmLogClear EX", t);
            }
        }
    }

    private File getExportDirFile() {
        File f = getExternalFilesDir(android.os.Environment.DIRECTORY_DOWNLOADS);
        return f == null ? getFilesDir() : f;
    }

    /* ContentValues 兼容封装（避免直接使用 android.content.ContentValues 额外 import 别名冲突） */
    private static class ContentValuesCompat {
        private final android.content.ContentValues v = new android.content.ContentValues();
        void put(String k, String val) { v.put(k, val); }
        android.content.ContentValues values() { return v; }
    }

    /* ===== v9.78：本地模型保活（前台服务，防系统回收进程导致模型丢失） ===== */
    private static final String PREFS_KEEPALIVE = "cet4_prefs";
    private static final String KEY_KEEPALIVE = "model_keepalive";

    private boolean keepAliveEnabled() {
        try { return getSharedPreferences(PREFS_KEEPALIVE, MODE_PRIVATE).getBoolean(KEY_KEEPALIVE, true); }
        catch (Throwable t) { return true; }
    }

    private void startKeepAlive() {
        if (!keepAliveEnabled()) return;
        try {
            if (ModelKeepAliveService.running) return;
            Intent i = new Intent(this, ModelKeepAliveService.class);
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(i); else startService(i);
        } catch (Throwable t) { Log.w(TAG, "startKeepAlive fail", t); }
    }

    private void stopKeepAlive() {
        try { stopService(new Intent(this, ModelKeepAliveService.class)); }
        catch (Throwable t) { Log.w(TAG, "stopKeepAlive fail", t); }
    }

    @Override
    public void onBackPressed() {
        /* API<33 传统返回键走这里；API 33+ 由 onCreate 注册的 OnBackPressedCallback 接管 */
        handleAppBack();
    }

    /* 返回键统一处理：调前端 __appBack 钩子完成分层导航（弹窗/子面板/回首页）。
       v9.35：不再依赖返回值判断退出——退出由前端在"双按确认"后显式调 App.appExit()。
       这样 evaluateJavascript 的返回值（predictive back/JS 异常时为 null）不再导致误退出。 */
    private void handleAppBack() {
        if (webView != null) {
            webView.evaluateJavascript(
                "window.__appBack ? window.__appBack() : true",
                null);
        } else {
            finish();
        }
    }

    @Override
    protected void onStop() {
        super.onStop();
        /* v9.62：onStop / onStart / onDestroy 全部【不再卸载本地模型】。
           用户需求：手动加载后模型永驻，只有退出应用并在任务管理器清理进程才重新加载。
           Android 的 onStop/onDestroy 在旋转屏幕、输入法闪断、系统内存压力回收 Activity（进程仍存活）
           等场景都会触发——旧逻辑在这些时机 unload 会反复误杀模型。
           LocalLlm 是进程级单例（Kotlin object）：进程死亡时系统自动回收全部内存（无需显式 unload）；
           进程存活时 Activity 重建/前后台切换不影响模型。
           卸载仅保留用户手动操作（设置页「卸载模型」按钮 → App.llmUnload）。 */
    }

    @Override
    protected void onStart() {
        super.onStart();
    }

    @Override
    protected void onResume() {
        super.onResume();
        /* 通知 JS 回到前台：若使用本地模型则自动重新加载（仅当进程重启/被系统回收后才需要） */
        if (webView != null) {
            try { runJs("try{ window.__onAppForeground && window.__onAppForeground(); }catch(e){}"); } catch (Throwable ignored) {}
        }
    }

    @Override
    protected void onDestroy() {
        /* v9.62：onDestroy 不再卸载本地模型！
           用户需求：手动加载后模型永驻，只有用户退出应用并在任务管理器清理进程才需要重新加载。
           Android 中 onDestroy 除了真正退出外，还会在【旋转屏幕】【系统内存压力回收 Activity（进程仍存活）】
           时触发——旧逻辑在这里 unload → 模型被误杀，用户每次旋转屏幕都要重新加载。
           LocalLlm 是进程级单例（Kotlin object）：进程死亡时系统自动回收全部内存，无需显式 unload；
           进程存活时 Activity 重建不影响模型。卸载仅保留用户手动操作（设置页「卸载模型」按钮）。 */
        synchronized (aiConns) { for (HttpURLConnection c : aiConns) { try { c.disconnect(); } catch (Exception ignored) {} } aiConns.clear(); }
        if (webView != null) webView.destroy();
        super.onDestroy();
    }
}
