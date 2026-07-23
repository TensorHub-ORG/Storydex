<template>
  <aside ref="dockRef" class="coomi-dock">
    <header class="coomi-header">
      <div class="coomi-title">
        <span>Coomi</span>
      </div>
      <div class="coomi-header-actions">
        <button class="coomi-icon-btn" type="button" title="新建会话" @click="handleNewSession">
          <span class="material-symbols-rounded">add_box</span>
        </button>
        <button class="coomi-icon-btn" type="button" title="History" @click="sessionMenuOpen = !sessionMenuOpen">
          <span class="material-symbols-rounded">history</span>
        </button>
        <button class="coomi-icon-btn" type="button" title="Settings" @click="configPanelOpen = true">
          <span class="material-symbols-rounded">settings</span>
        </button>
        <div class="coomi-run-state" :class="{ running: agentStore.isRunning }">
          <span class="coomi-dot"></span>
          <span>{{ headerStatusLabel }}</span>
        </div>
      </div>
    </header>

    <div class="coomi-stream-shell">
      <main ref="streamRef" class="coomi-stream" :class="{ 'config-open': configPanelOpen }" @scroll.passive="handleStreamScroll">
      <CoomiConfigPanel
        v-if="configPanelOpen"
        :visible="configPanelOpen"
        @close="configPanelOpen = false"
        @saved="handleConfigSaved"
      />

      <section v-else-if="sessionMenuOpen" class="coomi-session-view">
        <div class="coomi-session-title">Sessions</div>
        <div v-if="sessionSummaries.length === 0" class="coomi-empty">No Coomi sessions yet.</div>
        <div
          v-for="session in sessionSummaries"
          :key="session.sessionId"
          class="coomi-session-item"
          :class="{ active: session.sessionId === agentStore.currentSessionId }"
        >
          <button class="coomi-session-select" type="button" @click="handleSessionSelect(session.sessionId)">
            <span>{{ session.firstPrompt || session.sessionId }}</span>
            <small>{{ formatDate(session.updatedAt) }}</small>
          </button>
          <button
            class="coomi-session-delete"
            type="button"
            title="删除会话"
            :disabled="agentStore.isRunning"
            @click.stop="handleSessionDelete(session.sessionId)"
          >
            <span class="material-symbols-rounded">delete</span>
          </button>
        </div>
      </section>

      <section v-else-if="conversationRuns.length === 0" class="coomi-welcome">
        <img src="@/assets/storydex_icon_01.png" alt="Storydex" />
        <div class="coomi-welcome-title">Coomi 已就绪</div>
        <p class="coomi-welcome-copy">如果你有任何使用上的疑问，都可以向我询问。</p>
      </section>

      <section v-else class="coomi-runs">
        <article v-for="run in conversationRuns" :key="run.traceId" class="coomi-run">
          <div class="coomi-run-head">
            <span>Coomi</span>
            <span :class="['coomi-run-status', run.status]">{{ formatStatus(run.status, run.errorMessage) }}</span>
            <span
              v-if="run.noRestorePoint"
              class="coomi-no-restore-point"
              title="本轮没有可用恢复点"
              aria-label="本轮没有可用恢复点"
            >
              <span class="material-symbols-rounded">warning_amber</span>
              无恢复点
            </span>
            <span>{{ formatDate(run.updatedAt) }}</span>
            <div v-if="canRollbackRun(run)" class="coomi-run-actions">
              <button
                class="coomi-run-action"
                type="button"
                title="编辑最新消息"
                aria-label="编辑最新消息"
                :disabled="agentStore.isRollingBack || agentStore.isReexecuting"
                @click="handleRollbackEdit(run)"
              >
                <span class="material-symbols-rounded">edit</span>
              </button>
              <button
                class="coomi-run-action danger"
                type="button"
                title="删除本轮"
                aria-label="删除本轮"
                :disabled="agentStore.isRollingBack || agentStore.isReexecuting"
                @click="handleRollbackDelete(run)"
              >
                <span class="material-symbols-rounded">delete</span>
              </button>
            </div>
          </div>

          <div class="coomi-waterfall">
            <template v-for="entry in displayEntries(run)" :key="entry.id">
              <section
                v-if="entry.kind === 'item'"
                class="coomi-event"
                :class="[`type-${entry.item.type}`, `status-${entry.item.status}`]"
              >
                <button
                  v-if="entry.item.type === 'reasoning'"
                  class="coomi-fold-head"
                  type="button"
                  @click="toggleFold(entry.id, isActiveReasoning(run, entry.item))"
                >
                  <span>{{ reasoningTitle(run, entry.item) }}</span>
                  <span class="coomi-fold-meta">{{ isFoldOpen(entry.id, isActiveReasoning(run, entry.item)) ? "hide" : "show" }}</span>
                </button>
                <div v-else class="coomi-event-head">
                  <span class="coomi-event-type">{{ formatItemType(entry.item.type) }}</span>
                  <span class="coomi-event-time">{{ formatDate(entry.item.timestamp, true) }}</span>
                </div>

                <div
                  v-if="entry.item.type !== 'reasoning' || isFoldOpen(entry.id, isActiveReasoning(run, entry.item))"
                  class="coomi-event-body"
                >
                  <p v-if="entry.item.type === 'user'" class="coomi-user-text">{{ entry.item.content }}</p>
                  <div
                    v-else-if="entry.item.type === 'assistant'"
                    class="coomi-assistant-text coomi-markdown"
                    @click="handleMarkdownLinkClick"
                    v-html="renderMarkdown(entry.item.content)"
                  ></div>
                  <div v-else-if="entry.item.type === 'reasoning'" class="coomi-reasoning-text">{{ entry.item.content }}</div>
                  <div v-else-if="entry.item.type === 'error'" class="coomi-error-text">{{ entry.item.content }}</div>
                  <div v-else-if="entry.item.type === 'phase'" class="coomi-phase-text" aria-live="polite">
                    {{ entry.item.content }}
                  </div>
                </div>
              </section>

              <section
                v-else
                class="coomi-event coomi-tool-group"
                :class="[`status-${entry.status}`]"
              >
                <button class="coomi-fold-head" type="button" @click="toggleFold(entry.id, toolGroupDefaultOpen(entry))">
                  <span>{{ toolGroupTitle(entry) }}</span>
                  <span class="coomi-fold-meta">{{ isToolGroupOpen(entry) ? "hide" : "show" }}</span>
                </button>

                <div v-if="isToolGroupOpen(entry)" class="coomi-tool-list">
                  <section v-for="chunk in toolChunks(entry)" :key="chunk.id" class="coomi-tool-chunk">
                    <button
                      v-if="entry.tools.length > 5"
                      class="coomi-tool-chunk-head"
                      type="button"
                      @click="toggleToolChunk(chunk.id, toolChunkDefaultOpen(entry, chunk))"
                    >
                      <span>{{ toolChunkTitle(chunk) }}</span>
                      <span>{{ isToolChunkOpen(entry, chunk) ? "收起" : "展开" }}</span>
                    </button>
                    <div
                      v-if="entry.tools.length <= 5 || isToolChunkOpen(entry, chunk)"
                      class="coomi-tool-chunk-list"
                    >
                      <article v-for="tool in chunk.tools" :key="tool.id" class="coomi-tool-row">
                        <button class="coomi-tool-row-head" type="button" @click="toggleToolRow(toolRowId(entry, tool))">
                          <span>{{ toolSummary(tool) }}</span>
                          <span>{{ isToolRowOpen(entry, tool) ? "收起" : "详情" }}</span>
                        </button>
                        <div v-if="isToolRowOpen(entry, tool)" class="coomi-tool-preview">
                          <details v-if="tool.arguments && Object.keys(tool.arguments).length" class="coomi-details">
                            <summary>参数</summary>
                            <pre>{{ compactJson(tool.arguments) }}</pre>
                          </details>
                          <details v-if="tool.resultPreview" class="coomi-details">
                            <summary>结果</summary>
                            <pre>{{ compactText(tool.resultPreview) }}</pre>
                          </details>
                        </div>
                      </article>
                    </div>
                  </section>
                </div>
              </section>
            </template>
            <div v-if="run.status === 'running'" class="coomi-running-tail" aria-live="polite">
              <span>执行中</span><span class="coomi-running-dots"> · · ·</span>
            </div>
          </div>
        </article>
      </section>
      </main>
      <button
        v-if="showScrollToLatest"
        class="coomi-scroll-latest"
        type="button"
        title="回到最新输出"
        aria-label="回到最新输出"
        @click="scrollToBottom(true)"
      >
        <span class="material-symbols-rounded">south</span>
      </button>
    </div>

    <footer ref="composerRef" class="coomi-composer">
      <div v-if="composerError" class="coomi-error">{{ composerError }}</div>
      <div v-if="collapsedHandlesVisible" class="coomi-collapsed-handles">
        <button
          v-if="executionFloatVisible && executionFloatCollapsed"
          class="coomi-collapsed-handle"
          type="button"
          title="展开文件变更摘要"
          aria-label="展开文件变更摘要"
          @click="expandExecutionFloat"
        ></button>
        <button
          v-if="promptDockActive && promptDockCollapsed"
          class="coomi-collapsed-handle"
          type="button"
          :title="promptDockHandleTitle"
          :aria-label="promptDockHandleTitle"
          @click="expandPromptDock"
        ></button>
      </div>
      <div class="coomi-composer-status">
        <span class="coomi-status-pill" :title="modelLabel">模型：{{ modelLabel }}</span>
        <div class="coomi-status-control">
          <button
            class="coomi-status-pill coomi-status-button"
            :class="permissionToneClass(activePermissionTone)"
            type="button"
            title="Shift+Tab"
            :aria-expanded="permissionMenuOpen"
            @click="togglePermissionMenu"
          >
            {{ permissionControlLabel }}
          </button>
          <div v-if="permissionMenuOpen" class="coomi-status-popover coomi-permission-popover">
            <button
              v-for="option in permissionOptions"
              :key="option.value"
              type="button"
              class="coomi-choice-card"
              :class="[permissionToneClass(option.value), { active: isPermissionOptionActive(option.value) }]"
              @mousedown.prevent="selectPermissionOption(option.value)"
            >
              <span>{{ option.label }}</span>
              <small>{{ option.description }}</small>
            </button>
          </div>
        </div>
        <div class="coomi-status-control">
          <button
            class="coomi-status-pill coomi-status-button"
            type="button"
            :aria-expanded="reasoningMenuOpen"
            @click="toggleReasoningMenu"
          >
            {{ reasoningLabel }}
          </button>
          <div v-if="reasoningMenuOpen" class="coomi-status-popover coomi-reasoning-popover">
            <button
              v-for="option in reasoningOptions"
              :key="option.value"
              type="button"
              class="coomi-choice-card"
              :class="{ active: selectedReasoningMode === option.value }"
              @mousedown.prevent="selectReasoningOption(option.value)"
            >
              <span>{{ option.label }}</span>
              <small>{{ option.description }}</small>
            </button>
          </div>
        </div>
        <div class="coomi-status-control coomi-story-control">
          <button
            class="coomi-status-pill coomi-status-button coomi-story-toggle"
            type="button"
            title="故事生成参数"
            :aria-expanded="storyOptionsOpen"
            @click="toggleStoryOptions"
          >
            <span class="material-symbols-rounded">tune</span>
            <span>{{ storyOptionsLabel }}</span>
            <span class="material-symbols-rounded coomi-story-caret">
              {{ storyOptionsOpen ? "expand_more" : "chevron_right" }}
            </span>
          </button>
          <div v-if="storyOptionsOpen" class="coomi-status-popover coomi-story-popover">
            <label class="coomi-story-field">
              <span>片段数量</span>
              <input
                type="number"
                min="1"
                step="1"
                :value="agentStore.storyFragmentCount"
                :disabled="isSingleFileChapterTemplate"
                :title="isSingleFileChapterTemplate ? '单正文文件模板固定为 1 个文件' : '同一章节目录下生成的片段文件数量'"
                @input="updateStoryFragmentCount"
              />
            </label>
            <label class="coomi-story-field">
              <span>片段字数</span>
              <input
                type="number"
                min="100"
                max="20000"
                step="100"
                :value="agentStore.storyFragmentWordCount"
                @input="updateStoryFragmentWordCount"
              />
            </label>
            <label class="coomi-story-field coomi-story-template-field">
              <span>章节模板</span>
              <select
                :value="agentStore.storyChapterTemplateId"
                :disabled="agentStore.storyChapterTemplatesLoading"
                @change="updateStoryChapterTemplate"
              >
                <option v-if="agentStore.storyChapterTemplatesLoading" value="default_chapter_directory">
                  读取中
                </option>
                <option
                  v-for="template in agentStore.storyChapterTemplates"
                  :key="template.id"
                  :value="template.id"
                >
                  {{ template.name }}
                </option>
              </select>
            </label>
            <small v-if="selectedChapterTemplateDescription" class="coomi-story-template-hint">
              {{ selectedChapterTemplateDescription }}
            </small>
            <small v-else-if="storyChapterTemplateErrorMessage" class="coomi-story-template-hint error">
              {{ storyChapterTemplateErrorMessage }}
            </small>
            <small class="coomi-story-template-hint">
              片段字数由 Storydex 内置统计精确验收：忽略空白后，每个 Unicode 字符计 1 字。
            </small>
          </div>
        </div>
        <span
          class="coomi-context-ring"
          :class="contextLevel"
          :style="contextRingStyle"
          :title="contextTooltip"
          aria-label="上下文窗口用量"
        ></span>
      </div>
      <div v-if="activeApproval && !promptDockCollapsed" class="coomi-command-menu coomi-approval-menu">
        <div class="coomi-approval-head">
          <div class="coomi-approval-head-main">
            <span>{{ activeApproval.header }}</span>
            <small>{{ compactText(activeApproval.question, 220) }}</small>
          </div>
          <div class="coomi-approval-head-tools">
            <div v-if="approvalQueue.length > 1" class="coomi-approval-nav">
              <button
                class="coomi-approval-nav-btn"
                type="button"
                title="上一个问题"
                aria-label="上一个问题"
                :disabled="approvalCursor <= 0"
                @mousedown.prevent="goToApproval(approvalCursor - 1)"
              >
                <span class="material-symbols-rounded">chevron_left</span>
              </button>
              <span class="coomi-approval-nav-count">{{ approvalCursor + 1 }}/{{ approvalQueue.length }}</span>
              <button
                class="coomi-approval-nav-btn"
                type="button"
                title="下一个问题"
                aria-label="下一个问题"
                :disabled="approvalCursor >= approvalQueue.length - 1"
                @mousedown.prevent="goToApproval(approvalCursor + 1)"
              >
                <span class="material-symbols-rounded">chevron_right</span>
              </button>
            </div>
            <button
              class="coomi-approval-collapse"
              type="button"
              title="暂时收起"
              aria-label="暂时收起问题面板"
              @click="collapsePromptDock"
            >
              <span class="material-symbols-rounded">keyboard_arrow_down</span>
            </button>
          </div>
        </div>
        <button
          v-for="option in activeApproval.options"
          :key="option.value"
          type="button"
          class="coomi-command-option coomi-approval-option"
          :class="{ active: activeApprovalDraft?.value === option.value }"
          @mousedown.prevent="selectApprovalOption(option.value)"
        >
          <span>{{ approvalOptionLabel(option.label, option.value) }}</span>
          <small>{{ option.description || approvalOptionDescription(option.value) }}</small>
        </button>
        <textarea
          v-if="activeApproval.allowText"
          class="coomi-approval-input"
          placeholder="输入补充回复"
          rows="2"
          :value="activeApprovalDraft?.text || ''"
          @input="updateApprovalDraftText(($event.target as HTMLTextAreaElement).value)"
          @keydown.stop
        ></textarea>
        <div class="coomi-approval-actions">
          <button class="coomi-approval-action" type="button" @mousedown.prevent="handleApprovalCancel">
            取消
          </button>
          <button
            class="coomi-approval-action primary"
            type="button"
            :disabled="!canConfirmApproval"
            :title="approvalConfirmTitle"
            @mousedown.prevent="handleApprovalConfirm"
          >
            {{ approvalConfirmLabel }}
          </button>
        </div>
      </div>
      <div
        v-else-if="agentStore.pendingCommitPrompt && !promptDockCollapsed"
        class="coomi-command-menu coomi-approval-menu coomi-commit-menu"
      >
        <div class="coomi-approval-head">
          <div class="coomi-approval-head-main">
            <span>本轮有未提交修改</span>
            <small>{{ commitPromptSummary }}</small>
          </div>
          <button
            class="coomi-approval-collapse"
            type="button"
            title="暂时收起"
            aria-label="暂时收起提交面板"
            @click="collapsePromptDock"
          >
            <span class="material-symbols-rounded">keyboard_arrow_down</span>
          </button>
        </div>
        <ul v-if="commitPromptFiles.length" class="coomi-commit-file-list">
          <li v-for="file in commitPromptFiles" :key="file" class="coomi-commit-file" :title="file">
            <span class="material-symbols-rounded">edit_note</span>
            <span>{{ file }}</span>
          </li>
        </ul>
        <button
          type="button"
          class="coomi-command-option coomi-approval-option"
          :disabled="agentStore.isCommittingGit"
          @click="handleCommitPromptAuto"
        >
          <span>确认提交，并自动生成提交信息</span>
          <small>调用一次 LLM 生成提交信息后提交</small>
        </button>
        <button
          type="button"
          class="coomi-command-option coomi-approval-option"
          :class="{ active: commitPromptMode === 'manual' }"
          :disabled="agentStore.isCommittingGit"
          @click="selectCommitPromptManual"
        >
          <span>确认提交</span>
          <small>手动输入提交信息后直接提交</small>
        </button>
        <textarea
          v-if="commitPromptMode === 'manual'"
          v-model="commitMessage"
          class="coomi-approval-input"
          placeholder="输入提交信息"
          rows="2"
          @keydown.stop
        ></textarea>
        <div v-if="commitPromptMode === 'manual'" class="coomi-approval-actions">
          <button
            class="coomi-approval-action primary"
            type="button"
            :disabled="agentStore.isCommittingGit || !commitMessage.trim()"
            @click="handleCommitPromptManual"
          >
            提交
          </button>
        </div>
        <button
          type="button"
          class="coomi-command-option coomi-approval-option"
          :disabled="agentStore.isCommittingGit"
          @click="handleCommitPromptSkip"
        >
          <span>暂不进行提交</span>
          <small>保留当前未提交修改</small>
        </button>
      </div>
      <div
        v-else-if="agentStore.isCommittingGit || agentStore.commitActionLabel"
        class="coomi-commit-progress"
        :class="{ completed: !agentStore.isCommittingGit }"
        aria-live="polite"
      >
        <span class="coomi-commit-progress-dot"></span>
        <span>{{ agentStore.commitActionLabel || "正在处理本地版本" }}</span>
      </div>
      <div v-else-if="commandMenuVisible" class="coomi-command-menu">
        <button
          v-for="(command, index) in filteredCommands"
          :key="command.value"
          type="button"
          class="coomi-command-option"
          :class="{ active: index === selectedCommandIndex }"
          @mousedown.prevent="selectCommand(index)"
        >
          <span>{{ command.value }}</span>
          <small>{{ command.description }}</small>
        </button>
      </div>
      <section v-if="agentStore.editingTraceId" class="coomi-edit-session" aria-live="polite">
        <div class="coomi-edit-session-copy">
          <strong>正在编辑最新一条用户消息</strong>
          <span>原消息、回答和 trace 会保留到“重新执行”请求被后端接受。</span>
          <span v-if="agentStore.editingHasFileChanges" class="coomi-edit-file-warning">
            重新执行只替换对话，不自动撤销文件变更。
          </span>
        </div>
        <div class="coomi-edit-session-actions">
          <button type="button" class="coomi-secondary-action" :disabled="agentStore.isReexecuting" @click="handleCancelEdit">
            取消编辑
          </button>
          <button
            type="button"
            class="coomi-primary-action"
            :disabled="agentStore.isReexecuting || !agentStore.promptInput.trim()"
            @click="handleReexecuteEdit"
          >
            {{ agentStore.isReexecuting ? "正在重新执行" : "重新执行" }}
          </button>
        </div>
      </section>
      <section
        v-if="agentStore.followupPaused || visibleFollowups.length"
        class="coomi-followup-mailbox"
        aria-label="待发送信息"
      >
        <header class="coomi-followup-head">
          <div>
            <strong>待发送信息</strong>
            <span>{{ visibleFollowups.length }} 条</span>
          </div>
          <button
            v-if="agentStore.followupPaused"
            type="button"
            class="coomi-followup-resume"
            :disabled="agentStore.isReexecuting"
            @click="handleResumeFollowups"
          >
            恢复发送
          </button>
        </header>
        <p v-if="agentStore.followupPaused" class="coomi-followup-paused">
          队列已暂停：{{ followupPauseLabel(agentStore.followupPauseReason) }}
        </p>
        <article
          v-for="message in visibleFollowups"
          :key="message.messageId"
          class="coomi-followup-item"
          :class="[`mode-${message.mode}`, `status-${message.status}`]"
        >
          <div class="coomi-followup-meta">
            <span>{{ message.mode === "steer" ? "引导" : "排队" }}</span>
            <span>{{ followupStatusLabel(message) }}</span>
          </div>
          <textarea
            v-if="editingFollowupId === message.messageId"
            v-model="editingFollowupDraft"
            class="coomi-followup-editor"
            rows="2"
            @keydown.stop
          ></textarea>
          <p v-else class="coomi-followup-content">{{ message.content }}</p>
          <div class="coomi-followup-actions">
            <template v-if="editingFollowupId === message.messageId">
              <button type="button" @click="cancelFollowupEdit">取消</button>
              <button type="button" :disabled="!editingFollowupDraft.trim()" @click="saveFollowupEdit(message)">保存</button>
            </template>
            <template v-else>
              <button v-if="canEditFollowup(message)" type="button" @click="beginFollowupEdit(message)">编辑</button>
              <button
                v-if="canSteerFollowup(message)"
                type="button"
                class="steer"
                :disabled="agentStore.isReexecuting || !agentStore.isRunning || !agentStore.currentTraceId"
                :title="agentStore.isRunning ? '将这条排队信息改为立即引导' : '当前没有运行中的执行'"
                @click="handleSteerFollowup(message)"
              >
                立即引导执行
              </button>
              <button v-if="canEditFollowup(message)" type="button" class="danger" @click="handleDeleteFollowup(message)">
                删除
              </button>
            </template>
          </div>
        </article>
      </section>
      <div v-if="executionFloatVisible && !executionFloatCollapsed" class="coomi-execution-float-slot">
        <AgentExecutionFloatBar @collapse="collapseExecutionFloat" />
      </div>
      <div class="coomi-input-shell">
        <textarea
          ref="inputRef"
          v-model="agentStore.promptInput"
          class="coomi-input"
          :style="{ maxHeight: `${effectiveComposerMaxHeight}px` }"
          :disabled="agentStore.isReexecuting"
          :placeholder="composerPlaceholder"
          rows="1"
          @keydown="handleComposerKeydown"
          @input="handleComposerInput"
        ></textarea>
        <button
          class="coomi-send"
          type="button"
          :disabled="agentStore.isReexecuting || Boolean(agentStore.editingTraceId) || !agentStore.promptInput.trim()"
          :title="agentStore.isRunning ? '加入待发送队列' : '发送'"
          :aria-label="agentStore.isRunning ? '加入待发送队列' : '发送消息'"
          @click="handleSubmitOrStop"
        >
          <span class="material-symbols-rounded">arrow_upward</span>
        </button>
        <button
          v-if="agentStore.isRunning"
          class="coomi-stop-run"
          type="button"
          :disabled="agentStore.isStopping"
          title="停止执行"
          aria-label="停止执行"
          @click="handleStopRun"
        >
          <span class="coomi-stop-glyph" aria-hidden="true"></span>
        </button>
      </div>
    </footer>

    <div
      v-if="agentStore.pendingSnapshotConfirmation"
      class="coomi-snapshot-modal-mask"
      role="presentation"
      @click.self="handleNoSnapshotCancel"
    >
      <section class="coomi-snapshot-modal" role="dialog" aria-modal="true" aria-labelledby="snapshot-modal-title">
        <div class="coomi-snapshot-modal-title-row">
          <span class="material-symbols-rounded coomi-snapshot-modal-icon">warning_amber</span>
          <h2 id="snapshot-modal-title">无法创建恢复点</h2>
        </div>
        <p class="coomi-snapshot-modal-copy">
          当前项目未能创建本轮恢复点。继续执行后，自动恢复能力不可用，但仍会保留执行记录。
        </p>
        <div class="coomi-snapshot-modal-actions">
          <button type="button" class="coomi-snapshot-modal-button" @click="handleNoSnapshotCancel">
            <span class="material-symbols-rounded">close</span>
            取消
          </button>
          <button type="button" class="coomi-snapshot-modal-button primary" @click="handleNoSnapshotConfirm">
            <span class="material-symbols-rounded">play_arrow</span>
            继续执行
          </button>
        </div>
      </section>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import MarkdownIt from "markdown-it";
