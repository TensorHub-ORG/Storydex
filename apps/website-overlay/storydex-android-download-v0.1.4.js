(() => {
  const WINDOWS_URL = 'https://updates.septemc.com/storydex/windows/StorydexSetup-x64-2.0.9.exe'
  const ANDROID_URL = 'https://updates.septemc.com/storydex/android/Storydex-Android-arm64-v0.1.4.apk'
  const ANDROID_STATS_URL = '/api/stats/download-android'
  const GITHUB_URL = 'https://github.com/TensorHub-ORG/Storydex'
  const VIEW_OFFSET = 266
  const DESKTOP_DOWNLOAD_OFFSET = 116
  const ANDROID_INITIAL_DOWNLOADS = 28
  const numberFormatter = new Intl.NumberFormat('zh-CN')

  function createEventId() {
    if (window.crypto?.randomUUID) return `download-android-${window.crypto.randomUUID()}`
    return `download-android-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  }

  function cloneDownloadButton(source, href, text, action) {
    const button = source.cloneNode(true)
    button.removeAttribute('id')
    button.href = href
    button.removeAttribute('target')
    button.removeAttribute('rel')
    button.dataset.statAction = action
    button.textContent = text
    return button
  }

  function patchDownloadLinks() {
    document.querySelectorAll('[data-stat-action="download"]').forEach(link => {
      if (link instanceof HTMLAnchorElement) link.href = WINDOWS_URL
    })

    const heroActions = document.querySelector('.hero-actions')
    if (heroActions) {
      const windows = heroActions.querySelector('[data-stat-action="download"]')
      let android = heroActions.querySelector('[data-stat-action="download-android"]')
      const github = Array.from(heroActions.querySelectorAll('a')).find(
        link => link.textContent?.trim() === 'GitHub'
      )
      if (!android && windows && github) {
        android = cloneDownloadButton(windows, ANDROID_URL, 'Android 版（测试）', 'download-android')
        android.id = 'heroAndroidDownloadBtn'
        github.replaceWith(android)
      }
      if (android instanceof HTMLAnchorElement) {
        android.href = ANDROID_URL
        if (android.textContent?.trim() !== 'Android 版（测试）') {
          android.textContent = 'Android 版（测试）'
        }
      }

      if (!document.querySelector('.hero-github-more')) {
        const githubMore = document.createElement('a')
        githubMore.href = GITHUB_URL
        githubMore.target = '_blank'
        githubMore.rel = 'noreferrer'
        githubMore.className = 'closing-link font-serif hero-github-more'
        githubMore.textContent = '去 GitHub 上为我们点颗星 →'
        heroActions.insertAdjacentElement('afterend', githubMore)
      }
    }

    const closing = document.querySelector('.closing .closing-inner')
    if (closing) {
      const windows = closing.querySelector(':scope > [data-stat-action="download"]')
        || closing.querySelector('[data-stat-action="download"]')
      let actions = closing.querySelector('.closing-download-actions')
      if (windows && !actions) {
        actions = document.createElement('div')
        actions.className = 'closing-download-actions'
        windows.replaceWith(actions)
        actions.appendChild(windows)
        const followingBreak = actions.nextElementSibling
        if (followingBreak?.tagName === 'BR') followingBreak.remove()
      }
      if (actions && windows && !actions.querySelector('[data-stat-action="download-android"]')) {
        actions.appendChild(cloneDownloadButton(
          windows,
          ANDROID_URL,
          'Android 版（测试）',
          'download-android'
        ))
      }
    }

    document.querySelectorAll('.closing-link').forEach(link => {
      if (link.textContent?.includes('GitHub')
          && link.textContent?.trim() !== '去 GitHub 上为我们点颗星 →') {
        link.textContent = '去 GitHub 上为我们点颗星 →'
      }
    })

    const productLinks = document.querySelector('.footer-col .footer-nav')
    if (productLinks && !productLinks.querySelector('[data-stat-action="download-android"]')) {
      const item = document.createElement('li')
      const link = document.createElement('a')
      link.href = ANDROID_URL
      link.dataset.statAction = 'download-android'
      link.textContent = 'Android 版（测试）'
      item.appendChild(link)
      productLinks.firstElementChild?.insertAdjacentElement('afterend', item)
    }
  }

  function calibratedNumber(item, offset) {
    const value = item?.querySelector('.stat-num')
    if (!value) return
    const current = value.textContent?.trim() || ''
    if (current === item.dataset.storydexAdjustedValue) return
    const raw = Number(current.replaceAll(',', ''))
    if (!Number.isFinite(raw)) return
    const adjusted = numberFormatter.format(raw + offset)
    item.dataset.storydexRawValue = String(raw)
    item.dataset.storydexAdjustedValue = adjusted
    if (current !== adjusted) value.textContent = adjusted
  }

  function setAndroidDownloads(count) {
    const value = document.querySelector('#storydexAndroidDownloads .stat-num')
    if (value) value.textContent = numberFormatter.format(Math.max(ANDROID_INITIAL_DOWNLOADS, count))
  }

  function patchStats() {
    const stats = document.querySelector('#footerStats')
    if (!stats) return false
    const views = stats.querySelector('[title="页面总浏览量"]')
    const desktop = stats.querySelector('[title="累计下载量"], [title="桌面版累计下载量"]')
    calibratedNumber(views, VIEW_OFFSET)
    if (desktop) {
      desktop.title = '桌面版累计下载量'
      calibratedNumber(desktop, DESKTOP_DOWNLOAD_OFFSET)
    }

    if (!document.getElementById('storydexAndroidDownloads')) {
      const item = desktop?.cloneNode(true) || document.createElement('div')
      item.id = 'storydexAndroidDownloads'
      item.className = 'stat-item'
      item.title = 'Android 累计下载量'
      item.removeAttribute('data-storydex-raw-value')
      item.removeAttribute('data-storydex-adjusted-value')
      let value = item.querySelector('.stat-num')
      if (!value) {
        value = document.createElement('span')
        value.className = 'stat-num'
        item.appendChild(value)
      }
      value.textContent = numberFormatter.format(ANDROID_INITIAL_DOWNLOADS)
      desktop?.insertAdjacentElement('afterend', item)
    }
    return true
  }

  async function loadAndroidDownloads() {
    try {
      const response = await fetch(ANDROID_STATS_URL, {
        headers: { Accept: 'application/json' },
        credentials: 'omit',
        cache: 'no-store'
      })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const data = await response.json()
      setAndroidDownloads(Number(data.androidDownloads))
    } catch {
      setAndroidDownloads(ANDROID_INITIAL_DOWNLOADS)
    }
  }

  function trackAndroidDownload(event) {
    const target = event.target?.closest?.('[data-stat-action="download-android"]')
    if (!target) return
    const current = document.querySelector('#storydexAndroidDownloads .stat-num')
    const value = Number((current?.textContent || '').replaceAll(',', ''))
    if (Number.isFinite(value)) setAndroidDownloads(value + 1)
    fetch(ANDROID_STATS_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ eventId: createEventId() }),
      credentials: 'omit',
      cache: 'no-store',
      keepalive: true
    }).then(response => response.ok ? response.json() : Promise.reject())
      .then(data => setAndroidDownloads(Number(data.androidDownloads)))
      .catch(() => {})
  }

  function addStyles() {
    if (document.getElementById('storydex-android-download-style')) return
    const style = document.createElement('style')
    style.id = 'storydex-android-download-style'
    style.textContent = `
      .hero-actions,
      .closing-download-actions {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 10px;
        width: min(100%, 460px);
      }
      .hero-actions .btn,
      .closing-download-actions .btn {
        min-width: 0;
        padding-inline: 18px !important;
        justify-content: center;
        white-space: nowrap;
      }
      .closing-download-actions { margin-inline: auto; }
      .hero-github-more { display: inline-flex; margin-top: 16px; }
      @media (max-width: 640px) {
        .hero-actions,
        .closing-download-actions { grid-template-columns: 1fr; }
        .hero-actions .btn,
        .closing-download-actions .btn { width: 100%; }
        .hero-github-more { margin-top: 13px; }
      }
    `
    document.head.appendChild(style)
  }

  function patchPage() {
    patchDownloadLinks()
    addStyles()
    return patchStats()
  }

  document.addEventListener('click', trackAndroidDownload, true)
  const observer = new MutationObserver(() => patchPage())
  observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true })

  let attempts = 0
  const timer = window.setInterval(() => {
    attempts += 1
    if (patchPage() && attempts >= 20) window.clearInterval(timer)
    if (attempts >= 100) window.clearInterval(timer)
  }, 100)
  patchPage()
  loadAndroidDownloads()
})()
