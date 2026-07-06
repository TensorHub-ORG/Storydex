<template>
  <section
    class="story-state-panel"
    :class="{ 'is-loading': panelLoading, 'is-expanded': expanded, 'is-relationship-only': relationshipOnly }"
  >
    <header class="ssp-header">
      <div class="ssp-title">
        <span class="material-symbols-rounded ssp-title-icon">{{ relationshipOnly ? "hub" : "timeline" }}</span>
        <span>{{ relationshipOnly ? "知识图谱" : "项目状态" }}</span>
      </div>
      <div class="ssp-header-actions">
        <button
          v-if="relationshipOnly"
          class="ssp-refresh ssp-rebuild"
          type="button"
          :disabled="panelLoading"
          title="Agent 深度生成知识图谱"
          @click="runWikiAgentWorkflow('generate')"
        >
          <span class="material-symbols-rounded">auto_awesome</span>
        </button>
        <button
          v-if="relationshipOnly"
          class="ssp-refresh"
          type="button"
          :disabled="panelLoading"
          title="Agent 增量更新 WIKI"
          @click="runWikiAgentWorkflow('update')"
        >
          <span class="material-symbols-rounded">published_with_changes</span>
        </button>
        <button
          v-if="relationshipOnly"
          class="ssp-refresh"
          type="button"
          :disabled="panelLoading"
          title="Agent 审阅 WIKI"
          @click="runWikiAgentWorkflow('review')"
        >
          <span class="material-symbols-rounded">fact_check</span>
        </button>
        <button
          class="ssp-refresh"
          type="button"
          :disabled="panelLoading"
          :title="panelLoading ? '加载中...' : '刷新'"
          @click="refreshPanel"
        >
          <span class="material-symbols-rounded">refresh</span>
        </button>
      </div>
    </header>

    <nav v-if="!relationshipOnly" class="ssp-tabs" aria-label="项目状态分类">
      <button
        v-for="tab in visibleTabs"
        :key="tab.id"
        class="ssp-tab"
        :class="{ active: activeTab === tab.id }"
        type="button"
        @click="activeTab = tab.id"
      >
        <span>{{ tab.label }}</span>
        <span v-if="tab.count > 0" class="ssp-tab-count">{{ tab.count }}</span>
      </button>
    </nav>

    <div class="ssp-body">
      <div v-if="errorMessage || wikiErrorMessage" class="ssp-empty">⚠ {{ errorMessage || wikiErrorMessage }}</div>

      <div v-else-if="relationshipOnly" class="ssp-wiki-workspace">
        <section class="ssp-wiki-toolbar">
          <nav class="ssp-wiki-category-tabs" role="tablist" aria-label="知识图谱分类">
            <button
              v-for="category in wikiCategoryTabs"
              :key="category.id"
              class="ssp-wiki-category-tab"
              :class="{ active: selectedWikiCategory === category.id }"
              type="button"
              role="tab"
              :aria-selected="selectedWikiCategory === category.id"
              @click="selectWikiCategory(category.id)"
            >
              <span class="material-symbols-rounded">{{ category.icon }}</span>
              <span class="ssp-wiki-category-name">{{ category.label }}</span>
              <small v-if="category.count > 0">{{ category.count }}</small>
            </button>
          </nav>
          <form class="ssp-wiki-search-form" @submit.prevent="submitWikiGraphSearch">
            <span class="material-symbols-rounded">search</span>
            <input
              v-model="wikiGraphSearchInput"
              class="ssp-wiki-search-input"
              type="search"
              placeholder="搜索条目、节点或关系"
            />
            <button
              v-if="wikiGraphSearchQuery || wikiGraphSearchInput"
              class="ssp-wiki-search-clear"
              type="button"
              title="清除搜索"
              @click="clearWikiGraphSearch"
            >
              <span class="material-symbols-rounded">close</span>
            </button>
          </form>
          <div class="ssp-wiki-toolbar-stats">
            <span>{{ wikiGraphNodes.length }} 节点 · {{ wikiGraphEdges.length }} 连接</span>
            <span v-if="wikiNeedsReviewCount > 0" class="ssp-wiki-review-alert">待确认 {{ wikiNeedsReviewCount }}</span>
          </div>
        </section>

        <div v-if="wikiAgentStatus" class="ssp-wiki-agent-status" :class="wikiAgentTone">
          {{ wikiAgentStatus }}
        </div>

        <main class="ssp-wiki-main">
          <section ref="wikiGraphPanelRef" class="ssp-wiki-graph-panel">
            <div v-if="wikiLoading || wikiGraphLoading" class="ssp-wiki-graph-empty">
              <span class="material-symbols-rounded is-spinning">progress_activity</span>
              <p>正在生成/读取知识图谱...</p>
            </div>
            <template v-else-if="wikiGraphNodes.length">
              <svg
                ref="wikiGraphSvgRef"
                class="ssp-wiki-graph"
                :viewBox="`0 0 ${wikiCanvasSize.width} ${wikiCanvasSize.height}`"
                role="img"
                aria-label="Storydex knowledge graph"
                @wheel.prevent="handleWikiGraphWheel"
                @pointerdown.self="beginWikiGraphPan"
                @pointermove="moveWikiGraphPointer"
                @pointerup="endWikiGraphPointer"
                @pointercancel="endWikiGraphPointer"
                @click.self="clearWikiGraphSelection"
              >
                <g :transform="`translate(${wikiGraphPan.x} ${wikiGraphPan.y}) scale(${wikiGraphZoom})`">
                  <path
                    v-for="edge in wikiGraphEdges"
                    :key="edge.id"
                    class="ssp-wiki-edge"
                    :class="[`edge-${edge.type}`, {
                      active: edge.active,
                      dimmed: isWikiEdgeDimmed(edge),
                      'edge-co-occurrence': edge.coOccurrence,
                      'edge-real-relation': edge.realRelation,
                    }]"
                    :d="edge.pathD"
                    @click.stop="selectWikiEdge(edge.id)"
                    @pointerenter="hoveredWikiEdgeId = edge.id"
                    @pointerleave="hoveredWikiEdgeId = ''"
                  />
                  <g
                    v-for="node in wikiGraphNodes"
                    :key="node.id"
                    class="ssp-wiki-node"
                    :class="[
                      `node-${node.type}`,
                      `tone-${node.tone}`,
                      {
                        active: isWikiNodeSelectable(node) && (selectedWikiNodeId === node.id || (!!node.entryId && selectedWikiEntry?.id === node.entryId)),
                        dimmed: isWikiNodeDimmed(node),
                        'is-neighbor': node.neighbor,
                        'is-synthetic': node.synthetic,
                        'is-selectable': isWikiNodeSelectable(node),
                        'needs-review': node.needsReview,
                      },
                    ]"
                    :transform="`translate(${node.x} ${node.y})`"
                    @pointerdown.stop="beginWikiNodeDrag($event, node)"
                    @pointerenter="hoveredWikiNodeId = node.id"
                    @pointerleave="hoveredWikiNodeId = ''"
                    @click.stop="selectWikiNode(node)"
                  >
                    <circle class="ssp-wiki-node-halo" :r="node.radius + 6" />
                    <circle class="ssp-wiki-node-dot" :r="node.radius" />
                    <text :y="node.radius + 14" text-anchor="middle">{{ node.shortLabel }}</text>
                    <title>{{ node.label }}</title>
                  </g>
                  <g
                    v-for="edge in visibleWikiGraphLabelEdges"
                    :key="`${edge.id}-label`"
                    class="ssp-wiki-edge-label"
                    :class="{ active: edge.active }"
                    :transform="`translate(${edge.labelX} ${edge.labelY})`"
                    @click.stop="selectWikiEdge(edge.id)"
                  >
                    <rect :x="-edge.labelWidth / 2" y="-9" :width="edge.labelWidth" height="18" rx="9" />
                    <text text-anchor="middle" dominant-baseline="middle">{{ edge.displayLabel }}</text>
                  </g>
                </g>
              </svg>
              <div v-if="wikiHiddenIsolatedNodeCount > 0" class="ssp-wiki-graph-note">
                另有 {{ wikiHiddenIsolatedNodeCount }} 个孤立条目，可通过搜索查看
              </div>
              <div v-if="wikiGraphLegend.length" class="ssp-wiki-graph-legend">
                <span v-for="item in wikiGraphLegend" :key="item.key" class="ssp-wiki-legend-item">
                  <i :class="`tone-${item.key}`"></i>{{ item.label }}
                </span>
              </div>
              <div class="ssp-wiki-graph-hud">
                <button class="ssp-hud-btn" type="button" title="缩小" @click="zoomWikiGraphStep(-1)">
                  <span class="material-symbols-rounded">remove</span>
                </button>
                <span class="ssp-hud-zoom">{{ Math.round(wikiGraphZoom * 100) }}%</span>
                <button class="ssp-hud-btn" type="button" title="放大" @click="zoomWikiGraphStep(1)">
                  <span class="material-symbols-rounded">add</span>
                </button>
                <button class="ssp-hud-btn" type="button" title="适配视图" @click="resetWikiGraphView">
                  <span class="material-symbols-rounded">fit_screen</span>
                </button>
              </div>
            </template>
            <div v-else class="ssp-wiki-graph-empty">
              <span class="material-symbols-rounded">hub</span>
              <p>暂无知识图谱数据</p>
              <small>点击右上角 ✦ 让 Agent 深度生成，或保存正文后自动同步。</small>
            </div>
          </section>

          <aside class="ssp-wiki-inspector">
            <header class="ssp-wiki-inspector-head">
              <div class="ssp-wiki-project">
                <span class="material-symbols-rounded">travel_explore</span>
                <strong>{{ wikiData?.projectName || "Storydex" }}</strong>
              </div>
              <div class="ssp-wiki-run-meta">
                <span>{{ wikiGenerationModeLabel }}</span>
                <span>{{ wikiUpdatedAtLabel }}</span>
              </div>
            </header>

            <div class="ssp-wiki-inspector-body">
              <article v-if="selectedWikiRelationEdge" class="ssp-wiki-inspector-detail">
                <div class="ssp-wiki-entry-kicker">{{ selectedWikiRelationEdge.coOccurrence ? "章节共现" : "角色关系" }}</div>
                <h3>{{ selectedWikiRelationEdge.label }}</h3>
                <div class="ssp-wiki-relation-endpoints">
                  <span>{{ selectedWikiRelationEdge.source.label }}</span>
                  <span class="material-symbols-rounded">sync_alt</span>
                  <span>{{ selectedWikiRelationEdge.target.label }}</span>
                </div>
                <div v-if="selectedWikiRelationEdge.level !== null" class="ssp-wiki-relation-level">
                  <span>强度 {{ selectedWikiRelationEdge.level }}</span>
                  <div class="ssp-level-bar">
                    <div
                      class="ssp-level-fill"
                      :style="{ width: levelWidth(selectedWikiRelationEdge.level), background: levelColor(selectedWikiRelationEdge.level) }"
                    ></div>
                  </div>
                </div>
                <p v-if="selectedWikiRelationEdge.coOccurrence">
                  此连线仅表示两角色在同一章节被共同提及，不代表既定关系。
                  <span v-if="selectedWikiRelationEdge.evidence" class="ssp-wiki-evidence-note">共现章节：{{ selectedWikiRelationEdge.evidence }}</span>
                </p>
                <p v-else>{{ selectedWikiRelationEdge.evidence || "该关系来自知识图谱，可在下方相关条目查看更多上下文。" }}</p>
              </article>

              <article v-else-if="selectedWikiNode || selectedWikiDetailEntry" class="ssp-wiki-inspector-detail">
                <div class="ssp-wiki-entry-kicker">{{ selectedWikiDetailKicker }}</div>
                <h3>{{ selectedWikiNode?.label || selectedWikiDetailEntry?.title }}</h3>
                <div
                  v-if="selectedWikiDetailEntry?.needsReview || selectedWikiSourceLabel"
                  class="ssp-wiki-entry-meta"
                >
                  <span v-if="selectedWikiDetailEntry?.needsReview" class="ssp-wiki-review-chip">需要人工确认</span>
                  <span v-if="selectedWikiSourceLabel" class="ssp-wiki-source-chip">{{ selectedWikiSourceLabel }}</span>
                </div>
                <p>{{ selectedWikiNode?.summary || selectedWikiDetailEntry?.summary || "暂无摘要。" }}</p>
                <ul v-if="selectedWikiDetailEntry?.details?.length">
                  <li v-for="(detail, index) in selectedWikiDetailEntry.details.slice(0, 18)" :key="index">{{ detail }}</li>
                </ul>
                <div v-if="selectedWikiDetailEntry?.sourcePaths?.length" class="ssp-wiki-sources">
                  <span v-for="source in selectedWikiDetailEntry.sourcePaths.slice(0, 8)" :key="source">{{ source }}</span>
                </div>
              </article>

              <article v-else class="ssp-wiki-inspector-detail is-empty">
                <div class="ssp-wiki-entry-kicker">{{ selectedWikiCategoryLabel }}</div>
                <h3>{{ wikiInspectorEmptyTitle }}</h3>
                <p>{{ wikiInspectorEmptyHint }}</p>
              </article>

              <section class="ssp-wiki-entry-list">
                <div class="ssp-wiki-inspector-heading">相关条目 · {{ visibleWikiEntries.length }}</div>
                <button
                  v-for="entry in visibleWikiEntries"
                  :key="entry.id"
                  class="ssp-wiki-entry-button"
                  :class="{ active: selectedWikiEntry?.id === entry.id }"
                  type="button"
                  @click="selectWikiEntry(entry.id)"
                >
                  <strong>{{ entry.title }}</strong>
                  <span>{{ entry.summary }}</span>
                  <small v-if="entry.needsReview" class="ssp-wiki-review-chip">需要人工确认</small>
                </button>
              </section>
            </div>
          </aside>
        </main>
      </div>

      <!-- 变更 ledger -->
      <div v-else-if="activeTab === 'changes'">
        <div v-if="changeEntries.length === 0" class="ssp-empty">暂无变更记录。</div>
        <ul v-else class="ssp-list">
          <li v-for="(entry, idx) in changeEntries" :key="idx" class="ssp-list-item">
            <div class="ssp-list-head">
              <span class="ssp-tag">{{ entry.chapter_id || '-' }} · {{ entry.segment_id || '-' }}</span>
              <span class="ssp-time">{{ formatTime(entry.at) }}</span>
            </div>
            <div v-if="entry.snapshot_comment" class="ssp-snapshot">{{ entry.snapshot_comment }}</div>
            <div class="ssp-sizes">
              <span v-for="(value, key) in entry.sizes" :key="key" v-show="value > 0" class="ssp-size-pill">
                {{ shortFieldLabel(String(key)) }}: {{ value }}
              </span>
            </div>
            <div v-if="entry.error_count" class="ssp-error">{{ entry.error_count }} 条 error</div>
            <div class="ssp-action-row">
              <button
                class="ssp-btn rollback"
                type="button"
                :disabled="rollingBack === idx || !entryHasSegmentPath(entry)"
                :title="entryHasSegmentPath(entry) ? '回档到该段之前（保留该段；后续段落与变量将备份后删除）' : '该条记录缺少可定位的 segment 路径'"
                @click="rollbackToEntry(entry, idx)"
              >
                <span class="material-symbols-rounded">undo</span>
                回档至此
              </button>
            </div>
          </li>
        </ul>
      </div>

      <!-- 章节大纲 -->
      <div v-else-if="activeTab === 'outline'">
        <div v-if="outlineChapters.length === 0" class="ssp-empty">暂无章节大纲。</div>
        <ul v-else class="ssp-list">
          <li v-for="chap in outlineChapters" :key="chap.chapter_id" class="ssp-list-item">
            <div class="ssp-list-head">
              <span class="ssp-tag">{{ chap.chapter_id }}</span>
              <span class="ssp-time">最近段: {{ chap.last_updated_in || '-' }}</span>
            </div>
            <div v-if="chap.arc_summary" class="ssp-snapshot">{{ chap.arc_summary }}</div>
            <div class="ssp-section-head">里程碑（{{ chap.milestones?.length || 0 }}）</div>
            <ol class="ssp-mini-list">
              <li v-for="(m, i) in (chap.milestones || []).slice(-5)" :key="i">
                <span class="ssp-mini-meta">[{{ m.segment_id }}]</span> {{ m.summary }}
              </li>
            </ol>
            <div v-if="chap.unresolved?.length" class="ssp-section-head">未解决（{{ chap.unresolved.length }}）</div>
            <ul v-if="chap.unresolved?.length" class="ssp-mini-list">
              <li v-for="(u, i) in chap.unresolved" :key="i">{{ u }}</li>
            </ul>
          </li>
        </ul>
      </div>

      <!-- 冲突裁定 -->
      <div v-else-if="activeTab === 'conflicts'">
        <div v-if="conflictEntries.length === 0" class="ssp-empty">暂无角色字段冲突。</div>
        <ul v-else class="ssp-list">
          <li
            v-for="(entry, idx) in conflictEntries"
            :key="idx"
            class="ssp-list-item"
            :class="{ resolved: entry.resolved }"
          >
            <div class="ssp-list-head">
              <span class="ssp-tag">{{ entry.character_name }} · {{ entry.field }}</span>
              <span class="ssp-time">{{ formatTime(entry.at) }}</span>
            </div>
            <div class="ssp-conflict-row">
              <div class="ssp-conflict-cell">
                <div class="ssp-cell-label">既有</div>
                <div class="ssp-cell-body">{{ entry.existing || '(空)' }}</div>
              </div>
              <div class="ssp-conflict-cell">
                <div class="ssp-cell-label">本轮入参</div>
                <div class="ssp-cell-body">{{ entry.incoming || '(空)' }}</div>
              </div>
            </div>
            <div v-if="entry.resolved" class="ssp-resolved-tag">
              已裁定: {{ entry.resolution }}{{ entry.applied ? ' (已写入角色卡)' : '' }}
            </div>
            <div v-else class="ssp-action-row">
              <button class="ssp-btn accept" type="button" :disabled="resolving === idx" @click="resolveConflict(idx, 'accept_incoming')">
                采用本轮
              </button>
              <button class="ssp-btn keep" type="button" :disabled="resolving === idx" @click="resolveConflict(idx, 'keep_existing')">
                保留既有
              </button>
              <button class="ssp-btn dismiss" type="button" :disabled="resolving === idx" @click="resolveConflict(idx, 'dismiss')">
                忽略
              </button>
            </div>
          </li>
        </ul>
      </div>

      <!-- 关系图 -->
      <div v-else-if="activeTab === 'relations'">
        <div v-if="hasRelationshipGraphContent" class="ssp-relation-inspector" data-testid="storydex-relationship-graph">
          <div class="ssp-relation-toolbar" aria-label="relationship graph tools">
            <span class="ssp-relation-mode">关系图谱</span>
            <div class="ssp-relation-controls">
              <button class="ssp-icon-btn" type="button" title="缩小" @click="zoomRelationshipGraph(-0.15)">
                <span class="material-symbols-rounded">zoom_out</span>
              </button>
              <span class="ssp-zoom-label">{{ Math.round(relationshipGraphZoom * 100) }}%</span>
              <button class="ssp-icon-btn" type="button" title="放大" @click="zoomRelationshipGraph(0.15)">
                <span class="material-symbols-rounded">zoom_in</span>
              </button>
              <button class="ssp-icon-btn" type="button" title="重置视图" @click="resetRelationshipGraphView">
                <span class="material-symbols-rounded">center_focus_strong</span>
              </button>
            </div>
          </div>
          <div class="ssp-relation-content">
            <svg
              ref="relationshipGraphSvgRef"
              class="ssp-relation-graph"
              viewBox="0 0 880 520"
              role="img"
              aria-label="Storydex relationship graph"
              @wheel.prevent="handleRelationshipGraphWheel"
              @pointerdown.self="beginRelationshipGraphPan"
              @pointermove="moveRelationshipGraphPointer"
              @pointerup="endRelationshipGraphPointer"
              @pointercancel="endRelationshipGraphPointer"
              @pointerleave="endRelationshipGraphPointer"
            >
              <g class="ssp-relation-viewport" :transform="`translate(${relationshipGraphPan.x} ${relationshipGraphPan.y}) scale(${relationshipGraphZoom})`">
                <path
                  v-for="edge in relationshipGraphEdges"
                  :key="`edge-${edge.index}`"
                  class="ssp-relation-edge"
                  :class="{ selected: !selectedRelationshipNode && selectedRelationshipIndex === edge.index }"
                  :d="edge.pathD"
                  @click.stop="selectRelationshipEdge(edge.index)"
                />
                <g
                  v-for="edge in relationshipGraphEdges"
                  :key="`label-${edge.index}`"
                  class="ssp-relation-edge-label"
                  :class="{ selected: !selectedRelationshipNode && selectedRelationshipIndex === edge.index }"
                  :transform="`translate(${edge.labelX} ${edge.labelY})`"
                  @click.stop="selectRelationshipEdge(edge.index)"
                >
                  <rect :x="-edge.labelWidth / 2" y="-13" :width="edge.labelWidth" height="24" rx="5" />
                  <text text-anchor="middle" dominant-baseline="middle">{{ edge.dimensionLabel }} · {{ edge.edge.current_level }}</text>
                </g>
                <g
                  v-for="node in relationshipGraphNodes"
                  :key="node.id"
                  class="ssp-relation-node"
                  :class="{ active: selectedRelationshipNode?.id === node.id || (!selectedRelationshipNode && selectedRelationshipEdge && (selectedRelationshipEdge.source === node.id || selectedRelationshipEdge.target === node.id)) }"
                  :transform="`translate(${node.x} ${node.y})`"
                  @pointerdown.stop="beginRelationshipNodeDrag($event, node.id)"
                  @click.stop="selectRelationshipNode(node.id)"
                >
                  <circle :r="node.radius" />
                  <text text-anchor="middle" dominant-baseline="middle">{{ node.shortLabel }}</text>
                  <title>{{ node.label }}</title>
                </g>
              </g>
            </svg>
            <aside class="ssp-relation-side-panel">
              <section v-if="selectedRelationshipNode" class="ssp-relation-evidence">
                <div class="ssp-side-title">角色信息</div>
                <div class="ssp-character-name">{{ selectedRelationshipNode.label }}</div>
                <dl class="ssp-side-fields">
                  <template v-for="item in selectedRelationshipNodeInfo" :key="item.label">
                    <dt>{{ item.label }}</dt>
                    <dd>{{ item.value }}</dd>
                  </template>
                </dl>
              </section>
              <section v-else-if="selectedRelationshipEdge" class="ssp-relation-evidence">
                <div class="ssp-side-title">关系信息</div>
                <div class="ssp-list-head">
                  <span class="ssp-tag">{{ selectedRelationshipEdge.source }} → {{ selectedRelationshipEdge.target }}</span>
                </div>
                <div class="ssp-side-kv">
                  <span>关系</span>
                  <strong>{{ relationshipDimensionLabel(selectedRelationshipEdge) }}</strong>
                </div>
                <div class="ssp-side-kv">
                  <span>强度</span>
                  <strong>{{ selectedRelationshipEdge.current_level }}</strong>
                </div>
                <div class="ssp-level-bar">
                  <div
                    class="ssp-level-fill"
                    :style="{ width: levelWidth(selectedRelationshipEdge.current_level), background: levelColor(selectedRelationshipEdge.current_level) }"
                  ></div>
                </div>
                <div v-if="selectedRelationshipLatestHistory" class="ssp-mini-meta">
                  最近 [{{ selectedRelationshipLatestHistory.segment_id }}]
                  {{ selectedRelationshipLatestHistory.delta }}/{{ selectedRelationshipLatestHistory.magnitude }}
                  — {{ selectedRelationshipLatestHistory.detail }}
                </div>
              </section>
            </aside>
          </div>
        </div>
        <div v-if="!hasRelationshipGraphContent" class="ssp-empty">暂无关系记录。</div>
        <div v-else-if="relationshipEdges.length === 0" class="ssp-empty">已有角色，暂无关系边。</div>
        <ul v-else class="ssp-list">
          <li v-for="(edge, idx) in relationshipEdges" :key="idx" class="ssp-list-item">
            <div class="ssp-list-head">
              <span class="ssp-tag">{{ edge.source }} → {{ edge.target }}</span>
              <span class="ssp-time">{{ edge.dimension }} · level {{ edge.current_level }}</span>
            </div>
            <div class="ssp-level-bar">
              <div class="ssp-level-fill" :style="{ width: levelWidth(edge.current_level), background: levelColor(edge.current_level) }"></div>
            </div>
            <div v-if="edge.history?.length" class="ssp-mini-meta">
              最近: [{{ edge.history[edge.history.length - 1].segment_id }}]
              {{ edge.history[edge.history.length - 1].delta }}/{{ edge.history[edge.history.length - 1].magnitude }}
              — {{ edge.history[edge.history.length - 1].detail }}
            </div>
          </li>
        </ul>
      </div>

      <!-- 伏笔台账 -->
      <div v-else-if="activeTab === 'foreshadow'">
        <div v-if="foreshadowThreads.length === 0" class="ssp-empty">暂无伏笔记录。</div>
        <ul v-else class="ssp-list">
          <li v-for="thread in foreshadowThreads" :key="thread.id" class="ssp-list-item">
            <div class="ssp-list-head">
              <span class="ssp-tag" :class="`status-${thread.status}`">{{ thread.id }}</span>
              <span class="ssp-time">{{ thread.status }}{{ thread.id_collisions ? ` · 碰撞 ${thread.id_collisions}` : '' }}</span>
            </div>
            <div v-if="thread.planted_at" class="ssp-mini-meta">
              <strong>plant</strong> [{{ thread.planted_at.segment_id }}]: {{ thread.planted_at.summary }}
            </div>
            <div v-if="thread.callbacks?.length" class="ssp-mini-meta">
              <strong>callbacks</strong>: {{ thread.callbacks.length }} 次（最近 [{{ thread.callbacks[thread.callbacks.length - 1].segment_id }}]）
            </div>
            <div v-if="thread.resolved_at" class="ssp-mini-meta resolved">
              <strong>resolve</strong> [{{ thread.resolved_at.segment_id }}]: {{ thread.resolved_at.summary }}
            </div>
          </li>
        </ul>
      </div>

      <!-- 时间线 -->
      <div v-else-if="activeTab === 'timeline'">
        <div v-if="timelineEntries.length === 0" class="ssp-empty">暂无时间线记录。</div>
        <ul v-else class="ssp-list">
          <li v-for="(entry, idx) in timelineEntries" :key="idx" class="ssp-list-item">
            <div class="ssp-list-head">
              <span class="ssp-tag">{{ entry.chapter_id || '-' }} · {{ entry.segment_id || '-' }}</span>
              <span class="ssp-time">{{ formatTime(entry.at) }}</span>
            </div>
            <div v-if="entry.advance" class="ssp-snapshot">advance: {{ entry.advance }}</div>
            <div v-if="entry.anchor" class="ssp-mini-meta">anchor: {{ entry.anchor }}</div>
          </li>
        </ul>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { apiClient, describeTransportError, unwrapEnvelope } from "@/api/client";
