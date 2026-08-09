package app.coomi;

import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.view.View;
import android.view.Window;

import androidx.annotation.NonNull;

import com.termux.R;

/** Applies the same five appearance palettes to native pages and the WebView. */
public final class CoomiTheme {

    /** Desktop-compatible palette codes. Legacy values remain readable during migration. */
    public static final String MODE_SYSTEM = "system";
    public static final String MODE_LIGHT = "light";
    public static final String MODE_DARK = "dark";
    public static final String MODE_WHITE = "white";
    public static final String MODE_DEFAULT = "default";
    public static final String MODE_SNOW = "snow";
    public static final String MODE_BOOK = "book";

    public static final String PREF_THEME_MODE = "coomi.themeMode";
    private static final String PREF_NAME = "coomi_settings";

    private CoomiTheme() {}

    /** 当前档位，非法值一律回落到默认的雪纸蓝白。 */
    @NonNull
    public static String getMode(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE);
        String mode = prefs.getString(PREF_THEME_MODE, MODE_SNOW);
        if (MODE_SYSTEM.equals(mode) || MODE_LIGHT.equals(mode)) return MODE_SNOW;
        return isValid(mode) ? mode : MODE_SNOW;
    }

    /** 保存档位并立即应用系统栏颜色（Activity 已创建后的运行时切换）。 */
    public static void setMode(Context context, String mode) {
        if (!isValid(mode)) return;
        context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE)
            .edit().putString(PREF_THEME_MODE, mode).apply();
    }

    /** 只有纯净暗色使用深色系统栏图标策略。 */
    public static boolean isDark(Context context) {
        String mode = getMode(context);
        return MODE_DARK.equals(mode);
    }

    private static boolean isValid(String mode) {
        return MODE_WHITE.equals(mode) || MODE_DEFAULT.equals(mode) || MODE_SNOW.equals(mode)
            || MODE_BOOK.equals(mode) || MODE_DARK.equals(mode);
    }

    /** 常规页面必须在 {@code super.onCreate} 之前应用主题。 */
    public static void applyTheme(Activity activity) {
        activity.setTheme(baseTheme(getMode(activity)));
    }

    /** 页面底色变体，用于 Dashboard 和设置页。 */
    public static void applyPageTheme(Activity activity) {
        activity.setTheme(pageTheme(getMode(activity)));
    }

    /** WebView 宿主闪屏使用与网页相同的背景色。 */
    public static void applyWebTheme(Activity activity) {
        activity.setTheme(webTheme(getMode(activity)));
    }

    private static int baseTheme(String mode) {
        switch (mode) {
            case MODE_WHITE: return R.style.Theme_Coomi_White;
            case MODE_DEFAULT: return R.style.Theme_Coomi_Default;
            case MODE_BOOK: return R.style.Theme_Coomi_Book;
            case MODE_DARK: return R.style.Theme_Coomi_Night;
            case MODE_SNOW:
            default: return R.style.Theme_Coomi_Snow;
        }
    }

    private static int pageTheme(String mode) {
        switch (mode) {
            case MODE_WHITE: return R.style.Theme_Coomi_White_Page;
            case MODE_DEFAULT: return R.style.Theme_Coomi_Default_Page;
            case MODE_BOOK: return R.style.Theme_Coomi_Book_Page;
            case MODE_DARK: return R.style.Theme_Coomi_Night_Page;
            case MODE_SNOW:
            default: return R.style.Theme_Coomi_Snow_Page;
        }
    }

    private static int webTheme(String mode) {
        switch (mode) {
            case MODE_WHITE: return R.style.Theme_Coomi_White_Web;
            case MODE_DEFAULT: return R.style.Theme_Coomi_Default_Web;
            case MODE_BOOK: return R.style.Theme_Coomi_Book_Web;
            case MODE_DARK: return R.style.Theme_Coomi_Night_Web;
            case MODE_SNOW:
            default: return R.style.Theme_Coomi_Snow_Web;
        }
    }

    private static int systemBarColor(Activity activity, String mode) {
        switch (mode) {
            case MODE_WHITE: return activity.getColor(R.color.coomi_theme_white_bg);
            case MODE_DEFAULT: return activity.getColor(R.color.coomi_theme_default_bg);
            case MODE_BOOK: return activity.getColor(R.color.coomi_theme_book_bg);
            case MODE_DARK: return activity.getColor(R.color.coomi_night_bg);
            case MODE_SNOW:
            default: return activity.getColor(R.color.coomi_theme_snow_bg);
        }
    }

    /**
     * Activity 已创建后的运行时系统栏刷新（setThemeMode 切换档位时调用）。
     * 状态栏颜色与图标跟随 isDark；导航栏也一并处理。
     */
    public static void applySystemBars(Activity activity) {
        boolean dark = isDark(activity);
        int background = systemBarColor(activity, getMode(activity));
        Window window = activity.getWindow();
        window.setStatusBarColor(background);
        window.setNavigationBarColor(background);
        View decor = window.getDecorView();
        int flags = decor.getSystemUiVisibility();
        if (dark) {
            decor.setSystemUiVisibility(flags & ~View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR);
        } else {
            decor.setSystemUiVisibility(flags | View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR);
        }
    }
}
