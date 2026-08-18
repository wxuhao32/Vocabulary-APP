package com.example.cet4;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

/**
 * 本地模型保活前台服务（v9.78）。
 *
 * 作用：模型加载成功后，把进程提升到前台优先级（foreground service），
 * 防止切后台/内存压力下被系统 Low Memory Killer 回收。
 * 进程一旦被杀，3.6GB 权重全部丢失 → 用户每次回来都要重新加载（10~60s）——
 * 这正是"离开对话界面/新建对话后模型被卸载"的次生根因。
 *
 * 生命周期：
 *  - 模型加载成功 → MainActivity 启动本服务
 *  - 卸载模型 / 关闭「模型保活」开关 → MainActivity 停止本服务
 *  - Activity finish 但进程存活（用户退出界面）→ 服务保持 → 重新打开 App 模型秒回
 *
 * 通知策略：最低打扰（IMPORTANCE_MIN、不响不震不显示角标、常驻 ongoing），
 * 文案低调；Android 13+ 未授权通知权限时 startForeground 仍可运行（仅通知不可见）。
 *
 * 防御：前台启动失败（厂商限制/系统拒绝）→ stopSelf 静默降级，绝不影响主流程。
 */
public class ModelKeepAliveService extends Service {

    private static final String TAG = "CET4";
    private static final String CH_ID = "model_keepalive";
    private static final int NOTIF_ID = 42;

    /** 服务是否在运行（供 MainActivity 判断，避免重复启动） */
    public static volatile boolean running = false;

    @Override
    public void onCreate() {
        super.onCreate();
        running = true;
        try {
            NotificationChannel ch = new NotificationChannel(CH_ID, "本地模型保活",
                    NotificationManager.IMPORTANCE_MIN);
            ch.setDescription("保持本地模型常驻内存，避免被系统回收后需要重新加载");
            ch.setShowBadge(false);
            ch.setSound(null, null);
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null) nm.createNotificationChannel(ch);

            Notification n = new Notification.Builder(this, CH_ID)
                    .setSmallIcon(android.R.drawable.ic_menu_compass)
                    .setContentTitle("本地模型已就绪")
                    .setContentText("模型常驻内存，可随时离线对话")
                    .setOngoing(true)
                    .setCategory(Notification.CATEGORY_SERVICE)
                    .setVisibility(Notification.VISIBILITY_SECRET)
                    .build();

            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
            } else {
                startForeground(NOTIF_ID, n);
            }
            Log.d(TAG, "保活服务已启动（模型常驻内存）");
        } catch (Throwable t) {
            Log.w(TAG, "保活服务前台启动失败，静默降级: " + t);
            running = false;
            stopSelf();
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        Log.d(TAG, "保活服务已停止");
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