import AgentExecutionFloatBar from "@/components/AgentExecutionFloatBar.vue";
import CoomiConfigPanel from "@/components/CoomiConfigPanel.vue";
import { useAgentStore } from "@/stores/agent";
import { useGitStore } from "@/stores/git";
import { useWorkspaceStore } from "@/stores/workspace";
import {
  findMarkdownLinkAnchor,
  isExternalMarkdownHref,
  resolveMarkdownWorkspaceHref
} from "@/utils/workspaceLinks";
import type {
  AgentExecutionRun,
  AgentFollowupMessage,
  AgentPendingApproval,
  CoomiWaterfallItem,
  CoomiWaterfallItemStatus,
  CoomiWaterfallItemType
} from "@/types/agent";

type DisplayEntry =
  | { kind: "item"; id: string; item: CoomiWaterfallItem }
  | { kind: "tools"; id: string; tools: CoomiWaterfallItem[]; status: CoomiWaterfallItemStatus; terminal: boolean };
type ToolChunk = {
  id: string;
  index: number;
  start: number;
  end: number;
  tools: CoomiWaterfallItem[];
  status: CoomiWaterfallItemStatus;
};
type LiveOperationItem = {
  operationId: string;
  targetPath: string;
  usesWholePendingWrite?: boolean;
};
type PendingWriteLike = {
  token?: string;
  targetPaths?: string[];
  writePreview?: {
    items?: LiveOperationItem[];
  };
};
type PermissionChoice = "plan_mode" | "ask_approval" | "approve_for_me" | "full_access";
type ReasoningChoice = "auto" | "low" | "medium" | "high";
type ApprovalDraft = { value: string; text: string };

