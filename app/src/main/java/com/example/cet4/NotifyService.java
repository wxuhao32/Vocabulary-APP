package com.example.cet4;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.Manifest;
import android.net.ConnectivityManager;
import android.net.Network;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.IOException;
import java.util.concurrent.TimeUnit;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okio.ByteString;

/**
 * v9.112 消息系统通知服务（原生层，独立于 WebView）
 *
 * 职责分工（与 WebSocket 实时聊天互补，不替代 WebSocket）：
 * - WebView WebSocket：App 前台时的实时聊天渲染
 * - 本服务（原生前台服务 + 原生 WebSocket）：App 后台 / WebView 不可用 / WS 断开时，
 *   负责私聊消息的 Android 系统通知栏推送
 *
 * 关键设计：
 * 1. 前台服务（dataSync）+ START_STICKY：后台不易被回收；被系统杀死后自动重启并补拉离线通知
 * 2. 自身维护 OkHttp WebSocket：登录态下与 WebView 同连 /ws，收到 new_message 即时发通知
 * 3. 通知幂等（SQLite 持久化）：发完通知 → POST /api/v1/notify/ack（messages.notified=1）；
 *    启动/重连后 GET /api/v1/notify/pending 补发未 notified 消息 —— WebSocket 重连、
 *    App 重启、历史同步都不会重复推送
 * 4. 前台聊天界面抑制：JS 通过 App.setForegroundChat(pid) 通知本服务当前正查看的好友，
 *    该好友的新消息不弹通知、直接 ack（避免"聊天界面收到 + 通知栏又出现"双份）
 * 5. 已读独立：notified（已推送通知）与 read_at（已读）是服务端两个独立字段；
 *    只有用户真正进入聊天（list_messages）才标记已读
 * 6. 点击通知 → MainActivity（nt_type/nt_pid）→ JS __onNotifTap → 打开对应聊天
 */
public class NotifyService extends Service {

    private static final String TAG = "NotifySvc";
    private static final String CH_SVC = "cet4_notify_svc";   // 前台服务常驻通知渠道
    private static final String CH_MSG = "cet4_social";       // 业务通知渠道（复用 MainActivity）
    private static final int FGS_ID = 1001;
    /** 聊天通知 id 偏移：通知 id = 1000000 + mid%1000000，绝不与前台通知 FGS_ID(1001) 冲突
        （此前直接用 mid 作为通知 id，当消息 id 恰好为 1001 时会把前台"运行中"通知顶掉/被顶掉） */
    private static int notifId(long mid) { return 1000000 + (int) (mid % 1000000L); }
    private static final String PREFS = "cet4_auth";
    private static final MediaType JSON_TYPE = MediaType.get("application/json; charset=utf-8");
    private static final long HEARTBEAT_MS = 25000;
    /* v9.120：pending 兜底轮询周期（WS 断开时消息通知的兜底送达间隔） */
    private static final long PENDING_POLL_MS = 60000;
    private static final long[] RECONNECT_DELAYS = {1000, 2000, 4000, 8000, 15000, 30000};

    /** 前台聊天好友 public_id（由 JS setForegroundChat 更新；空 = 不在任何聊天界面） */
    public static volatile String foregroundChatPid = "";

    private OkHttpClient client;
    private WebSocket ws;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Object wsLock = new Object();

    private String apiBase = "http://127.0.0.1:8000";
    private String accessToken = "";
    private String refreshToken = "";
    private int retry = 0;
    private boolean stopped = false;
    private boolean wsOpen = false;

    private final Runnable heartbeatRunnable = new Runnable() {
        @Override public void run() {
            if (!stopped && wsOpen) {
                try { if (ws != null) ws.send("{\"type\":\"ping\"}"); } catch (Throwable ignored) {}
                handler.postDelayed(this, HEARTBEAT_MS);
            }
        }
    };

    private final Runnable reconnectRunnable = new Runnable() {
        @Override public void run() { connect(); }
    };

    /* v9.120：pending 兜底轮询 —— WS 断开/隧道波动时消息通知仍能送达。
       每 60s 拉一次"发给我的未通知消息"补发系统通知；服务端幂等（notified 去重 +
       已读排除 + 会话过滤），WS 正常时重复拉取无害。 */
    private final Runnable pendingRunnable = new Runnable() {
        @Override public void run() {
            if (!stopped) {
                try { readAuth(); pullPending(); }   /* 每次先重读 token（可能已刷新） */
                catch (Throwable t) { Log.w(TAG, "pendingPoll", t); }
            }
            handler.postDelayed(this, PENDING_POLL_MS);
        }
    };