import { useStoryStore } from "@/stores/story";
import { useWorkspaceStore } from "@/stores/workspace";
import type { ApiEnvelope } from "@/types/api";
import { computeForceLayout, type ForceEdge, type ForceNode } from "@/utils/forceLayout";

type StoryStateTab = "changes" | "outline" | "conflicts" | "relations" | "foreshadow" | "timeline";

const props = withDefaults(defineProps<{
  initialTab?: StoryStateTab;
  relationshipOnly?: boolean;
  expanded?: boolean;
}>(), {
  initialTab: "changes",
  relationshipOnly: false,
  expanded: false,
});

const storyStore = useStoryStore();
const workspaceStore = useWorkspaceStore();

interface ChangeEntry {
  chapter_id?: string;
  segment_id?: string;
  at?: string;
  written?: string[];
  error_count?: number;
  sizes?: Record<string, number>;
  snapshot_comment?: string;
}

interface OutlineChapter {
  chapter_id: string;
  milestones?: Array<{ segment_id: string; summary: string; evidence?: string; at?: string }>;
  unresolved?: string[];
  arc_summary?: string;
  last_updated_in?: string;
  last_updated_at?: string;
}

interface ConflictEntry {
  character_id: string;
  character_name: string;
  field: string;
  existing: string;
  incoming: string;
  segment_id?: string;
  at?: string;
  resolved?: boolean;
  resolution?: string;
  applied?: boolean;
}

interface RelationshipEdge {
  source: string;
  target: string;
  dimension: string;
  current_level: number;
  history?: Array<{ delta: string; magnitude: string; detail: string; segment_id: string; at: string }>;
  last_updated_in?: string;
  last_updated_at?: string;
}

interface RelationshipGraphNodePayload {
  id?: string;
  label?: string;
  name?: string;
  source?: string;
  kind?: string;
  characterId?: string;
}

