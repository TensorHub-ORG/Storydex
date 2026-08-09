package app.coomi;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.net.ConnectivityManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.view.View;
import android.widget.Button;
import android.widget.TextView;

import androidx.annotation.Nullable;
import androidx.core.app.NotificationManagerCompat;

import com.termux.R;
import com.termux.app.TermuxInstaller;
import com.termux.shared.logger.Logger;

import java.io.File;

/**
 * Coomi Launcher / Splash Activity.
 *
 * Phase 1 (Welcome): Permission guides (notification + battery).
 * Phase 2 (Loading): Route based on setup state:
 *   1. Bootstrap not extracted → wait for TermuxInstaller
 *   2. coomi-rs not deployed → SetupActivity (deploy)
 *   3. API key not configured → SetupActivity (auth)
 *   4. All ready → DashboardActivity
 */
public class CoomiLauncherActivity extends Activity {

    public static final String EXTRA_SETTINGS_MODE = "settings_mode";

    private static final String LOG_TAG = "CoomiLauncherActivity";
    private static final int REQUEST_CODE_NOTIFICATION = 1001;
    private static final int REQUEST_CODE_BATTERY = 1002;
    private static final String PREFS_NAME = "coomi_launcher";
    private static final String PREF_CONTINUE = "onboarding_continue";

    private View mWelcomeContainer;
    private View mLoadingContainer;
    private TextView mStatusText;
    private Button mNotificationButton;
    private Button mBatteryButton;
    private Button mRootButton;
    private Button mShizukuButton;
    private Button mContinueButton;

    private Handler mHandler = new Handler(Looper.getMainLooper());
    private RootAccessController mRootAccessController;
    private ShizukuAccessController mShizukuAccessController;
    private boolean mRootCheckInFlight = false;
    private boolean mShizukuCheckInFlight = false;
    private boolean mPermissionsDone = false;
    private boolean mContinuePersisted = false;
    private boolean mSettingsMode = false;

    @Override
    protected void onCreate(@Nullable Bundle savedInstanceState) {
        CoomiTheme.applyTheme(this);
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_coomi_launcher);

        mWelcomeContainer = findViewById(R.id.welcome_container);
        mLoadingContainer = findViewById(R.id.loading_container);
        mStatusText = findViewById(R.id.launcher_status_text);
        mNotificationButton = findViewById(R.id.btn_notification_permission);
        mBatteryButton = findViewById(R.id.btn_battery_permission);
        mRootButton = findViewById(R.id.btn_root_permission);
        mShizukuButton = findViewById(R.id.btn_shizuku_permission);
        mContinueButton = findViewById(R.id.btn_continue);
        mRootAccessController = new RootAccessController();
        mShizukuAccessController = new ShizukuAccessController();
        mShizukuAccessController.setStateListener(this::updateShizukuButton);

        mContinuePersisted = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
            .getBoolean(PREF_CONTINUE, false);
        mSettingsMode = getIntent().getBooleanExtra(EXTRA_SETTINGS_MODE, false);
        if (mSettingsMode) mContinuePersisted = false;

