package app.coomi;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.os.Bundle;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.Build;
import android.provider.Settings;
import android.net.Uri;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ImageButton;
import android.widget.TextView;
import android.widget.Toast;

import com.termux.BuildConfig;

import androidx.annotation.Nullable;

import com.termux.R;
import com.termux.app.TermuxActivity;
import com.termux.shared.logger.Logger;
import com.termux.shared.termux.TermuxConstants;

import java.io.File;

/**
 * Coomi Dashboard — main screen after setup.
 *
 * Shows engine status, restart/stop controls, and quick links.
 */
public class CoomiDashboardActivity extends Activity {

    private static final String LOG_TAG = "CoomiDashboardActivity";
    private static final int STATUS_REFRESH_MS = 5000;

    private View mStatusIndicator;
    private TextView mStatusText;
    private View mOpenChatButton;
    private Button mRestartButton;
    private Button mStopButton;
    private View mOpenWebUiButton;
    private View mWebUiButtonContainer;
    private View mUsbBridgeButton;
    private androidx.appcompat.widget.SwitchCompat mUsbBridgeSwitch;
    private TextView mUsbBridgeDesc;
    private View mCatalogButton;
    private View mFilesButton;
    private View mProvidersButton;
    private View mRuntimeButton;
    private View mCheckUpdateButton;
    private TextView mCheckUpdateDesc;
    private View mUpdateDot;
    private View mHomeSettingsButton;
    private View mPermissionSettingsButton;
    private View mStorageSettingsButton;
    private TextView mStoryProjectText;
    private TextView mStoryProjectName;
    private View mBackupButton;
    private String mAppliedTheme;

    private CoomiService mCoomiService;
    private boolean mBound = false;
    private Handler mHandler = new Handler(Looper.getMainLooper());
    private Runnable mStatusRunnable;

    private ServiceConnection mConnection = new ServiceConnection() {
        @Override
        public void onServiceConnected(ComponentName name, IBinder service) {
            CoomiService.LocalBinder binder = (CoomiService.LocalBinder) service;
            mCoomiService = binder.getService();
            mBound = true;
            Logger.logDebug(LOG_TAG, "CoomiService bound");
            refreshStatus();
        }

        @Override
        public void onServiceDisconnected(ComponentName name) {
            mCoomiService = null;
            mBound = false;
        }
    };

    @Override
    protected void onCreate(@Nullable Bundle savedInstanceState) {
        mAppliedTheme = CoomiTheme.getMode(this);
        CoomiTheme.applyPageTheme(this);
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_coomi_dashboard);

        mStatusIndicator = findViewById(R.id.dashboard_status_indicator);
        mStatusText = findViewById(R.id.dashboard_status_text);
        mOpenChatButton = findViewById(R.id.btn_open_chat);
        mRestartButton = findViewById(R.id.btn_restart);
        mStopButton = findViewById(R.id.btn_stop);
        mOpenWebUiButton = findViewById(R.id.btn_open_webui);
        mWebUiButtonContainer = findViewById(R.id.webui_button_container);
        mUsbBridgeButton = findViewById(R.id.btn_usb_bridge);
        mUsbBridgeSwitch = findViewById(R.id.switch_usb_bridge);
        mUsbBridgeDesc = findViewById(R.id.txt_usb_bridge_desc);
        mCatalogButton = findViewById(R.id.btn_web_catalog);
        mFilesButton = findViewById(R.id.btn_web_files);
        mCheckUpdateButton = findViewById(R.id.btn_check_update);
        mCheckUpdateDesc = findViewById(R.id.txt_check_update_desc);
        mUpdateDot = findViewById(R.id.dot_update);
        mHomeSettingsButton = findViewById(R.id.btn_home_settings);
        mCheckUpdateDesc.setText(getString(R.string.coomi_dash_check_update_desc, BuildConfig.VERSION_NAME));
        checkUpdateSilently();
        mBackupButton = findViewById(R.id.btn_backup_data);
        mPermissionSettingsButton = findViewById(R.id.btn_permission_settings);
        mStorageSettingsButton = findViewById(R.id.btn_storage_settings);
        mStoryProjectText = findViewById(R.id.txt_story_project);
        mStoryProjectName = findViewById(R.id.txt_story_project_name);
        refreshStoryProjectCard();
        findViewById(R.id.btn_story_project).setOnClickListener(v -> openStoryProjectFiles());
        findViewById(R.id.btn_choose_story_project).setOnClickListener(v -> chooseStoryProject());
        findViewById(R.id.btn_create_story_project).setOnClickListener(v -> showCreateStoryDialog());
        findViewById(R.id.btn_appearance_settings).setOnClickListener(v ->
            startActivity(new Intent(this, CoomiAppearanceActivity.class)));
        findViewById(R.id.btn_feedback).setOnClickListener(v -> openFeedback());