interface RelationshipGraphNode {
  id: string;
  label: string;
  x: number;
  y: number;
  radius: number;
  shortLabel: string;
  source?: string;
  kind?: string;
  characterId?: string;
}

interface RelationshipGraphEdge {
  index: number;
  edge: RelationshipEdge;
  source: RelationshipGraphNode;
  target: RelationshipGraphNode;
  pathD: string;
  labelX: number;
  labelY: number;
  labelWidth: number;
  dimensionLabel: string;
}

interface ForeshadowThread {
  id: string;
  status: "open" | "recalled" | "resolved";
  planted_at?: { segment_id: string; at: string; summary: string; evidence?: string };
  callbacks?: Array<{ segment_id: string; at: string; summary: string; evidence?: string; kind?: string }>;
  resolved_at?: { segment_id: string; at: string; summary: string; evidence?: string };
  id_collisions?: number;
}

interface TimelineEntry {
  chapter_id?: string;
  segment_id?: string;
  advance?: string;
  anchor?: string;
  evidence?: string;
  at?: string;
}

interface EvolutionSnapshot {
  currentDir?: string;
  changeLedger?: { entries?: ChangeEntry[] };
  relationshipGraph?: { nodes?: RelationshipGraphNodePayload[]; edges?: RelationshipEdge[] };
  foreshadowLedger?: { threads?: Record<string, ForeshadowThread> };
  chapterOutline?: { chapters?: Record<string, OutlineChapter> };
  characterConflicts?: { entries?: ConflictEntry[] };
  timeline?: { entries?: TimelineEntry[] };
}

interface StoryWikiEntry {
  id: string;
  title: string;
  category: StoryWikiCategory;
  categoryLabel: string;
  summary: string;
  details?: string[];
  sourcePaths?: string[];
  confidence?: number;
  needsReview?: boolean;
  updatedAt?: string;
}

type StoryWikiSyntheticRole = "categoryHub" | "projectHub";
type StoryWikiCategory = "overview" | "characters" | "setting" | "plot" | "relationships";

interface StoryWikiNodePayload {
  id: string;
  label: string;
  type: string;
  category?: string;
  entryId?: string;
  summary?: string;
  synthetic?: boolean;
  role?: StoryWikiSyntheticRole;
  selectable?: boolean;
  neighbor?: boolean;
  count?: number;
  needsReviewCount?: number;
  needsReview?: boolean;
}

interface StoryWikiEdgePayload {
  source: string;
  target: string;
  label: string;
  type: string;
  weight?: number;
  evidence?: string;
  synthetic?: boolean;
  coOccurrence?: boolean;
  level?: number;
  dimension?: string;
}

interface StoryWikiData {
  projectName: string;
  generatedAt: string;
  generator: string;
  generationMode?: string;
  lastUpdatedAt?: string;
  lastWorkflow?: string;
  lastWorkflowStatus?: string;
  changedSourcePaths?: string[];
  agent?: {
    attempted?: boolean;
    completed?: boolean;
    traceId?: string;
    errorMessage?: string;
    eventCount?: number;
  };
  summary: string;
  categoryLabels?: Record<string, string>;
  nodeTypeLabels?: Record<string, string>;
  entries: StoryWikiEntry[];
  graph: {
    nodes: StoryWikiNodePayload[];
    edges: StoryWikiEdgePayload[];
  };
}

interface StoryWikiGraphQueryResponse {
  mode: "category" | "search" | "entry" | "node" | "overview";
  query: string;
  category: string;
  entryId: string;
  nodeId: string;
  depth: number;
  limit: number;
  entries: StoryWikiEntry[];
  graph: {
    nodes: StoryWikiNodePayload[];
    edges: StoryWikiEdgePayload[];
  };
  matchedEntryIds: string[];
  total: {
    entryCount: number;
    nodeCount: number;
    edgeCount: number;
  };
}

interface StoryWikiAgentWorkflowResponse {
  ok: boolean;
  workflow: string;
  status: string;
  traceId: string;
  agentAttempted: boolean;
  agentCompleted: boolean;
  fallbackUsed: boolean;
  summary: string;
  changedSourcePaths: string[];
  writtenPaths: string[];
  errorMessage?: string;
  wiki: StoryWikiData;
  review?: Record<string, unknown>;
}

interface WikiGraphNode {
  id: string;
  label: string;
  type: string;
  category: string;
  entryId: string;
  summary: string;
  synthetic: boolean;
  role: StoryWikiSyntheticRole | "";
  selectable: boolean;
  neighbor: boolean;
  degree: number;
  tone: string;
  count: number;
  needsReviewCount: number;
  needsReview: boolean;
  x: number;
  y: number;
  radius: number;
  shortLabel: string;
}

interface WikiGraphEdge {
  id: string;
  label: string;
  displayLabel: string;
  type: string;
  evidence: string;
  synthetic: boolean;
  active: boolean;
  coOccurrence: boolean;
  realRelation: boolean;
  level: number | null;
  dimension: string;
  source: WikiGraphNode;
  target: WikiGraphNode;
  pathD: string;
  labelX: number;
  labelY: number;
  labelWidth: number;
}

const loading = ref(false);
const errorMessage = ref("");
const snapshot = ref<EvolutionSnapshot>({});
const wikiLoading = ref(false);
const wikiRebuilding = ref(false);
const wikiAgentRunning = ref(false);
const wikiAgentStatus = ref("");
const wikiAgentTone = ref<"idle" | "success" | "warning" | "error">("idle");
const wikiErrorMessage = ref("");
const wikiData = ref<StoryWikiData | null>(null);
const wikiGraphQueryData = ref<StoryWikiGraphQueryResponse | null>(null);
const wikiGraphLoading = ref(false);
const wikiGraphSearchInput = ref("");
const wikiGraphSearchQuery = ref("");
const selectedWikiCategory = ref<StoryWikiCategory>("relationships");
const selectedWikiEntryId = ref("");
const selectedWikiNodeId = ref("");
const selectedWikiEdgeId = ref("");
const hoveredWikiNodeId = ref("");
const hoveredWikiEdgeId = ref("");
const wikiGraphZoom = ref(1);
const wikiGraphPan = ref({ x: 0, y: 0 });
const wikiGraphSvgRef = ref<SVGSVGElement | null>(null);
const wikiGraphPanelRef = ref<HTMLElement | null>(null);
const wikiCanvasSize = ref({ width: 960, height: 600 });
const wikiLayoutPositions = ref<Record<string, { x: number; y: number }>>({});
const wikiNodeOverrides = ref<Record<string, { x: number; y: number }>>({});
const wikiGraphDragState = ref<{ nodeId: string; pointerId: number; offsetX: number; offsetY: number } | null>(null);
const wikiGraphPanState = ref<{
  pointerId: number;
  startX: number;
  startY: number;
  originX: number;
  originY: number;
} | null>(null);
const activeTab = ref<StoryStateTab>(props.relationshipOnly ? "relations" : props.initialTab);
const resolving = ref<number | null>(null);
const rollingBack = ref<number | null>(null);
const lastRollbackId = ref("");
const relationshipGraphSvgRef = ref<SVGSVGElement | null>(null);
const relationshipGraphZoom = ref(1);
const relationshipGraphPan = ref({ x: 0, y: 0 });
const selectedRelationshipIndex = ref(0);
const selectedRelationshipNodeId = ref("");
const relationshipNodeOverrides = ref<Record<string, { x: number; y: number }>>({});
const relationshipDragState = ref<{
  nodeId: string;
  pointerId: number;
  offsetX: number;
  offsetY: number;
} | null>(null);
const relationshipPanState = ref<{
  pointerId: number;
  startX: number;
  startY: number;
  originX: number;
  originY: number;
} | null>(null);
let snapshotLoadSeq = 0;
let wikiGraphLoadSeq = 0;

const RELATION_GRAPH_WIDTH = 880;
const RELATION_GRAPH_HEIGHT = 520;
const RELATION_GRAPH_CENTER_X = RELATION_GRAPH_WIDTH / 2;
const RELATION_GRAPH_CENTER_Y = RELATION_GRAPH_HEIGHT / 2;
const WIKI_CATEGORY_TABS: Array<{ id: StoryWikiCategory; label: string; icon: string }> = [
  { id: "relationships", label: "关系", icon: "hub" },
  { id: "characters", label: "角色", icon: "groups" },
  { id: "plot", label: "剧情", icon: "timeline" },
  { id: "setting", label: "设定", icon: "auto_stories" },
  { id: "overview", label: "总览", icon: "dashboard" },
];
// 节点色组：type -> 图例色调。granular type 折叠成四个视觉组，避免五颜六色。
const WIKI_NODE_TONES: Record<string, string> = {
  character: "character",
  chapter: "plot",
  event: "plot",
  timeline: "plot",
  world: "setting",
  setting: "setting",
  item: "setting",
  location: "setting",
  faction: "setting",
  foreshadow: "setting",
  project: "hub",
  categoryHub: "hub",
};
const WIKI_TONE_LABELS: Record<string, string> = {
  character: "角色",
  plot: "剧情",
  setting: "设定",
  hub: "导航",
  misc: "其他",
};
const WIKI_ISOLATED_NODE_VISIBLE_LIMIT = 8;

const panelLoading = computed(() => (
  props.relationshipOnly ? wikiLoading.value || wikiRebuilding.value || wikiAgentRunning.value : loading.value
));

const changeEntries = computed<ChangeEntry[]>(() => {
  const entries = snapshot.value.changeLedger?.entries ?? [];
  return [...entries].reverse().slice(0, 30);
});

const outlineChapters = computed<OutlineChapter[]>(() => {
  const chapters = snapshot.value.chapterOutline?.chapters ?? {};
  return Object.values(chapters);
});

const conflictEntries = computed<ConflictEntry[]>(() => {
  return snapshot.value.characterConflicts?.entries ?? [];
});

const relationshipEdges = computed<RelationshipEdge[]>(() => {
  return snapshot.value.relationshipGraph?.edges ?? [];
});

const relationshipGraphNodes = computed<RelationshipGraphNode[]>(() => {
  const ids = new Set<string>();
  const payloadById = new Map<string, RelationshipGraphNodePayload>();
  (snapshot.value.relationshipGraph?.nodes ?? []).forEach((node) => {
    const nodeId = relationshipNodePayloadId(node);
    if (!nodeId) {
      return;
    }
    ids.add(nodeId);
    payloadById.set(nodeId, node);
  });
  relationshipEdges.value.forEach((edge) => {
    if (edge.source) ids.add(edge.source);
    if (edge.target) ids.add(edge.target);
  });
  const names = Array.from(ids);
  const count = Math.max(1, names.length);
  const radiusX = count <= 2 ? 260 : 315;
  const radiusY = count <= 2 ? 0 : 180;
  return names.map((id, index) => {
    const angle = count === 1 ? -Math.PI / 2 : (Math.PI * 2 * index) / count - Math.PI / 2;
    const fallback = {
      x: RELATION_GRAPH_CENTER_X + Math.cos(angle) * radiusX,
      y: RELATION_GRAPH_CENTER_Y + Math.sin(angle) * radiusY,
    };
    if (count === 2) {
      fallback.y = RELATION_GRAPH_CENTER_Y;
    }
    const payload = payloadById.get(id);
    const label = relationshipNodePayloadLabel(payload, id);
    const override = relationshipNodeOverrides.value[id];
    return {
      id,
      label,
      x: override?.x ?? fallback.x,
      y: override?.y ?? fallback.y,
      radius: Math.max(34, Math.min(54, 28 + label.length * 2)),
      shortLabel: shortNodeLabel(label),
      source: payload?.source,
      kind: payload?.kind,
      characterId: payload?.characterId,
    };
  });
});

const relationshipGraphEdges = computed<RelationshipGraphEdge[]>(() => {
  const nodeById = new Map(relationshipGraphNodes.value.map((node) => [node.id, node]));
  return relationshipEdges.value.flatMap((edge, index) => {
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    if (!source || !target) {
      return [];
    }
    const dimensionLabel = relationshipDimensionLabel(edge);
    return [
      {
        index,
        edge,
        source,
        target,
        pathD: buildRelationshipEdgePath(source, target, index),
        labelX: buildRelationshipEdgeLabelPoint(source, target, index).x,
        labelY: buildRelationshipEdgeLabelPoint(source, target, index).y - 14,
        labelWidth: Math.max(76, Math.min(150, `${dimensionLabel} ${edge.current_level}`.length * 12 + 34)),
        dimensionLabel,
      },
    ];
  });
});

const hasRelationshipGraphContent = computed(() => relationshipGraphNodes.value.length > 0 || relationshipEdges.value.length > 0);

const selectedRelationshipEdge = computed<RelationshipEdge | null>(() => {
  if (relationshipEdges.value.length === 0) {
    return null;
  }
  return relationshipEdges.value[selectedRelationshipIndex.value] ?? relationshipEdges.value[0] ?? null;
});

const selectedRelationshipNode = computed<RelationshipGraphNode | null>(() => {
  if (!selectedRelationshipNodeId.value) {
    return null;
  }
  return relationshipGraphNodes.value.find((node) => node.id === selectedRelationshipNodeId.value) ?? null;
});

const selectedRelationshipNodeInfo = computed<Array<{ label: string; value: string }>>(() => {
  const node = selectedRelationshipNode.value;
  if (!node) {
    return [];
  }
  return [
    { label: "节点 ID", value: node.id },
    { label: "类型", value: formatRelationshipNodeKind(node.kind) },
    { label: "来源", value: node.source || "关系图快照" },
    ...(node.characterId && node.characterId !== node.id ? [{ label: "角色 ID", value: node.characterId }] : []),
  ];
});

const selectedRelationshipLatestHistory = computed(() => {
  const history = selectedRelationshipEdge.value?.history ?? [];
  return history[history.length - 1] ?? null;
});

const foreshadowThreads = computed<ForeshadowThread[]>(() => {
  const threads = snapshot.value.foreshadowLedger?.threads ?? {};
  return Object.values(threads);
});

const timelineEntries = computed<TimelineEntry[]>(() => {
  const entries = snapshot.value.timeline?.entries ?? [];
  return [...entries].reverse().slice(0, 30);
});

const tabs = computed(() => [
  { id: "changes" as const, label: "变更", count: changeEntries.value.length },
  { id: "outline" as const, label: "大纲", count: outlineChapters.value.length },
  { id: "conflicts" as const, label: "冲突", count: conflictEntries.value.filter((e) => !e.resolved).length },
  { id: "relations" as const, label: "关系", count: relationshipGraphNodes.value.length || relationshipEdges.value.length },
  { id: "foreshadow" as const, label: "伏笔", count: foreshadowThreads.value.length },
  { id: "timeline" as const, label: "时间", count: timelineEntries.value.length },
]);

