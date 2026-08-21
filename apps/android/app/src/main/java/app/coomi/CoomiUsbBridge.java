package app.coomi;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Environment;

import com.termux.shared.logger.Logger;
import com.termux.shared.termux.TermuxConstants;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.TimeZone;

/**
 * USB 调试桥：把运行中引擎的「端口 + 访问令牌」发布到共享存储，
 * 供 PC 端经 {@code adb forward} 调用 /api/fs/* 读写应用私有目录里的故事项目。
 *
 * <p>为什么需要它：应用私有目录（{@code /data/user/0/<pkg>/files/stories}）在 release 包上
 * 对 adb 完全不可达——非 debuggable 所以 {@code run-as} 失败，{@code allowBackup="false"}
 * 堵掉了 adb backup，四个 provider 或未导出或需要签名级 {@code MANAGE_DOCUMENTS}。
 * 而引擎自身已经提供了完备的文件读写 API，且绑定在 {@code 127.0.0.1}（正是 adb forward
 * 需要的形状）；端口也能从 {@code /proc/net/tcp} 探到。唯一真正拿不到的，就是每次启动
 * 随机生成、只存在于 argv 与 WebView URL 里的访问令牌。本类就只交出这一样东西。
 *
 * <p><b>安全约定（修改本类时务必保持）：</b>
 * <ul>
 *   <li>默认关闭。令牌等同于「应用私有目录的读写凭据」，发布它必须是用户的显式选择。</li>
 *   <li>只在开关为开 <em>且</em> 引擎已启动时写文件；引擎停止或开关关闭时立即删除。</li>
 *   <li>令牌每次引擎启动都重新生成，所以「关掉开关 + 重启引擎」即可让已泄露的令牌失效。</li>
 *   <li>不改变网络暴露面：引擎仍然只监听 127.0.0.1，本桥不碰绑定地址。</li>
 * </ul>
 */
public final class CoomiUsbBridge {

    private static final String LOG_TAG = "CoomiUsbBridge";

    /** 与 {@link CoomiStoryPreference} 共用同一份应用设置，避免两套 prefs 文件。 */
    private static final String PREFS_NAME = "coomi_settings";
    private static final String KEY_ENABLED = "usb_bridge_enabled";

    private static final String BRIDGE_DIR_NAME = "Storydex";
    private static final String BRIDGE_FILE_NAME = "bridge.json";

    private CoomiUsbBridge() {}

    private static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
    }

    public static boolean isEnabled(Context context) {
        return prefs(context).getBoolean(KEY_ENABLED, false);
    }

    /** 只改开关本身；发布/删除由调用方在拿到引擎状态后决定（见 CoomiService#refreshUsbBridge）。 */
    public static void setEnabled(Context context, boolean enabled) {
        prefs(context).edit().putBoolean(KEY_ENABLED, enabled).apply();
    }

    /** {@code /sdcard/Storydex/bridge.json}——adb 可直接读取的位置。 */
    public static File bridgeFile() {
        return new File(new File(Environment.getExternalStorageDirectory(), BRIDGE_DIR_NAME), BRIDGE_FILE_NAME);
    }

    public static boolean isPublished() {
        return bridgeFile().isFile();
    }

    /**
     * 写入端口与令牌。开关为关时等价于 {@link #clear()}，所以调用方无需自己判断。
     *
     * @return 是否确实发布了文件
     */
    public static boolean publish(Context context, int port, String token, String cwd) {
        if (!isEnabled(context) || port <= 0 || token == null || token.isEmpty()) {
            clear();
            return false;
        }
        File file = bridgeFile();
        try {
            File parent = file.getParentFile();
            if (parent != null && !parent.isDirectory() && !parent.mkdirs()) {
                Logger.logWarn(LOG_TAG, "无法创建 " + parent.getAbsolutePath() + "，USB 调试桥未发布");
                return false;
            }
            String json = "{\n"
                + "  \"port\": " + port + ",\n"
                + "  \"token\": " + jsonString(token) + ",\n"
                + "  \"cwd\": " + jsonString(cwd) + ",\n"
                + "  \"storiesDir\": " + jsonString(cwd + "/" + CoomiStoryPreference.STORIES_DIR_NAME) + ",\n"
                + "  \"package\": " + jsonString(TermuxConstants.TERMUX_PACKAGE_NAME) + ",\n"
                + "  \"updatedAt\": " + jsonString(utcNow()) + "\n"
                + "}\n";
            Files.write(file.toPath(), json.getBytes(StandardCharsets.UTF_8));
            Logger.logInfo(LOG_TAG, "USB 调试桥已发布到 " + file.getAbsolutePath() + "（端口 " + port + "）");
            return true;
        } catch (Exception e) {
            Logger.logWarn(LOG_TAG, "USB 调试桥发布失败：" + e.getMessage());
            return false;
        }
    }

    /** 删除已发布的桥文件；文件不存在时静默返回。 */
    public static void clear() {
        try {
            File file = bridgeFile();
            if (file.isFile() && !file.delete()) {
                Logger.logWarn(LOG_TAG, "无法删除 " + file.getAbsolutePath());
            }
        } catch (Exception e) {
            Logger.logWarn(LOG_TAG, "USB 调试桥清理失败：" + e.getMessage());
        }
    }

    private static String utcNow() {
        SimpleDateFormat format = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US);
        format.setTimeZone(TimeZone.getTimeZone("UTC"));
        return format.format(new Date());
    }

    /** 最小 JSON 字符串转义：路径里可能出现反斜杠或引号。 */
    private static String jsonString(String value) {
        if (value == null) return "null";
        StringBuilder sb = new StringBuilder(value.length() + 2);
        sb.append('"');
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) sb.append(String.format(Locale.US, "\\u%04x", (int) c));
                    else sb.append(c);
            }
        }
        return sb.append('"').toString();
    }
}