const TOOL_CHUNK_SIZE = 5;
const agentStore = useAgentStore();
const gitStore = useGitStore();
const workspaceStore = useWorkspaceStore();
const configPanelOpen = ref(false);
const sessionMenuOpen = ref(false);
const dockRef = ref<HTMLElement | null>(null);
const streamRef = ref<HTMLElement | null>(null);
const composerRef = ref<HTMLElement | null>(null);
const inputRef = ref<HTMLTextAreaElement | null>(null);
const editingFollowupId = ref("");
const editingFollowupDraft = ref("");
const commandMenuOpen = ref(false);
const permissionMenuOpen = ref(false);
const reasoningMenuOpen = ref(false);
const storyOptionsOpen = ref(false);
const selectedCommandIndex = ref(0);
const foldState = ref<Record<string, boolean>>({});
const toolChunkState = ref<Record<string, boolean>>({});
const toolRowState = ref<Record<string, boolean>>({});
const selectedReasoningMode = ref<ReasoningChoice>("auto");
const approvalCursor = ref(0);
const approvalDrafts = ref<Record<string, ApprovalDraft>>({});
const commitPromptMode = ref<"auto" | "manual" | "skip" | "">("");
const commitMessage = ref("");
const executionFloatCollapsed = ref(false);
const promptDockCollapsed = ref(false);
const shouldFollowOutput = ref(true);
const showScrollToLatest = ref(false);
const runtimeNow = ref(Date.now());
let runtimeTimer: number | null = null;
const SCROLL_BOTTOM_THRESHOLD = 48;
const COMPOSER_MIN_HEIGHT = 34;
const DEFAULT_COMPOSER_MAX_HEIGHT = 180;
const composerHeightCeiling = ref(Number.POSITIVE_INFINITY);
const executionFloatSignature = computed(() => {
  if (workspaceStore.launchScreenVisible) {
    return "";
  }
  const run = agentStore.activeTraceRun;
  const ledger = agentStore.liveChangeLedger;
  const changedFiles = ledger?.changedFiles || [];
  const changedCount = ledger?.changedFileCount || changedFiles.length || 0;
  if (!run || changedCount <= 0) {
    return "";
  }
  return [
    run.traceId,
    ledger?.updatedAt || "",
    changedCount,
    ledger?.added || 0,
    ledger?.removed || 0,
    changedFiles.join("|")
  ].join("::");
});
const executionFloatVisible = computed(() => Boolean(executionFloatSignature.value));
const headerStatusLabel = computed(() => {
  if (!agentStore.isRunning) {
    return "Coomi · Ready";
  }
  const startedAt = agentStore.runStartedAt || Date.parse(agentStore.activeTraceRun?.createdAt || "") || runtimeNow.value;
  return `Coomi · Running ${formatRunDuration(runtimeNow.value - startedAt)}`;
});
const composerError = computed(() => {
  const message = agentStore.lastError.trim();
  if (!message || message === agentStore.activeTraceRun?.errorMessage?.trim()) {
    return "";
  }
  return message;
});
const visibleFollowups = computed(() =>
  agentStore.followups.filter((message) => message.status !== "sent" && message.status !== "cancelled")
);
const composerPlaceholder = computed(() => {
  if (agentStore.editingTraceId) {
    return "修改最新消息后选择“取消编辑”或“重新执行”";
  }
  if (agentStore.isRunning) {
    return "继续输入：发送将加入待执行队列";
  }
  return "输入信息（Enter发送，Shift+Enter换行，输入“/”查看可用指令）";
});
const slashCommands = [
  { value: "/plan", description: "进入计划模式" },
  { value: "/exit_plan", description: "退出计划模式" },
  { value: "/loop ", description: "运行循环任务" }
];
const permissionOptions: Array<{ value: PermissionChoice; label: string; description: string }> = [
  { value: "plan_mode", label: "计划模式", description: "只规划思路，不主动执行修改。" },
  { value: "ask_approval", label: "询问确认", description: "关键操作前等待你确认。" },
  { value: "approve_for_me", label: "自动批准", description: "常规操作自动通过。" },
  { value: "full_access", label: "完全访问", description: "允许 Coomi 直接执行任务。" }
];
const reasoningOptions: Array<{ value: ReasoningChoice; label: string; shortLabel: string; description: string }> = [
  { value: "auto", label: "自动", shortLabel: "自动", description: "由 Coomi 根据任务自动判断。" },
  { value: "low", label: "低", shortLabel: "低", description: "偏向快速响应。" },
  { value: "medium", label: "中", shortLabel: "中", description: "平衡速度和推理深度。" },
  { value: "high", label: "高", shortLabel: "高", description: "偏向更充分的推理。" }
];
const markdown = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true
});

function buildPendingTargetPathOperationItems(pendingWrite: PendingWriteLike | null | undefined): LiveOperationItem[] {
  const targetPaths = Array.isArray(pendingWrite?.targetPaths) ? pendingWrite.targetPaths : [];
  return targetPaths
    .map((targetPath, index) => ({
      operationId: `target-path-${index + 1}`,
      targetPath: String(targetPath || ""),
      usesWholePendingWrite: true,
    }))
    .filter((item) => item.targetPath.trim());
}

function buildLiveOperationItemsForPending(pendingWrite: PendingWriteLike | null | undefined): LiveOperationItem[] {
  const previewItems = Array.isArray(pendingWrite?.writePreview?.items) ? pendingWrite.writePreview.items : [];
  if (previewItems.length > 0) {
    return previewItems;
  }
  return buildPendingTargetPathOperationItems(pendingWrite);
}

function attachPendingWriteContext(items: LiveOperationItem[], pendingWrite: PendingWriteLike | null | undefined): LiveOperationItem[] {
  void pendingWrite;
  return items;
}

function shouldApplyWholePendingWrite(item: LiveOperationItem): boolean {
  return Boolean(item.usesWholePendingWrite);
}

async function handleApproveOperation(item: LiveOperationItem) {
  const operationIds = shouldApplyWholePendingWrite(item) ? undefined : [item.operationId];
  void operationIds;
}

async function handleRejectOperation(item: LiveOperationItem) {
  if (shouldApplyWholePendingWrite(item)) {
    return;
  }
}

const conversationRuns = computed(() => {
  if (workspaceStore.launchScreenVisible) {
    return [];
  }
  return [...agentStore.executionHistory].sort((left, right) => {
    const leftTime = Date.parse(left.createdAt || left.updatedAt);
    const rightTime = Date.parse(right.createdAt || right.updatedAt);
    return leftTime - rightTime || Date.parse(left.updatedAt) - Date.parse(right.updatedAt);
  });
});
const latestConversationTraceId = computed(() => {
  const runs = conversationRuns.value;
  return runs.length > 0 ? runs[runs.length - 1].traceId : "";
});
const sessionSummaries = computed(() => (workspaceStore.launchScreenVisible ? [] : agentStore.availableSessions));
const modelLabel = computed(() => agentStore.coomiStatus?.display || agentStore.coomiStatus?.model || "未配置");
const permissionControlLabel = computed(() => {
  if (agentStore.coomiStatus?.planMode) {
    return "计划模式";
  }
  return permissionOptions.find((option) => option.value === agentStore.coomiStatus?.permissionMode)?.label || agentStore.permissionModeLabel;
});
const activePermissionTone = computed<PermissionChoice>(() => {
  if (agentStore.coomiStatus?.planMode) {
    return "plan_mode";
  }
  const mode = agentStore.coomiStatus?.permissionMode;
  if (mode === "ask_approval" || mode === "approve_for_me" || mode === "full_access") {
    return mode;
  }
  return "full_access";
});
const selectedReasoningOption = computed(
  () => reasoningOptions.find((option) => option.value === selectedReasoningMode.value) || reasoningOptions[0]
);
const reasoningLabel = computed(() => `推理：${selectedReasoningOption.value.shortLabel}`);
const storyOptionsLabel = computed(
  () => `${agentStore.storyFragmentCount}段/${agentStore.storyFragmentWordCount}字`
);
const selectedChapterTemplate = computed(() =>
  agentStore.storyChapterTemplates.find((template) => template.id === agentStore.storyChapterTemplateId) || null
);
const isSingleFileChapterTemplate = computed(
  () => agentStore.storyChapterTemplateId === "single_file_chapter_directory"
    || selectedChapterTemplate.value?.contentMode === "single_file"
);
const selectedChapterTemplateDescription = computed(() => {
  const template = selectedChapterTemplate.value;
  if (!template) {
    return "";
  }
  const parts = [
    template.description,
    template.segmentNaming ? `文件：${template.segmentNaming}` : "",
    template.contentMode === "single_file" ? "片段数量固定为 1" : "片段数量不受每章 3 段限制"
  ].filter(Boolean);
  return parts.join(" · ");
});
const storyChapterTemplateErrorMessage = computed(() => {
  const message = String(agentStore.storyChapterTemplatesError || "").trim();
  if (!message || /request failed with status code 404|404|not found/i.test(message)) {
    return "";
  }
  return message;
});
const contextRatio = computed(() => {
  if (agentStore.usageRatio !== null && Number.isFinite(agentStore.usageRatio)) {
    return agentStore.usageRatio;
  }
  if (agentStore.contextWindow && agentStore.usedTokens !== null) {
    return agentStore.usedTokens / agentStore.contextWindow;
  }
  return null;
});
const contextLevel = computed(() => {
  const ratio = contextRatio.value;
  if ((agentStore.usedTokens ?? 0) <= 0) return "unknown";
  if (ratio === null) return "unknown";
  const warningRatio =
    agentStore.warningThreshold && agentStore.contextWindow
      ? agentStore.warningThreshold / agentStore.contextWindow
      : 0.6;
  const dangerRatio =
    agentStore.compactThreshold && agentStore.contextWindow
      ? agentStore.compactThreshold / agentStore.contextWindow
      : 0.85;
  if (ratio >= dangerRatio) return "danger";
  if (ratio >= warningRatio) return "warning";
  return "safe";
});
const contextRingStyle = computed<Record<string, string>>(() => {
  const ratio = contextRatio.value;
  const safeRatio = ratio === null || (agentStore.usedTokens ?? 0) <= 0 ? 0 : Math.min(1, Math.max(0, ratio));
  const colorByLevel: Record<string, string> = {
    safe: "#22c55e",
    warning: "#f59e0b",
    danger: "#ef4444",
    unknown: "rgba(148, 163, 184, 0.72)"
  };
  return {
    "--coomi-context-progress": `${Math.round(safeRatio * 360)}deg`,
    "--coomi-context-color": colorByLevel[contextLevel.value] || colorByLevel.unknown
  };
});
const contextTooltip = computed(() => {
  const ratio = contextRatio.value;
  const ctx =
    ratio !== null && agentStore.contextWindow && agentStore.usedTokens !== null
      ? `上下文：${(ratio * 100).toFixed(1)}% (${formatTokenCount(agentStore.usedTokens)} / ${formatTokenCount(agentStore.contextWindow)})`
      : "上下文：未知";
  const cum = `累计：${agentStore.cumulativeTokens !== null ? formatTokenCount(agentStore.cumulativeTokens) : "未知"} tokens`;
  const compression = `压缩：${agentStore.compressionStatus || "空闲"}`;
  return `${ctx}\n${cum}\n${compression}`;
});
const filteredCommands = computed(() => {
  const value = agentStore.promptInput.trimStart();
  if (!value.startsWith("/")) {
    return [];
  }
  const query = value.split(/\s/, 1)[0].toLowerCase();
  return slashCommands.filter((command) => command.value.trim().toLowerCase().startsWith(query));
});
const commandMenuVisible = computed(() => commandMenuOpen.value && filteredCommands.value.length > 0 && !agentStore.isRunning);
const approvalQueue = computed(() => agentStore.pendingApprovals);
const activeApproval = computed(
  () => approvalQueue.value[approvalCursor.value] || approvalQueue.value[approvalQueue.value.length - 1] || null
);
const activeApprovalDraft = computed(() =>
  activeApproval.value ? approvalDrafts.value[activeApproval.value.approvalId] || null : null
);
const allApprovalsComplete = computed(() =>
  approvalQueue.value.every((approval) => isApprovalDraftComplete(approval, approvalDrafts.value[approval.approvalId]))
);
const canConfirmApproval = computed(() => approvalQueue.value.length > 0 && allApprovalsComplete.value);
const approvalConfirmLabel = computed(() => (approvalQueue.value.length > 1 ? "提交全部答案" : "确认"));
const approvalConfirmTitle = computed(() =>
  approvalQueue.value.length > 1 && !canConfirmApproval.value ? "还有未回答的问题，可用左右箭头切换查看" : ""
);
const commitPromptSummary = computed(() => {
  const prompt = agentStore.pendingCommitPrompt;
  if (!prompt) {
    return "";
  }
  return `${prompt.changedFileCount} 个文件已修改 +${prompt.added} -${prompt.removed}`;
});
const commitPromptFiles = computed(() => agentStore.pendingCommitPrompt?.changedFiles || []);
const promptDockActive = computed(() => Boolean(agentStore.pendingApproval || agentStore.pendingCommitPrompt));
const promptDockHandleTitle = computed(() =>
  agentStore.pendingApproval ? "展开待回答的问题" : "展开未提交修改面板"
);
const collapsedHandlesVisible = computed(
  () =>
    (executionFloatVisible.value && executionFloatCollapsed.value) ||
    (promptDockActive.value && promptDockCollapsed.value)
);
function updateComposerHeightCeiling(): void {
  const panelHeight = dockRef.value?.clientHeight || 0;
  composerHeightCeiling.value = panelHeight > 0
    ? Math.max(COMPOSER_MIN_HEIGHT, Math.floor(panelHeight * 0.6))
    : Number.POSITIVE_INFINITY;
}
const effectiveComposerMaxHeight = computed(() =>
  clamp(DEFAULT_COMPOSER_MAX_HEIGHT, COMPOSER_MIN_HEIGHT, composerHeightCeiling.value)
);