const visibleTabs = computed(() => (
  props.relationshipOnly ? tabs.value.filter((tab) => tab.id === "relations") : tabs.value
));

const wikiEntries = computed<StoryWikiEntry[]>(() => wikiData.value?.entries ?? []);

const wikiCategoryTabs = computed(() => {
  const counts = new Map<StoryWikiCategory, number>();
  wikiEntries.value.forEach((entry) => {
    const category = normalizeWikiCategory(entry.category);
    counts.set(category, (counts.get(category) ?? 0) + 1);
  });
  const labels = wikiData.value?.categoryLabels ?? {};
  return WIKI_CATEGORY_TABS.map((tab) => ({
    ...tab,
    label: labels[tab.id] || tab.label,
    count: counts.get(tab.id) ?? 0,
  }));
});

const selectedWikiCategoryEntries = computed(() => {
  const category = selectedWikiCategory.value;
  return wikiEntries.value.filter((entry) => entry.category === category);
});

const visibleWikiEntries = computed(() => {
  return wikiGraphQueryData.value?.entries ?? selectedWikiCategoryEntries.value;
});

const selectedWikiEntry = computed<StoryWikiEntry | null>(() => {
  if (!wikiEntries.value.length) {
    return null;
  }
  return (
    wikiEntries.value.find((entry) => entry.id === selectedWikiEntryId.value) ??
    visibleWikiEntries.value[0] ??
    wikiEntries.value[0] ??
    null
  );
});

const selectedWikiCategoryLabel = computed(() => (
  wikiCategoryTabs.value.find((category) => category.id === selectedWikiCategory.value)?.label || "关系"
));

const selectedWikiNode = computed<WikiGraphNode | null>(() => {
  if (!selectedWikiNodeId.value) {
    return null;
  }
  return wikiGraphNodes.value.find((node) => node.id === selectedWikiNodeId.value) ?? null;
});

const selectedWikiRelationEdge = computed<WikiGraphEdge | null>(() => {
  if (!selectedWikiEdgeId.value) {
    return null;
  }
  return wikiGraphEdges.value.find((edge) => edge.id === selectedWikiEdgeId.value) ?? null;
});

// 节点连接度：驱动节点大小与布局向心力，主干角色一眼可辨。
const wikiGraphDegrees = computed<Map<string, number>>(() => {
  const degrees = new Map<string, number>();
  (wikiGraphQueryData.value?.graph?.edges ?? []).forEach((edge) => {
    degrees.set(edge.source, (degrees.get(edge.source) ?? 0) + 1);
    degrees.set(edge.target, (degrees.get(edge.target) ?? 0) + 1);
  });
  return degrees;
});

const wikiIsolatedRawGraphNodes = computed<StoryWikiNodePayload[]>(() => {
  const rawNodes = wikiGraphQueryData.value?.graph?.nodes ?? [];
  const degrees = wikiGraphDegrees.value;
  return rawNodes.filter((node) => !node.synthetic && (degrees.get(node.id) ?? 0) === 0);
});

const wikiShouldLimitIsolatedNodes = computed(() => (
  wikiGraphQueryData.value?.mode === "category"
  && wikiIsolatedRawGraphNodes.value.length > WIKI_ISOLATED_NODE_VISIBLE_LIMIT
));

const wikiHiddenIsolatedNodeCount = computed(() => (
  wikiShouldLimitIsolatedNodes.value
    ? wikiIsolatedRawGraphNodes.value.length - WIKI_ISOLATED_NODE_VISIBLE_LIMIT
    : 0
));

const wikiVisibleRawGraphNodes = computed<StoryWikiNodePayload[]>(() => {
  const rawNodes = wikiGraphQueryData.value?.graph?.nodes ?? [];
  if (!wikiShouldLimitIsolatedNodes.value) {
    return rawNodes;
  }
  const hiddenIds = new Set(
    wikiIsolatedRawGraphNodes.value
      .slice(WIKI_ISOLATED_NODE_VISIBLE_LIMIT)
      .map((node) => node.id),
  );
  return rawNodes.filter((node) => !hiddenIds.has(node.id));
});

function wikiNodeRadius(node: StoryWikiNodePayload, degree: number): number {
  if (node.synthetic) {
    return node.role === "projectHub" ? 20 : 15;
  }
  const base = node.neighbor ? 5.5 : 8;
  return base + Math.min(10, degree * 1.3);
}

function wikiNodeLabel(value: string): string {
  const trimmed = String(value || "").trim();
  return trimmed.length <= 12 ? trimmed : `${trimmed.slice(0, 11)}…`;
}

function recomputeWikiLayout(): void {
  const rawNodes = wikiVisibleRawGraphNodes.value;
  if (!rawNodes.length) {
    wikiLayoutPositions.value = {};
    return;
  }
  const degrees = wikiGraphDegrees.value;
  const previous = wikiLayoutPositions.value;
  const forceNodes: ForceNode[] = rawNodes.map((node) => {
    const seed = previous[node.id];
    return {
      id: node.id,
      // 半径额外加 16px 当作下方标签的保留空间，避免标签互相压盖。
      radius: wikiNodeRadius(node, degrees.get(node.id) ?? 0) + 16,
      x: seed?.x,
      y: seed?.y,
    };
  });
  const forceEdges: ForceEdge[] = (wikiGraphQueryData.value?.graph?.edges ?? []).map((edge) => ({
    source: edge.source,
    target: edge.target,
    weight: typeof edge.weight === "number" ? edge.weight : 1,
  }));
  wikiLayoutPositions.value = computeForceLayout(forceNodes, forceEdges, {
    width: wikiCanvasSize.value.width,
    height: wikiCanvasSize.value.height,
    padding: 36,
  });
}

const wikiGraphNodes = computed<WikiGraphNode[]>(() => {
  const rawNodes = wikiVisibleRawGraphNodes.value;
  const layout = wikiLayoutPositions.value;
  const overrides = wikiNodeOverrides.value;
  const degrees = wikiGraphDegrees.value;
  const centerX = wikiCanvasSize.value.width / 2;
  const centerY = wikiCanvasSize.value.height / 2;
  const spread = Math.max(80, Math.min(centerX, centerY) * 0.62);
  return rawNodes.map((node, index) => {
    const label = normalizeRelationshipGraphText(node.label || node.id);
    const degree = degrees.get(node.id) ?? 0;
    const angle = (Math.PI * 2 * index) / Math.max(1, rawNodes.length) - Math.PI / 2;
    const fallback = {
      x: centerX + Math.cos(angle) * spread,
      y: centerY + Math.sin(angle) * spread,
    };
    const position = overrides[node.id] ?? layout[node.id] ?? fallback;
    const type = normalizeRelationshipGraphText(node.type || "project").replace(/\s+/g, "-");
    return {
      id: node.id,
      label,
      type,
      category: node.category || "",
      entryId: node.entryId || "",
      summary: node.summary || "",
      synthetic: Boolean(node.synthetic),
      role: node.role || "",
      selectable: node.selectable !== false && !node.synthetic,
      neighbor: Boolean(node.neighbor),
      degree,
      tone: WIKI_NODE_TONES[type] ?? (node.synthetic ? "hub" : "misc"),
      count: typeof node.count === "number" ? node.count : 0,
      needsReviewCount: typeof node.needsReviewCount === "number" ? node.needsReviewCount : 0,
      needsReview: Boolean(node.needsReview),
      x: position.x,
      y: position.y,
      radius: wikiNodeRadius(node, degree),
      shortLabel: wikiNodeLabel(label),
    };
  });
});

const wikiGraphEdges = computed<WikiGraphEdge[]>(() => {
  const nodeById = new Map(wikiGraphNodes.value.map((node) => [node.id, node]));
  const rawEdges = wikiGraphQueryData.value?.graph?.edges ?? [];
  // 同一对节点的多条边（多维度关系）依次向两侧弯出，首条保持直线。
  const pairSeen = new Map<string, number>();
  return rawEdges.flatMap((edge, index) => {
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    if (!source || !target) {
      return [];
    }
    const pairKey = [edge.source, edge.target].sort().join("→");
    const parallelIndex = pairSeen.get(pairKey) ?? 0;
    pairSeen.set(pairKey, parallelIndex + 1);
    const label = normalizeRelationshipGraphText(edge.label || edge.type || "关联");
    const level = typeof edge.level === "number" ? edge.level : null;
    const displayLabel = level !== null ? `${label} ${level > 0 ? "+" : ""}${level}` : label;
    const dx = target.x - source.x;
    const dy = target.y - source.y;
    const distance = Math.hypot(dx, dy) || 1;
    let pathD: string;
    let labelX: number;
    let labelY: number;
    if (parallelIndex === 0) {
      pathD = `M ${source.x} ${source.y} L ${target.x} ${target.y}`;
      labelX = (source.x + target.x) / 2;
      labelY = (source.y + target.y) / 2;
    } else {
      const side = parallelIndex % 2 === 1 ? 1 : -1;
      const magnitude = Math.ceil(parallelIndex / 2) * clampNumber(distance * 0.16, 14, 42);
      const controlX = (source.x + target.x) / 2 + (-dy / distance) * side * magnitude;
      const controlY = (source.y + target.y) / 2 + (dx / distance) * side * magnitude;
      pathD = `M ${source.x} ${source.y} Q ${controlX} ${controlY} ${target.x} ${target.y}`;
      labelX = (source.x + 2 * controlX + target.x) / 4;
      labelY = (source.y + 2 * controlY + target.y) / 4;
    }
    const id = `${edge.source}-${edge.target}-${index}`;
    const coOccurrence = Boolean(edge.coOccurrence);
    return [{
      id,
      label,
      displayLabel,
      type: normalizeRelationshipGraphText(edge.type || "related").replace(/\s+/g, "-"),
      evidence: normalizeRelationshipGraphText(edge.evidence || ""),
      synthetic: Boolean(edge.synthetic),
      active: isWikiEdgeActive({ id, source, target }),
      coOccurrence,
      realRelation: edge.type === "relationship" && !coOccurrence,
      level,
      dimension: edge.dimension || "",
      source,
      target,
      pathD,
      labelX,
      labelY: labelY - 4,
      labelWidth: Math.max(40, Math.min(128, displayLabel.length * 11 + 18)),
    }];
  });
});

// 焦点态：hover/选中节点时只保留其邻域，其余淡出，密图也能一眼看清。
const wikiGraphFocusCenterId = computed(() => hoveredWikiNodeId.value || selectedWikiNodeId.value);

const wikiGraphFocusIds = computed<Set<string> | null>(() => {
  const centerId = wikiGraphFocusCenterId.value;
  if (centerId) {
    const ids = new Set<string>([centerId]);
    (wikiGraphQueryData.value?.graph?.edges ?? []).forEach((edge) => {
      if (edge.source === centerId) {
        ids.add(edge.target);
      }
      if (edge.target === centerId) {
        ids.add(edge.source);
      }
    });
    return ids;
  }
  if (selectedWikiEdgeId.value) {
    const edge = wikiGraphEdges.value.find((item) => item.id === selectedWikiEdgeId.value);
    if (edge) {
      return new Set([edge.source.id, edge.target.id]);
    }
  }
  return null;
});

function isWikiNodeDimmed(node: WikiGraphNode): boolean {
  const focus = wikiGraphFocusIds.value;
  return focus !== null && !focus.has(node.id);
}

function isWikiEdgeDimmed(edge: WikiGraphEdge): boolean {
  const focus = wikiGraphFocusIds.value;
  if (focus === null) {
    return false;
  }
  const centerId = wikiGraphFocusCenterId.value;
  if (centerId) {
    return edge.source.id !== centerId && edge.target.id !== centerId;
  }
  return !(focus.has(edge.source.id) && focus.has(edge.target.id));
}

// 边标签默认克制：真实关系边常显（关系名即核心信息），其余仅在 hover/选中时出现。
const visibleWikiGraphLabelEdges = computed(() => (
  wikiGraphEdges.value.filter((edge) => (
    edge.active
    || hoveredWikiEdgeId.value === edge.id
    || (edge.realRelation && !isWikiEdgeDimmed(edge))
  ))
));

const wikiGraphLegend = computed(() => {
  const present = new Set(wikiGraphNodes.value.map((node) => node.tone));
  return ["character", "plot", "setting", "hub", "misc"]
    .filter((key) => present.has(key))
    .map((key) => ({ key, label: WIKI_TONE_LABELS[key] ?? key }));
});

const selectedWikiDetailEntry = computed<StoryWikiEntry | null>(() => {
  const node = selectedWikiNode.value;
  if (!node) {
    return selectedWikiEntry.value;
  }
  if (!node.entryId) {
    return null;
  }
  return wikiEntries.value.find((entry) => entry.id === node.entryId) ?? null;
});

const selectedWikiDetailKicker = computed(() => {
  const node = selectedWikiNode.value;
  if (node) {
    const labels = wikiData.value?.nodeTypeLabels ?? {};
    return labels[node.type] || node.type;
  }
  return selectedWikiEntry.value?.categoryLabel || "条目";
});

const wikiInspectorEmptyTitle = computed(() => (
  selectedWikiCategory.value === "relationships" ? "角色关系网络" : `${selectedWikiCategoryLabel.value}图谱`
));

const wikiInspectorEmptyHint = computed(() => {
  if (wikiGraphSearchQuery.value) {
    return `正在展示“${wikiGraphSearchQuery.value}”的搜索结果，点击节点或连线查看详情。`;
  }
  if (selectedWikiCategory.value === "relationships") {
    return "实线是写作过程中沉淀的真实关系，虚线表示章节共现。点击节点聚焦它的邻居，点击连线查看关系详情。";
  }
  return "点击节点或连线查看详情；半透明小节点是跨分类的关联上下文。";
});

const wikiGenerationModeLabel = computed(() => {
  const mode = wikiData.value?.generationMode || wikiData.value?.generator || "local fallback";
  return `生成方式: ${mode}`;
});

const wikiUpdatedAtLabel = computed(() => {
  const value = wikiData.value?.lastUpdatedAt || wikiData.value?.generatedAt;
  return value ? `更新: ${formatTime(value)}` : "尚未生成";
});

const wikiNeedsReviewCount = computed(() => wikiEntries.value.filter((entry) => entry.needsReview).length);

const selectedWikiSourceLabel = computed(() => {
  const generator = wikiData.value?.generator || "";
  if (!generator) {
    return "";
  }
  return generator === "local-fallback-wiki" ? "本地推断" : "Agent 生成";
});

const wikiWorkflowLabel = computed(() => {
  const workflow = wikiData.value?.lastWorkflow || "read";
  const status = wikiData.value?.lastWorkflowStatus || "ready";
  return `${workflow} / ${status}`;
});

