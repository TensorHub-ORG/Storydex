package app.coomi;

import android.app.Activity;
import android.os.Bundle;
import android.widget.RadioGroup;

import com.termux.R;

/** Five mobile palettes mirror the desktop Storydex appearance presets. */
public final class CoomiAppearanceActivity extends Activity {
    private final int[] rows = {
        R.id.theme_white, R.id.theme_default, R.id.theme_snow, R.id.theme_book, R.id.theme_dark
    };
    private final String[] codes = {"white", "default", "snow", "book", "dark"};
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
        String active = CoomiTheme.getMode(this);
        for (int i = 0; i < rows.length; i++) {
            if (codes[i].equals(active)) {
                mThemeGroup.check(rows[i]);
                return;
            }
        }
    }
}