onMounted(async () => {
  window.addEventListener("pointerdown", handleDocumentPointerDown);
  window.addEventListener("resize", handleComposerPanelResize);
  updateComposerHeightCeiling();
  await nextTick();
  resizeComposer();
  await agentStore.refreshCoomiStatus();
  syncStoryGenerationOptionsFromProjectSettings();
  await agentStore.loadSessions();
  if (agentStore.currentSessionId) {
    await agentStore.loadHistory();
    await agentStore.loadFollowups();
  }
});

onBeforeUnmount(() => {
  window.removeEventListener("pointerdown", handleDocumentPointerDown);
  window.removeEventListener("resize", handleComposerPanelResize);
  stopRuntimeTimer();
});

watch(
  () => agentStore.executionHistory,
  () => {
    void nextTick(() => {
      if (shouldFollowOutput.value) {
        scrollToBottom(false);
      } else {
        updateScrollFollowState();
      }
    });
  },
  { deep: true }
);

watch(
  () => agentStore.currentTraceId,
  (traceId, previousTraceId) => {
    if (traceId && traceId !== previousTraceId && agentStore.isRunning) {
      shouldFollowOutput.value = true;
      showScrollToLatest.value = false;
      void nextTick(() => scrollToBottom(false));
    }
  }
);

watch(
  () => agentStore.isRunning,
  (running) => {
    if (running) {
      startRuntimeTimer();
      return;
    }
    stopRuntimeTimer();
  },
  { immediate: true }
);

watch(
  () => gitStore.summary?.clean,
  (clean) => {
    if (clean === true) {
      agentStore.clearLiveChanges();
    }
  },
  { immediate: true }
);

watch(
  () => agentStore.promptInput,
  (value) => {
    const normalized = value.trimStart();
    if (!normalized.startsWith("/")) {
      commandMenuOpen.value = false;
      selectedCommandIndex.value = 0;
      return;
    }
    selectedCommandIndex.value = Math.min(selectedCommandIndex.value, Math.max(0, filteredCommands.value.length - 1));
  }
);

watch(
  () => agentStore.pendingApprovals,
  (queue, previous) => {
    const validIds = new Set(queue.map((item) => item.approvalId));
    const drafts: Record<string, ApprovalDraft> = {};
    for (const [id, draft] of Object.entries(approvalDrafts.value)) {
      if (validIds.has(id)) {
        drafts[id] = draft;
      }
    }
    for (const approval of queue) {
      if (!drafts[approval.approvalId]) {
        drafts[approval.approvalId] = {
          value:
            approval.options.find((option) => option.isRecommended)?.value ||
            approval.options[0]?.value ||
            "",
          text: ""
        };
      }
    }
    approvalDrafts.value = drafts;
    if (!queue.length) {
      approvalCursor.value = 0;
      return;
    }
    if (!previous?.length) {
      approvalCursor.value = 0;
    } else if (approvalCursor.value > queue.length - 1) {
      approvalCursor.value = queue.length - 1;
    }
    promptDockCollapsed.value = false;
  }
);

watch(
  () => agentStore.pendingCommitPrompt,
  (prompt) => {
    commitPromptMode.value = "";
    commitMessage.value = "";
    if (prompt) {
      promptDockCollapsed.value = false;
    }
  }
);

watch(executionFloatSignature, (signature, previousSignature) => {
  if (!signature) {
    executionFloatCollapsed.value = false;
    return;
  }
  if (signature !== previousSignature) {
    executionFloatCollapsed.value = false;
  }
});

watch(
  () => [
    workspaceStore.currentProject?.workspaceRoot || "",
    workspaceStore.storySettings.storyFragmentCount,
    workspaceStore.storySettings.storyFragmentWordCount,
    workspaceStore.storySettings.storyChapterTemplateId
  ],
  () => {
    syncStoryGenerationOptionsFromProjectSettings();
  },
  { immediate: true }
);

async function handleSubmitOrStop(): Promise<void> {
  if (agentStore.editingTraceId || agentStore.isReexecuting) {
    return;
  }
  await agentStore.runPrompt();
  await nextTick();
  resizeComposer();
}

async function handleStopRun(): Promise<void> {
  await agentStore.stopActiveRun();
}

async function handleNoSnapshotConfirm(): Promise<void> {
  await agentStore.confirmNoSnapshot();
}

function handleNoSnapshotCancel(): void {
  agentStore.cancelNoSnapshot();
}

function handleComposerKeydown(event: KeyboardEvent): void {
  if (agentStore.editingTraceId) {
    if (event.key === "Escape") {
      event.preventDefault();
      handleCancelEdit();
    }
    return;
  }
  if (agentStore.pendingApproval) {
    if (event.key === "Escape") {
      event.preventDefault();
      void handleApprovalCancel();
    }
    return;
  }
  if (agentStore.pendingCommitPrompt) {
    if (event.key === "Escape") {
      event.preventDefault();
      void handleCommitPromptSkip();
    }
    return;
  }
  if (commandMenuVisible.value) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      selectedCommandIndex.value = (selectedCommandIndex.value + 1) % filteredCommands.value.length;
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      selectedCommandIndex.value =
        (selectedCommandIndex.value - 1 + filteredCommands.value.length) % filteredCommands.value.length;
      return;
    }
    if (event.key === "Enter" || event.key === "Tab") {
      event.preventDefault();
      selectCommand(selectedCommandIndex.value);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      commandMenuOpen.value = false;
      return;
    }
  }
  if (event.key === "Tab" && event.shiftKey) {
    event.preventDefault();
    void handleCyclePermission();
    return;
  }
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void handleSubmitOrStop();
  }
}

async function handleCyclePermission(): Promise<void> {
  await agentStore.cycleCoomiPermission();
}

function togglePermissionMenu(): void {
  permissionMenuOpen.value = !permissionMenuOpen.value;
  reasoningMenuOpen.value = false;
  storyOptionsOpen.value = false;
  commandMenuOpen.value = false;
}

function toggleReasoningMenu(): void {
  reasoningMenuOpen.value = !reasoningMenuOpen.value;
  permissionMenuOpen.value = false;
  storyOptionsOpen.value = false;
  commandMenuOpen.value = false;
}

function toggleStoryOptions(): void {
  storyOptionsOpen.value = !storyOptionsOpen.value;
  permissionMenuOpen.value = false;
  reasoningMenuOpen.value = false;
  commandMenuOpen.value = false;
  if (storyOptionsOpen.value) {
    void agentStore.loadStoryChapterTemplates({ force: true });
    if (!workspaceStore.launchScreenVisible) {
      void workspaceStore.refreshStorySettings().then(syncStoryGenerationOptionsFromProjectSettings);
    }
  }
}

function handleDocumentPointerDown(event: PointerEvent): void {
  const target = event.target instanceof Node ? event.target : null;
  if (target && composerRef.value?.contains(target)) {
    return;
  }
  permissionMenuOpen.value = false;
  reasoningMenuOpen.value = false;
  storyOptionsOpen.value = false;
}

function updateStoryFragmentCount(event: Event): void {
  const target = event.target as HTMLInputElement | null;
  void persistStoryGenerationOptions({ fragmentCount: Number(target?.value || 1) });
}

function updateStoryFragmentWordCount(event: Event): void {
  const target = event.target as HTMLInputElement | null;
  void persistStoryGenerationOptions({ fragmentWordCount: Number(target?.value || 2000) });
}

function updateStoryChapterTemplate(event: Event): void {
  const target = event.target as HTMLSelectElement | null;
  void persistStoryGenerationOptions({
    chapterTemplateId: target?.value || "default_chapter_directory"
  });
}

function syncStoryGenerationOptionsFromProjectSettings(): void {
  if (workspaceStore.launchScreenVisible) {
    return;
  }
  agentStore.setStoryGenerationOptions({
    fragmentCount: workspaceStore.storySettings.storyFragmentCount,
    fragmentWordCount: workspaceStore.storySettings.storyFragmentWordCount,
    chapterTemplateId: workspaceStore.storySettings.storyChapterTemplateId || "default_chapter_directory"
  });
}

async function persistStoryGenerationOptions(options: {
  fragmentCount?: number;
  fragmentWordCount?: number;
  chapterTemplateId?: string;
}): Promise<void> {
  agentStore.setStoryGenerationOptions(options);
  if (workspaceStore.launchScreenVisible || !workspaceStore.currentProject) {
    return;
  }
  try {
    await workspaceStore.updateStorySettings({
      storyFragmentCount: agentStore.storyFragmentCount,
      storyFragmentWordCount: agentStore.storyFragmentWordCount,
      storyChapterTemplateId: agentStore.storyChapterTemplateId
    });
  } catch (error: unknown) {
    console.warn("Failed to persist Storydex story generation options.", error);
  }
}

function isPermissionOptionActive(value: PermissionChoice): boolean {
  if (value === "plan_mode") {
    return Boolean(agentStore.coomiStatus?.planMode);
  }
  return !agentStore.coomiStatus?.planMode && agentStore.coomiStatus?.permissionMode === value;
}

function permissionToneClass(value: PermissionChoice): string {
  return `permission-${value.replace(/_/g, "-")}`;
}

async function selectPermissionOption(value: PermissionChoice): Promise<void> {
  permissionMenuOpen.value = false;
  if (agentStore.isRunning) {
    return;
  }
  if (value === "plan_mode") {
    if (!agentStore.coomiStatus?.planMode) {
      await runCoomiCommand("/plan");
    }
    return;
  }
  if (agentStore.coomiStatus?.planMode) {
    await runCoomiCommand("/exit_plan");
  }
  await agentStore.setCoomiPermission(value);
}

function selectReasoningOption(value: ReasoningChoice): void {
  selectedReasoningMode.value = value;
  reasoningMenuOpen.value = false;
}

async function runCoomiCommand(command: string): Promise<void> {
  const draft = agentStore.promptInput;
  agentStore.promptInput = command;
  try {
    await agentStore.runPrompt();
  } finally {
    if (draft.trim()) {
      agentStore.promptInput = draft;
    }
    await nextTick();
    resizeComposer();
  }
}

function handleNewSession(): void {
  agentStore.createNewSession();
  sessionMenuOpen.value = false;
  configPanelOpen.value = false;
  void nextTick(() => inputRef.value?.focus());
}

async function handleSessionSelect(sessionId: string): Promise<void> {
  await agentStore.selectSession(sessionId);
  sessionMenuOpen.value = false;
}

async function handleSessionDelete(sessionId: string): Promise<void> {
  const session = sessionSummaries.value.find((item) => item.sessionId === sessionId);
  const label = session?.firstPrompt || session?.sessionId || sessionId;
  if (!window.confirm(`删除会话“${label}”？此操作会清空该会话的历史记录。`)) {
    return;
  }
  await agentStore.deleteSession(sessionId);
}

function canRollbackRun(run: AgentExecutionRun): boolean {
  return Boolean(
    run.traceId
    && run.traceId === latestConversationTraceId.value
    && run.status !== "running"
    && run.status !== "superseded"
    && !agentStore.isRunning
    && !agentStore.isRollingBack
    && !agentStore.isReexecuting
    && !agentStore.editingTraceId
  );
}

async function handleRollbackEdit(run: AgentExecutionRun): Promise<void> {
  if (!canRollbackRun(run)) {
    return;
  }
  if (!agentStore.beginEditLatestRun(run)) {
    return;
  }
  await nextTick();
  inputRef.value?.focus();
  const cursor = inputRef.value?.value.length || 0;
  inputRef.value?.setSelectionRange(cursor, cursor);
  resizeComposer();
}

function handleCancelEdit(): void {
  agentStore.cancelEditLatestRun();
  void nextTick(() => {
    inputRef.value?.focus();
    resizeComposer();
  });
}

