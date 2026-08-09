(() => {
  const ANDROID_URL = 'https://updates.septemc.com/storydex/android/Storydex-Android-arm64-v0.1.0.apk'
  const GITHUB_URL = 'https://github.com/TensorHub-ORG/Storydex'

  function patchHeroDownloads() {
    const actions = document.querySelector('.hero-actions')
    if (!actions) return false

    const links = Array.from(actions.querySelectorAll('a'))
    const windows = links.find(link => link.textContent?.includes('Windows'))
    const github = links.find(link => link.textContent?.trim() === 'GitHub')
    if (!windows || !github) return false

    github.href = ANDROID_URL
    github.removeAttribute('target')
    github.removeAttribute('rel')
    github.className = windows.className
    github.style.cssText = windows.style.cssText
    github.id = 'heroAndroidDownloadBtn'
    github.dataset.statAction = 'download-android'
    github.textContent = '下载 Android 版（测试）'

    if (!document.querySelector('.hero-github-more')) {
      const githubMore = document.createElement('a')
      githubMore.href = GITHUB_URL
      githubMore.target = '_blank'
      githubMore.rel = 'noreferrer'
      githubMore.className = 'closing-link font-serif hero-github-more'
      githubMore.textContent = '或在 GitHub 上了解更多 →'
      actions.insertAdjacentElement('afterend', githubMore)
    }

    if (!document.getElementById('storydex-android-download-style')) {
      const style = document.createElement('style')
      style.id = 'storydex-android-download-style'
      style.textContent = `
        .hero-actions {
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          width: min(100%, 460px);
        }
        .hero-actions .btn {
          min-width: 0;
          padding-inline: 18px !important;
          justify-content: center;
          white-space: nowrap;
        }
        .hero-github-more { display: inline-flex; margin-top: 16px; }
        @media (max-width: 640px) {
          .hero-actions { grid-template-columns: 1fr; }
          .hero-actions .btn { width: 100%; }
          .hero-github-more { margin-top: 13px; }
        }
      `
      document.head.appendChild(style)
    }

    return true
  }

  let attempts = 0
  const timer = window.setInterval(() => {
    attempts += 1
    if (patchHeroDownloads() || attempts >= 100) window.clearInterval(timer)
  }, 100)
  patchHeroDownloads()
})()