const WIKI_AGENT_ENDPOINTS: Record<"generate" | "update" | "review", string> = {
  generate: "/story/wiki/agent/generate",
  update: "/story/wiki/agent/update",
  review: "/story/wiki/agent/review",
};

interface WikiGraphQueryParams {
  q?: string;
  category?: StoryWikiCategory;
  entryId?: string;
  nodeId?: string;
  depth?: number;
}

const FIELD_LABELS: Record<string, string> = {
  character_updates: "角色",
  variable_updates: "变量",
  event_updates: "事件",
  memory_updates: "记忆",
  worldbook_updates: "世界",
  relationship_updates: "关系",
  foreshadow_updates: "伏笔",
  timeline_updates: "时间",
  chapter_outline_updates: "大纲",
};

function shortFieldLabel(key: string): string {
  return FIELD_LABELS[key] ?? key;
}

function formatTime(value?: string): string {
  if (!value) return "";
  try {
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return value;
    return d.toLocaleString("zh-CN", { hour12: false });
  } catch {
    return value;
  }
}

function levelWidth(level: number): string {
  const pct = Math.min(100, Math.abs(level) * 10);
  return `${pct}%`;
}

function levelColor(level: number): string {
  if (level > 0) return "linear-gradient(90deg, #1f9d55, #5ab97d)";
  if (level < 0) return "linear-gradient(90deg, #c53030, #e07171)";
  return "#888";
}

const RELATIONSHIP_DIMENSION_LABELS: Record<string, string> = {
  trust: "信任",
  intimacy: "亲密",
  hostility: "敌对",
  loyalty: "忠诚",
  alliance: "同盟",
  rivalry: "竞争",
  family: "家族",
  professional: "职业",
};

function formatRelationshipDimension(value: unknown): string {
  const key = normalizeRelationshipGraphText(value).toLowerCase();
  return RELATIONSHIP_DIMENSION_LABELS[key] ?? (normalizeRelationshipGraphText(value) || "关系");
}

function relationshipDimensionLabel(edge: RelationshipEdge): string {
  return formatRelationshipDimension(edge.dimension);
}

function formatRelationshipNodeKind(value: unknown): string {
  const key = normalizeRelationshipGraphText(value).toLowerCase();
  if (key === "character") {
    return "角色";
  }
  return normalizeRelationshipGraphText(value) || "角色";
}

function normalizeRelationshipGraphText(value: unknown): string {
  return String(value ?? "").trim().replace(/\s+/g, " ");
}

function relationshipNodePayloadId(node: RelationshipGraphNodePayload): string {
  return normalizeRelationshipGraphText(node.id ?? node.label ?? node.name ?? "");
}

function relationshipNodePayloadLabel(node: RelationshipGraphNodePayload | undefined, fallback: string): string {
  return normalizeRelationshipGraphText(node?.label ?? node?.name ?? fallback) || fallback;
}

function shortNodeLabel(value: string): string {
  const trimmed = String(value || "").trim();
  if (trimmed.length <= 5) {
    return trimmed;
  }
  return `${trimmed.slice(0, 4)}…`;
}

function buildRelationshipEdgePath(source: RelationshipGraphNode, target: RelationshipGraphNode, index: number): string {
  const curve = buildRelationshipCurve(source, target, index);
  return `M ${source.x} ${source.y} C ${curve.controlAX} ${curve.controlAY}, ${curve.controlBX} ${curve.controlBY}, ${target.x} ${target.y}`;
}

function buildRelationshipEdgeLabelPoint(
  source: RelationshipGraphNode,
  target: RelationshipGraphNode,
  index: number
): { x: number; y: number } {
  const curve = buildRelationshipCurve(source, target, index);
  return {
    x: (source.x + 3 * curve.controlAX + 3 * curve.controlBX + target.x) / 8,
    y: (source.y + 3 * curve.controlAY + 3 * curve.controlBY + target.y) / 8,
  };
}

function buildRelationshipCurve(source: RelationshipGraphNode, target: RelationshipGraphNode, index: number) {
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  const distance = Math.max(1, Math.hypot(dx, dy));
  const normalX = -dy / distance;
  const normalY = dx / distance;
  const offsetDirection = index % 2 === 0 ? 1 : -1;
  const offset = offsetDirection * (34 + (index % 3) * 12);
  const drift = ((index % 5) - 2) * 4;

  return {
    controlAX: source.x + dx * 0.33 + normalX * offset + drift,
    controlAY: source.y + dy * 0.33 + normalY * offset - drift,
    controlBX: source.x + dx * 0.67 + normalX * offset - drift,
    controlBY: source.y + dy * 0.67 + normalY * offset + drift,
  };
}

function zoomRelationshipGraph(delta: number): void {
  relationshipGraphZoom.value = clampNumber(relationshipGraphZoom.value + delta, 0.65, 1.8);
}

function resetRelationshipGraphView(): void {
  relationshipGraphZoom.value = 1;
  relationshipGraphPan.value = { x: 0, y: 0 };
  relationshipNodeOverrides.value = {};
}

function selectRelationshipEdge(index: number): void {
  selectedRelationshipIndex.value = index;
  selectedRelationshipNodeId.value = "";
}

function selectRelationshipNode(nodeId: string): void {
  selectedRelationshipNodeId.value = nodeId;
}

function handleRelationshipGraphWheel(event: WheelEvent): void {
  zoomRelationshipGraph(event.deltaY > 0 ? -0.08 : 0.08);
}

function beginRelationshipNodeDrag(event: PointerEvent, nodeId: string): void {
  const point = relationshipGraphPointFromEvent(event);
  const node = relationshipGraphNodes.value.find((item) => item.id === nodeId);
  if (!point || !node) {
    return;
  }
  relationshipDragState.value = {
    nodeId,
    pointerId: event.pointerId,
    offsetX: point.x - node.x,
    offsetY: point.y - node.y,
  };
  (event.currentTarget as Element | null)?.setPointerCapture?.(event.pointerId);
}

function beginRelationshipGraphPan(event: PointerEvent): void {
  if (event.button !== 0) {
    return;
  }
  const point = relationshipGraphRawPointFromEvent(event);
  if (!point) {
    return;
  }
  event.preventDefault();
  relationshipPanState.value = {
    pointerId: event.pointerId,
    startX: point.x,
    startY: point.y,
    originX: relationshipGraphPan.value.x,
    originY: relationshipGraphPan.value.y,
  };
  (event.currentTarget as Element | null)?.setPointerCapture?.(event.pointerId);
}

function moveRelationshipGraphPointer(event: PointerEvent): void {
  const drag = relationshipDragState.value;
  if (drag && drag.pointerId === event.pointerId) {
    const point = relationshipGraphPointFromEvent(event);
    if (!point) {
      return;
    }
    relationshipNodeOverrides.value = {
      ...relationshipNodeOverrides.value,
      [drag.nodeId]: {
        x: clampNumber(point.x - drag.offsetX, 58, RELATION_GRAPH_WIDTH - 58),
        y: clampNumber(point.y - drag.offsetY, 54, RELATION_GRAPH_HEIGHT - 54),
      },
    };
    return;
  }

  const pan = relationshipPanState.value;
  if (!pan || pan.pointerId !== event.pointerId) {
    return;
  }
  const point = relationshipGraphRawPointFromEvent(event);
  if (!point) {
    return;
  }
  relationshipGraphPan.value = {
    x: pan.originX + point.x - pan.startX,
    y: pan.originY + point.y - pan.startY,
  };
}

function endRelationshipGraphPointer(event: PointerEvent): void {
  const drag = relationshipDragState.value;
  if (drag && drag.pointerId === event.pointerId) {
    relationshipDragState.value = null;
  }
  const pan = relationshipPanState.value;
  if (pan && pan.pointerId === event.pointerId) {
    relationshipPanState.value = null;
  }
}

function relationshipGraphRawPointFromEvent(event: PointerEvent | WheelEvent): { x: number; y: number } | null {
  const svg = relationshipGraphSvgRef.value;
  if (!svg) {
    return null;
  }
  const rect = svg.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) {
    return null;
  }
  const rawX = ((event.clientX - rect.left) / rect.width) * RELATION_GRAPH_WIDTH;
  const rawY = ((event.clientY - rect.top) / rect.height) * RELATION_GRAPH_HEIGHT;
  return { x: rawX, y: rawY };
}

function relationshipGraphPointFromEvent(event: PointerEvent | WheelEvent): { x: number; y: number } | null {
  const raw = relationshipGraphRawPointFromEvent(event);
  if (!raw) {
    return null;
  }
  return {
    x: (raw.x - relationshipGraphPan.value.x) / relationshipGraphZoom.value,
    y: (raw.y - relationshipGraphPan.value.y) / relationshipGraphZoom.value,
  };
}

