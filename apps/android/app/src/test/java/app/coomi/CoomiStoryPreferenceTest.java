package app.coomi;

import android.content.Context;

import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;

import java.io.File;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

/** CoomiStoryPreference 的 stories 根约束测试（根目录/故事目录两级设计）。 */
@RunWith(RobolectricTestRunner.class)
public class CoomiStoryPreferenceTest {

    private Context context() {
        return RuntimeEnvironment.getApplication();
    }

    private File filesDir() {
        return context().getFilesDir();
    }

    @Test
    public void defaultProjectLivesUnderStoriesRoot() {
        File project = new File(CoomiStoryPreference.getProjectPath(context()));
        String storiesRoot = CoomiStoryPreference.getStoriesRoot(context()).getAbsolutePath();
        assertTrue("默认项目应位于 stories 根内",
            project.getAbsolutePath().startsWith(storiesRoot + File.separator));
    }

    @Test
    public void acceptsDirectoryUnderStoriesRoot() {
        File target = new File(CoomiStoryPreference.getStoriesRoot(context()), "project-a");
        assertTrue(CoomiStoryPreference.setProjectPath(context(), target.getAbsolutePath()));
        assertEquals(target.getAbsolutePath(), CoomiStoryPreference.getProjectPath(context()));
        assertTrue(new File(CoomiStoryPreference.getProjectPath(context()), "chapters").isDirectory());
    }

    @Test
    public void rejectsStoriesRootItself() {
        // 根目录是存放各故事项目的容器，本身不能作为故事项目
        assertFalse(CoomiStoryPreference.setProjectPath(context(),
            CoomiStoryPreference.getStoriesRoot(context()).getAbsolutePath()));
    }

    @Test
    public void rejectsPathOutsideStoriesRoot() {
        // 外部存储、filesDir 内但不在 stories 下、系统根目录等越界路径一律拒绝
        assertFalse(CoomiStoryPreference.setProjectPath(context(), "/sdcard/story"));
        assertFalse(CoomiStoryPreference.setProjectPath(context(), "/data"));
        assertFalse(CoomiStoryPreference.setProjectPath(context(),
            new File(filesDir(), "outside").getAbsolutePath()));
        assertFalse(CoomiStoryPreference.setProjectPath(context(),
            new File(filesDir(), "home/project").getAbsolutePath()));
    }

    @Test
    public void rejectsDotDotEscape() {
        // canonical 解析后穿越出 stories 根的路径必须被拒绝
        File escape = new File(CoomiStoryPreference.getStoriesRoot(context()), "../outside");
        assertFalse(CoomiStoryPreference.setProjectPath(context(), escape.getAbsolutePath()));
    }

    @Test
    public void rejectsNullOrBlank() {
        assertFalse(CoomiStoryPreference.setProjectPath(context(), null));
        assertFalse(CoomiStoryPreference.setProjectPath(context(), "   "));
    }

    @Test
    public void fallsBackToDefaultWhenStoredPathIsOutside() {
        // 旧版本遗留的外部路径或非 stories 路径：应回退到 stories 根内
        context().getSharedPreferences("coomi_settings", Context.MODE_PRIVATE)
            .edit().putString("story_project_path", "/sdcard/legacy-story").apply();
        File project = new File(CoomiStoryPreference.getProjectPath(context()));
        String storiesRoot = CoomiStoryPreference.getStoriesRoot(context()).getAbsolutePath();
        assertTrue("越界历史值应回退到 stories 根内",
            project.getAbsolutePath().startsWith(storiesRoot + File.separator));
    }

    @Test
    public void fallsBackToDefaultWhenStoredPathIsFilesDirButNotStories() {
        // filesDir 内但不在 stories 下的历史值同样回退
        context().getSharedPreferences("coomi_settings", Context.MODE_PRIVATE)
            .edit().putString("story_project_path",
                new File(filesDir(), "other-dir").getAbsolutePath()).apply();
        File project = new File(CoomiStoryPreference.getProjectPath(context()));
        String storiesRoot = CoomiStoryPreference.getStoriesRoot(context()).getAbsolutePath();
        assertTrue(project.getAbsolutePath().startsWith(storiesRoot + File.separator));
    }
}