        mOpenChatButton.setOnClickListener(v -> openChat());
        mRestartButton.setOnClickListener(v -> restartEngine());
        mStopButton.setOnClickListener(v -> stopEngine());
        mOpenWebUiButton.setOnClickListener(v -> openWebUi());
        mUsbBridgeButton.setOnClickListener(v -> toggleUsbBridge());
        mCatalogButton.setOnClickListener(v -> openCatalog());
        mFilesButton.setOnClickListener(v -> openFiles());
        mProvidersButton = findViewById(R.id.btn_web_providers);
        mRuntimeButton = findViewById(R.id.btn_web_runtime);
        mProvidersButton.setOnClickListener(v -> openProviders());
        mRuntimeButton.setOnClickListener(v -> openRuntime());
        mCheckUpdateButton.setOnClickListener(v -> checkUpdate());
        mHomeSettingsButton.setOnClickListener(v ->
            startActivity(new Intent(this, CoomiHomeSettingActivity.class)));
        mBackupButton.setOnClickListener(v ->
            startActivity(new Intent(this, CoomiBackupActivity.class)));
        mPermissionSettingsButton.setOnClickListener(v -> openPermissionSettings());
        mStorageSettingsButton.setOnClickListener(v -> openStorageSettings());

        // Start auto-refresh
        mStatusRunnable = new Runnable() {
            @Override
            public void run() {
                refreshStatus();
                mHandler.postDelayed(this, STATUS_REFRESH_MS);
            }
        };

        if (CoomiDemo.isEnabled()) {
            applyDemoState();
            return;
        }