function clampNumber(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

async function loadSnapshot() {
  const requestSeq = ++snapshotLoadSeq;
  loading.value = true;
  errorMessage.value = "";
  try {
    const response = await apiClient.get<ApiEnvelope<EvolutionSnapshot>>("/story/evolution-snapshot");
    const data = unwrapEnvelope(response.data, "Story evolution snapshot request failed.");
    if (requestSeq !== snapshotLoadSeq) {
      return;
    }
    snapshot.value = data.data ?? {};
    if (selectedRelationshipIndex.value >= relationshipEdges.value.length) {
      selectedRelationshipIndex.value = 0;
    }
  } catch (error: unknown) {
    if (requestSeq !== snapshotLoadSeq) {
      return;
    }
    errorMessage.value = (error as Error)?.message || "无法读取项目状态。";
  } finally {
    if (requestSeq === snapshotLoadSeq) {
      loading.value = false;
    }
  }
}

function currentWikiGraphQueryParams(): WikiGraphQueryParams {
  if (wikiGraphSearchQuery.value.trim()) {
    return { q: wikiGraphSearchQuery.value.trim() };
  }
  if (selectedWikiNodeId.value) {
    return { nodeId: selectedWikiNodeId.value };
  }
  return { category: selectedWikiCategory.value };
}

async function loadWikiGraph(params: WikiGraphQueryParams = currentWikiGraphQueryParams()): Promise<void> {
  const requestSeq = ++wikiGraphLoadSeq;
  wikiGraphLoading.value = true;
  const queryParams: Record<string, string | number> = {
    depth: params.depth ?? (params.nodeId ? 1 : 1),
    limit: 60,
  };
  if (params.nodeId) {
    queryParams.nodeId = params.nodeId;
  } else if (params.entryId) {
    queryParams.entryId = params.entryId;
  } else if (params.q?.trim()) {
    queryParams.q = params.q.trim();
  } else {
    queryParams.category = normalizeWikiCategory(params.category || selectedWikiCategory.value);
  }

  try {
    const response = await apiClient.get<ApiEnvelope<StoryWikiGraphQueryResponse>>("/story/wiki/graph", {
      params: queryParams,
    });
    const data = unwrapEnvelope(response.data, "Story wiki graph query failed.");
    if (requestSeq !== wikiGraphLoadSeq) {
      return;
    }
    wikiErrorMessage.value = "";
    wikiGraphQueryData.value = data.data ?? null;
    ensureWikiGraphSelection();
  } catch (error: unknown) {
    if (requestSeq === wikiGraphLoadSeq) {
      wikiErrorMessage.value = describeTransportError(error, "知识图谱查询失败。");
      wikiGraphQueryData.value = null;
    }
  } finally {
    if (requestSeq === wikiGraphLoadSeq) {
      wikiGraphLoading.value = false;
    }
  }
}

async function loadWiki(): Promise<void> {
  wikiLoading.value = true;
  wikiErrorMessage.value = "";
  try {
    const response = await apiClient.get<ApiEnvelope<StoryWikiData>>("/story/wiki");
    const data = unwrapEnvelope(response.data, "Story wiki request failed.");
    wikiData.value = data.data ?? null;
    ensureWikiSelection();
    await loadWikiGraph({ category: selectedWikiCategory.value });
  } catch (error: unknown) {
    wikiErrorMessage.value = describeTransportError(error, "无法读取知识图谱。");
  } finally {
    wikiLoading.value = false;
  }
}

async function rebuildWiki(): Promise<void> {
  wikiRebuilding.value = true;
  wikiErrorMessage.value = "";
  try {
    const response = await apiClient.post<ApiEnvelope<StoryWikiData>>("/story/wiki/rebuild");
    const data = unwrapEnvelope(response.data, "Story wiki rebuild request failed.");
    wikiData.value = data.data ?? null;
    ensureWikiSelection();
    await loadWikiGraph(currentWikiGraphQueryParams());
  } catch (error: unknown) {
    wikiErrorMessage.value = describeTransportError(error, "知识图谱重新生成失败。");
  } finally {
    wikiRebuilding.value = false;
  }
}

async function runWikiAgentWorkflow(workflow: "generate" | "update" | "review"): Promise<void> {
  wikiAgentRunning.value = true;
  wikiAgentTone.value = "idle";
  wikiAgentStatus.value = workflow === "generate"
    ? "Agent 正在深度生成 WIKI..."
    : workflow === "update"
      ? "Agent 正在增量更新 WIKI..."
      : "Agent 正在审阅 WIKI...";
  wikiErrorMessage.value = "";
  try {
    const response = await apiClient.post<ApiEnvelope<StoryWikiAgentWorkflowResponse>>(WIKI_AGENT_ENDPOINTS[workflow]);
    const data = unwrapEnvelope(response.data, "Story wiki agent workflow failed.");
    const payload = data.data;
    if (payload?.wiki) {
      wikiData.value = payload.wiki;
      ensureWikiSelection();
      await loadWikiGraph(currentWikiGraphQueryParams());
    }
    if (payload?.fallbackUsed) {
      wikiAgentTone.value = "warning";
      wikiAgentStatus.value = `${payload.summary} 已使用本地 fallback。`;
    } else {
      wikiAgentTone.value = "success";
      wikiAgentStatus.value = payload?.summary || "Agent WIKI 流程完成。";
    }
    if (payload?.errorMessage) {
      wikiAgentStatus.value += ` ${payload.errorMessage}`;
    }
  } catch (error: unknown) {
    wikiAgentTone.value = "error";
    wikiAgentStatus.value = describeTransportError(error, "Agent WIKI 流程失败。");
    wikiErrorMessage.value = wikiAgentStatus.value;
  } finally {
    wikiAgentRunning.value = false;
  }
}

function refreshPanel(): void {
  if (props.relationshipOnly) {
    void loadWiki();
    return;
  }
  void loadSnapshot();
}

const wikiSyncing = ref(false);
let wikiSyncTimer: ReturnType<typeof setTimeout> | null = null;
let wikiSyncPending = false;

async function syncWiki(): Promise<void> {
  if (wikiSyncing.value) {
    wikiSyncPending = true;
    return;
  }
  // 不打断手动 Agent 流程或重建。
  if (wikiAgentRunning.value || wikiRebuilding.value || wikiLoading.value) {
    wikiSyncPending = true;
    return;
  }
  wikiSyncing.value = true;
  try {
    const response = await apiClient.post<ApiEnvelope<StoryWikiData>>("/story/wiki/sync");
    const data = unwrapEnvelope(response.data, "Story wiki sync failed.");
    if (data.data) {
      wikiData.value = data.data;
      ensureWikiSelection();
      await loadWikiGraph(currentWikiGraphQueryParams());
    }
  } catch {
    // 自动同步失败静默处理：不打扰创作，手动按钮仍可用。
  } finally {
    wikiSyncing.value = false;
    if (wikiSyncPending) {
      wikiSyncPending = false;
      scheduleWikiSync();
    }
  }
}

function scheduleWikiSync(): void {
  if (wikiSyncTimer) {
    clearTimeout(wikiSyncTimer);
  }
  // 防抖：连续保存/写作后静置约 3s 再做一次本地增量同步，避免频繁写盘。
  wikiSyncTimer = setTimeout(() => {
    wikiSyncTimer = null;
    void syncWiki();
  }, 3000);
}

function ensureWikiSelection(): void {
  // 默认坚持关系视图：真实关系边由查询端注入（写作演进快照），
  // 是否回退在拿到查询结果后由 ensureWikiGraphSelection 决定。
  selectedWikiCategory.value = normalizeWikiCategory(selectedWikiCategory.value || preferredWikiCategory());
  if (!wikiEntries.value.some((entry) => entry.id === selectedWikiEntryId.value)) {
    selectedWikiEntryId.value = selectedWikiCategoryEntries.value[0]?.id || wikiEntries.value[0]?.id || "";
  }
}

function ensureWikiGraphSelection(): void {
  const data = wikiGraphQueryData.value;
  if (
    data &&
    data.mode === "category" &&
    data.category === "relationships" &&
    selectedWikiCategory.value === "relationships" &&
    !(data.graph?.nodes?.length)
  ) {
    const fallback = preferredWikiCategory();
    if (fallback !== "relationships") {
      selectedWikiCategory.value = fallback;
      void loadWikiGraph({ category: fallback });
      return;
    }
  }
  const visibleEntries = visibleWikiEntries.value;
  if (visibleEntries.length && !visibleEntries.some((entry) => entry.id === selectedWikiEntryId.value)) {
    selectedWikiEntryId.value = visibleEntries[0].id;
  }
  if (selectedWikiNodeId.value && !wikiGraphNodes.value.some((node) => node.id === selectedWikiNodeId.value)) {
    selectedWikiNodeId.value = "";
  }
  if (selectedWikiEdgeId.value && !wikiGraphEdges.value.some((edge) => edge.id === selectedWikiEdgeId.value)) {
    selectedWikiEdgeId.value = "";
  }
}

function normalizeWikiCategory(value: string | undefined): StoryWikiCategory {
  return WIKI_CATEGORY_TABS.some((category) => category.id === value) ? value as StoryWikiCategory : "overview";
}

function preferredWikiCategory(): StoryWikiCategory {
  const counts = new Map<StoryWikiCategory, number>();
  wikiEntries.value.forEach((entry) => {
    const category = normalizeWikiCategory(entry.category);
    counts.set(category, (counts.get(category) ?? 0) + 1);
  });
  if (hasWikiRelationshipNetwork()) {
    return "relationships";
  }
  if ((counts.get("characters") ?? 0) > 0) {
    return "characters";
  }
  return "overview";
}

function hasWikiRelationshipNetwork(): boolean {
  const nodes = wikiData.value?.graph?.nodes ?? [];
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  return (wikiData.value?.graph?.edges ?? []).some((edge) => {
    if (edge.type !== "relationship") {
      return false;
    }
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    return source?.type === "character" && target?.type === "character";
  });
}

function selectWikiCategory(categoryId: StoryWikiCategory): void {
  selectedWikiCategory.value = normalizeWikiCategory(categoryId);
  selectedWikiEntryId.value = wikiEntries.value.find((entry) => entry.category === selectedWikiCategory.value)?.id || "";
  selectedWikiNodeId.value = "";
  selectedWikiEdgeId.value = "";
  wikiGraphSearchInput.value = "";
  wikiGraphSearchQuery.value = "";
  void loadWikiGraph({ category: categoryId });
}

function selectWikiEntry(entryId: string): void {
  selectedWikiEntryId.value = entryId;
  const node = wikiGraphNodes.value.find((item) => item.entryId === entryId);
  selectedWikiNodeId.value = node?.id || "";
  selectedWikiEdgeId.value = "";
}

function isWikiNodeSelectable(node: WikiGraphNode): boolean {
  return node.selectable !== false && !node.synthetic;
}

function selectWikiNode(node: WikiGraphNode): void {
  if (!isWikiNodeSelectable(node)) {
    return;
  }
  selectedWikiNodeId.value = node.id;
  selectedWikiEdgeId.value = "";
  if (node.entryId) {
    selectedWikiEntryId.value = node.entryId;
  }
}

function selectWikiEdge(edgeId: string): void {
  selectedWikiEdgeId.value = edgeId;
  selectedWikiNodeId.value = "";
}

function isWikiEdgeActive(edge: Pick<WikiGraphEdge, "id" | "source" | "target">): boolean {
  if (selectedWikiEdgeId.value) {
    return selectedWikiEdgeId.value === edge.id;
  }
  if (!selectedWikiNodeId.value) {
    return false;
  }
  return edge.source.id === selectedWikiNodeId.value || edge.target.id === selectedWikiNodeId.value;
}

function submitWikiGraphSearch(): void {
  const query = wikiGraphSearchInput.value.trim();
  wikiGraphSearchQuery.value = query;
  selectedWikiNodeId.value = "";
  selectedWikiEdgeId.value = "";
  if (!query) {
    void loadWikiGraph({ category: selectedWikiCategory.value });
    return;
  }
  void loadWikiGraph({ q: query });
}

function clearWikiGraphSearch(): void {
  wikiGraphSearchInput.value = "";
  wikiGraphSearchQuery.value = "";
  selectedWikiNodeId.value = "";
  selectedWikiEdgeId.value = "";
  void loadWikiGraph({ category: selectedWikiCategory.value });
}

function zoomWikiGraphAt(point: { x: number; y: number }, factor: number): void {
  const next = clampNumber(wikiGraphZoom.value * factor, 0.35, 2.6);
  const ratio = next / wikiGraphZoom.value;
  wikiGraphPan.value = {
    x: point.x - (point.x - wikiGraphPan.value.x) * ratio,
    y: point.y - (point.y - wikiGraphPan.value.y) * ratio,
  };
  wikiGraphZoom.value = next;
}

function zoomWikiGraphStep(direction: number): void {
  zoomWikiGraphAt(
    { x: wikiCanvasSize.value.width / 2, y: wikiCanvasSize.value.height / 2 },
    direction > 0 ? 1.2 : 1 / 1.2,
  );
}

// 依据节点包围盒自动适配缩放与平移，使整张图居中呈现。
function fitWikiGraphView(): void {
  const nodes = wikiGraphNodes.value;
  const { width, height } = wikiCanvasSize.value;
  if (!nodes.length || width <= 0 || height <= 0) {
    wikiGraphZoom.value = 1;
    wikiGraphPan.value = { x: 0, y: 0 };
    return;
  }
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  nodes.forEach((node) => {
    minX = Math.min(minX, node.x - node.radius - 22);
    maxX = Math.max(maxX, node.x + node.radius + 22);
    minY = Math.min(minY, node.y - node.radius - 14);
    // 下方标签需要额外空间。
    maxY = Math.max(maxY, node.y + node.radius + 30);
  });
  const boxWidth = Math.max(1, maxX - minX);
  const boxHeight = Math.max(1, maxY - minY);
  const zoom = clampNumber(Math.min(width / boxWidth, height / boxHeight), 0.35, 1.45);
  wikiGraphZoom.value = zoom;
  wikiGraphPan.value = {
    x: (width - boxWidth * zoom) / 2 - minX * zoom,
    y: (height - boxHeight * zoom) / 2 - minY * zoom,
  };
}

function resetWikiGraphView(): void {
  wikiNodeOverrides.value = {};
  recomputeWikiLayout();
  fitWikiGraphView();
}

function clearWikiGraphSelection(): void {
  selectedWikiNodeId.value = "";
  selectedWikiEdgeId.value = "";
}

function wikiGraphRawPointFromEvent(event: PointerEvent | WheelEvent): { x: number; y: number } | null {
  const svg = wikiGraphSvgRef.value;
  if (!svg) {
    return null;
  }
  const rect = svg.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) {
    return null;
  }
  return {
    x: ((event.clientX - rect.left) / rect.width) * wikiCanvasSize.value.width,
    y: ((event.clientY - rect.top) / rect.height) * wikiCanvasSize.value.height,
  };
}

function wikiGraphPointFromEvent(event: PointerEvent | WheelEvent): { x: number; y: number } | null {
  const raw = wikiGraphRawPointFromEvent(event);
  if (!raw) {
    return null;
  }
  return {
    x: (raw.x - wikiGraphPan.value.x) / wikiGraphZoom.value,
    y: (raw.y - wikiGraphPan.value.y) / wikiGraphZoom.value,
  };
}

function handleWikiGraphWheel(event: WheelEvent): void {
  const point = wikiGraphRawPointFromEvent(event);
  if (!point) {
    return;
  }
  // 以鼠标位置为锚点缩放，保持指针下的内容不漂移。
  zoomWikiGraphAt(point, event.deltaY > 0 ? 1 / 1.12 : 1.12);
}

function beginWikiGraphPan(event: PointerEvent): void {
  if (event.button !== 0) {
    return;
  }
  const point = wikiGraphRawPointFromEvent(event);
  if (!point) {
    return;
  }
  event.preventDefault();
  wikiGraphPanState.value = {
    pointerId: event.pointerId,
    startX: point.x,
    startY: point.y,
    originX: wikiGraphPan.value.x,
    originY: wikiGraphPan.value.y,
  };
  (event.currentTarget as Element | null)?.setPointerCapture?.(event.pointerId);
}

function beginWikiNodeDrag(event: PointerEvent, node: WikiGraphNode): void {
  const point = wikiGraphPointFromEvent(event);
  if (!point) {
    return;
  }
  wikiGraphDragState.value = {
    nodeId: node.id,
    pointerId: event.pointerId,
    offsetX: point.x - node.x,
    offsetY: point.y - node.y,
  };
  (event.currentTarget as Element | null)?.setPointerCapture?.(event.pointerId);
}

function moveWikiGraphPointer(event: PointerEvent): void {
  const drag = wikiGraphDragState.value;
  if (drag && drag.pointerId === event.pointerId) {
    const point = wikiGraphPointFromEvent(event);
    if (!point) {
      return;
    }
    wikiNodeOverrides.value = {
      ...wikiNodeOverrides.value,
      [drag.nodeId]: {
        x: clampNumber(point.x - drag.offsetX, 24, wikiCanvasSize.value.width - 24),
        y: clampNumber(point.y - drag.offsetY, 24, wikiCanvasSize.value.height - 40),
      },
    };
    return;
  }
  const pan = wikiGraphPanState.value;
  if (!pan || pan.pointerId !== event.pointerId) {
    return;
  }
  const point = wikiGraphRawPointFromEvent(event);
  if (!point) {
    return;
  }
  wikiGraphPan.value = {
    x: pan.originX + point.x - pan.startX,
    y: pan.originY + point.y - pan.startY,
  };
}

function endWikiGraphPointer(event: PointerEvent): void {
  const drag = wikiGraphDragState.value;
  if (drag && drag.pointerId === event.pointerId) {
    wikiGraphDragState.value = null;
  }
  const pan = wikiGraphPanState.value;
  if (pan && pan.pointerId === event.pointerId) {
    wikiGraphPanState.value = null;
  }
}

async function resolveConflict(idx: number, decision: "accept_incoming" | "keep_existing" | "dismiss") {
  resolving.value = idx;
  try {
    await apiClient.post<ApiEnvelope<unknown>>("/story/character-conflicts/resolve", {
      entryIndex: idx,
      decision,
    });
    await loadSnapshot();
  } catch (error: unknown) {
    errorMessage.value = (error as Error)?.message || "裁定提交失败。";
  } finally {
    resolving.value = null;
  }
}

function entryHasSegmentPath(entry: ChangeEntry): boolean {
  if (!entry.written || entry.written.length === 0) {
    return false;
  }
  return entry.written.some((p) => /^chapters\/.+\.(md|txt)$/i.test(p));
}

async function rollbackToEntry(entry: ChangeEntry, idx: number) {
  if (!entryHasSegmentPath(entry)) {
    return;
  }
  const segmentPath = (entry.written || []).find((p) => /^chapters\/.+\.(md|txt)$/i.test(p));
  if (!segmentPath) {
    return;
  }
  const confirmText = `确认回档到该段之前？\n该段 (${segmentPath}) 及之后的所有正文与变量快照会被备份后删除。\n可点击 toast 中的"撤销回档"恢复。`;
  // eslint-disable-next-line no-alert
  if (!window.confirm(confirmText)) {
    return;
  }
  rollingBack.value = idx;
  try {
    const response = await apiClient.post<ApiEnvelope<{ ok: boolean; rollbackId?: string; deletedSegmentCount?: number; reason?: string }>>(
      "/story/rollback",
      { target_segment_relative_path: segmentPath, keep_target: true },
    );
    const data = unwrapEnvelope(response.data, "Rollback request failed.");
    const payload = data.data;
    if (payload?.ok && payload.rollbackId) {
      lastRollbackId.value = payload.rollbackId;
      // eslint-disable-next-line no-alert
      window.alert(`已回档。删除片段数: ${payload.deletedSegmentCount ?? 0}\n备份 ID: ${payload.rollbackId}\n如需撤销，请点击下方 "撤销最近回档"。`);
    } else {
      errorMessage.value = `回档失败：${payload?.reason ?? "未知原因"}`;
    }
    await loadSnapshot();
  } catch (error: unknown) {
    errorMessage.value = (error as Error)?.message || "回档请求失败。";
  } finally {
    rollingBack.value = null;
  }
}