async function handleReexecuteEdit(): Promise<void> {
  const accepted = await agentStore.reexecuteEditedLatestRun();
  await nextTick();
  resizeComposer();
  if (!accepted) {
    inputRef.value?.focus();
  }
}

async function handleRollbackDelete(run: AgentExecutionRun): Promise<void> {
  if (!canRollbackRun(run)) {
    return;
  }
  if (!window.confirm("删除本轮对话记录？此操作不会回滚已产生的文件变更。")) {
    return;
  }
  await agentStore.rollbackLatestRun({ refillComposer: false });
}

function beginFollowupEdit(message: AgentFollowupMessage): void {
  if (!canEditFollowup(message)) {
    return;
  }
  editingFollowupId.value = message.messageId;
  editingFollowupDraft.value = message.content;
}

function cancelFollowupEdit(): void {
  editingFollowupId.value = "";
  editingFollowupDraft.value = "";
}

async function saveFollowupEdit(message: AgentFollowupMessage): Promise<void> {
  const content = editingFollowupDraft.value.trim();
  if (!content || editingFollowupId.value !== message.messageId) {
    return;
  }
  if (await agentStore.editFollowup(message.messageId, content)) {
    cancelFollowupEdit();
  }
}

async function handleDeleteFollowup(message: AgentFollowupMessage): Promise<void> {
  if (!canEditFollowup(message) || !window.confirm("删除这条待发送信息？")) {
    return;
  }
  if (await agentStore.deleteFollowup(message.messageId)) {
    if (editingFollowupId.value === message.messageId) {
      cancelFollowupEdit();
    }
  }
}

async function handleSteerFollowup(message: AgentFollowupMessage): Promise<void> {
  if (
    !canSteerFollowup(message) ||
    !agentStore.isRunning ||
    !agentStore.currentTraceId ||
    agentStore.isReexecuting
  ) {
    return;
  }
  await agentStore.steerFollowup(message.messageId);
}

async function handleResumeFollowups(): Promise<void> {
  await agentStore.resumeFollowups();
}

function canEditFollowup(message: AgentFollowupMessage): boolean {
  return message.status === "pending" || message.status === "steering";
}

function canSteerFollowup(message: AgentFollowupMessage): boolean {
  return message.mode === "queued" && message.status === "pending";
}

function followupStatusLabel(message: AgentFollowupMessage): string {
  if (message.statusDetail) {
    return message.statusDetail;
  }
  return {
    pending: "等待当前轮完成",
    steering: "等待安全中断点",
    dispatching: "正在启动下一轮",
    sent: "已发送",
    cancelled: "已删除",
    failed: "发送失败"
  }[message.status] || message.status;
}

function followupPauseLabel(reason: string): string {
  return {
    manual_stop: "已手动停止执行",
    execution_stopped: "当前执行已停止",
    permission_request: "等待权限确认",
    snapshot_confirmation: "等待恢复点确认",
    git_commit_prompt: "等待本地版本处理",
    needs_user_input: "等待补充信息",
    execution_error: "当前执行发生错误",
    preflight_error: "执行预处理失败",
    client_disconnected: "连接已中断"
  }[reason] || reason || "等待用户恢复";
}

async function handleConfigSaved(): Promise<void> {
  await agentStore.refreshCoomiStatus();
}

function insertCommand(command: string): void {
  agentStore.promptInput = command;
  commandMenuOpen.value = false;
  void nextTick(() => {
    inputRef.value?.focus();
    resizeComposer();
  });
}

function selectCommand(index: number): void {
  const command = filteredCommands.value[index];
  if (!command) {
    return;
  }
  insertCommand(command.value);
}

function handleComposerInput(): void {
  resizeComposer();
  const value = agentStore.promptInput.trimStart();
  commandMenuOpen.value = value.startsWith("/");
  selectedCommandIndex.value = Math.min(selectedCommandIndex.value, Math.max(0, filteredCommands.value.length - 1));
}

function resizeComposer(): void {
  const input = inputRef.value;
  if (!input) {
    return;
  }
  input.style.height = "auto";
  input.style.height = `${Math.min(effectiveComposerMaxHeight.value, Math.max(COMPOSER_MIN_HEIGHT, input.scrollHeight))}px`;
}

function handleComposerPanelResize(): void {
  updateComposerHeightCeiling();
  resizeComposer();
}

function isApprovalDraftComplete(approval: AgentPendingApproval, draft: ApprovalDraft | undefined): boolean {
  if (!draft) {
    return false;
  }
  if (approval.kind === "question" && approval.allowText && draft.value === "answer") {
    return Boolean(draft.text.trim());
  }
  return Boolean(draft.value || draft.text.trim());
}

function goToApproval(index: number): void {
  if (index < 0 || index > approvalQueue.value.length - 1) {
    return;
  }
  approvalCursor.value = index;
}

function selectApprovalOption(value: string): void {
  const approval = activeApproval.value;
  if (!approval) {
    return;
  }
  const draft = approvalDrafts.value[approval.approvalId] || { value: "", text: "" };
  approvalDrafts.value = {
    ...approvalDrafts.value,
    [approval.approvalId]: { ...draft, value }
  };
  // 选完自动进入下一题；可随时用头部箭头返回修改之前的选择。
  const needsText = approval.kind === "question" && approval.allowText && value === "answer";
  if (!needsText && approvalCursor.value < approvalQueue.value.length - 1) {
    approvalCursor.value += 1;
  }
}

function updateApprovalDraftText(text: string): void {
  const approval = activeApproval.value;
  if (!approval) {
    return;
  }
  const draft = approvalDrafts.value[approval.approvalId] || { value: "", text: "" };
  approvalDrafts.value = {
    ...approvalDrafts.value,
    [approval.approvalId]: { ...draft, text }
  };
}

async function handleApprovalConfirm(): Promise<void> {
  const queue = [...approvalQueue.value];
  if (!queue.length || !canConfirmApproval.value) {
    return;
  }
  for (const approval of queue) {
    const draft = approvalDrafts.value[approval.approvalId] || { value: "", text: "" };
    const selectedOption = approval.options.find((option) => option.value === draft.value);
    const selectedValue = draft.value || selectedOption?.value || "answer";
    const response = {
      option: selectedValue,
      label: selectedOption?.label || selectedValue,
      other_text: draft.text.trim() || null
    };
    const decision: "allow" | "deny" | "answer" =
      selectedValue === "allow" ? "allow" : selectedValue === "deny" ? "deny" : "answer";
    await agentStore.resolvePendingApproval(decision, response, approval.approvalId);
  }
}

async function handleApprovalCancel(): Promise<void> {
  const queue = [...approvalQueue.value];
  for (const approval of queue) {
    await agentStore.resolvePendingApproval("cancel", { "__cancelled__": true }, approval.approvalId);
  }
}

function collapseExecutionFloat(): void {
  executionFloatCollapsed.value = true;
}

function expandExecutionFloat(): void {
  executionFloatCollapsed.value = false;
}

function collapsePromptDock(): void {
  promptDockCollapsed.value = true;
}

function expandPromptDock(): void {
  promptDockCollapsed.value = false;
}

async function handleCommitPromptAuto(): Promise<void> {
  commitPromptMode.value = "auto";
  promptDockCollapsed.value = true;
  await agentStore.resolvePendingCommitPrompt("auto");
  if (agentStore.lastError && agentStore.pendingCommitPrompt) {
    promptDockCollapsed.value = false;
  }
}

function selectCommitPromptManual(): void {
  commitPromptMode.value = "manual";
}

async function handleCommitPromptManual(): Promise<void> {
  commitPromptMode.value = "manual";
  promptDockCollapsed.value = true;
  await agentStore.resolvePendingCommitPrompt("manual", commitMessage.value);
  if (agentStore.lastError && agentStore.pendingCommitPrompt) {
    promptDockCollapsed.value = false;
  }
}

async function handleCommitPromptSkip(): Promise<void> {
  commitPromptMode.value = "skip";
  promptDockCollapsed.value = true;
  await agentStore.resolvePendingCommitPrompt("skip");
  if (agentStore.lastError && agentStore.pendingCommitPrompt) {
    promptDockCollapsed.value = false;
  }
}

function approvalOptionLabel(label: string, value: string): string {
  if (value === "allow") {
    return label === "Allow" ? "允许" : label;
  }
  if (value === "deny") {
    return label === "Deny" ? "拒绝" : label;
  }
  return label;
}

function approvalOptionDescription(value: string): string {
  return value === "allow" ? "批准本次工具调用。" : "拒绝本次工具调用。";
}

function isStreamNearBottom(stream: HTMLElement): boolean {
  return stream.scrollHeight - stream.scrollTop - stream.clientHeight <= SCROLL_BOTTOM_THRESHOLD;
}

function updateScrollFollowState(): void {
  const stream = streamRef.value;
  if (!stream) {
    return;
  }
  const nearBottom = isStreamNearBottom(stream);
  shouldFollowOutput.value = nearBottom;
  showScrollToLatest.value = !nearBottom;
}

function handleStreamScroll(): void {
  updateScrollFollowState();
}

function scrollToBottom(resumeFollow = true): void {
  const stream = streamRef.value;
  if (!stream) {
    return;
  }
  if (resumeFollow) {
    shouldFollowOutput.value = true;
  }
  stream.scrollTop = stream.scrollHeight;
  showScrollToLatest.value = false;
}

