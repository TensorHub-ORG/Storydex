package app.coomi;

import android.content.Context;
import android.content.SharedPreferences;

import java.io.File;

/** Persists the one story project that scopes every mobile story run. */
public final class CoomiStoryPreference {
    private static final String PREFS_NAME = "coomi_settings";
    private static final String KEY_PROJECT_PATH = "story_project_path";

    private CoomiStoryPreference() {}

    public static String getProjectPath(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        String fallback = new File(context.getFilesDir(), "stories/default").getAbsolutePath();
        String value = prefs.getString(KEY_PROJECT_PATH, fallback);
        File directory = new File(value == null || value.trim().isEmpty() ? fallback : value.trim());
        if (!ensureStructure(directory)) {
            directory = new File(fallback);
            ensureStructure(directory);
        }
        return directory.getAbsolutePath();
    }

    public static boolean setProjectPath(Context context, String path) {
        if (path == null || path.trim().isEmpty()) return false;
        File directory = new File(path.trim());
        try {
            String target = directory.getCanonicalPath();
            directory = new File(target);
            if (!directory.isAbsolute() || !ensureStructure(directory)) return false;
            context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .edit().putString(KEY_PROJECT_PATH, target).apply();
            return true;
        } catch (Exception ignored) {
            return false;
        }
    }

    private static boolean ensureStructure(File directory) {
        if (!directory.exists() && !directory.mkdirs()) return false;
        if (!directory.isDirectory() || !directory.canRead() || !directory.canWrite()) return false;
        String[] children = {
            "chapters",
            ".storydex/characters",
            ".storydex/worldbook",
            ".storydex/wiki"
        };
        for (String child : children) {
            File path = new File(directory, child);
            if (!path.exists() && !path.mkdirs()) return false;
            if (!path.isDirectory()) return false;
        }
        return true;
    }
}