async function undoLastRollback() {
  if (!lastRollbackId.value) {
    return;
  }
  try {
    await apiClient.post<ApiEnvelope<unknown>>("/story/rollback/undo", { rollbackId: lastRollbackId.value });
    lastRollbackId.value = "";
    await loadSnapshot();
  } catch (error: unknown) {
    errorMessage.value = (error as Error)?.message || "撤销回档失败。";
  }
}

function isCharacterAssetPath(value: unknown): boolean {
  const normalized = String(value || "").trim().replace(/\\/g, "/");
  return /^\.storydex\/characters\/.+\.(json|md|txt)$/i.test(normalized);
}

watch(
  () => storyStore.lastLoadedAt,
  (next, previous) => {
    if (props.relationshipOnly || !next || next === previous) {
      return;
    }
    void loadSnapshot();
  }
);

watch(
  () => [workspaceStore.lastSavedAt, workspaceStore.activeFile] as const,
  ([nextSavedAt, activeFile], [previousSavedAt]) => {
    if (props.relationshipOnly || !nextSavedAt || nextSavedAt === previousSavedAt || !isCharacterAssetPath(activeFile)) {
      return;
    }
    void loadSnapshot();
  }
);

watch(
  () => activeTab.value,
  (next, previous) => {
    if (props.relationshipOnly || next !== "relations" || next === previous) {
      return;
    }
    void loadSnapshot();
  }
);

watch(
  () => wikiGraphQueryData.value,
  () => {
    wikiNodeOverrides.value = {};
    hoveredWikiNodeId.value = "";
    hoveredWikiEdgeId.value = "";
    recomputeWikiLayout();
    fitWikiGraphView();
  }
);

// 画布跟随容器实际尺寸（1:1 viewBox，不再固定比例拉伸变形）。
let wikiResizeObserver: ResizeObserver | null = null;
let wikiResizeTimer: ReturnType<typeof setTimeout> | null = null;

watch(
  wikiGraphPanelRef,
  (panel) => {
    wikiResizeObserver?.disconnect();
    wikiResizeObserver = null;
    if (!panel || typeof ResizeObserver === "undefined") {
      return;
    }
    wikiResizeObserver = new ResizeObserver((observed) => {
      const rect = observed[0]?.contentRect;
      if (!rect) {
        return;
      }
      const width = Math.max(320, Math.round(rect.width));
      const height = Math.max(280, Math.round(rect.height));
      if (
        Math.abs(width - wikiCanvasSize.value.width) < 4 &&
        Math.abs(height - wikiCanvasSize.value.height) < 4
      ) {
        return;
      }
      wikiCanvasSize.value = { width, height };
      if (wikiResizeTimer) {
        clearTimeout(wikiResizeTimer);
      }
      wikiResizeTimer = setTimeout(() => {
        wikiResizeTimer = null;
        recomputeWikiLayout();
        fitWikiGraphView();
      }, 160);
    });
    wikiResizeObserver.observe(panel);
  },
  { immediate: true }
);

watch(
  () => [workspaceStore.lastSavedAt, storyStore.lastLoadedAt] as const,
  ([nextSaved, nextLoaded], [prevSaved, prevLoaded]) => {
    if (!props.relationshipOnly) {
      return;
    }
    if (nextSaved === prevSaved && nextLoaded === prevLoaded) {
      return;
    }
    if (!nextSaved && !nextLoaded) {
      return;
    }
    // 保存文件或 Agent 写作后，自动做一次本地增量同步让知识图谱跟上变更。
    scheduleWikiSync();
  }
);

onBeforeUnmount(() => {
  if (wikiSyncTimer) {
    clearTimeout(wikiSyncTimer);
    wikiSyncTimer = null;
  }
  if (wikiResizeTimer) {
    clearTimeout(wikiResizeTimer);
    wikiResizeTimer = null;
  }
  wikiResizeObserver?.disconnect();
  wikiResizeObserver = null;
});

onMounted(() => {
  if (props.relationshipOnly) {
    void loadWiki();
    return;
  }
  void loadSnapshot();
});

defineExpose({ loadSnapshot, undoLastRollback });
</script>