        mHandler.post(mStatusRunnable);

    }

    @Override
    protected void onResume() {
        super.onResume();
        String activeTheme = CoomiTheme.getMode(this);
        if (!activeTheme.equals(mAppliedTheme)) {
            recreate();
            return;
        }
        refreshStoryProjectCard();
    }

    /**
     * 切换故事项目目录：只允许在 Storydex 内置环境（filesDir）内选择。
     * 复用前端文件管理器（pick 模式）浏览并选择内置环境中的目录，
     * 不再走系统 SAF 目录选择器（避免选取外部存储目录）。
     */
    private void chooseStoryProject() {
        Intent intent = new Intent(this, com.termux.app.CoomiActivity.class);
        intent.putExtra(com.termux.app.CoomiActivity.EXTRA_ROUTE, "#/files?pick=1");
        startActivity(intent);
    }

    private void refreshStoryProjectCard() {
        if (mStoryProjectText == null || mStoryProjectName == null) return;
        File project = new File(CoomiStoryPreference.getProjectPath(this));
        String name = project.getName();
        mStoryProjectName.setText(name == null || name.trim().isEmpty()
            ? getString(R.string.storydex_project_current) : name);
        mStoryProjectText.setText(project.getAbsolutePath());
    }

    private void openStoryProjectFiles() {
        Intent intent = new Intent(this, com.termux.app.CoomiActivity.class);
        intent.putExtra(com.termux.app.CoomiActivity.EXTRA_ROUTE, "#/files?root=story");
        startActivity(intent);
    }

    private void showCreateStoryDialog() {
        EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setHint(R.string.storydex_project_create_hint);
        int padding = Math.round(20 * getResources().getDisplayMetrics().density);
        android.widget.FrameLayout container = new android.widget.FrameLayout(this);
        container.setPadding(padding, 0, padding, 0);
        container.addView(input);
        android.app.AlertDialog dialog = new android.app.AlertDialog.Builder(this)
            .setTitle(R.string.storydex_project_create_title)
            .setMessage(R.string.storydex_project_create_message)
            .setView(container)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.storydex_project_create_action, null)
            .create();
        dialog.setOnShowListener(ignored -> dialog.getButton(android.app.AlertDialog.BUTTON_POSITIVE)
            .setOnClickListener(v -> {
                String name = input.getText().toString().trim();
                if (name.isEmpty() || name.equals(".") || name.equals("..")
                    || name.contains("/") || name.contains("\\")) {
                    input.setError(getString(R.string.storydex_project_name_invalid));
                    return;
                }
                // 新建故事一律创建在内置环境的 stories 根目录下，避免跟随旧项目位置跑到外部。
                File parent = CoomiStoryPreference.getStoriesRoot(this);
                File project = new File(parent, name);
                if (project.exists()) {
                    input.setError(getString(R.string.storydex_project_exists));
                    return;
                }
                if (!CoomiStoryPreference.setProjectPath(this, project.getAbsolutePath())) {
                    input.setError(getString(R.string.storydex_project_unavailable));
                    return;
                }
                refreshStoryProjectCard();
                dialog.dismiss();
                Toast.makeText(this, R.string.storydex_project_created, Toast.LENGTH_SHORT).show();
            }));
        dialog.show();
    }

    /** 演示包：引擎和终端都不存在，界面上直说，别让人以为它在跑。 */
    private void applyDemoState() {
        mStatusIndicator.setBackgroundResource(R.drawable.coomi_dot_idle);
        mStatusText.setText(R.string.coomi_demo_dash_status);
        mRestartButton.setEnabled(false);
        mStopButton.setEnabled(false);
        if (mWebUiButtonContainer != null) mWebUiButtonContainer.setVisibility(View.GONE);
    }

    @Override
    protected void onStart() {
        super.onStart();
        // 演示包不连服务、不拉引擎守护 —— 它们干的都是真事。
        if (CoomiDemo.isEnabled()) return;
        Intent intent = new Intent(this, CoomiService.class);
        bindService(intent, mConnection, Context.BIND_AUTO_CREATE);
        // Start the engine monitor if not running
        Intent monitorIntent = new Intent(this, CoomiEngineMonitor.class);
        startService(monitorIntent);
    }

    @Override
    protected void onStop() {
        super.onStop();
        if (mBound) {
            unbindService(mConnection);
            mBound = false;
        }
        mHandler.removeCallbacks(mStatusRunnable);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        mHandler.removeCallbacksAndMessages(null);
    }

    // ── Status refresh ──

    private void refreshStatus() {
        if (!mBound || mCoomiService == null) return;

        mCoomiService.getEngineStatus(result -> {
            if (!result.success) return;
            runOnUiThread(() -> {
                String status = result.stdout.trim();
                boolean running = status.equals("running");
                boolean starting = status.equals("starting");
                int indicator = running ? R.drawable.coomi_dot_ok
                    : starting ? R.drawable.coomi_dot_warn
                    : R.drawable.coomi_dot_idle;
                int label = running ? R.string.coomi_dash_engine_running
                    : starting ? R.string.coomi_dash_engine_starting
                    : R.string.coomi_dash_engine_stopped;
                mStatusIndicator.setBackgroundResource(indicator);
                mStatusText.setText(label);
                mRestartButton.setEnabled(!starting);
                mStopButton.setEnabled(running);
                if (mWebUiButtonContainer != null) {
                    mWebUiButtonContainer.setVisibility(running ? View.VISIBLE : View.GONE);
                }
                renderUsbBridge();
            });
        });
    }

    // ── Actions ──

    private void openChat() {
        startActivity(new Intent(this, com.termux.app.CoomiActivity.class));
    }

    private void openPermissionSettings() {
        Intent intent = new Intent(this, CoomiLauncherActivity.class);
        intent.putExtra(CoomiLauncherActivity.EXTRA_SETTINGS_MODE, true);
        startActivity(intent);
    }

    private void openStorageSettings() {
        try {
            Intent intent;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                intent = new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                    Uri.parse("package:" + getPackageName()));
            } else {
                intent = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:" + getPackageName()));
            }
            startActivity(intent);
        } catch (Exception error) {
            Toast.makeText(this, "无法打开手机存储权限设置", Toast.LENGTH_SHORT).show();
        }
    }

    @Override
    public void onBackPressed() {
        // 需求：控制台返回 = 退出 app，且退出后终止所有由 coomi 启动的进程。
        // 先异步停引擎（Rust 侧收到终止信号会清理全部工具子进程），
        // 再停前台保活服务与引擎宿主，最后退出。
        if (mBound && mCoomiService != null) {
            mCoomiService.stopEngine(result -> runOnUiThread(this::shutdownApp));
        } else {
            shutdownApp();
        }
    }

    private void shutdownApp() {
        try {
            stopService(new Intent(this, CoomiEngineMonitor.class));
            stopService(new Intent(this, CoomiService.class));
        } catch (Exception ignored) { /* 服务可能未启动 */ }
        finishAffinity();
    }

    private void restartEngine() {
        if (!mBound || mCoomiService == null) {
            Toast.makeText(this, R.string.coomi_dash_toast_no_service, Toast.LENGTH_SHORT).show();
            return;
        }
        mRestartButton.setEnabled(false);
        mStatusText.setText(R.string.coomi_dash_engine_starting);
        mCoomiService.restartEngine(result -> {
            runOnUiThread(() -> {
                mRestartButton.setEnabled(true);
                if (result.success) {
                    Toast.makeText(this, R.string.coomi_dash_toast_started, Toast.LENGTH_SHORT).show();
                } else {
                    Toast.makeText(this,
                        getString(R.string.coomi_dash_toast_start_failed, result.stderr),
                        Toast.LENGTH_LONG).show();
                }
                refreshStatus();
            });
        });
    }

    private void stopEngine() {
        if (!mBound || mCoomiService == null) return;
        mCoomiService.stopEngine(result -> {
            runOnUiThread(() -> {
                if (result.success) {
                    Toast.makeText(this, R.string.coomi_dash_toast_stopped, Toast.LENGTH_SHORT).show();
                }
                refreshStatus();
            });
        });
    }

    private void openTui() {
        if (demoUnavailable()) return;
        // 1) 先打开终端：确保 TermuxService / 终端会话先就绪
        Intent terminal = new Intent(this, TermuxActivity.class);
        terminal.putExtra("com.storydex.android.app.TERMUX_DIR", TermuxConstants.TERMUX_HOME_DIR_PATH);
        startActivity(terminal);
        // 2) 稍作延迟等终端会话起来后，再在新会话里执行 `coomi`（无子命令 = 交互式 TUI）。
        //    立即执行的话命令会跑在尚未就绪的 shell 上，导致打开的只是普通终端。
        new Handler(Looper.getMainLooper()).postDelayed(this::launchCoomiTui, 1200);
    }

    private void launchCoomiTui() {
        try {
            Intent intent = new Intent();
            intent.setClassName(this, TermuxConstants.TERMUX_APP.RUN_COMMAND_SERVICE_NAME);
            intent.setAction(TermuxConstants.TERMUX_APP.RUN_COMMAND_SERVICE.ACTION_RUN_COMMAND);
            intent.putExtra(TermuxConstants.TERMUX_APP.RUN_COMMAND_SERVICE.EXTRA_COMMAND_PATH,
                TermuxConstants.TERMUX_PREFIX_DIR_PATH + "/bin/coomi");
            intent.putExtra(TermuxConstants.TERMUX_APP.RUN_COMMAND_SERVICE.EXTRA_ARGUMENTS,
                new String[0]);
            intent.putExtra(TermuxConstants.TERMUX_APP.RUN_COMMAND_SERVICE.EXTRA_WORKDIR,
                TermuxConstants.TERMUX_HOME_DIR_PATH);
            // 0 = 切换到新会话并打开终端界面，前台执行命令
            intent.putExtra(TermuxConstants.TERMUX_APP.RUN_COMMAND_SERVICE.EXTRA_SESSION_ACTION,
                String.valueOf(TermuxConstants.TERMUX_APP.TERMUX_SERVICE.VALUE_EXTRA_SESSION_ACTION_SWITCH_TO_NEW_SESSION_AND_OPEN_ACTIVITY));
            startService(intent);
        } catch (Exception e) {
            Logger.logError(LOG_TAG, "Failed to launch Coomi TUI: " + e.getMessage());
        }
    }

    private void openTerminal() {
        if (demoUnavailable()) return;
        // Open Termux shell for debugging. TERMUX_DIR must match the bootstrap's baked-in
        // home path, so it comes from TermuxConstants rather than a literal.
        Intent intent = new Intent(this, TermuxActivity.class);
        intent.putExtra("com.storydex.android.app.TERMUX_DIR", TermuxConstants.TERMUX_HOME_DIR_PATH);
        startActivity(intent);
    }

    /** 演示包里终端后面没有 bootstrap，点进去只会看到一个空壳，直接说明白。 */
    private boolean demoUnavailable() {
        if (!CoomiDemo.isEnabled()) return false;
        Toast.makeText(this, R.string.coomi_demo_dash_unavailable, Toast.LENGTH_SHORT).show();
        return true;
    }

    private void openWebUi() {
        if (!mBound || mCoomiService == null) return;
        int port = mCoomiService.getEnginePort();
        // 与 WebView 一致：携带引擎令牌，浏览器打开后所有 API 才可用。
        String token = mCoomiService.getEngineToken();
        String url = "http://127.0.0.1:" + port + "/?token=" + token;
        Intent intent = new Intent(Intent.ACTION_VIEW);
        intent.setData(android.net.Uri.parse(url));
        try {
            startActivity(intent);
        } catch (Exception e) {
            Toast.makeText(this, R.string.coomi_dash_toast_no_browser, Toast.LENGTH_SHORT).show();
        }
    }

    /** 按开关与发布状态刷新 USB 调试桥那一行的文案。 */
    private void renderUsbBridge() {
        if (mUsbBridgeSwitch == null || mUsbBridgeDesc == null) return;
        boolean enabled = CoomiUsbBridge.isEnabled(this);
        mUsbBridgeSwitch.setChecked(enabled);
        int desc = !enabled ? R.string.coomi_dash_usb_bridge_off
            : CoomiUsbBridge.isPublished() ? R.string.coomi_dash_usb_bridge_on
            : R.string.coomi_dash_usb_bridge_pending;
        mUsbBridgeDesc.setText(desc);
    }

    /**
     * 开启前必须弹确认框：这会把引擎访问令牌写入共享存储，等于把私有目录的读写权限
     * 交给任何能读 /sdcard 的一方。关闭则直接生效，无需确认。
     */
    private void toggleUsbBridge() {
        if (CoomiUsbBridge.isEnabled(this)) {
            CoomiUsbBridge.setEnabled(this, false);
            applyUsbBridge();
            Toast.makeText(this, R.string.coomi_dash_usb_bridge_toast_off, Toast.LENGTH_SHORT).show();
            return;
        }
        new android.app.AlertDialog.Builder(this)
            .setTitle(R.string.coomi_dash_usb_bridge_confirm_title)
            .setMessage(R.string.coomi_dash_usb_bridge_confirm_message)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.coomi_dash_usb_bridge_confirm_ok, (d, w) -> {
                CoomiUsbBridge.setEnabled(this, true);
                applyUsbBridge();
                Toast.makeText(this, R.string.coomi_dash_usb_bridge_toast_on, Toast.LENGTH_LONG).show();
            })
            .show();
    }

    /** 让服务按新开关状态发布或收回桥文件，再回主线程刷新文案。 */
    private void applyUsbBridge() {
        if (mBound && mCoomiService != null) mCoomiService.refreshUsbBridge();
        else CoomiUsbBridge.clear();
        renderUsbBridge();
    }

    /** 打开应用内 SKILL / MCP 管理页（WebView 直达 #/catalog）。 */
    private void openCatalog() {
        Intent intent = new Intent(this, com.termux.app.CoomiActivity.class);
        intent.putExtra(com.termux.app.CoomiActivity.EXTRA_ROUTE, "#/catalog");
        startActivity(intent);
    }

    /** 打开应用内文件管理页（WebView 直达 #/files）。 */
    private void openFiles() {
        Intent intent = new Intent(this, com.termux.app.CoomiActivity.class);
        intent.putExtra(com.termux.app.CoomiActivity.EXTRA_ROUTE, "#/files");
        startActivity(intent);
    }

    /** 打开应用内 Provider / API Key 配置页（WebView 直达 #/providers）。 */
    private void openProviders() {
        Intent intent = new Intent(this, com.termux.app.CoomiActivity.class);
        intent.putExtra(com.termux.app.CoomiActivity.EXTRA_ROUTE, "#/providers");
        startActivity(intent);
    }

    /** 打开应用内内置环境页（WebView 直达 #/runtime）。 */
    private void openRuntime() {
        Intent intent = new Intent(this, com.termux.app.CoomiActivity.class);
        intent.putExtra(com.termux.app.CoomiActivity.EXTRA_ROUTE, "#/runtime");
        startActivity(intent);
    }

    private void openFeedback() {
        Intent intent = new Intent(this, com.termux.app.CoomiActivity.class);
        intent.putExtra(com.termux.app.CoomiActivity.EXTRA_ROUTE, "#/feedback");
        startActivity(intent);
    }

    /** 软件内检查更新：读取更新源 latest.json，有新版本则下载并安装。 */
    private void checkUpdate() {
        Toast.makeText(this, R.string.coomi_dash_checking, Toast.LENGTH_SHORT).show();
        UpdateChecker.checkAndPrompt(this, () -> refreshStatus());
    }

    /** 进入控制台时静默检查一次：有新版本则在「检查更新」旁亮红点提示。 */
    private void checkUpdateSilently() {
        UpdateChecker.checkSilent(this, (hasUpdate, version, notes, error) -> {
            if (hasUpdate) {
                mUpdateDot.setVisibility(View.VISIBLE);
                mCheckUpdateDesc.setText("发现新版本 " + version + "，点击更新");
            }
        });
    }

}
