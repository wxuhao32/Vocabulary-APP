package com.example.cet4;

import android.Manifest;
import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;
import android.util.Log;

import java.util.Calendar;

/**
 * 每日背单词提醒闹钟接收器（独立顶层类，manifest 需能直接引用 .ReminderReceiver）。
 * 触发后发系统通知，并自动排定下一天（每日重复）。
 */
public class ReminderReceiver extends BroadcastReceiver {
    private static final String TAG = "CET4";
    private static final String REMIND_PREFS = "cet4_remind";
    private static final String REMIND_CHANNEL = "remind";

    @Override
    public void onReceive(Context ctx, Intent intent) {
        String action = intent == null ? "" : String.valueOf(intent.getAction());
        SharedPreferences sp = ctx.getSharedPreferences(REMIND_PREFS, Context.MODE_PRIVATE);
        boolean enabled = sp.getBoolean("enabled", false);
        int hour = sp.getInt("hour", 21);
        int minute = sp.getInt("minute", 0);
        Log.d(TAG, "ReminderReceiver onReceive action=" + action + " enabled=" + enabled + " time=" + hour + ":" + minute);
        if (Intent.ACTION_BOOT_COMPLETED.equals(action)) {
            // 开机后恢复每日闹钟（重启会清空 AlarmManager）
            if (enabled) scheduleReminder(ctx, hour, minute);
            /* v9.130：已登录则恢复消息通知服务 —— 手机重启后 App 未打开也能收系统通知
               （NotifyService 前台服务被重启清掉，登录态在 SharedPreferences） */
            try {
                String tok = ctx.getSharedPreferences("cet4_auth", Context.MODE_PRIVATE).getString("access", "");
                if (tok != null && !tok.isEmpty()) {
                    Intent svc = new Intent(ctx, NotifyService.class);
                    if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(svc);
                    else ctx.startService(svc);
                    Log.d(TAG, "boot: NotifyService 已恢复（已登录）");
                }
            } catch (Throwable t) { Log.w(TAG, "boot NotifyService fail", t); }
            return;
        }
        if (!enabled) return;
        if (Build.VERSION.SDK_INT >= 33 && ctx.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            // 未授权通知权限：不打扰，照常排下一次（用户授权后即恢复）
            scheduleReminder(ctx, hour, minute);
            return;
        }
        ensureChannel(ctx);
        Intent open = new Intent(ctx, MainActivity.class);
        open.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        PendingIntent contentPi = PendingIntent.getActivity(ctx, 1, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder nb = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(ctx, REMIND_CHANNEL)
                : new Notification.Builder(ctx);
        nb.setSmallIcon(android.R.drawable.ic_dialog_info)
          .setContentTitle("该背单词啦")
          .setContentText("今日单词任务还没完成，去「我爱背单词」背一组吧")
          .setAutoCancel(true)
          .setContentIntent(contentPi);
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm != null) nm.notify(1, nb.build());
        // 每日重复：触发后立即排下一次
        scheduleReminder(ctx, hour, minute);
    }

    static void scheduleReminder(Context ctx, int hour, int minute) {
        Calendar cal = Calendar.getInstance();
        cal.set(Calendar.HOUR_OF_DAY, hour);
        cal.set(Calendar.MINUTE, minute);
        cal.set(Calendar.SECOND, 0);
        cal.set(Calendar.MILLISECOND, 0);
        if (cal.getTimeInMillis() <= System.currentTimeMillis()) {
            cal.add(Calendar.DAY_OF_YEAR, 1); // 今天已过则明天
        }
        AlarmManager am = (AlarmManager) ctx.getSystemService(Context.ALARM_SERVICE);
        if (am == null) return;
        Intent i = new Intent(ctx, ReminderReceiver.class);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;
        PendingIntent pi = PendingIntent.getBroadcast(ctx, 0, i, flags);
        boolean exact = Build.VERSION.SDK_INT < 31 || am.canScheduleExactAlarms();
        try {
            if (exact) am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, cal.getTimeInMillis(), pi);
            else am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, cal.getTimeInMillis(), pi);
        } catch (Exception e) {
            try { am.set(AlarmManager.RTC_WAKEUP, cal.getTimeInMillis(), pi); }
            catch (Exception e2) { Log.e(TAG, "alarm set fail", e2); }
        }
        Log.d(TAG, "reminder scheduled at " + hour + ":" + minute + " exact=" + exact + " next=" + cal.getTimeInMillis());
    }

    static void cancelReminder(Context ctx) {
        AlarmManager am = (AlarmManager) ctx.getSystemService(Context.ALARM_SERVICE);
        if (am == null) return;
        Intent i = new Intent(ctx, ReminderReceiver.class);
        PendingIntent pi = PendingIntent.getBroadcast(ctx, 0, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        am.cancel(pi);
    }

    static void ensureChannel(Context ctx) {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(REMIND_CHANNEL, "背单词提醒", NotificationManager.IMPORTANCE_HIGH);
            ch.setDescription("每日定时提醒背单词");
            NotificationManager nm = ctx.getSystemService(NotificationManager.class);
            if (nm != null) nm.createNotificationChannel(ch);
        }
    }
}
