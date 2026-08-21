package app.coomi;

import android.app.Activity;
import android.os.Bundle;
import android.widget.RadioGroup;

import com.termux.R;

/** Ten mobile palettes mirror the desktop Storydex appearance presets. */
public final class CoomiAppearanceActivity extends Activity {
    // 与 CoomiTheme.MODE_* / 前端 config.ts 的 THEME_MODES 顺序一致：先五档浅色，再五档深色。
    private final int[] rows = {
        R.id.theme_white, R.id.theme_default, R.id.theme_snow, R.id.theme_book,
        R.id.theme_celadon, R.id.theme_linen,
        R.id.theme_dark, R.id.theme_ink, R.id.theme_abyss, R.id.theme_ember
    };
    private final String[] codes = {
        CoomiTheme.MODE_WHITE, CoomiTheme.MODE_DEFAULT, CoomiTheme.MODE_SNOW, CoomiTheme.MODE_BOOK,
        CoomiTheme.MODE_CELADON, CoomiTheme.MODE_LINEN,
        CoomiTheme.MODE_DARK, CoomiTheme.MODE_INK, CoomiTheme.MODE_ABYSS, CoomiTheme.MODE_EMBER
    };
    private RadioGroup mThemeGroup;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        CoomiTheme.applyPageTheme(this);
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_storydex_appearance);
        findViewById(R.id.btn_appearance_back).setOnClickListener(v -> finish());
        mThemeGroup = findViewById(R.id.theme_group);
        mThemeGroup.setOnCheckedChangeListener((group, checkedId) -> select(checkedId));
        refresh();
    }

    private void select(int checkedId) {
        String code = null;
        for (int i = 0; i < rows.length; i++) {
            if (rows[i] == checkedId) {
                code = codes[i];
                break;
            }
        }
        if (code == null || code.equals(CoomiTheme.getMode(this))) return;
        CoomiTheme.setMode(this, code);
        recreate();
    }

    private void refresh() {
        // codes 必须覆盖 CoomiTheme.isValid 的全部档位：getMode 只会回落到 snow，
        // 少一档就会走完循环一个都不选中，这个页面于是显示成「没有任何选项被选」。
        String active = CoomiTheme.getMode(this);
        for (int i = 0; i < rows.length; i++) {
            if (codes[i].equals(active)) {
                mThemeGroup.check(rows[i]);
                return;
            }
        }
    }
}
