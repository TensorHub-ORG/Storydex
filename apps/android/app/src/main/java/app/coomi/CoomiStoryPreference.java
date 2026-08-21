package app.coomi;

import android.content.Context;
import android.content.SharedPreferences;

import java.io.File;
import java.io.IOException;

/**
 * Persists the one story project that scopes every mobile story run.
 *
 * 两级目录设计：
 * - 根目录（stories 根）：filesDir/stories，用于存放所有故事项目目录。
 * - 故事目录：根目录下的某个具体子目录（如 filesDir/stories/default），
 *   即当前选中的故事项目。
 *
 * 约束：故事项目目录必须是 stories 根的子目录（根目录本身是容器，不可作为项目）。
 * setProjectPath 拒绝越界路径；getProjectPath 对越界的历史值回退到 stories/default。
 */
public final class CoomiStoryPreference {
    private static final String PREFS_NAME = "coomi_settings";
    private static final String KEY_PROJECT_PATH = "story_project_path";
    /** 包内可见：CoomiUsbBridge 发布桥文件时要拼出 stories 目录的绝对路径。 */
    static final String STORIES_DIR_NAME = "stories";

    private CoomiStoryPreference() {}

    /** 内置环境中存放所有故事项目的根目录：filesDir/stories。 */
    public static File getStoriesRoot(Context context) {
        return new File(context.getFilesDir(), STORIES_DIR_NAME);
    }

    /** 项目路径是否位于 stories 根之内（必须是根目录的子目录，根目录本身不算）。 */
    private static boolean isInsideStoriesRoot(Context context, File directory) {
        String root;
        try {
            root = getStoriesRoot(context).getCanonicalPath();
        } catch (IOException e) {
            root = getStoriesRoot(context).getAbsolutePath();
        }
        String target;
        try {
            target = directory.getCanonicalPath();
        } catch (IOException e) {
            target = directory.getAbsolutePath();
        }
        return target.startsWith(root + File.separator);
    }

    public static String getProjectPath(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        String fallback = new File(getStoriesRoot(context), "default").getAbsolutePath();
        String value = prefs.getString(KEY_PROJECT_PATH, fallback);
        File directory = new File(value == null || value.trim().isEmpty() ? fallback : value.trim());
        // 越界（不在 stories 根内）或不可用的历史值一律回退到 stories/default。
        if (!isInsideStoriesRoot(context, directory) || !ensureStructure(directory)) {
            directory = new File(fallback);
            ensureStructure(directory);
        }
        return directory.getAbsolutePath();
    }

    public static boolean setProjectPath(Context context, String path) {
        if (path == null || path.trim().isEmpty()) return false;
        File directory = new File(path.trim());
        try {
            directory = directory.getCanonicalFile();
            if (!directory.isAbsolute() || !isInsideStoriesRoot(context, directory)) return false;
            if (!ensureStructure(directory)) return false;
            context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .edit().putString(KEY_PROJECT_PATH, directory.getAbsolutePath()).apply();
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
