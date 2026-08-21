package app.coomi;

import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.view.View;
import android.view.Window;

import androidx.annotation.NonNull;

import com.termux.R;

/** Applies the same ten appearance palettes to native pages and the WebView. */
public final class CoomiTheme {

    /** Desktop-compatible palette codes. Legacy values remain readable during migration. */
    public static final String MODE_SYSTEM = "system";
    public static final String MODE_LIGHT = "light";
    public static final String MODE_DARK = "dark";
    public static final String MODE_WHITE = "white";
    public static final String MODE_DEFAULT = "default";
    public static final String MODE_SNOW = "snow";
    public static final String MODE_BOOK = "book";
    public static final String MODE_CELADON = "celadon";
    public static final String MODE_LINEN = "linen";
    public static final String MODE_INK = "ink";
    public static final String MODE_ABYSS = "abyss";
    public static final String MODE_EMBER = "ember";

    public static final String PREF_THEME_MODE = "coomi.themeMode";
    private static final String PREF_NAME = "coomi_settings";

    private CoomiTheme() {}

    /** 当前档位，非法值一律回落到默认的经典蓝白。 */
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

    /** 深色系全档位都用浅色系统栏图标策略（前端 config.ts 的 DARK_THEMES 与此一一对应）。 */
    public static boolean isDark(Context context) {
        String mode = getMode(context);
        return MODE_DARK.equals(mode) || MODE_INK.equals(mode)
            || MODE_ABYSS.equals(mode) || MODE_EMBER.equals(mode);
    }

    private static boolean isValid(String mode) {
        return MODE_WHITE.equals(mode) || MODE_DEFAULT.equals(mode) || MODE_SNOW.equals(mode)
            || MODE_BOOK.equals(mode) || MODE_DARK.equals(mode)
            || MODE_CELADON.equals(mode) || MODE_LINEN.equals(mode)
            || MODE_INK.equals(mode) || MODE_ABYSS.equals(mode) || MODE_EMBER.equals(mode);
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
            case MODE_CELADON: return R.style.Theme_Coomi_Celadon;
            case MODE_LINEN: return R.style.Theme_Coomi_Linen;
            case MODE_DARK: return R.style.Theme_Coomi_Night;
            case MODE_INK: return R.style.Theme_Coomi_Ink;
            case MODE_ABYSS: return R.style.Theme_Coomi_Abyss;
            case MODE_EMBER: return R.style.Theme_Coomi_Ember;
            case MODE_SNOW:
            default: return R.style.Theme_Coomi_Snow;
        }
    }

    private static int pageTheme(String mode) {
        switch (mode) {
            case MODE_WHITE: return R.style.Theme_Coomi_White_Page;
            case MODE_DEFAULT: return R.style.Theme_Coomi_Default_Page;
            case MODE_BOOK: return R.style.Theme_Coomi_Book_Page;
            case MODE_CELADON: return R.style.Theme_Coomi_Celadon_Page;
            case MODE_LINEN: return R.style.Theme_Coomi_Linen_Page;
            case MODE_DARK: return R.style.Theme_Coomi_Night_Page;
            case MODE_INK: return R.style.Theme_Coomi_Ink_Page;
            case MODE_ABYSS: return R.style.Theme_Coomi_Abyss_Page;
            case MODE_EMBER: return R.style.Theme_Coomi_Ember_Page;
            case MODE_SNOW:
            default: return R.style.Theme_Coomi_Snow_Page;
        }
    }

    private static int webTheme(String mode) {
        switch (mode) {
            case MODE_WHITE: return R.style.Theme_Coomi_White_Web;
            case MODE_DEFAULT: return R.style.Theme_Coomi_Default_Web;
            case MODE_BOOK: return R.style.Theme_Coomi_Book_Web;
            case MODE_CELADON: return R.style.Theme_Coomi_Celadon_Web;
            case MODE_LINEN: return R.style.Theme_Coomi_Linen_Web;
            case MODE_DARK: return R.style.Theme_Coomi_Night_Web;
            case MODE_INK: return R.style.Theme_Coomi_Ink_Web;
            case MODE_ABYSS: return R.style.Theme_Coomi_Abyss_Web;
            case MODE_EMBER: return R.style.Theme_Coomi_Ember_Web;
            case MODE_SNOW:
            default: return R.style.Theme_Coomi_Snow_Web;
        }
    }

    private static int systemBarColor(Activity activity, String mode) {
        switch (mode) {
            case MODE_WHITE: return activity.getColor(R.color.coomi_theme_white_bg);
            case MODE_DEFAULT: return activity.getColor(R.color.coomi_theme_default_bg);
            case MODE_BOOK: return activity.getColor(R.color.coomi_theme_book_bg);
            case MODE_CELADON: return activity.getColor(R.color.coomi_theme_celadon_bg);
            case MODE_LINEN: return activity.getColor(R.color.coomi_theme_linen_bg);
            case MODE_DARK: return activity.getColor(R.color.coomi_night_bg);
            case MODE_INK: return activity.getColor(R.color.coomi_theme_ink_bg);
            case MODE_ABYSS: return activity.getColor(R.color.coomi_theme_abyss_bg);
            case MODE_EMBER: return activity.getColor(R.color.coomi_theme_ember_bg);
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