<style scoped>
.story-state-panel {
  width: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-sidebar);
  border-top: 1px solid var(--border-ghost);
  color: var(--text-main);
  font-size: 12px;
  box-sizing: border-box;
}
.story-state-panel.is-expanded {
  height: 100%;
  border-top: 0;
  background: var(--bg-editor);
}
.story-state-panel.is-relationship-only .ssp-header {
  min-height: 48px;
}
.story-state-panel.is-expanded .ssp-body {
  max-height: none;
  padding: 10px 20px 14px;
}
.story-state-panel.is-relationship-only.is-expanded .ssp-body {
  padding: 8px 14px 12px;
  overflow: hidden;
}
.story-state-panel.is-expanded .ssp-relation-graph {
  height: min(68vh, 760px);
  min-height: 520px;
}
.story-state-panel.is-expanded .ssp-relation-inspector {
  gap: 16px;
}
.ssp-header {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 38px;
  padding: 9px 18px 7px;
  gap: 8px;
}
.ssp-title {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-soft);
  font-size: 12px;
  font-weight: 700;
}
.ssp-title-icon {
  flex: 0 0 auto;
  color: var(--text-muted);
  font-size: 17px;
}
.ssp-header-actions {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.ssp-refresh {
  flex: 0 0 auto;
  width: 26px;
  height: 26px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  cursor: pointer;
  padding: 0;
  color: var(--text-main);
}
.ssp-refresh .material-symbols-rounded {
  font-size: 17px;
}
.ssp-refresh:hover {
  background: var(--bg-hover);
}
.ssp-rebuild {
  color: var(--accent-strong);
}
.ssp-tabs {
  flex: 0 0 auto;
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  padding: 0 18px 8px;
}
.ssp-tab {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
  border: 1px solid var(--border-ghost);
  background: var(--bg-elevated, transparent);
  padding: 4px 7px;
  border-radius: 6px;
  font: inherit;
  font-size: 11px;
  cursor: pointer;
  color: var(--text-muted);
}
.ssp-tab.active {
  background: var(--bg-selected);
  border-color: var(--border-subtle);
  color: var(--accent-strong);
}
.ssp-tab-count {
  flex: 0 0 auto;
  min-width: 16px;
  text-align: center;
  font-size: 10px;
  background: var(--bg-hover);
  color: var(--text-faint);
  border-radius: 999px;
  padding: 0 4px;
}
.ssp-body {
  flex: 1 1 auto;
  min-height: 0;
  padding: 4px 18px 12px;
  overflow-y: auto;
  max-height: min(360px, 45vh);
}
.ssp-empty {
  padding: 12px 0;
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.6;
}
.ssp-wiki-workspace {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ssp-wiki-toolbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}
.ssp-wiki-category-tabs {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 3px;
  border: 1px solid var(--border-ghost);
  border-radius: 9px;
  background: color-mix(in srgb, var(--bg-card) 72%, transparent);
}
.ssp-wiki-category-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 30px;
  padding: 0 12px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--text-muted);
  font: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: background 140ms ease, color 140ms ease, box-shadow 140ms ease;
}
.ssp-wiki-category-tab .material-symbols-rounded {
  font-size: 16px;
}
.ssp-wiki-category-tab small {
  color: var(--text-faint);
  font-size: 10px;
  font-weight: 600;
}
.ssp-wiki-category-tab:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}
.ssp-wiki-category-tab.active {
  background: var(--bg-card);
  color: var(--accent-strong);
  box-shadow: 0 1px 4px rgba(15, 23, 42, 0.1);
}
.ssp-wiki-category-tab.active small {
  color: var(--accent);
}
.ssp-wiki-search-form {
  flex: 1 1 220px;
  max-width: 400px;
  min-height: 32px;
  display: flex;
  align-items: center;
  gap: 7px;
  border: 1px solid var(--border-ghost);
  border-radius: 9px;
  padding: 0 6px 0 10px;
  background: color-mix(in srgb, var(--bg-card) 72%, transparent);
  transition: border-color 140ms ease;
}
.ssp-wiki-search-form:focus-within {
  border-color: color-mix(in srgb, var(--accent) 55%, transparent);
}
.ssp-wiki-search-form > .material-symbols-rounded {
  flex: 0 0 auto;
  color: var(--text-faint);
  font-size: 16px;
}
.ssp-wiki-search-input {
  flex: 1 1 auto;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
}
.ssp-wiki-search-input::placeholder {
  color: var(--text-faint);
}
.ssp-wiki-search-clear {
  flex: 0 0 auto;
  width: 22px;
  height: 22px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
}
.ssp-wiki-search-clear:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}
.ssp-wiki-search-clear .material-symbols-rounded {
  font-size: 14px;
}
.ssp-wiki-toolbar-stats {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 10px;
  color: var(--text-faint);
  font-size: 11px;
  white-space: nowrap;
}
.ssp-wiki-review-alert {
  color: var(--warning);
  font-weight: 700;
}
.ssp-wiki-evidence-note {
  display: block;
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 11px;
  font-style: italic;
}
.ssp-wiki-source-chip {
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px solid var(--border-ghost);
  color: var(--text-muted);
  font-size: 10px;
  background: color-mix(in srgb, var(--bg-card) 60%, transparent);
}
.ssp-wiki-agent-status {
  flex: 0 0 auto;
  border: 1px solid var(--border-ghost);
  border-radius: 8px;
  padding: 6px 10px;
  color: var(--text-soft);
  background: color-mix(in srgb, var(--bg-card) 76%, transparent);
  font-size: 11px;
  line-height: 1.45;
}
.ssp-wiki-agent-status.success {
  color: var(--success);
}
.ssp-wiki-agent-status.warning {
  color: var(--warning);
}
.ssp-wiki-agent-status.error {
  color: var(--danger);
}
.ssp-wiki-main {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(280px, 340px);
  gap: 12px;
}
.ssp-wiki-graph-panel {
  position: relative;
  min-width: 0;
  min-height: 0;
  border: 1px solid var(--border-ghost);
  border-radius: 12px;
  overflow: hidden;
  background:
    radial-gradient(color-mix(in srgb, var(--text-faint) 18%, transparent) 1px, transparent 1.4px),
    linear-gradient(180deg, color-mix(in srgb, var(--bg-card) 46%, var(--bg-editor)), var(--bg-editor));
  background-size: 23px 23px, 100% 100%;
}
.ssp-wiki-graph {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
  touch-action: none;
  cursor: grab;
}
.ssp-wiki-graph:active {
  cursor: grabbing;
}
.ssp-wiki-edge {
  fill: none;
  stroke: color-mix(in srgb, var(--text-muted) 32%, transparent);
  stroke-width: 1;
  stroke-linecap: round;
  cursor: pointer;
  transition: opacity 160ms ease;
}
.ssp-wiki-edge.edge-real-relation {
  stroke: color-mix(in srgb, var(--accent) 62%, transparent);
  stroke-width: 1.7;
}
.ssp-wiki-edge.edge-co-occurrence {
  stroke-dasharray: 3 5;
}
.ssp-wiki-edge:hover {
  stroke: var(--accent);
}
.ssp-wiki-edge.active {
  stroke: var(--accent-strong);
  stroke-width: 2.2;
}
.ssp-wiki-edge.dimmed {
  opacity: 0.08;
  pointer-events: none;
}
.ssp-wiki-edge-label {
  cursor: pointer;
  transition: opacity 160ms ease;
}
.ssp-wiki-edge-label rect {
  fill: color-mix(in srgb, var(--bg-editor) 90%, transparent);
}
.ssp-wiki-edge-label text {
  fill: var(--text-muted);
  font-size: 10px;
  font-weight: 600;
}
.ssp-wiki-edge-label.active rect {
  fill: color-mix(in srgb, var(--accent) 14%, var(--bg-editor));
}
.ssp-wiki-edge-label.active text {
  fill: var(--accent-strong);
}
.ssp-wiki-node {
  cursor: default;
  transition: opacity 160ms ease;
}
.ssp-wiki-node.is-selectable {
  cursor: pointer;
}
.ssp-wiki-node.tone-character {
  --wiki-node-color: #5b8def;
}
.ssp-wiki-node.tone-plot {
  --wiki-node-color: #dda13f;
}
.ssp-wiki-node.tone-setting {
  --wiki-node-color: #45b384;
}
.ssp-wiki-node.tone-misc {
  --wiki-node-color: #9d8bd6;
}
.ssp-wiki-node.tone-hub {
  --wiki-node-color: color-mix(in srgb, var(--text-muted) 72%, transparent);
}
.ssp-wiki-node-dot {
  fill: var(--wiki-node-color, var(--text-muted));
  stroke: color-mix(in srgb, var(--bg-editor) 85%, transparent);
  stroke-width: 1.5;
  transition: fill 140ms ease;
}
.ssp-wiki-node.needs-review .ssp-wiki-node-dot {
  stroke: color-mix(in srgb, var(--warn, #d9a441) 82%, var(--wiki-node-color, var(--text-muted)));
  stroke-width: 2;
  stroke-dasharray: 4 3;
}
.ssp-wiki-node-halo {
  fill: transparent;
  stroke: transparent;
  stroke-width: 1.4;
  transition: stroke 140ms ease, fill 140ms ease;
}
.ssp-wiki-node text {
  fill: var(--text-soft);
  font-size: 11px;
  font-weight: 600;
  paint-order: stroke;
  stroke: color-mix(in srgb, var(--bg-editor) 90%, transparent);
  stroke-width: 3px;
  stroke-linejoin: round;
  pointer-events: none;
  transition: fill 140ms ease;
}
.ssp-wiki-node:hover .ssp-wiki-node-halo,
.ssp-wiki-node.active .ssp-wiki-node-halo {
  stroke: color-mix(in srgb, var(--wiki-node-color, var(--accent)) 80%, transparent);
  fill: color-mix(in srgb, var(--wiki-node-color, var(--accent)) 14%, transparent);
}
.ssp-wiki-node:hover text,
.ssp-wiki-node.active text {
  fill: var(--text-main);
}
.ssp-wiki-node.dimmed {
  opacity: 0.13;
}
.ssp-wiki-node.is-neighbor {
  opacity: 0.6;
}
.ssp-wiki-node.is-neighbor.dimmed {
  opacity: 0.1;
}
.ssp-wiki-node.is-neighbor text {
  font-size: 10px;
  font-weight: 500;
  fill: var(--text-muted);
}
.ssp-wiki-node.is-synthetic .ssp-wiki-node-dot {
  fill: color-mix(in srgb, var(--wiki-node-color, var(--text-muted)) 32%, var(--bg-editor));
  stroke: var(--wiki-node-color, var(--text-muted));
  stroke-dasharray: 3 3;
}
.ssp-wiki-graph-note {
  position: absolute;
  left: 50%;
  bottom: 48px;
  transform: translateX(-50%);
  max-width: min(520px, calc(100% - 32px));
  padding: 6px 12px;
  border: 1px solid var(--border-ghost);
  border-radius: 8px;
  background: color-mix(in srgb, var(--bg-card) 88%, transparent);
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.4;
  text-align: center;
  backdrop-filter: blur(8px);
  pointer-events: none;
}
.ssp-wiki-graph-legend {
  position: absolute;
  left: 12px;
  bottom: 12px;
  display: inline-flex;
  align-items: center;
  gap: 12px;
  padding: 5px 11px;
  border: 1px solid var(--border-ghost);
  border-radius: 8px;
  background: color-mix(in srgb, var(--bg-card) 82%, transparent);
  backdrop-filter: blur(8px);
  color: var(--text-muted);
  font-size: 11px;
  pointer-events: none;
}
.ssp-wiki-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.ssp-wiki-legend-item i {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-muted);
}
.ssp-wiki-legend-item i.tone-character {
  background: #5b8def;
}
.ssp-wiki-legend-item i.tone-plot {
  background: #dda13f;
}
.ssp-wiki-legend-item i.tone-setting {
  background: #45b384;
}
.ssp-wiki-legend-item i.tone-misc {
  background: #9d8bd6;
}
.ssp-wiki-legend-item i.tone-hub {
  background: color-mix(in srgb, var(--text-muted) 72%, transparent);
}
.ssp-wiki-graph-hud {
  position: absolute;
  right: 12px;
  bottom: 12px;
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 3px;
  border: 1px solid var(--border-ghost);
  border-radius: 9px;
  background: color-mix(in srgb, var(--bg-card) 88%, transparent);
  backdrop-filter: blur(8px);
  box-shadow: var(--shadow-sm);
}
.ssp-hud-btn {
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}
.ssp-hud-btn:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}
.ssp-hud-btn .material-symbols-rounded {
  font-size: 16px;
}
.ssp-hud-zoom {
  min-width: 40px;
  text-align: center;
  color: var(--text-faint);
  font-size: 11px;
}
.ssp-wiki-graph-empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-content: center;
  justify-items: center;
  gap: 6px;
  padding: 20px;
  color: var(--text-muted);
  text-align: center;
}
.ssp-wiki-graph-empty .material-symbols-rounded {
  font-size: 34px;
  color: var(--text-faint);
}
.ssp-wiki-graph-empty .is-spinning {
  animation: spin-ring 1s linear infinite;
}
.ssp-wiki-graph-empty p {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
}
.ssp-wiki-graph-empty small {
  color: var(--text-faint);
  font-size: 11px;
  line-height: 1.6;
}
.ssp-wiki-inspector {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border-ghost);
  border-radius: 12px;
  overflow: hidden;
  background: color-mix(in srgb, var(--bg-card) 76%, transparent);
}
.ssp-wiki-inspector-head {
  flex: 0 0 auto;
  padding: 12px 14px 10px;
  border-bottom: 1px solid var(--border-ghost);
}
.ssp-wiki-project {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--text-main);
  font-size: 13px;
  font-weight: 700;
}
.ssp-wiki-project strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ssp-wiki-project .material-symbols-rounded {
  flex: 0 0 auto;
  color: var(--accent-strong);
  font-size: 17px;
}
.ssp-wiki-run-meta {
  margin-top: 4px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  color: var(--text-faint);
  font-size: 10px;
  line-height: 1.4;
}
.ssp-wiki-inspector-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}
.ssp-wiki-inspector-detail {
  padding: 13px 14px 15px;
  border-bottom: 1px solid var(--border-ghost);
}
.ssp-wiki-inspector-detail h3 {
  margin: 4px 0 8px;
  color: var(--text-main);
  font-size: 15px;
  line-height: 1.4;
}
.ssp-wiki-inspector-detail p {
  margin: 0;
  color: var(--text-soft);
  font-size: 12px;
  line-height: 1.65;
}
.ssp-wiki-inspector-detail ul {
  margin: 10px 0 0;
  padding-left: 18px;
}
.ssp-wiki-inspector-detail li {
  color: var(--text-soft);
  font-size: 12px;
  line-height: 1.7;
}
.ssp-wiki-inspector-detail.is-empty p {
  color: var(--text-muted);
}
.ssp-wiki-relation-endpoints {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px;
  margin: 2px 0 8px;
  color: var(--text-main);
  font-size: 13px;
  font-weight: 600;
}
.ssp-wiki-relation-endpoints .material-symbols-rounded {
  color: var(--text-faint);
  font-size: 15px;
}
.ssp-wiki-relation-level {
  display: grid;
  gap: 4px;
  margin: 0 0 8px;
  color: var(--text-muted);
  font-size: 11px;
}
.ssp-wiki-entry-kicker {
  color: var(--accent-strong);
  font-size: 11px;
  font-weight: 700;
}
.ssp-wiki-entry-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: -2px 0 8px;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.4;
}
.ssp-wiki-review-chip {
  border: 1px solid color-mix(in srgb, var(--warning) 42%, transparent);
  border-radius: 999px;
  padding: 1px 6px;
  color: var(--warning);
  background: color-mix(in srgb, var(--warning) 10%, transparent);
  font-size: 10px;
  font-weight: 700;
  line-height: 1.5;
}
.ssp-wiki-inspector-heading {
  margin: 0 0 6px;
  padding: 0 6px;
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 700;
}
.ssp-wiki-entry-list {
  padding: 10px 8px 12px;
}
.ssp-wiki-entry-button {
  width: 100%;
  display: grid;
  gap: 3px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  padding: 7px 8px;
  color: var(--text-main);
  text-align: left;
  cursor: pointer;
}
.ssp-wiki-entry-button strong {
  font-size: 12px;
}
.ssp-wiki-entry-button span {
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.45;
}
.ssp-wiki-entry-button small {
  justify-self: start;
}
.ssp-wiki-entry-button:hover,
.ssp-wiki-entry-button.active {
  border-color: var(--border-subtle);
  background: var(--bg-hover);
}
.ssp-wiki-sources {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 12px;
}
.ssp-wiki-sources span {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  border: 1px solid var(--border-ghost);
  border-radius: 5px;
  padding: 2px 6px;
  color: var(--text-muted);
  font-size: 10px;
}
.ssp-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ssp-list-item {
  background: var(--bg-elevated, transparent);
  border: 1px solid var(--border-ghost);
  border-radius: 6px;
  padding: 8px;
}
.ssp-list-item.resolved {
  opacity: 0.6;
}
.ssp-list-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
  font-size: 12px;
}
.ssp-tag {
  min-width: 0;
  font-weight: 600;
  background: var(--bg-hover);
  color: var(--text-main);
  padding: 1px 6px;
  border-radius: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ssp-tag.status-open {
  color: #c08020;
}
.ssp-tag.status-recalled {
  color: #4060b0;
}
.ssp-tag.status-resolved {
  color: #1f9d55;
}
.ssp-time {
  flex: 0 0 auto;
  font-size: 11px;
  color: var(--text-muted);
}
.ssp-snapshot {
  font-size: 12px;
  margin: 2px 0 4px;
  line-height: 1.5;
  color: var(--text-main);
  overflow-wrap: anywhere;
}
.ssp-sizes {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.ssp-size-pill {
  font-size: 11px;
  background: var(--bg-hover);
  color: var(--text-muted);
  border-radius: 8px;
  padding: 1px 6px;
}
.ssp-error {
  font-size: 11px;
  color: #c53030;
  margin-top: 4px;
}
.ssp-section-head {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-soft);
  margin: 6px 0 2px;
}
.ssp-mini-list {
  list-style: disc inside;
  padding-left: 4px;
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-main);
}
.ssp-mini-meta {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 2px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.ssp-mini-meta.resolved {
  color: #1f9d55;
}
.ssp-conflict-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  margin-top: 4px;
}
.ssp-conflict-cell {
  background: var(--bg-hover);
  border-radius: 4px;
  padding: 4px 6px;
}
.ssp-cell-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-soft);
  margin-bottom: 2px;
}
.ssp-cell-body {
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}
.ssp-action-row {
  display: flex;
  gap: 6px;
  margin-top: 6px;
}
.ssp-btn {
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 4px;
  border: 1px solid var(--border-ghost);
  background: transparent;
  cursor: pointer;
  color: inherit;
}
.ssp-btn.accept {
  border-color: #1f9d55;
  color: #1f9d55;
}
.ssp-btn.keep {
  border-color: #5070d0;
  color: #5070d0;
}
.ssp-btn.dismiss {
  opacity: 0.7;
}
.ssp-btn.rollback {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border-color: #c08020;
  color: #c08020;
}
.ssp-btn.rollback .material-symbols-rounded {
  font-size: 14px;
}
.ssp-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.ssp-resolved-tag {
  font-size: 11px;
  color: #1f9d55;
  margin-top: 4px;
}
.ssp-level-bar {
  height: 6px;
  background: var(--bg-hover);
  border-radius: 3px;
  overflow: hidden;
  margin: 4px 0 2px;
}
.ssp-level-fill {
  height: 100%;
}
.ssp-relation-inspector {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin-bottom: 8px;
}
.ssp-relation-content {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(180px, 260px);
  gap: 12px;
  align-items: stretch;
}
.ssp-relation-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.ssp-relation-mode {
  min-width: 0;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-soft);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ssp-relation-controls {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: 0 0 auto;
}
.ssp-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 23px;
  height: 23px;
  padding: 0;
  border: 1px solid var(--border-ghost);
  border-radius: 4px;
  background: var(--bg-elevated, transparent);
  color: var(--text-muted);
  cursor: pointer;
}
.ssp-icon-btn:hover {
  background: var(--bg-hover);
  color: var(--accent-strong);
}
.ssp-icon-btn .material-symbols-rounded {
  font-size: 16px;
}
.ssp-zoom-label {
  min-width: 34px;
  text-align: center;
  font-size: 11px;
  color: var(--text-muted);
}
.ssp-relation-graph {
  width: 100%;
  height: 220px;
  min-height: 220px;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  background:
    linear-gradient(color-mix(in srgb, var(--border-subtle) 54%, transparent) 1px, transparent 1px),
    linear-gradient(90deg, color-mix(in srgb, var(--border-subtle) 54%, transparent) 1px, transparent 1px),
    var(--bg-card);
  background-size: 28px 28px;
  touch-action: none;
}
.ssp-relation-edge {
  fill: none;
  stroke: color-mix(in srgb, var(--text-muted) 52%, transparent);
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-dasharray: 7 9;
  vector-effect: non-scaling-stroke;
  pointer-events: stroke;
  cursor: pointer;
}
.ssp-relation-edge.selected {
  stroke: var(--accent-strong);
  stroke-width: 2.4;
  stroke-dasharray: none;
}
.ssp-relation-edge-label {
  cursor: pointer;
  pointer-events: all;
}
.ssp-relation-edge-label rect {
  fill: var(--bg-sidebar);
  stroke: var(--border-ghost);
  stroke-width: 1;
}
.ssp-relation-edge-label.selected rect {
  fill: var(--bg-selected);
  stroke: var(--accent-strong);
}
.ssp-relation-edge-label text {
  fill: var(--text-muted);
  font-family: var(--font-ui);
  font-size: 14px;
  font-weight: 600;
}
.ssp-relation-node {
  cursor: grab;
}
.ssp-relation-node:active {
  cursor: grabbing;
}
.ssp-relation-node circle {
  fill: var(--bg-card);
  stroke: var(--text-muted);
  stroke-width: 1.8;
  filter: drop-shadow(0 2px 4px rgba(17, 24, 39, 0.08));
}
.ssp-relation-node.active circle {
  fill: var(--bg-selected);
  stroke: var(--accent-strong);
  stroke-width: 3;
}
.ssp-relation-node text {
  fill: var(--text-main);
  font-family: var(--font-ui);
  font-size: 15px;
  font-weight: 700;
  pointer-events: none;
}
.ssp-relation-side-panel {
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.ssp-relation-evidence {
  flex: 1 1 auto;
  background: var(--bg-elevated, transparent);
  border: 1px solid var(--border-ghost);
  border-radius: 6px;
  padding: 10px 10px 12px;
  min-height: 0;
  overflow: auto;
}
.ssp-side-title {
  font-size: 12px;
  font-weight: 700;
  color: var(--text-soft);
  margin-bottom: 8px;
}
.ssp-character-name {
  font-size: 15px;
  font-weight: 700;
  color: var(--text-main);
  margin-bottom: 10px;
  overflow-wrap: anywhere;
}
.ssp-side-fields {
  display: grid;
  gap: 7px;
  margin: 0;
}
.ssp-side-fields dt,
.ssp-side-kv span {
  font-size: 11px;
  color: var(--text-muted);
}
.ssp-side-fields dd {
  margin: 1px 0 0;
  font-size: 12px;
  line-height: 1.45;
  color: var(--text-main);
  overflow-wrap: anywhere;
}
.ssp-side-kv {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin: 7px 0;
}
.ssp-side-kv strong {
  font-size: 12px;
  color: var(--text-main);
}
.ssp-relation-inspector + .ssp-list {
  display: none;
}

@media (max-width: 1180px) {
  .ssp-wiki-toolbar {
    gap: 8px;
  }

  .ssp-wiki-search-form {
    max-width: none;
  }

  .ssp-wiki-toolbar-stats {
    margin-left: 0;
    width: 100%;
  }

  .ssp-wiki-main {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: minmax(360px, 1.7fr) minmax(220px, 1fr);
  }
}

@media (max-width: 720px) {
  .story-state-panel.is-expanded .ssp-body {
    padding: 12px 12px 14px;
  }

  .ssp-wiki-category-tabs {
    width: 100%;
    overflow-x: auto;
  }

  .ssp-wiki-category-tab {
    flex: 1 0 auto;
    justify-content: center;
    padding: 0 9px;
  }

  .ssp-wiki-category-name {
    display: none;
  }

  .ssp-wiki-main {
    grid-template-rows: minmax(300px, 1.5fr) minmax(220px, 1fr);
  }

  .ssp-wiki-graph-legend {
    display: none;
  }

  .story-state-panel.is-expanded .ssp-relation-graph {
    height: 58vh;
    min-height: 420px;
  }

  .ssp-relation-content {
    grid-template-columns: 1fr;
  }

  .ssp-relation-side-panel {
    min-height: 150px;
  }

  .ssp-relation-toolbar {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .ssp-relation-edge-label text {
    font-size: 12px;
  }

  .ssp-relation-node text {
    font-size: 13px;
  }
}
</style>