function formatRunDuration(durationMs: number): string {
  const totalSeconds = Math.max(0, Math.floor(durationMs / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h${minutes}m${seconds}s`;
  if (minutes > 0) return `${minutes}m${seconds}s`;
  return `${seconds}s`;
}

function startRuntimeTimer(): void {
  stopRuntimeTimer();
  runtimeNow.value = Date.now();
  runtimeTimer = window.setInterval(() => {
    runtimeNow.value = Date.now();
  }, 1000);
}

function stopRuntimeTimer(): void {
  if (runtimeTimer !== null) {
    window.clearInterval(runtimeTimer);
    runtimeTimer = null;
  }
  runtimeNow.value = Date.now();
}

function displayEntries(run: AgentExecutionRun): DisplayEntry[] {
  const entries: DisplayEntry[] = [];
  let activeTools: CoomiWaterfallItem[] = [];

  const flushTools = (terminal = false): void => {
    if (!activeTools.length) {
      return;
    }
    const first = activeTools[0];
    const status = toolGroupStatus(activeTools, terminal);
    entries.push({
      kind: "tools",
      id: `${run.traceId}-tools-${first.id}`,
      tools: activeTools,
      status,
      terminal
    });
    activeTools = [];
  };

  for (const item of run.items) {
    if (item.type === "usage" || item.type === "compression" || item.type === "system") {
      continue;
    }
    if (item.type === "tool") {
      activeTools.push(item);
      continue;
    }
    flushTools(true);
    entries.push({ kind: "item", id: item.id, item });
  }
  flushTools(run.status !== "running");
  return entries;
}

function toolGroupStatus(tools: CoomiWaterfallItem[], terminal = false): CoomiWaterfallItemStatus {
  if (!terminal && tools.some((tool) => tool.status === "running")) {
    return "running";
  }
  if (tools.some((tool) => tool.status === "error")) {
    return "error";
  }
  return "success";
}

function isFoldOpen(id: string, defaultOpen = false): boolean {
  return foldState.value[id] ?? defaultOpen;
}

function toggleFold(id: string, defaultOpen = false): void {
  foldState.value = { ...foldState.value, [id]: !isFoldOpen(id, defaultOpen) };
}

function toolGroupDefaultOpen(entry: Extract<DisplayEntry, { kind: "tools" }>): boolean {
  return entry.status === "running";
}

function isToolGroupOpen(entry: Extract<DisplayEntry, { kind: "tools" }>): boolean {
  return isFoldOpen(entry.id, toolGroupDefaultOpen(entry));
}

function toolChunks(entry: Extract<DisplayEntry, { kind: "tools" }>): ToolChunk[] {
  const chunks: ToolChunk[] = [];
  for (let index = 0; index < entry.tools.length; index += TOOL_CHUNK_SIZE) {
    const tools = entry.tools.slice(index, index + TOOL_CHUNK_SIZE);
    const chunkIndex = Math.floor(index / TOOL_CHUNK_SIZE);
    const status = toolGroupStatus(tools, entry.terminal);
    chunks.push({
      id: `${entry.id}-chunk-${chunkIndex}`,
      index: chunkIndex,
      start: index + 1,
      end: index + tools.length,
      tools,
      status
    });
  }
  return chunks;
}

function toolChunkDefaultOpen(
  entry: Extract<DisplayEntry, { kind: "tools" }>,
  chunk: ToolChunk
): boolean {
  return entry.status === "running" && chunk.status === "running";
}

function isToolChunkOpen(entry: Extract<DisplayEntry, { kind: "tools" }>, chunk: ToolChunk): boolean {
  return toolChunkState.value[chunk.id] ?? toolChunkDefaultOpen(entry, chunk);
}

function toggleToolChunk(id: string, defaultOpen = false): void {
  toolChunkState.value = { ...toolChunkState.value, [id]: !(toolChunkState.value[id] ?? defaultOpen) };
}

function reasoningTitle(run: AgentExecutionRun, item: CoomiWaterfallItem): string {
  const lineCount = item.content.split(/\r?\n/).filter((line) => line.trim()).length || 1;
  const status = isActiveReasoning(run, item) ? "running" : "completed";
  return `Thinking - ${status} - ${lineCount} lines`;
}

function isActiveReasoning(run: AgentExecutionRun, item: CoomiWaterfallItem): boolean {
  if (run.status !== "running") {
    return false;
  }
  const entries = displayEntries(run);
  const last = [...entries].reverse().find((entry) => entry.kind === "item" || entry.kind === "tools");
  return last?.kind === "item" && last.item.type === "reasoning" && last.item.id === item.id;
}

function toolGroupTitle(entry: Extract<DisplayEntry, { kind: "tools" }>): string {
  if (entry.tools.length === 1) {
    return toolSummary(entry.tools[0]);
  }
  const status = toolStatusLabel(entry.status);
  return `工具 · ${status} · ${entry.tools.length} 次调用`;
}

function toolChunkTitle(chunk: ToolChunk): string {
  return `工具 ${chunk.start}-${chunk.end} · ${toolStatusLabel(chunk.status)}`;
}

function toolSummary(tool: CoomiWaterfallItem): string {
  const name = tool.toolName || tool.title || "工具";
  const status = toolStatusLabel(tool.status);
  const detail = compactToolDetail(tool);
  return detail ? `${name} · ${status} · ${detail}` : `${name} · ${status}`;
}

function toolStatusLabel(status: CoomiWaterfallItemStatus): string {
  const labels: Record<string, string> = {
    success: "已完成",
    running: "运行中",
    error: "错误",
    pending: "等待中"
  };
  return labels[status] || status;
}

function compactToolDetail(tool: CoomiWaterfallItem): string {
  const pathValue = firstStringFromRecord(tool.arguments, ["path", "file", "file_path", "relative_path", "query", "pattern"]);
  if (pathValue) {
    return pathValue;
  }
  if (tool.resultPreview) {
    return compactText(tool.resultPreview, 80);
  }
  return "";
}

function toolRowId(entry: Extract<DisplayEntry, { kind: "tools" }>, tool: CoomiWaterfallItem): string {
  return `${entry.id}-${tool.id}`;
}

function isToolRowOpen(entry: Extract<DisplayEntry, { kind: "tools" }>, tool: CoomiWaterfallItem): boolean {
  return toolRowState.value[toolRowId(entry, tool)] ?? false;
}

function toggleToolRow(id: string): void {
  toolRowState.value = { ...toolRowState.value, [id]: !toolRowState.value[id] };
}

function formatItemType(type: CoomiWaterfallItemType): string {
  const labels: Record<CoomiWaterfallItemType, string> = {
    user: "用户",
    assistant: "助手",
    reasoning: "推理",
    tool: "工具",
    usage: "用量",
    compression: "压缩",
    phase: "阶段",
    system: "系统",
    error: "错误"
  };
  return labels[type] || type;
}

function formatStatus(status: string, errorMessage: string): string {
  if (errorMessage) return "错误";
  if (status === "running") return "运行中";
  if (status === "completed") return "已完成";
  if (status === "superseded") return "已被替换";
  if (status === "cancelled" || status === "stopped") return "已停止";
  if (status === "failed") return "错误";
  return status || "空闲";
}

function formatDate(value: string, timeOnly = false): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value || "";
  }
  return date.toLocaleString("zh-CN", {
    hour12: false,
    ...(timeOnly ? { hour: "2-digit", minute: "2-digit", second: "2-digit" } : {})
  });
}

function compactJson(value: unknown): string {
  try {
    return compactText(JSON.stringify(value, null, 2), 1800);
  } catch {
    return compactText(String(value ?? ""), 1800);
  }
}

function renderMarkdown(value: string): string {
  return markdown.render(value || "");
}

function handleMarkdownLinkClick(event: MouseEvent): void {
  const anchor = findMarkdownLinkAnchor(event.target);
  const href = anchor?.getAttribute("href") || "";
  const relativePath = resolveMarkdownWorkspaceHref(href, workspaceStore.activeFileBindingOrPath);
  if (relativePath) {
    event.preventDefault();
    event.stopPropagation();
    void workspaceStore.openFile(relativePath);
    return;
  }

  if (isExternalMarkdownHref(href)) {
    event.preventDefault();
    window.open(anchor?.href || href, "_blank", "noopener,noreferrer");
  }
}

function compactText(value: unknown, limit = 1800): string {
  const text = String(value ?? "").trim();
  return text.length > limit ? `${text.slice(0, limit)}\n……（已截断）` : text;
}

function firstStringFromRecord(value: unknown, keys: string[]): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return "";
  }
  const record = value as Record<string, unknown>;
  for (const key of keys) {
    const candidate = record[key];
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate.trim();
    }
  }
  return "";
}

function formatTokenCount(value: number): string {
  if (!Number.isFinite(value)) {
    return "unknown";
  }
  const absolute = Math.abs(value);
  if (absolute >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(1)}M`;
  }
  if (absolute >= 1_000) {
    return `${(value / 1_000).toFixed(1)}K`;
  }
  return String(Math.round(value));
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

defineExpose({
  __testUtils: import.meta.env.MODE === "test" ? {
    conversationRuns,
    latestConversationTraceId,
    sessionSummaries,
    modelLabel,
    permissionControlLabel,
    activePermissionTone,
    selectedReasoningOption,
    reasoningLabel,
    storyOptionsLabel,
    selectedChapterTemplate,
    isSingleFileChapterTemplate,
    selectedChapterTemplateDescription,
    storyChapterTemplateErrorMessage,
    contextRatio,
    contextLevel,
    contextRingStyle,
    contextTooltip,
    filteredCommands,
    commandMenuVisible,
    visibleFollowups,
    composerPlaceholder,
    approvalQueue,
    activeApproval,
    activeApprovalDraft,
    allApprovalsComplete,
    canConfirmApproval,
    approvalConfirmLabel,
    approvalConfirmTitle,
    commitPromptSummary,
    commitPromptFiles,
    promptDockActive,
    promptDockHandleTitle,
    collapsedHandlesVisible,
    commandMenuOpen,
    permissionMenuOpen,
    reasoningMenuOpen,
    storyOptionsOpen,
    selectedReasoningMode,
    selectedCommandIndex,
    approvalCursor,
    approvalDrafts,
    commitPromptMode,
    commitMessage,
    executionFloatCollapsed,
    promptDockCollapsed,
    composerHeightCeiling,
    effectiveComposerMaxHeight,
    buildPendingTargetPathOperationItems,
    buildLiveOperationItemsForPending,
    attachPendingWriteContext,
    shouldApplyWholePendingWrite,
    handleApproveOperation,
    handleRejectOperation,
    handleSubmitOrStop,
    handleStopRun,
    handleNoSnapshotConfirm,
    handleNoSnapshotCancel,
    handleComposerKeydown,
    handleCyclePermission,
    togglePermissionMenu,
    toggleReasoningMenu,
    toggleStoryOptions,
    handleDocumentPointerDown,
    updateStoryFragmentCount,
    updateStoryFragmentWordCount,
    updateStoryChapterTemplate,
    syncStoryGenerationOptionsFromProjectSettings,
    persistStoryGenerationOptions,
    isPermissionOptionActive,
    permissionToneClass,
    selectPermissionOption,
    selectReasoningOption,
    runCoomiCommand,
    handleNewSession,
    handleSessionSelect,
    handleSessionDelete,
    canRollbackRun,
    handleRollbackEdit,
    handleRollbackDelete,
    handleCancelEdit,
    handleReexecuteEdit,
    beginFollowupEdit,
    cancelFollowupEdit,
    saveFollowupEdit,
    handleDeleteFollowup,
    handleSteerFollowup,
    handleResumeFollowups,
    canEditFollowup,
    canSteerFollowup,
    followupStatusLabel,
    followupPauseLabel,
    handleConfigSaved,
    insertCommand,
    selectCommand,
    handleComposerInput,
    resizeComposer,
    isApprovalDraftComplete,
    goToApproval,
    selectApprovalOption,
    updateApprovalDraftText,
    handleApprovalConfirm,
    handleApprovalCancel,
    collapseExecutionFloat,
    expandExecutionFloat,
    collapsePromptDock,
    expandPromptDock,
    handleCommitPromptAuto,
    selectCommitPromptManual,
    handleCommitPromptManual,
    handleCommitPromptSkip,
    approvalOptionLabel,
    approvalOptionDescription,
    isStreamNearBottom,
    updateScrollFollowState,
    handleStreamScroll,
    scrollToBottom,
    formatRunDuration,
    startRuntimeTimer,
    stopRuntimeTimer,
    displayEntries,
    toolGroupStatus,
    isFoldOpen,
    toggleFold,
    toolGroupDefaultOpen,
    isToolGroupOpen,
    toolChunks,
    toolChunkDefaultOpen,
    isToolChunkOpen,
    toggleToolChunk,
    reasoningTitle,
    isActiveReasoning,
    toolGroupTitle,
    toolChunkTitle,
    toolSummary,
    toolStatusLabel,
    compactToolDetail,
    toolRowId,
    isToolRowOpen,
    toggleToolRow,
    formatItemType,
    formatStatus,
    formatDate,
    compactJson,
    renderMarkdown,
    handleMarkdownLinkClick,
    compactText,
    firstStringFromRecord,
    formatTokenCount
  } : null
});
</script>

<style scoped>
.coomi-dock {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  max-height: 100%;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
  background: var(--bg-agent);
  color: var(--text-main);
  border-left: 1px solid var(--border-subtle);
}

.coomi-header,
.coomi-composer {
  flex: 0 0 auto;
}

.coomi-header {
  min-height: 44px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 12px;
  background: var(--bg-header);
  border-bottom: 1px solid var(--border-subtle);
}

.coomi-title {
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
  font-weight: 700;
}

.coomi-header-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

.coomi-icon-btn,
.coomi-send,
.coomi-stop-run {
  border: 0;
  background: transparent;
  color: inherit;
  font-family: inherit;
  cursor: pointer;
}

.coomi-icon-btn {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  color: var(--text-muted);
}

.coomi-icon-btn:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}

.coomi-run-state {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 8px;
  border-radius: 4px;
  color: var(--text-muted);
  background: color-mix(in srgb, var(--text-main) 6%, transparent);
  font-size: 11px;
}

.coomi-run-state.running {
  color: var(--warning);
  background: color-mix(in srgb, var(--warning) 14%, transparent);
}

.coomi-dot {
  width: 6px;
  height: 6px;
  border-radius: 999px;
  background: currentColor;
}

.coomi-stream {
  height: 100%;
  max-height: 100%;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 16px 18px 22px;
}

.coomi-stream-shell {
  position: relative;
  flex: 1 1 0;
  min-height: 0;
}

.coomi-scroll-latest {
  position: absolute;
  right: 18px;
  bottom: 12px;
  z-index: 7;
  width: 30px;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid color-mix(in srgb, var(--text-muted) 22%, transparent);
  border-radius: 50%;
  background: color-mix(in srgb, var(--bg-card) 96%, transparent);
  color: var(--text-secondary);
  box-shadow: var(--shadow-popover);
  cursor: pointer;
}

.coomi-scroll-latest:hover {
  border-color: color-mix(in srgb, var(--accent) 40%, var(--border-subtle));
  background: color-mix(in srgb, var(--accent-soft) 24%, var(--bg-card));
  color: var(--accent);
}

.coomi-scroll-latest .material-symbols-rounded {
  font-size: 18px;
}

.coomi-stream.config-open {
  padding-top: 4px;
}

.coomi-welcome {
  min-height: 100%;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  gap: 10px;
  color: var(--text-muted);
  text-align: center;
}

.coomi-welcome img {
  width: 52px;
  height: 52px;
}

.coomi-welcome-title {
  color: var(--text-main);
  font-size: 16px;
  font-weight: 700;
}

.coomi-welcome-copy {
  max-width: 280px;
  margin: 0;
  padding: 6px 10px;
  border: 1px solid var(--border-ghost);
  border-radius: 6px;
  background: color-mix(in srgb, var(--bg-input) 72%, transparent);
  color: var(--text-soft);
  font-size: 12px;
  line-height: 1.5;
}

.coomi-runs,
.coomi-waterfall,
.coomi-session-view {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.coomi-run + .coomi-run {
  padding-top: 18px;
  border-top: 1px solid var(--border-subtle);
}

.coomi-run-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  color: var(--text-muted);
  font-size: 11px;
}

.coomi-run-status {
  margin-left: auto;
}

.coomi-run-status.running {
  color: var(--warning);
}

.coomi-run-status.completed {
  color: var(--success);
}

.coomi-run-status.failed {
  color: var(--danger);
}

.coomi-run-actions {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.14s ease;
}

.coomi-run:hover .coomi-run-actions,
.coomi-run:focus-within .coomi-run-actions {
  opacity: 1;
  pointer-events: auto;
}

.coomi-run-action {
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  border-radius: 4px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.coomi-run-action:hover:not(:disabled),
.coomi-run-action:focus-visible {
  border-color: color-mix(in srgb, var(--accent) 28%, transparent);
  background: color-mix(in srgb, var(--accent-soft) 20%, transparent);
  color: var(--accent);
  outline: none;
}

.coomi-run-action.danger:hover:not(:disabled),
.coomi-run-action.danger:focus-visible {
  border-color: color-mix(in srgb, var(--danger) 28%, transparent);
  background: color-mix(in srgb, var(--danger) 10%, transparent);
  color: var(--danger);
}

.coomi-run-action:disabled {
  cursor: default;
  opacity: 0.45;
}

.coomi-run-action .material-symbols-rounded {
  font-size: 16px;
}

.coomi-no-restore-point {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--warning);
  font-weight: 650;
}

.coomi-no-restore-point .material-symbols-rounded,
.coomi-snapshot-modal-icon {
  font-size: 16px;
}

.coomi-snapshot-modal-mask {
  position: fixed;
  inset: 0;
  z-index: 80;
  display: grid;
  place-items: center;
  padding: 16px;
  overflow-y: auto;
  background: color-mix(in srgb, var(--bg-agent) 58%, rgba(9, 14, 22, 0.42));
  backdrop-filter: blur(4px);
}

.coomi-snapshot-modal {
  width: min(420px, 100%);
  max-height: calc(100dvh - 32px);
  padding: 18px;
  overflow-y: auto;
  border: 1px solid color-mix(in srgb, var(--warning) 42%, var(--border-subtle));
  border-radius: 6px;
  background: var(--bg-card);
  box-shadow: var(--shadow-md);
}

.coomi-snapshot-modal-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--warning);
}