    private final ConnectivityManager.NetworkCallback networkCallback = new ConnectivityManager.NetworkCallback() {
        @Override public void onAvailable(Network network) {
            if (!stopped && !wsOpen) scheduleReconnect(0);
        }
    };

    /* ---------------- 生命周期 ---------------- */
    @Override
    public void onCreate() {
        super.onCreate();
        try { ensureChannels(); } catch (Throwable t) { Log.w(TAG, "ensureChannels", t); }
        readAuth();
        client = new OkHttpClient.Builder()
                .connectTimeout(10, TimeUnit.SECONDS)
                .readTimeout(0, TimeUnit.MILLISECONDS)   // WS 长连接不设读超时
                .pingInterval(0, TimeUnit.MILLISECONDS)  // 自己用应用层 ping
                .build();
        try {
            startForeground(FGS_ID, buildServiceNotification());
        } catch (Throwable t) {
            Log.w(TAG, "startForeground fail, 降级继续", t);
        }
        try {
            ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm != null) cm.registerDefaultNetworkCallback(networkCallback);
        } catch (Throwable t) { Log.w(TAG, "network callback fail", t); }
        /* v9.120：启动 pending 兜底轮询（首次 60s 后；与 WS 心跳并存，WS 断时不丢通知） */
        handler.postDelayed(pendingRunnable, PENDING_POLL_MS);
        connect();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        readAuth();                    // 登录/刷新后 token 可能已更新
        if (!stopped) connect();       // 被杀重启（START_STICKY）时恢复连接
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        stopped = true;
        handler.removeCallbacksAndMessages(null);
        try {
            ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm != null) cm.unregisterNetworkCallback(networkCallback);
        } catch (Throwable ignored) {}
        synchronized (wsLock) {
            if (ws != null) { try { ws.close(1000, "svc-stop"); } catch (Throwable ignored) {} ws = null; }
            wsOpen = false;
        }
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    /* ---------------- 鉴权信息（JS setAuth 写入 SharedPreferences） ---------------- */
    private void readAuth() {
        SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
        accessToken = sp.getString("access", "") == null ? "" : sp.getString("access", "");
        refreshToken = sp.getString("refresh", "") == null ? "" : sp.getString("refresh", "");
        String base = sp.getString("api_base", "");
        if (base != null && !base.isEmpty()) apiBase = base;
    }

    /* ---------------- WebSocket ---------------- */
    private String wsUrl() {
        String b = apiBase == null || apiBase.isEmpty() ? "http://127.0.0.1:8000" : apiBase;
        return b.replaceFirst("^http", "ws") + "/ws?token=" + (accessToken == null ? "" : accessToken);
    }

    private void connect() {
        synchronized (wsLock) {
            if (stopped || wsOpen) return;
            if (accessToken == null || accessToken.isEmpty()) return;   // 未登录不连
            final WebSocket old = ws;
            ws = null;
            if (old != null) { try { old.close(1000, "reconnect"); } catch (Throwable ignored) {} }
            try {
                Request req = new Request.Builder().url(wsUrl()).build();
                ws = client.newWebSocket(req, new WsListener());
            } catch (Throwable t) {
                Log.w(TAG, "connect fail", t);
                scheduleReconnect(retry + 1);
            }
        }
    }

    private void scheduleReconnect(int attempt) {
        if (stopped) return;
        handler.removeCallbacks(reconnectRunnable);
        int idx = Math.min(attempt, RECONNECT_DELAYS.length - 1);
        long delay = RECONNECT_DELAYS[idx];
        retry = attempt;
        handler.postDelayed(reconnectRunnable, delay);
        Log.d(TAG, "WS 断开，将在 " + delay + "ms 后重连（第 " + attempt + " 次）");
    }

    private class WsListener extends WebSocketListener {
        @Override public void onOpen(WebSocket webSocket, Response response) {
            wsOpen = true;
            retry = 0;
            handler.removeCallbacks(reconnectRunnable);
            handler.removeCallbacks(heartbeatRunnable);
            handler.postDelayed(heartbeatRunnable, HEARTBEAT_MS);
            reportLog("NWS", "WS onOpen t=" + System.currentTimeMillis());   /* v9.114 诊断 */
            pullPending();   // 连接/重连成功 → 补发离线期间未通知消息
            Log.d(TAG, "WS 已连接");
        }

        @Override public void onMessage(WebSocket webSocket, String text) {
            try {
                JSONObject obj = new JSONObject(text);
                String type = obj.optString("type", "");
                if ("new_message".equals(type)) {
                    reportLog("NWS", "WS 收到 new_message t=" + System.currentTimeMillis());   /* v9.114 诊断 */
                    handleNewMessage(obj);
                } else if ("revoked".equals(type)) {
                    // 对方撤回消息 → 通知栏同步撤销（通知 id == notifId(消息 id)）
                    JSONArray ids = obj.optJSONArray("ids");
                    if (ids != null) {
                        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                        for (int i = 0; i < ids.length(); i++) {
                            long mid = ids.optLong(i, -1);
                            if (mid > 0 && nm != null) nm.cancel(notifId(mid));
                        }
                    }
                }
                // notification（好友申请等）暂由前端 WebView 负责；pong 忽略
            } catch (Throwable t) { Log.w(TAG, "onMessage parse", t); }
        }

        @Override public void onClosed(WebSocket webSocket, int code, String reason) {
            wsOpen = false;
            handler.removeCallbacks(heartbeatRunnable);
            reportLog("NWS", "WS onClosed code=" + code + " t=" + System.currentTimeMillis());   /* v9.114 诊断 */
            if (!stopped) scheduleReconnect(retry + 1);
        }

        @Override public void onFailure(WebSocket webSocket, Throwable t, Response response) {
            wsOpen = false;
            handler.removeCallbacks(heartbeatRunnable);
            reportLog("NWS", "WS onFailure t=" + System.currentTimeMillis() + " err=" + (t == null ? "" : t.toString()));   /* v9.114 诊断 */
            if (!stopped) scheduleReconnect(retry + 1);
        }
    }

    /* ---------------- 消息处理：通知 + 幂等 ack ---------------- */
    private void handleNewMessage(JSONObject obj) {
        JSONObject msg = obj.optJSONObject("message");
        JSONObject friend = obj.optJSONObject("friend");
        if (msg == null || friend == null) return;
        long mid = msg.optLong("id", -1);
        String pid = friend.optString("id", "");
        if (mid <= 0) return;
        String fg = foregroundChatPid == null ? "" : foregroundChatPid;
        if (pid.equals(fg)) {
            // 正在看该好友聊天：不弹通知，ack 标记已处理（避免重连补拉重复通知）
            ack(new long[]{mid});
            return;
        }
        String type = msg.optString("type", "text");
        String content = msg.optString("content", "");
        String nick = friend.optString("nickname", "好友");
        String title = "来自「" + nick + "」的消息";
        String notifType = "file".equals(type) ? "file_message" : "new_message";
        /* v9.125：通话记录消息（content 为 JSON {event,duration}）→ 可读文案，避免通知栏裸 JSON */
        if ("call".equals(type)) {
            content = callRecordText(content);
            title = "与「" + nick + "」的通话";
            notifType = "new_message";
        }
        /* v9.114 日志诊断：通知链路全记录（标题/正文/notifId/显示结果/ack） */
        reportLog("NWS", "handle mid=" + mid + " pid=" + pid + " type=" + type
                + " title=" + title + " content=" + (content.length() > 30 ? content.substring(0, 30) : content));
        /* v9.114：仅真正显示通知才 ack —— 通知权限缺失时消息留在 pending，
           授权后重连/重启自动补发（此前未显示也 ack 导致消息永久静默丢失） */
        if (showNotification(notifId(mid), title, content, notifType, pid)) {
            reportLog("NWS", "通知已显示 notifId=" + notifId(mid) + " mid=" + mid + " → ack");
            ack(new long[]{mid});
        } else {
            reportLog("NWS", "通知未显示 notifId=" + notifId(mid) + " mid=" + mid + " → 不 ack（留 pending）");
        }
    }

    /* v9.125：通话记录 content(JSON {event,duration,media}) → 可读文案（与服务端 call.record_text 一致）
       v9.129：media=video → 视频通话前缀 */
    private static String callRecordText(String contentJson) {
        try {
            JSONObject o = new JSONObject(contentJson == null ? "{}" : contentJson);
            String ev = o.optString("event", "");
            int dur = Math.max(0, o.optInt("duration", 0));
            boolean vid = "video".equals(o.optString("media", ""));
            String pre = vid ? "视频通话" : "通话";
            switch (ev) {
                case "end":     return String.format("%s %02d:%02d", pre, dur / 60, dur % 60);
                case "missed":  return vid ? "视频通话未接听" : "未接听";
                case "rejected":return "已拒绝";
                case "canceled":return "已取消";
                default:        return pre;
            }
        } catch (Throwable t) { return "通话"; }
    }

    private void pullPending() {
        if (accessToken == null || accessToken.isEmpty()) return;
        String url = apiBase + "/api/v1/notify/pending?limit=50";
        Request req = new Request.Builder().url(url)
                .header("Authorization", "Bearer " + accessToken).build();
        client.newCall(req).enqueue(new Callback() {
            @Override public void onFailure(Call call, IOException e) { reportLog("NWS", "pullPending 网络失败 " + (e == null ? "" : e.getMessage())); /* 下次重连再补 */ }
            @Override public void onResponse(Call call, Response response) throws IOException {
                try {
                    if (!response.isSuccessful()) { reportLog("NWS", "pullPending HTTP " + response.code()); return; }
                    JSONObject body = new JSONObject(response.body() == null ? "{}" : response.body().string());
                    JSONArray arr = body.optJSONArray("messages");
                    if (arr == null) return;
                    java.util.List<Long> ackIds = new java.util.ArrayList<>();
                    StringBuilder sb = new StringBuilder();
                    for (int i = 0; i < arr.length(); i++) {
                        JSONObject m = arr.optJSONObject(i);
                        if (m == null) continue;
                        long mid = m.optLong("id", -1);
                        String pid = m.optJSONObject("friend") == null ? "" : m.optJSONObject("friend").optString("id", "");
                        String fg = foregroundChatPid == null ? "" : foregroundChatPid;
                        if (mid <= 0) continue;
                        sb.append(mid).append(",");
                        if (pid.equals(fg)) { ackIds.add(mid); continue; }   // 前台聊天：不通知（仍 ack）
                        String type = m.optString("type", "text");
                        String nick = m.optJSONObject("friend") == null ? "好友" : m.optJSONObject("friend").optString("nickname", "好友");
                        /* v9.125：通话记录转可读文案 */
                        String mContent = m.optString("content", "");
                        String mTitle = "来自「" + nick + "」的消息";
                        if ("call".equals(type)) { mContent = callRecordText(mContent); mTitle = "与「" + nick + "」的通话"; }
                        /* v9.114：仅真正显示才 ack（权限缺失留在 pending 等授权后补发） */
                        if (showNotification(notifId(mid), mTitle,
                                mContent, "file".equals(type) ? "file_message" : "new_message", pid)) {
                            ackIds.add(mid);
                        }
                    }
                    reportLog("NWS", "pullPending n=" + arr.length() + " ids=" + sb.toString() + " → ack " + ackIds.size() + " 条");   /* v9.114 诊断 */
                    if (!ackIds.isEmpty()) {
                        long[] arr2 = new long[ackIds.size()];
                        for (int i = 0; i < ackIds.size(); i++) arr2[i] = ackIds.get(i);
                        ack(arr2);
                    }
                } catch (Throwable t) { Log.w(TAG, "pullPending", t); }
            }
        });
    }

    private void ack(long[] ids) {
        if (ids == null || ids.length == 0) return;
        try {
            JSONArray arr = new JSONArray();
            for (long id : ids) arr.put(id);
            JSONObject body = new JSONObject();
            body.put("ids", arr);
            Request req = new Request.Builder()
                    .url(apiBase + "/api/v1/notify/ack")
                    .header("Authorization", "Bearer " + accessToken)
                    .post(RequestBody.create(body.toString(), JSON_TYPE)).build();
            client.newCall(req).enqueue(new Callback() {
                @Override public void onFailure(Call call, IOException e) { reportLog("NWS", "ack 失败(网络) ids=" + ids.length + " err=" + (e == null ? "" : e.getMessage())); }
                @Override public void onResponse(Call call, Response response) throws IOException {
                    reportLog("NWS", "ack resp status=" + response.code() + " ids=" + ids.length);
                    if (response.body() != null) response.body().close();
                }
            });
        } catch (Throwable t) { Log.w(TAG, "ack", t); }
    }

    /* v9.114 日志诊断：上报关键事件到服务端 /api/v1/debug/log（仅打印，不落库）
       使真机上的原生服务行为可在服务器日志中还原。 */
    private void reportLog(String tag, String msg) {
        try {
            if (accessToken == null || accessToken.isEmpty()) return;
            JSONObject body = new JSONObject();
            body.put("tag", tag);
            body.put("msg", msg);
            Request req = new Request.Builder()
                    .url(apiBase + "/api/v1/debug/log")
                    .header("Authorization", "Bearer " + accessToken)
                    .post(RequestBody.create(body.toString(), JSON_TYPE)).build();
            client.newCall(req).enqueue(new Callback() {
                @Override public void onFailure(Call call, IOException e) { }
                @Override public void onResponse(Call call, Response response) throws IOException {
                    if (response.body() != null) response.body().close();
                }
            });
        } catch (Throwable t) { /* 静默 */ }
    }

    /* ---------------- 系统通知 ---------------- */
    private boolean hasNotifPermission() {
        return Build.VERSION.SDK_INT < 33
                || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
    }

    private void ensureChannels() {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (nm == null) return;
        if (Build.VERSION.SDK_INT >= 26) {
            // 前台服务常驻通知（最低打扰；v9.117：渠道名/文案不含"服务运行中"等状态字样，
            // 避免用户误以为是聊天消息通知 —— 业务聊天通知走 CH_MSG 显示真实发送者与内容）
            NotificationChannel svc = new NotificationChannel(CH_SVC, "消息通知",
                    NotificationManager.IMPORTANCE_MIN);
            svc.setShowBadge(false);
            svc.setSound(null, null);
            nm.createNotificationChannel(svc);
            // 业务消息通知（高打扰，复用 MainActivity 渠道定义）
            NotificationChannel msg = new NotificationChannel(CH_MSG, "好友与消息",
                    NotificationManager.IMPORTANCE_HIGH);
            msg.setDescription("好友申请 / 新消息 / 文件通知");
            nm.createNotificationChannel(msg);
        }
    }

    private Notification buildServiceNotification() {
        Notification.Builder nb = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CH_SVC)
                : new Notification.Builder(this);
        /* v9.117：前台占位通知文案简洁中性（不含"服务运行中"等状态字样），
           且 channel 为 IMPORTANCE_MIN 不响铃不打扰；业务消息通知走 CH_MSG 显示真实内容 */
        return nb.setSmallIcon(android.R.drawable.ic_menu_compass)
                .setContentTitle("消息通知")
                .setContentText("新消息将在此提醒")
                .setOngoing(true)
                .setCategory(Notification.CATEGORY_SERVICE)
                .setVisibility(Notification.VISIBILITY_SECRET)
                .build();
    }

    /** 发送聊天消息通知。返回 true=已真正显示（调用方此时才 ack 幂等标记）。
        v9.114：权限缺失返回 false 且不抛错 —— 消息留在服务端 pending，授权后自动补发。 */
    private boolean showNotification(int id, String title, String content, String type, String pid) {
        try {
            if (!hasNotifPermission()) {
                Log.w(TAG, "通知权限未授予，跳过（消息保留 pending 待授权后补发）id=" + id);
                reportLog("NWS", "showNotification 权限未授予 → 跳过 notifId=" + id);   /* v9.114 诊断 */
                return false;
            }
            ensureChannels();
            Intent intent = new Intent(this, MainActivity.class);
            intent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
            if (type != null) intent.putExtra("nt_type", type);
            if (pid != null) intent.putExtra("nt_pid", pid);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 23) flags |= PendingIntent.FLAG_IMMUTABLE;
            PendingIntent pi = PendingIntent.getActivity(this, id, intent, flags);
            Notification.Builder nb = Build.VERSION.SDK_INT >= 26
                    ? new Notification.Builder(this, CH_MSG)
                    : new Notification.Builder(this);
            Notification n = nb
                    .setSmallIcon(android.R.drawable.ic_dialog_info)
                    .setContentTitle(title == null ? "" : title)
                    .setContentText(content == null ? "" : content)
                    .setAutoCancel(true)
                    .setContentIntent(pi)
                    .build();
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null) nm.notify(id, n);
            Log.d(TAG, "通知已发: id=" + id + " pid=" + pid + " title=" + title);
            return true;
        } catch (Throwable t) { Log.e(TAG, "showNotification EX", t); reportLog("NWS", "showNotification 异常 " + (t == null ? "" : t.toString())); return false; }
    }

    /** 由 MainActivity（JS 桥 setApiBase）调用：服务器地址变化 → 重连新地址 */
    public static void onApiBaseChanged(Context ctx, String url) {
        if (ctx != null && url != null && !url.isEmpty()) {
            ctx.getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString("api_base", url).apply();
        }
    }
}