        mNotificationButton.setOnClickListener(v -> openNotificationSettings());
        mBatteryButton.setOnClickListener(v -> requestBatteryExemption());
        mRootButton.setOnClickListener(v -> checkRootPermission());
        mShizukuButton.setOnClickListener(v -> requestShizukuPermission());
        mContinueButton.setOnClickListener(v -> {
            mPermissionsDone = true;
            getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
                .edit().putBoolean(PREF_CONTINUE, true).apply();
            mContinuePersisted = true;
            if (mSettingsMode) {
                startActivity(new Intent(this, CoomiDashboardActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT));
                finish();
                return;
            }
            showLoadingPhase();
            mHandler.postDelayed(this::checkAndRoute, 300);
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (mSettingsMode) {
            showWelcomePhase();
            updatePermissionStatus();
            return;
        }
        if (mPermissionsDone || mContinuePersisted) {
            showLoadingPhase();
            mHandler.postDelayed(this::checkAndRoute, 300);
            return;
        }
        showWelcomePhase();
        updatePermissionStatus();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        mHandler.removeCallbacksAndMessages(null);
        if (mRootAccessController != null) mRootAccessController.cancel();
        if (mShizukuAccessController != null) mShizukuAccessController.close();
    }

    // ── Phase display ──

    private void showWelcomePhase() {
        mWelcomeContainer.setVisibility(View.VISIBLE);
        mLoadingContainer.setVisibility(View.GONE);
    }

    private void showLoadingPhase() {
        mWelcomeContainer.setVisibility(View.GONE);
        mLoadingContainer.setVisibility(View.VISIBLE);
    }

    // ── Permissions ──

    private boolean areNotificationsEnabled() {
        return NotificationManagerCompat.from(this).areNotificationsEnabled();
    }

    private boolean isBatteryExempt() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            return pm != null && pm.isIgnoringBatteryOptimizations(getPackageName());
        }
        return true;
    }

    private void openNotificationSettings() {
        try {
            Intent intent = new Intent();
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                intent.setAction(Settings.ACTION_APP_NOTIFICATION_SETTINGS);
                intent.putExtra(Settings.EXTRA_APP_PACKAGE, getPackageName());
            } else {
                intent.setAction("android.settings.APP_NOTIFICATION_SETTINGS");
                intent.putExtra("app_package", getPackageName());
                intent.putExtra("app_uid", getApplicationInfo().uid);
            }
            startActivityForResult(intent, REQUEST_CODE_NOTIFICATION);
        } catch (Exception e) {
            Logger.logError(LOG_TAG, "Failed to open notification settings: " + e.getMessage());
        }
    }

    private void requestBatteryExemption() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            try {
                Intent intent = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
                intent.setData(Uri.parse("package:" + getPackageName()));
                startActivityForResult(intent, REQUEST_CODE_BATTERY);
            } catch (Exception e) {
                Logger.logError(LOG_TAG, "Battery exemption request failed: " + e.getMessage());
            }
        }
    }

    /** Root 权限检查（可选）：调用 RootAccessController 执行 su -c id 探测授权状态。 */
    private void checkRootPermission() {
        if (mRootCheckInFlight || mRootAccessController == null) return;
        mRootCheckInFlight = true;
        mRootButton.setEnabled(false);
        mRootButton.setText(R.string.coomi_root_checking);
        mRootAccessController.check(result -> {
            if (isFinishing()
                || (Build.VERSION.SDK_INT >= Build.VERSION_CODES.JELLY_BEAN_MR1 && isDestroyed())) {
                return;
            }
            mRootCheckInFlight = false;
            switch (result.status) {
                case GRANTED:
                    mRootButton.setText(R.string.coomi_authorized);
                    mRootButton.setEnabled(false);
                    break;
                case DENIED:
                case TIMEOUT:
                case ERROR:
                default:
                    mRootButton.setText(R.string.coomi_root_retry);
                    mRootButton.setEnabled(true);
                    break;
                case UNAVAILABLE:
                    mRootButton.setText(R.string.coomi_root_unavailable);
                    mRootButton.setEnabled(true);
                    break;
            }
        });
    }

    private void requestShizukuPermission() {
        if (mShizukuCheckInFlight || mShizukuAccessController == null) return;
        mShizukuCheckInFlight = true;
        mShizukuButton.setEnabled(false);
        mShizukuButton.setText(R.string.coomi_shizuku_checking);
        mShizukuAccessController.request(result -> {
            if (isFinishing()
                || (Build.VERSION.SDK_INT >= Build.VERSION_CODES.JELLY_BEAN_MR1 && isDestroyed())) {
                return;
            }
            mShizukuCheckInFlight = false;
            updateShizukuButton(result);
        });
    }

    private void updateShizukuButton(ShizukuAccessController.Result result) {
        if (mShizukuButton == null || result == null) return;
        if (mShizukuCheckInFlight && result.status != ShizukuAccessController.Status.GRANTED) {
            mShizukuButton.setEnabled(false);
            mShizukuButton.setText(R.string.coomi_shizuku_checking);
            return;
        }
        switch (result.status) {
            case GRANTED:
                mShizukuButton.setText(R.string.coomi_authorized);
                mShizukuButton.setEnabled(false);
                break;
            case REQUESTABLE:
                mShizukuButton.setText(R.string.coomi_shizuku_grant);
                mShizukuButton.setEnabled(true);
                break;
            case DENIED:
                mShizukuButton.setText(R.string.coomi_shizuku_retry);
                mShizukuButton.setEnabled(true);
                break;
            case NOT_RUNNING:
                mShizukuButton.setText(R.string.coomi_shizuku_not_running);
                mShizukuButton.setEnabled(true);
                break;
            case UNAVAILABLE:
            case ERROR:
            default:
                mShizukuButton.setText(R.string.coomi_shizuku_unavailable);
                mShizukuButton.setEnabled(true);
                break;
        }
    }

    private void updatePermissionStatus() {
        boolean notifOk = areNotificationsEnabled();
        boolean battOk = isBatteryExempt();

        // 药丸自己表达状态：未授权=蓝底白字「允许」，已授权=浅绿底绿字且不可点。
        mNotificationButton.setEnabled(!notifOk);
        mNotificationButton.setText(notifOk ? R.string.coomi_enabled : R.string.coomi_allow);

        mBatteryButton.setEnabled(!battOk);
        mBatteryButton.setText(battOk ? R.string.coomi_granted : R.string.coomi_allow);

        if (mShizukuAccessController != null) {
            updateShizukuButton(mShizukuAccessController.getStatus());
        }

        // 演示包不为权限拦人：这两个开关只影响引擎常驻，而演示包没有引擎。
        mContinueButton.setEnabled(CoomiDemo.isEnabled() || (notifOk && battOk));
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQUEST_CODE_NOTIFICATION || requestCode == REQUEST_CODE_BATTERY) {
            updatePermissionStatus();
        }
    }

    // ── Routing ──

    private void checkAndRoute() {
        // 演示包：不查 bootstrap、不查原生引擎、不查 API Key —— 走完引导就进仪表盘。
        if (CoomiDemo.isEnabled()) {
            mStatusText.setText(R.string.coomi_demo_routing);
            Intent demoIntent;
            if (CoomiDemo.isOnboarded(this)) {
                demoIntent = new Intent(this, CoomiDashboardActivity.class);
            } else {
                demoIntent = new Intent(this, CoomiSetupActivity.class);
                demoIntent.putExtra(CoomiSetupActivity.EXTRA_START_STEP, CoomiConstants.STEP_DEPLOY);
            }
            startActivity(demoIntent);
            finish();
            return;
        }

        if (!CoomiService.isBootstrapInstalled()) {
            Logger.logInfo(LOG_TAG, "Bootstrap not ready");
            mStatusText.setText(R.string.coomi_setting_up_environment);
            TermuxInstaller.setupBootstrapIfNeeded(this, this::checkAndRoute);
            return;
        }

        if (!CoomiService.isDeployComplete()) {
            Logger.logInfo(LOG_TAG, "coomi-rs not deployed, routing to setup");
            mStatusText.setText(R.string.coomi_setup_required);
            Intent intent = new Intent(this, CoomiSetupActivity.class);
            intent.putExtra(CoomiSetupActivity.EXTRA_START_STEP, CoomiConstants.STEP_DEPLOY);
            startActivity(intent);
            finish();
            return;
        }

        if (!CoomiConfig.isConfigured()) {
            Logger.logInfo(LOG_TAG, "Not configured, routing to auth step");
            mStatusText.setText(R.string.coomi_setup_required);
            Intent intent = new Intent(this, CoomiSetupActivity.class);
            intent.putExtra(CoomiSetupActivity.EXTRA_START_STEP, CoomiConstants.STEP_AUTH);
            startActivity(intent);
            finish();
            return;
        }

        // 主界面路由：按「启动首页」设置决定进控制台还是直接进对话。
        Logger.logInfo(LOG_TAG, "All ready, routing to "
            + (CoomiHomePreference.isChatHome(this) ? "chat" : "dashboard"));
        mStatusText.setText(R.string.coomi_starting);
        Intent intent = new Intent(this, CoomiHomePreference.isChatHome(this)
            ? com.termux.app.CoomiActivity.class : CoomiDashboardActivity.class);
        startActivity(intent);
        finish();
    }
}