.coomi-snapshot-modal-title-row h2 {
  margin: 0;
  color: var(--text-main);
  font-size: 16px;
  line-height: 1.35;
}

.coomi-snapshot-modal-copy {
  margin: 12px 0 0;
  color: var(--text-soft);
  font-size: 13px;
  line-height: 1.65;
}

.coomi-snapshot-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 18px;
}

.coomi-snapshot-modal-button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 32px;
  padding: 0 11px;
  border: 1px solid var(--border-subtle);
  border-radius: 5px;
  background: var(--bg-card-muted);
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.coomi-snapshot-modal-button:hover {
  border-color: color-mix(in srgb, var(--warning) 52%, var(--border-subtle));
}

.coomi-snapshot-modal-button.primary {
  border-color: color-mix(in srgb, var(--warning) 55%, var(--border-subtle));
  background: color-mix(in srgb, var(--warning) 14%, var(--bg-card));
  color: var(--warning);
}

.coomi-snapshot-modal-button .material-symbols-rounded {
  font-size: 16px;
}

.coomi-event {
  padding: 2px 0 2px 12px;
  border-left: 2px solid color-mix(in srgb, var(--border-strong) 80%, transparent);
}

.coomi-event.type-user {
  border-left-color: var(--info);
}

.coomi-event.type-assistant {
  border-left-color: var(--accent);
}

.coomi-event.type-reasoning {
  border-left-color: var(--warning);
}

.coomi-event.type-tool {
  border-left-color: var(--info);
}

.coomi-event.type-phase {
  border-left-color: var(--info);
}

.coomi-event.status-error {
  border-left-color: var(--danger);
}

.coomi-event-head {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-muted);
  font-size: 11px;
  text-transform: uppercase;
}

.coomi-event-title {
  color: var(--text-soft);
  text-transform: none;
}

.coomi-event-time {
  margin-left: auto;
}

.coomi-event-body {
  margin-top: 6px;
}

.coomi-user-text,
.coomi-assistant-text,
.coomi-reasoning-text,
.coomi-error-text {
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--text-main);
  font-family: inherit;
  font-size: 13px;
  line-height: 1.72;
}

.coomi-assistant-text {
  font-size: 14px;
  line-height: 1.78;
}

.coomi-reasoning-text {
  color: var(--text-soft);
}

.coomi-error-text {
  color: var(--danger);
}

.coomi-phase-text {
  color: var(--text-soft);
  font-size: 12px;
  line-height: 1.5;
  font-variant-numeric: tabular-nums;
}

.coomi-tool-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 6px;
  color: var(--text-muted);
  font-size: 11px;
}

.coomi-details {
  margin-top: 8px;
  color: var(--text-muted);
  font-size: 12px;
}

.coomi-details summary {
  cursor: pointer;
}

.coomi-details pre {
  margin: 7px 0 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--text-main);
  padding: 8px;
  max-height: 220px;
  overflow: auto;
  background: var(--bg-card-muted);
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 11px;
}

.coomi-fold-head,
.coomi-tool-chunk-head,
.coomi-tool-row-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-width: 0;
  border: 0;
  background: transparent;
  color: var(--text-soft);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.coomi-fold-head {
  min-height: 28px;
  padding: 2px 0;
  font-size: 12px;
}

.coomi-fold-head span:first-child,
.coomi-tool-chunk-head span:first-child,
.coomi-tool-row-head span:first-child {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.coomi-fold-meta,
.coomi-tool-chunk-head span:last-child,
.coomi-tool-row-head span:last-child {
  flex: 0 0 auto;
  min-width: 2.5em;
  margin-left: auto;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.2;
  text-align: right;
  white-space: nowrap;
}

.coomi-tool-group {
  border-left-color: var(--info);
}

.coomi-tool-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 6px;
}

.coomi-tool-chunk {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.coomi-tool-chunk-head {
  min-height: 24px;
  padding: 2px 0;
  color: var(--text-muted);
  font-size: 11px;
}

.coomi-tool-chunk-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.coomi-tool-row {
  padding: 2px 0;
  border: 0;
  border-radius: 0;
  background: transparent;
}

.coomi-tool-row-head {
  min-height: 24px;
  padding: 1px 0;
  font-size: 12px;
}

.coomi-tool-preview {
  margin: 4px 0 4px 14px;
}

.coomi-tool-preview .coomi-details pre {
  max-height: 160px;
}

.coomi-running-tail {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  align-self: flex-start;
  margin: 2px 0 0 12px;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.4;
}

.coomi-running-dots {
  animation: coomi-running-pulse 1.2s ease-in-out infinite;
}

@keyframes coomi-running-pulse {
  0%,
  100% {
    opacity: 0.35;
  }
  50% {
    opacity: 1;
  }
}

.coomi-session-title {
  color: var(--text-main);
  font-size: 13px;
  font-weight: 700;
}

.coomi-session-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 28px;
  gap: 4px;
  align-items: center;
  width: 100%;
  border: 0;
  border-bottom: 1px solid var(--border-ghost);
  background: transparent;
}

.coomi-session-select {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  width: 100%;
  min-width: 0;
  padding: 10px 4px;
  border: 0;
  background: transparent;
  color: var(--text-main);
  text-align: left;
  font: inherit;
  cursor: pointer;
}

.coomi-session-item:hover,
.coomi-session-item.active {
  background: var(--bg-hover);
}

.coomi-session-select span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.coomi-session-select small,
.coomi-empty {
  color: var(--text-muted);
}

.coomi-session-delete {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.coomi-session-delete:hover:not(:disabled) {
  background: color-mix(in srgb, var(--danger) 14%, transparent);
  color: var(--danger);
}

.coomi-session-delete:disabled {
  opacity: 0.45;
  cursor: default;
}

.coomi-session-delete .material-symbols-rounded {
  font-size: 17px;
}

.coomi-composer {
  position: relative;
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 0 12px 12px;
  background: var(--bg-header);
  border-top: 1px solid var(--border-subtle);
}

.coomi-error {
  color: var(--danger);
  font-size: 12px;
  line-height: 1.5;
}

.coomi-context-ring {
  flex: 0 0 auto;
  width: 18px;
  height: 18px;
  position: relative;
  display: inline-block;
  border-radius: 999px;
  border: 0;
  background:
    conic-gradient(
      var(--coomi-context-color, rgba(148, 163, 184, 0.72)) var(--coomi-context-progress, 0deg),
      rgba(248, 250, 252, 0.9) 0deg
    );
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--border-subtle) 75%, transparent);
}

.coomi-context-ring::after {
  content: "";
  position: absolute;
  inset: 3px;
  border-radius: inherit;
  background: var(--bg-header);
}

.coomi-context-ring.safe {
  --coomi-context-color: #22c55e;
}

.coomi-context-ring.warning {
  --coomi-context-color: #f59e0b;
}

.coomi-context-ring.danger {
  --coomi-context-color: #ef4444;
}

.coomi-context-ring.unknown {
  --coomi-context-color: rgba(148, 163, 184, 0.72);
}

.coomi-composer-status {
  position: relative;
  container: coomi-composer-status / inline-size;
  display: flex;
  align-items: center;
  gap: 7px;
  min-height: 30px;
  padding: 5px 8px 0;
  overflow: visible;
}

.coomi-composer-status > .coomi-status-pill,
.coomi-composer-status > .coomi-status-control {
  min-height: 24px;
  display: inline-flex;
  align-items: center;
}

.coomi-status-pill {
  min-width: 0;
  max-width: 145px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-muted);
  font-size: 11px;
}

.coomi-status-button {
  flex: 0 0 auto;
  min-height: 24px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--text-soft);
  font-family: inherit;
  line-height: 1;
  cursor: pointer;
}

.coomi-status-button.permission-plan-mode,
.coomi-choice-card.permission-plan-mode span {
  color: #60a5fa;
}

.coomi-status-button.permission-ask-approval {
  color: var(--text-main);
}

.coomi-choice-card.permission-ask-approval span {
  color: var(--accent-strong);
}

.coomi-status-button.permission-approve-for-me,
.coomi-choice-card.permission-approve-for-me span {
  color: #22c55e;
}

.coomi-status-button.permission-full-access,
.coomi-choice-card.permission-full-access span {
  color: #f59e0b;
}

.coomi-status-control {
  position: relative;
  flex: 0 1 auto;
  min-width: 0;
}

.coomi-status-popover {
  position: absolute;
  left: 0;
  bottom: calc(100% + 8px);
  z-index: 8;
  width: 230px;
  display: grid;
  gap: 6px;
  padding: 8px;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: var(--bg-input);
  box-shadow: var(--shadow-popover);
}

.coomi-reasoning-popover {
  width: 205px;
}

.coomi-story-control {
  position: static;
  flex: 0 0 auto;
}

.coomi-story-toggle {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  max-width: 118px;
  height: 24px;
  vertical-align: middle;
}

.coomi-story-toggle .material-symbols-rounded {
  flex: 0 0 auto;
  font-size: 15px;
  line-height: 1;
  align-self: center;
}

.coomi-story-toggle span:nth-child(2) {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.coomi-story-caret {
  color: var(--text-muted);
}

.coomi-story-popover {
  right: 8px;
  left: auto;
  width: min(260px, calc(100% - 16px));
  max-width: calc(100% - 16px);
  max-height: min(360px, calc(100vh - 120px));
  box-sizing: border-box;
  gap: 8px;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-color: color-mix(in srgb, var(--text-muted) 45%, transparent) transparent;
  scrollbar-width: thin;
}

.coomi-story-field {
  display: grid;
  grid-template-columns: 62px minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  color: var(--text-muted);
  font-size: 12px;
}

.coomi-story-field input,
.coomi-story-field select {
  width: 100%;
  min-width: 0;
  height: 28px;
  padding: 0 8px;
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  background: color-mix(in srgb, var(--bg-input) 86%, black);
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
  outline: none;
}

.coomi-story-field select {
  cursor: pointer;
  text-overflow: ellipsis;
}

.coomi-story-field input:focus,
.coomi-story-field select:focus {
  border-color: var(--accent);
}

.coomi-story-template-hint {
  display: block;
  margin: -2px 0 0 70px;
  overflow-wrap: anywhere;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.35;
}

.coomi-story-template-hint.error {
  color: var(--danger);
}

@container coomi-composer-status (max-width: 250px) {
  .coomi-story-popover {
    right: 6px;
    width: calc(100% - 12px);
    max-width: calc(100% - 12px);
    padding: 7px;
  }

  .coomi-story-field {
    grid-template-columns: minmax(0, 1fr);
    gap: 4px;
  }

  .coomi-story-template-hint {
    margin-left: 0;
  }
}

.coomi-choice-card {
  width: 100%;
  min-width: 0;
  display: grid;
  gap: 3px;
  padding: 8px 9px;
  border: 1px solid var(--border-ghost);
  border-radius: 4px;
  background: transparent;
  color: var(--text-main);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.coomi-choice-card:hover,
.coomi-choice-card.active {
  border-color: color-mix(in srgb, var(--accent) 42%, var(--border-subtle));
  background: color-mix(in srgb, var(--accent) 12%, var(--bg-input));
}

.coomi-choice-card span {
  font-size: 12px;
  font-weight: 650;
}

.coomi-choice-card small {
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.4;
}

.coomi-composer-status .coomi-context-ring {
  margin-left: auto;
  width: 17px;
  height: 17px;
}

.coomi-execution-float-slot {
  position: absolute;
  left: 12px;
  right: 12px;
  bottom: calc(100% + 8px);
  z-index: 4;
  display: flex;
  align-items: center;
  justify-content: center;
  min-width: 0;
  pointer-events: none;
}

.coomi-execution-float-slot :deep(.agent-execution-float) {
  margin: 0;
  pointer-events: auto;
}

.coomi-markdown :deep(*) {
  max-width: 100%;
}

.coomi-markdown {
  overflow-x: auto;
  white-space: normal;
}

.coomi-markdown :deep(p) {
  margin: 0 0 0.65em;
}

.coomi-markdown :deep(p:last-child) {
  margin-bottom: 0;
}

.coomi-markdown :deep(h1),
.coomi-markdown :deep(h2),
.coomi-markdown :deep(h3),
.coomi-markdown :deep(h4) {
  margin: 0.9em 0 0.45em;
  color: var(--text-main);
  font-weight: 700;
  line-height: 1.35;
}

.coomi-markdown :deep(h1) {
  font-size: 18px;
}

.coomi-markdown :deep(h2) {
  font-size: 16px;
}

.coomi-markdown :deep(h3),
.coomi-markdown :deep(h4) {
  font-size: 14px;
}

.coomi-markdown :deep(ul),
.coomi-markdown :deep(ol) {
  margin: 0.4em 0 0.75em;
  padding-left: 1.35em;
}

.coomi-markdown :deep(li + li) {
  margin-top: 0.2em;
}

.coomi-markdown :deep(hr) {
  height: 1px;
  margin: 12px 0;
  border: 0;
  background: var(--border-subtle);
}

.coomi-markdown :deep(code) {
  padding: 1px 4px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--text-main) 9%, transparent);
  color: var(--text-soft);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 0.88em;
}

.coomi-markdown :deep(pre) {
  max-height: 260px;
  margin: 0.7em 0;
  overflow: auto;
  padding: 10px;
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  background: var(--bg-card-muted);
}

.coomi-markdown :deep(pre code) {
  padding: 0;
  background: transparent;
  font-size: 12px;
}

.coomi-markdown :deep(table) {
  display: table;
  width: max-content;
  max-width: 100%;
  margin: 8px 0 10px;
  border-collapse: collapse;
  table-layout: auto;
  font-size: 12px;
  line-height: 1.35;
}

.coomi-markdown :deep(th),
.coomi-markdown :deep(td) {
  padding: 4px 7px;
  border: 1px solid var(--border-subtle);
  text-align: left;
  vertical-align: top;
  white-space: normal;
}

.coomi-markdown :deep(th) {
  background: color-mix(in srgb, var(--text-main) 6%, transparent);
  color: var(--text-main);
  font-weight: 700;
}

.coomi-command-menu {
  position: absolute;
  left: 12px;
  right: 58px;
  bottom: calc(100% + 6px);
  z-index: 5;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: var(--bg-input);
  background-color: var(--bg-input);
  backdrop-filter: none;
  box-shadow: var(--shadow-popover);
}

.coomi-commit-progress {
  position: absolute;
  left: 12px;
  right: 58px;
  bottom: calc(100% + 6px);
  z-index: 6;
  min-height: 38px;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 8px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: var(--bg-input);
  color: var(--text-soft);
  box-shadow: var(--shadow-popover);
  font-size: 12px;
}

.coomi-commit-progress-dot {
  width: 8px;
  height: 8px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--accent);
  animation: coomi-running-pulse 1.2s ease-in-out infinite;
}

.coomi-commit-progress.completed .coomi-commit-progress-dot {
  background: var(--success);
  animation: none;
}

.coomi-approval-menu {
  gap: 4px;
  padding: 8px;
}

.coomi-approval-head {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: start;
  gap: 8px;
  padding: 4px 2px 7px;
  border-bottom: 1px solid var(--border-ghost);
}

.coomi-approval-head-main {
  display: grid;
  gap: 4px;
  min-width: 0;
}

.coomi-approval-head-main span {
  color: var(--text-main);
  font-size: 12px;
  font-weight: 700;
}

.coomi-approval-head-main small {
  white-space: pre-wrap;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.45;
}

.coomi-approval-head-tools {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.coomi-approval-nav {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}

.coomi-approval-nav-btn {
  width: 22px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.coomi-approval-nav-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-main);
}

.coomi-approval-nav-btn:disabled {
  opacity: 0.4;
  cursor: default;
}

.coomi-approval-nav-btn .material-symbols-rounded {
  font-size: 17px;
}

.coomi-approval-nav-count {
  min-width: 2.4em;
  color: var(--text-muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  text-align: center;
}

.coomi-approval-collapse {
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.coomi-approval-collapse:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}

.coomi-approval-collapse .material-symbols-rounded {
  font-size: 18px;
}

.coomi-commit-file-list {
  margin: 0;
  padding: 6px 2px;
  border-bottom: 1px solid var(--border-ghost);
  list-style: none;
  max-height: 132px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.coomi-commit-file {
  display: grid;
  grid-template-columns: 16px minmax(0, 1fr);
  align-items: center;
  gap: 6px;
  min-height: 22px;
  color: var(--text-soft);
  font-size: 11px;
}

.coomi-commit-file .material-symbols-rounded {
  color: var(--text-muted);
  font-size: 15px;
}

.coomi-commit-file span:last-child {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
}

.coomi-command-option {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 34px;
  padding: 7px 10px;
  border: 0;
  background: transparent;
  color: var(--text-main);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.coomi-command-option:hover,
.coomi-command-option.active {
  background: var(--bg-hover);
}

.coomi-command-option:disabled {
  cursor: default;
  opacity: 0.56;
}

.coomi-command-option:disabled:hover {
  background: transparent;
}

.coomi-command-option span {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 12px;
}

.coomi-command-option small {
  min-width: 0;
  overflow: hidden;
  color: var(--text-muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.coomi-approval-input {
  width: 100%;
  min-height: 58px;
  max-height: 120px;
  resize: vertical;
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  padding: 8px 9px;
  background: color-mix(in srgb, var(--bg-input) 88%, black);
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
  line-height: 1.45;
  outline: none;
}

.coomi-approval-input:focus {
  border-color: var(--accent);
}

.coomi-approval-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding-top: 2px;
}

.coomi-approval-action {
  min-width: 64px;
  height: 30px;
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  background: transparent;
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.coomi-approval-action.primary {
  border-color: transparent;
  background: var(--accent);
  color: var(--accent-contrast);
}

.coomi-approval-action:hover:not(:disabled) {
  background: var(--bg-hover);
}

.coomi-approval-action.primary:hover:not(:disabled) {
  background: var(--accent-strong);
}

.coomi-approval-action:disabled {
  opacity: 0.5;
  cursor: default;
}

.coomi-edit-session,
.coomi-followup-mailbox {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 9px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: color-mix(in srgb, var(--bg-input) 94%, transparent);
}

.coomi-edit-session {
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  border-color: color-mix(in srgb, var(--accent) 34%, var(--border-subtle));
}

.coomi-edit-session-copy {
  min-width: 0;
  display: grid;
  gap: 3px;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.4;
}

.coomi-edit-session-copy strong {
  color: var(--text-main);
  font-size: 12px;
}

.coomi-edit-file-warning {
  color: var(--warning);
}

.coomi-edit-session-actions,
.coomi-followup-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
}

.coomi-secondary-action,
.coomi-primary-action,
.coomi-followup-resume,
.coomi-followup-actions button {
  min-height: 27px;
  padding: 4px 8px;
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  background: transparent;
  color: var(--text-soft);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}

.coomi-primary-action,
.coomi-followup-resume {
  border-color: transparent;
  background: var(--accent);
  color: var(--accent-contrast);
}

.coomi-followup-actions button.danger {
  color: var(--danger);
}

.coomi-followup-actions button.steer {
  border-color: color-mix(in srgb, var(--accent) 34%, var(--border-subtle));
  color: var(--accent);
}

.coomi-followup-actions button.steer:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}

.coomi-secondary-action:disabled,
.coomi-primary-action:disabled,
.coomi-followup-resume:disabled,
.coomi-followup-actions button:disabled {
  cursor: default;
  opacity: 0.5;
}

.coomi-followup-mailbox {
  max-height: 180px;
  overflow-y: auto;
}

.coomi-followup-head,
.coomi-followup-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.coomi-followup-head > div {
  display: flex;
  align-items: baseline;
  gap: 7px;
}

.coomi-followup-head strong {
  color: var(--text-main);
  font-size: 12px;
}

.coomi-followup-head span,
.coomi-followup-meta,
.coomi-followup-paused {
  color: var(--text-muted);
  font-size: 10px;
}

.coomi-followup-paused {
  margin: 0;
  color: var(--warning);
}

.coomi-followup-item {
  display: grid;
  gap: 5px;
  padding-top: 7px;
  border-top: 1px solid var(--border-ghost);
}

.coomi-followup-item.mode-steer .coomi-followup-meta span:first-child {
  color: var(--accent);
}

.coomi-followup-content {
  margin: 0;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
  color: var(--text-soft);
  font-size: 12px;
  line-height: 1.45;
}

.coomi-followup-editor {
  width: 100%;
  min-height: 54px;
  resize: vertical;
  padding: 7px 8px;
  border: 1px solid var(--border-strong);
  border-radius: 4px;
  outline: none;
  background: var(--bg-card-muted);
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
}

.coomi-input-shell {
  position: relative;
  display: flex;
  align-items: flex-end;
  gap: 10px;
  padding: 8px 10px 8px 12px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--bg-input);
  overflow: visible;
}

.coomi-collapsed-handles {
  position: absolute;
  left: 12px;
  right: 12px;
  top: -10px;
  z-index: 6;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  pointer-events: none;
}

.coomi-collapsed-handle {
  position: relative;
  width: 72px;
  height: 14px;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  cursor: pointer;
  pointer-events: auto;
}

.coomi-collapsed-handle::before {
  content: "";
  position: absolute;
  left: 16px;
  right: 16px;
  top: 6px;
  height: 3px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--accent-strong) 72%, transparent);
  opacity: 0.86;
  transition:
    opacity 0.16s ease,
    background-color 0.16s ease,
    transform 0.16s ease;
}

.coomi-collapsed-handle:hover::before,
.coomi-collapsed-handle:focus-visible::before {
  background: var(--accent-strong);
  opacity: 1;
  transform: scaleX(1.12);
}

.coomi-collapsed-handle:focus-visible {
  outline: 1px solid color-mix(in srgb, var(--accent-strong) 38%, transparent);
  outline-offset: 3px;
}

.coomi-input {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 34px;
  resize: none;
  border: 0;
  outline: none;
  padding: 6px 0;
  background: transparent;
  color: var(--text-main);
  font-family: inherit;
  font-size: 13px;
  line-height: 22px;
  box-sizing: border-box;
}

.coomi-input:disabled {
  opacity: 0.7;
}

.coomi-send {
  position: relative;
  flex: 0 0 auto;
  width: 34px;
  height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  background: color-mix(in srgb, var(--accent) 16%, transparent);
  color: var(--text-main);
  transition:
    transform 0.18s ease,
    color 0.18s ease,
    background-color 0.18s ease,
    box-shadow 0.18s ease;
  isolation: isolate;
  overflow: visible;
}

.coomi-stop-run {
  flex: 0 0 auto;
  width: 34px;
  height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  background: transparent;
  color: var(--text-muted);
}

.coomi-stop-run {
  color: var(--danger);
  background: color-mix(in srgb, var(--danger) 10%, transparent);
}

.coomi-stop-run:hover:not(:disabled) {
  background: color-mix(in srgb, var(--danger) 16%, transparent);
}

.coomi-stop-glyph {
  display: block;
  width: 9px;
  height: 9px;
  border-radius: 1px;
  background: currentColor;
  box-shadow: 0 0 0 0.5px currentColor;
}

.coomi-stop-run:disabled {
  cursor: default;
  opacity: 0.45;
}

.coomi-send:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 24%, transparent);
  transform: translateY(-1px);
}

.coomi-send:active:not(:disabled) {
  transform: translateY(0) scale(0.96);
}

.coomi-send .material-symbols-rounded {
  position: relative;
  z-index: 1;
  font-size: 18px;
  line-height: 1;
  transition: transform 0.18s ease;
}

.coomi-send:hover:not(:disabled) .material-symbols-rounded {
  transform: translateY(-1px);
}

.coomi-send.stop {
  color: var(--danger);
  background: color-mix(in srgb, var(--danger) 11%, var(--bg-input));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--danger) 34%, transparent);
}

.coomi-send.stop::after {
  content: "";
  position: absolute;
  inset: 12px;
  z-index: 0;
  border-radius: 2px;
  background: currentColor;
  pointer-events: none;
}

.coomi-send.stop:hover:not(:disabled) {
  background: color-mix(in srgb, var(--danger) 16%, var(--bg-input));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--danger) 46%, transparent);
  transform: translateY(-1px) scale(1.02);
}

.coomi-send.stop:active:not(:disabled) {
  transform: scale(0.96);
}

.coomi-send.stop .material-symbols-rounded {
  opacity: 0;
  transform: scale(0);
}

.coomi-send:disabled {
  opacity: 0.45;
  cursor: default;
}

@media (prefers-reduced-motion: reduce) {
  .coomi-send.stop,
  .coomi-send.stop:hover:not(:disabled),
  .coomi-send.stop:active:not(:disabled) {
    transform: none;
  }
}

@media (hover: none) {
  .coomi-run-actions {
    opacity: 1;
    pointer-events: auto;
  }
}
</style>
